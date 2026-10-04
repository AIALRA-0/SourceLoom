"""v1.1 resource usage and representation checks do not require a model."""
from io import BytesIO

import pytest
from bs4 import BeautifulSoup
from PIL import Image
from fastapi.testclient import TestClient

from sourceloom import processor
from sourceloom.app import create_app
from sourceloom.config import load_config
from sourceloom.store import Conflict, Store, digest


@pytest.fixture
def material(tmp_path):
    buf = BytesIO()
    Image.new('RGB',(72,72),'green').save(buf,'PNG')
    html = (b'<article><h1>Source table</h1><table>'
            b'<tr><th>Item</th><th>A</th><th>B</th></tr>'
            b'<tr><td>row</td><td><img src="symbol.png" alt="positive"></td><td>no</td></tr>'
            b'</table></article>')
    store = Store(tmp_path/'data')
    p = processor.create(store,'Controlled image-in-table')
    p = processor.prepare(store,p['id'],[('source.html',html),('symbol.png',buf.getvalue())])
    table = next(r for r in p['processor']['resources'] if r['kind']=='table')
    image = next(r for r in p['processor']['resources'] if r['kind']=='image')
    assert image['parent_id'] == table['id']
    return store,p,table,image


def table_markdown(value='yes'):
    return f'# Result\n\n| Item | A | B |\n| --- | --- | --- |\n| row | {value} | no |\n'


def test_selection_changes_exact_future_pack_but_not_saved_draft(material):
    store,p,table,image = material
    initial = processor.task_pack(store,p['id'])
    saved = processor.save_result(store,p['id'],table_markdown())
    first = processor.active_version(saved)
    assert first['resource_usages'] == {}
    changed = processor.set_resource_usages(store,p['id'],[
        {'source_id':table['id'],'usage':'reference'},
        {'source_id':image['id'],'usage':'exclude'}],initial['digest'])
    after = processor.task_pack(store,p['id'])
    assert after['digest'] != initial['digest']
    assert after['resource_selection'][image['id']] == 'exclude'
    assert not any(a['sha256']==image['sha256'] for a in after['attachments'])
    assert image['marker'] not in after['source_text']
    assert image['marker'] not in after['prompt']
    assert first['digest'] == processor.active_version(changed,first['id'])['digest']
    assert any(c['code']=='OMITTED_RESOURCE' and c['source_id']==image['id']
               for c in processor.active_version(changed,first['id'])['checks'])
    with pytest.raises(Conflict):
        processor.set_resource_usages(store,p['id'],[
            {'source_id':image['id'],'usage':'body'}],initial['digest'])


def test_context_resource_is_not_missing_but_insert_command_is_rejected(material):
    store,p,table,image = material
    processor.set_resource_usages(store,p['id'],[
        {'source_id':image['id'],'usage':'reference'},
        {'source_id':table['id'],'usage':'reference'}])
    p=store.get(p['id'])
    compiled = processor.compile_result(p,table_markdown())
    assert not any(c['code']=='OMITTED_RESOURCE' for c in compiled['checks'])
    invalid = processor.compile_result(p,table_markdown()+'\n'+image['marker'])
    assert any(c['code']=='RESOURCE_NOT_AUTHORIZED' for c in invalid['checks'])
    assert not invalid['mechanical_pass']


def test_explicit_table_representation_binds_rows_and_draft(material):
    store,p,table,image = material
    processor.set_resource_usages(store,p['id'],[
        {'source_id':table['id'],'usage':'reference'}])
    p=processor.save_result(store,p['id'],table_markdown())
    vid=processor.active_version(p)['id']
    assert any(c['code']=='OMITTED_RESOURCE' and c['source_id']==image['id']
               for c in processor.active_version(p)['checks'])
    p=processor.confirm_representation(store,p['id'],vid,dict(
        source_ids=[image['id']],block_id='block-0001',method='table',
        source_columns=['A','B'],source_grid=[['row','yes','no']]))
    v=processor.active_version(p)
    assert any(c['code']=='ALTERNATIVE_PRESENTATION' and c['source_id']==image['id'] for c in v['checks'])
    assert v['mechanical_pass']
    assert v['representations'][0]['confirmed_by']=='user_local_comparison'
    assert v['semantic_status']=='not_reviewed'
    assert processor.compile_result(p,v['markdown'],'readweave',
        resource_usages=v['resource_usages'],representations=v['representations'])['mechanical_pass']
    altered = processor.compile_result(p,table_markdown('no'),
        resource_usages=v['resource_usages'],representations=v['representations'])
    assert any(c['code']=='OMITTED_RESOURCE' and c['source_id']==image['id'] for c in altered['checks'])
    p=processor.save_result(store,p['id'],table_markdown('no'),base_version=vid)
    assert not processor.active_version(p)['representations']
    assert any(c['code']=='OMITTED_RESOURCE' and c['source_id']==image['id']
               for c in processor.active_version(p)['checks'])


def test_wrong_grid_or_missing_target_cannot_be_confirmed(material):
    store,p,table,image = material
    processor.set_resource_usages(store,p['id'],[
        {'source_id':table['id'],'usage':'reference'}])
    p=processor.save_result(store,p['id'],table_markdown())
    vid=processor.active_version(p)['id']
    with pytest.raises(Conflict):
        processor.confirm_representation(store,p['id'],vid,dict(
            source_ids=[image['id']],block_id='block-0001',method='table',
            source_columns=['A','B'],source_grid=[['row','no','yes']]))
    with pytest.raises(ValueError):
        processor.confirm_representation(store,p['id'],vid,dict(
            source_ids=[image['id']],block_id='missing',method='table'))
    assert not processor.active_version(store.get(p['id']))['representations']


def test_repeated_symbol_requires_structured_local_comparison_and_points_to_wrong_cell(material):
    store,p,table,image=material
    # One extracted bitmap may express several distinct table cells. A prose
    # note or a matching total count cannot authorize all those positions.
    store.change(p['id'],lambda current:next(row for row in current['processor']['resources']
        if row['id']==image['id']).update(placement_count=2))
    processor.set_resource_usages(store,p['id'],[{'source_id':table['id'],'usage':'reference'}])
    p=processor.save_result(store,p['id'],table_markdown('no'))
    vid=processor.active_version(p)['id']
    with pytest.raises(ValueError,match='逐行逐列'):
        processor.confirm_representation(store,p['id'],vid,dict(
            source_ids=[image['id']],block_id='block-0001',method='table',
            target_quote='row',comparison='I checked the source'))
    with pytest.raises(Conflict,match='表格行「row」第 2 列（A）不符'):
        processor.confirm_representation(store,p['id'],vid,dict(
            source_ids=[image['id']],block_id='block-0001',method='table',
            source_columns=['A','B'],source_grid=[['row','yes','no']]))
    assert processor.active_version(store.get(p['id']))['representations']==[]
    p=processor.save_result(store,p['id'],table_markdown('yes'),base_version=vid)
    confirmed=processor.confirm_representation(store,p['id'],processor.active_version(p)['id'],dict(
        source_ids=[image['id']],block_id='block-0001',method='table',
        source_columns=['A','B'],source_grid=[['row','yes','no']],
        target_quote='row',comparison='Compared with original table cells'))
    version=processor.active_version(confirmed)
    assert version['mechanical_pass']
    assert version['representations'][0]['comparison']=='Compared with original table cells'


def test_page_only_source_does_not_justify_table_reconstruction(material):
    store,p,table,image=material
    processor.set_resource_usages(store,p['id'],[
        {'source_id':table['id'],'usage':'reference'}])
    p=store.get(p['id'])
    result=processor.compile_result(p,'# Result\n\nOriginal source preserved elsewhere.\n\n'+image['marker'])
    assert not any(c['code']=='OMITTED_RESOURCE' and c['source_id']==image['id'] for c in result['checks'])
    assert result['semantic_status']=='not_reviewed'
    assert not any(c['code']=='ALTERNATIVE_PRESENTATION' for c in result['checks'])


def test_selection_snapshot_and_preview_source_ranges_are_explicit(material):
    store,p,table,image=material
    pack=processor.task_pack(store,p['id'])
    assert pack['template_version'].endswith('1.1.0')
    assert pack['template_digest']==pack['policy_digest']
    assert pack['skill_commit']=='d4d4b11d6122c0f538186b2f5553f7cce7eb2480'
    result=processor.compile_result(p,table_markdown()+'\n'+image['marker']+'\n')
    doc=BeautifulSoup(result['html'],'html.parser')
    section=doc.select_one('[data-block-id="block-0001"]')
    assert section['data-source-start-line']=='1'
    assert int(section['data-source-end-line'])>=5
    assert result['source_map'][0]['mapping']=='adjacent_resource'
    assert digest(pack['prompt'].encode())!=pack['digest']


def test_v11_resource_and_representation_api_survives_refresh(material):
    store,p,table,image=material
    config=load_config()
    config.update(data_dir=str(store.root),auth_mode='local',external_worker=True)
    route=f'/api/processor/projects/{p["id"]}'
    headers={'x-sourceloom':'1'}
    with TestClient(create_app(config)) as client:
        pack=client.get(route+'/pack').json()
        changed=client.post(route+'/resources',headers=headers,json={
            'base_pack_digest':pack['digest'],
            'changes':[{'source_id':table['id'],'usage':'reference'}]})
        assert changed.status_code==200,changed.text
        assert client.post(route+'/resources',headers=headers,json={
            'base_pack_digest':pack['digest'],
            'changes':[{'source_id':image['id'],'usage':'exclude'}]}).status_code==409
        saved=client.post(route+'/result',headers=headers,json={'markdown':table_markdown()})
        assert saved.status_code==200,saved.text
        vid=saved.json()['processor']['active_version']
        mapping=client.get(route+f'/versions/{vid}/source-map').json()
        assert mapping['draft_digest']==digest(table_markdown().encode())
        assert mapping['blocks'][0]['block_id']=='block-0001'
        assert client.get(route+'/progress').json()['phase']=='complete'
        confirmed=client.post(route+f'/versions/{vid}/representations',headers=headers,json={
            'source_ids':[image['id']],'block_id':'block-0001','method':'table',
            'source_columns':['A','B'],'source_grid':[['row','yes','no']]})
        assert confirmed.status_code==200,confirmed.text
        assert confirmed.json()['processor']['versions'][-1]['mechanical_pass']
    with TestClient(create_app(config)) as client:
        persisted=client.get(route+f'/versions/{vid}').json()
        assert persisted['representations'][0]['source_ids']==[image['id']]
        assert client.get(route+'/preview').status_code==200


def test_inline_svg_with_unbound_namespace_stays_a_visible_source_gap(tmp_path):
    raw=(b'<article><h1>Original article</h1><p>Important prose.</p>'
         b'<svg width="40" height="20"><use xlink:href="#icon"/></svg>'
         b'</article>')
    store=Store(tmp_path/'data')
    p=processor.create(store,'Namespace SVG')
    prepared=processor.prepare(store,p['id'],[('article.html',raw)])
    assert any(o['kind']=='text' and 'Important prose.' in o['text']
               for o in prepared['inventory']['objects'])
    assert any(o['kind']=='unknown' and '<svg' in o['raw']
               for o in prepared['inventory']['objects'])
    assert any('svg 未执行' in gap['reason'] for gap in prepared['inventory']['unknown'])


def test_unfetched_html_image_cannot_be_mistaken_for_a_restored_resource(tmp_path):
    store=Store(tmp_path/'data')
    p=processor.create(store,'Image without bytes')
    p=processor.prepare(store,p['id'],[('article.html',
        b'<article><h1>Report</h1><p>The diagram matters.</p>'
        b'<img src="relative-diagram.png" alt="Findings diagram"></article>')])
    image=next(r for r in p['processor']['resources'] if r['kind']=='image')
    assert not image.get('sha256')
    pack=processor.task_pack(store,p['id'])
    assert pack['requires_visual']
    assert image['marker'] not in pack['source_text']
    assert not any(a.get('resource_id')==image['id'] for a in pack['attachments'])
    result=processor.compile_result(p,'# Result\n\nThe diagram matters.\n\n'+image['marker'])
    assert not result['mechanical_pass']
    assert any(c['code']=='RESOURCE_BINARY_UNAVAILABLE' for c in result['checks'])
    assert 'data-resource-unavailable' in result['html']
    p=processor.save_result(store,p['id'],'# Result\n\n| What | Result |\n| --- | --- |\n| Diagram | unknown |')
    with pytest.raises(Conflict):
        processor.confirm_representation(store,p['id'],processor.active_version(p)['id'],dict(
            source_ids=[image['id']],block_id='block-0001',method='table'))


def test_unreferenced_web_asset_is_archived_but_not_sent_to_model(tmp_path):
    buf=BytesIO()
    Image.new('RGB',(16,16),'blue').save(buf,'PNG')
    store=Store(tmp_path/'data')
    p=processor.create(store,'Saved web bundle')
    p=processor.prepare(store,p['id'],[
        ('snapshot.html',b'<main><h1>Article</h1><p>Only text in the article.</p></main>'),
        ('web-assets/unused-logo.png',buf.getvalue())])
    pack=processor.task_pack(store,p['id'])
    assert any(a['name']=='web-assets/unused-logo.png' for a in p['inventory']['originals'])
    assert not any(a['name']=='web-assets/unused-logo.png' for a in pack['attachments'])


def test_frozen_legacy_web_image_manifest_can_be_reused_without_refetch(tmp_path):
    store=Store(tmp_path/'data')
    p=processor.create(store,'Archived web snapshot')
    p=processor.prepare(store,p['id'],[
        ('snapshot.html',b'<article><h1>Preserved page</h1><p>Original wording.</p></article>')],
        source_url='https://example.org/archived',web_manifest=[{'selected_url':
            'https://example.org/image.png','fetched':False}])
    assert p['inventory']['web_snapshot']['images'][0]['fetched'] is False
    assert 'Original wording.' in processor.task_pack(store,p['id'])['source_text']


def test_selected_original_web_image_is_packaged_once_without_unauthorized_api(tmp_path):
    buf=BytesIO()
    Image.new('RGB',(16,16),'red').save(buf,'PNG')
    store=Store(tmp_path/'data')
    p=processor.create(store,'Illustrated web bundle')
    p=processor.prepare(store,p['id'],[
        ('snapshot.html',b'<article><h1>Illustration</h1>'
         b'<img src="web-assets/diagram.png" alt="Research diagram"></article>'),
        ('web-assets/diagram.png',buf.getvalue())])
    image=next(r for r in p['processor']['resources'] if r['kind']=='image')
    pack=processor.task_pack(store,p['id'])
    delivered=[a for a in pack['attachments'] if a['sha256']==image['sha256']]
    assert len(delivered)==1
    assert delivered[0]['kind']=='image'
    assert delivered[0]['resource_id']==image['id']
    from sourceloom.processor_channels import _request
    with pytest.raises(ValueError):
        _request(store,dict(provider='openai-compatible',api_key='test-only',
            model='fixture',protocol='responses',processor_supports_images=True),pack,'api')


def test_article_share_controls_are_not_rewritten_as_content(tmp_path):
    html=(b'<main><article><h1>Research finding</h1>'
          b'<ul><li class="social-icon social-icon-example">'
          b'<a aria-label="Share this article" href="https://example.org/share?item=1">'
          b'<svg width="12" height="12"><circle cx="6" cy="6" r="5"/></svg>'
          b'</a></li></ul><p>'+b'Observed data. '*40+b'</p></article></main>')
    store=Store(tmp_path/'data')
    p=processor.create(store,'Official article snapshot')
    p=processor.prepare(store,p['id'],[('snapshot.html',html)],
        source_url='https://example.org/story')
    pack=processor.task_pack(store,p['id'])
    assert 'Observed data.' in pack['source_text']
    assert 'example.org/share' not in pack['prompt']
    assert not any(r['kind']=='image' for r in p['processor']['resources'])
    assert any(o['kind']=='link' and 'example.org/share' in o.get('target','') and
               o.get('source_scope')=='site_chrome' for o in p['inventory']['objects'])
