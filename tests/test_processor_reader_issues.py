"""Issue actions are explicit local choices, isolated from any model route."""
from io import BytesIO

import pytest
from PIL import Image

from sourceloom import processor
from sourceloom import processor_issues as issues
from sourceloom.store import Store, Conflict, digest


@pytest.fixture
def document(tmp_path):
    image=BytesIO(); Image.new('RGB',(12,12),'black').save(image,'PNG')
    source=b'<article><h1>Study</h1><table><tr><th>Item</th><th>A</th><th>B</th></tr><tr><td>row</td><td><img src="x.png"></td><td>no</td></tr></table></article>'
    store=Store(tmp_path/'data'); p=processor.create(store,'Local table')
    p=processor.prepare(store,p['id'],[('source.html',source),('x.png',image.getvalue())])
    row=next(r for r in p['processor']['resources'] if r['kind']=='image')
    def repeated(p):
        r=next(r for r in p['processor']['resources'] if r['id']==row['id'])
        r.update(placement_count=2,placements=[dict(page=1,bbox=[30,40,35,45]),dict(page=1,bbox=[70,40,75,45])])
    store.change(p['id'],repeated)
    markdown='# Result\n\n| Item | A | B |\n| --- | --- | --- |\n| row | yes | no |\n'
    p=processor.save_result(store,p['id'],markdown)
    v=processor.active_version(p)
    processor.confirm_representation(store,p['id'],v['id'],dict(source_ids=[row['id']],method='table',block_id='block-0001',source_columns=['A','B'],source_grid=[['row','yes','no']]))
    return store,p['id'],row,markdown


def _broken(document,markdown=None):
    store,pid,row,md=document
    prior=processor.active_version(store.get(pid))
    p=processor.save_result(store,pid,markdown or md.replace('yes','wrong'),base_version=prior['id'])
    return store,p,processor.active_version(p)


def test_table_current_version_rechecked_and_local_repair_is_idempotent(document):
    store,p,v=_broken(document)
    listing=issues.issues(p)
    table=next(g for g in listing['issues'] if g['kind']=='table')
    assert table['reliable_mapping'] and table['differences']==[dict(row='row',column='A',column_index=1,current='wrong',expected='yes',current_row_index=1,current_row_label='row')]
    request=dict(issue_id=table['id'],action='repair_table',difference_index=0)
    preview=issues.preview(store,p['id'],v['id'],request)
    assert preview['completion']=='已修正表格的 1 个单元格，其他内容未改'
    assert preview['apply_label']=='修正 1 格并保存'
    assert '2 个已保存来源单元格' in preview['evidence_scope']
    assert processor.active_version(store.get(p['id']))['markdown']==v['markdown']
    updated=issues.apply(store,p['id'],v['id'],dict(preview_id=preview['preview_id'],request=request))
    after=processor.active_version(updated)
    assert after['markdown']==v['markdown'].replace('wrong','yes')
    assert after['semantic_status']=='not_reviewed' and after['representations']
    assert not any(g['kind']=='table' for g in issues.issues(updated)['issues'])
    count=len(updated['processor']['versions'])
    again=issues.apply(store,p['id'],v['id'],dict(preview_id=preview['preview_id'],request=request))
    assert len(again['processor']['versions'])==count
    undo=issues.undo(store,p['id'],after['id'])
    assert undo['processor']['active_version']==v['id']


def test_stale_preview_rejected_and_undo_does_not_overwrite_editor(document):
    store,p,v=_broken(document)
    group=next(g for g in issues.issues(p)['issues'] if g['kind']=='table')
    request=dict(issue_id=group['id'],action='repair_table')
    preview=issues.preview(store,p['id'],v['id'],request)
    processor.save_result(store,p['id'],v['markdown']+'\nUser edit\n',base_version=v['id'])
    with pytest.raises(Conflict):
        issues.apply(store,p['id'],v['id'],dict(preview_id=preview['preview_id'],request=request))
    with pytest.raises(Conflict):
        issues.undo(store,p['id'],v['id'])


def test_required_source_cannot_be_dismissed_as_fixed_by_source_only(document):
    store,p,v=_broken(document)
    inventory=digest(p['inventory']); group=next(g for g in issues.issues(p)['issues'] if g['kind']=='table')
    request=dict(issue_id=group['id'],action='source_only')
    with pytest.raises(Conflict):
        issues.preview(store,p['id'],v['id'],request)
    request=dict(issue_id=group['id'],action='exclude_scope',acknowledged=True)
    preview=issues.preview(store,p['id'],v['id'],request)
    updated=issues.apply(store,p['id'],v['id'],dict(preview_id=preview['preview_id'],request=request))
    assert digest(updated['inventory'])==inventory
    assert processor.active_version(updated)['markdown']==v['markdown']
    assert processor.active_version(updated)['resource_usages'][group['source_ids'][0]]=='reference'
    assert not processor.active_version(updated)['mechanical_pass']
    assert any(c['code']=='EXPLICIT_SCOPE_REDUCTION' for c in processor.active_version(updated)['checks'])


def test_missing_reliable_grid_requires_explicit_user_confirmation(document):
    store,p,v=_broken(document)
    store.change(p['id'],lambda p:[v.update(representations=[]) for v in p['processor']['versions']])
    group=next(g for g in issues.issues(store.get(p['id']))['issues'] if g['kind']=='table')
    assert not group['reliable_mapping']
    with pytest.raises(Conflict):
        issues.preview(store,p['id'],v['id'],dict(issue_id=group['id'],action='confirm_manual'))
    preview=issues.preview(store,p['id'],v['id'],dict(issue_id=group['id'],action='confirm_manual',acknowledged=True))
    assert preview['representation']['confirmed_by']=='user_local_comparison'


def test_safe_format_does_not_touch_code_table_quote_or_formula(document):
    store,pid,row,md=document
    text=md+'\n作者。\n\n> 引语。\n\n```\n代码。\n```\n\n$$\n公式。\n$$\n'
    p=processor.save_result(store,pid,text)
    v=processor.active_version(p)
    preview=issues.preview(store,pid,v['id'],dict(issue_id='format',action='format'))
    assert '作者.' in preview['markdown']
    assert '引语。' in preview['markdown'] and '代码。' in preview['markdown'] and '公式。' in preview['markdown']


def test_valid_table_confirmation_does_not_rewrite_markdown(document):
    store,pid,row,md=document
    old=processor.active_version(store.get(pid))
    p=processor.save_result(store,pid,md,base_version=old['id'])
    v=processor.active_version(p)
    group=next(g for g in issues.issues(p)['resolved'] if g['kind']=='table')
    request=dict(issue_id=group['id'],action='confirm_table')
    preview=issues.preview(store,pid,v['id'],request)
    p=issues.apply(store,pid,v['id'],dict(preview_id=preview['preview_id'],request=request))
    assert processor.active_version(p)['markdown']==md
    assert len(p['processor']['versions'])==2
    assert processor.active_version(p)['id']==v['id']


def test_mismatch_cannot_be_confirmed_as_pass(document):
    store,p,v=_broken(document)
    group=next(g for g in issues.issues(p)['issues'] if g['kind']=='table')
    with pytest.raises(Conflict):
        issues.preview(store,p['id'],v['id'],dict(issue_id=group['id'],action='confirm_table'))


def test_table_crop_uses_matching_pdf_identity_not_first_pdf(tmp_path):
    import fitz
    store=Store(tmp_path/'data')
    originals=[]
    for name in ('first.pdf','second.pdf'):
        doc=fitz.open(); page=doc.new_page(); page.insert_text((40,120),name)
        originals.append(dict(name=name,sha256=store.blob(doc.tobytes())))
        doc.close()
    p=dict(inventory=dict(originals=originals),processor=dict(resources=[
        dict(id='symbol',kind='image',locator='second.pdf/page[1]/image[1]',placements=[dict(page=1,bbox=[100,140,108,148])])]))
    raw,meta=issues.table_image(p,dict(page=1,source_ids=['symbol'],source_document='second.pdf'),store)
    assert raw.startswith(b'\x89PNG') and meta['native_source_sha']==originals[1]['sha256']
    assert meta['precision']=='page' and meta['bbox']==[0.0,0.0,595.0,842.0]
    with pytest.raises(Conflict):
        issues.table_image(p,dict(page=1,source_ids=['symbol'],source_document='missing.pdf'),store)
    p['inventory']['originals']=originals[1:]
    with pytest.raises(Conflict):
        issues.table_image(p,dict(page=1,source_ids=['symbol'],source_document='missing.pdf'),store)


def test_legacy_pdf_missing_placement_metadata_compiles_readonly_manual_table_choice(tmp_path):
    import fitz
    pdf=fitz.open(); page=pdf.new_page();page.insert_text((40,75),'Table 1: comparison')
    for column,color in enumerate(('black','green')):
        image=BytesIO();Image.new('RGB',(12,12),color).save(image,'PNG')
        for row in range(3):
            page.insert_image(fitz.Rect(100+80*column,100+25*row,108+80*column,108+25*row),stream=image.getvalue())
    raw=pdf.tobytes();pdf.close()
    store=Store(tmp_path/'data');p=processor.create(store,'Legacy geometry')
    p=processor.prepare(store,p['id'],[('source.pdf',raw)])
    def old_shape(p):
        for row in p['processor']['resources']+p['inventory']['objects']:
            row.pop('placements',None);row.pop('placement_count',None)
    p=store.change(p['id'],old_shape)
    page_id=next(r['id'] for r in p['processor']['resources'] if r['kind']=='page')
    p=processor.save_result(store,p['id'],'{{source:'+page_id+'}}\n\n| Item | A | B |\n| --- | --- | --- |\n| row | yes | no |\n')
    before=digest(p)
    listing=issues.issues(p,store=store)
    tables=[g for g in listing['issues'] if g['kind']=='table']
    assert len(tables)==1 and set(tables[0]['source_ids'])=={r['id'] for r in p['processor']['resources'] if r['kind']=='image'}
    assert not tables[0]['reliable_mapping'] and 'confirm_manual' in tables[0]['actions']
    assert 'insert_resource' not in tables[0]['actions']
    assert digest(store.get(p['id']))==before
    version=processor.active_version(p)
    request=dict(issue_id=tables[0]['id'],action='add_page_reference')
    page_preview=issues.preview(store,p['id'],version['id'],request)
    assert '| row | yes | no |' in page_preview['markdown'] and '完整原页参考' in page_preview['markdown']
    assert '可编辑表格保留' in page_preview['summary']
    with_page=issues.apply(store,p['id'],version['id'],dict(preview_id=page_preview['preview_id'],request=request))
    assert processor.active_version(with_page)['markdown'].count('| row | yes | no |')==1
    issues.undo(store,p['id'],processor.active_version(with_page)['id'],page_preview['preview_id'])
    replacement=issues.preview(store,p['id'],version['id'],dict(issue_id=tables[0]['id'],action='replace_page_reference'))
    assert '| row | yes | no |' not in replacement['markdown'] and '移除当前正文表格' in replacement['summary']
    request=dict(issue_id=tables[0]['id'],action='confirm_manual',acknowledged=True)
    preview=issues.preview(store,p['id'],version['id'],request)
    applied=issues.apply(store,p['id'],version['id'],dict(preview_id=preview['preview_id'],request=request))
    saved=processor.active_version(applied)
    assert saved['markdown']==version['markdown']
    assert saved['representations'][0]['confirmed_by']=='user_local_comparison'
    reference=issues.image_group(applied,saved['id'],saved['representations'][0]['id'],store)
    rendered,bounds=issues.table_image(applied,reference,store)
    assert rendered.startswith(b'\x89PNG') and bounds['bbox']==[0.0,0.0,595.0,842.0]
    # The synthetic native header is well above the first symbol. A glyph
    # padding crop loses it; the honest fallback includes header and caption.
    with fitz.open(stream=raw,filetype='pdf') as pdf:
        words=pdf[0].get_text('words')
        header=next(w for w in words if w[4]=='Table')
        assert bounds['bbox'][1]<=header[1] and bounds['bbox'][3]>=header[3]
    with pytest.raises(KeyError):
        issues.image_group(applied,saved['id'],'another-document-reference',store)
    undone=issues.undo(store,p['id'],saved['id'])
    assert undone['processor']['active_version']==version['id']


def test_correct_source_cells_are_readonly_scoped_and_compiler_agrees(document):
    store,pid,row,md=document
    parent=processor.active_version(store.get(pid))
    p=processor.save_result(store,pid,md+'\nA separate author note\n',base_version=parent['id'])
    before=digest(p);listing=issues.issues(p)
    assert listing['resolved'][0]['status']=='checked'
    assert '2 个' in listing['resolved'][0]['evidence_scope']
    assert not listing['resolved'][0]['actions']
    assert not any(c.get('source_id')==row['id'] and c['severity']=='error' for c in processor.compile_result(p,md+'\nA separate author note\n')['checks'])
    assert digest(store.get(pid))==before and len(p['processor']['versions'])==2


def test_known_wrong_cell_has_one_repair_and_no_confirmation_escape(document):
    store,p,v=_broken(document)
    g=next(g for g in issues.issues(p)['issues'] if g['kind']=='table')
    assert g['category']=='repair' and g['recommended_action']=='repair_table'
    assert 'confirm_manual' not in g['actions'] and 'confirm_table' not in g['actions'] and 'source_only' not in g['actions']
    request=dict(issue_id=g['id'],action='retain_difference',acknowledged=True)
    receipt=issues.preview(store,p['id'],v['id'],request)
    after=issues.apply(store,p['id'],v['id'],dict(preview_id=receipt['preview_id'],request=request))
    assert processor.active_version(after)['digest']==v['digest']
    assert processor.active_version(after)['id']==v['id'] and not processor.active_version(after)['mechanical_pass']
    assert '来源差异' in after['processor']['last_issue_result']['completion']


def test_manual_confirmation_is_decision_not_empty_draft_and_undo_queries_same_identity(document):
    store,p,v=_broken(document)
    store.change(p['id'],lambda p:[v.update(representations=[]) for v in p['processor']['versions']])
    p=store.get(p['id']);g=next(g for g in issues.issues(p)['issues'] if g['kind']=='table')
    request=dict(issue_id=g['id'],action='confirm_manual',acknowledged=True)
    receipt=issues.preview(store,p['id'],v['id'],request)
    assert not receipt['body_changed'] and '用户对照' in receipt['completion']
    count=len(p['processor']['versions'])
    after=issues.apply(store,p['id'],v['id'],dict(preview_id=receipt['preview_id'],request=request))
    assert len(after['processor']['versions'])==count and processor.active_version(after)['id']==v['id']
    fresh=issues.issues(after)
    assert not any(candidate['id']==g['id'] for candidate in fresh['issues'])
    resolved=next(g0 for g0 in fresh['resolved'] if g0['id']==g['id'])
    assert resolved['confirmed_by']=='user_local_comparison' and '不是程序逐格' in resolved['evidence_scope']
    assert issues.operation(store,p['id'],receipt['preview_id'])['status']=='saved'
    assert issues.issues(after)['last_issue_result']['preview_id']==receipt['preview_id']
    duplicate=issues.apply(store,p['id'],v['id'],dict(preview_id=receipt['preview_id'],request=request))
    assert len(duplicate['processor']['versions'])==count
    undone=issues.undo(store,p['id'],v['id'],receipt['preview_id'])
    assert processor.active_version(undone)['markdown']==v['markdown']
    assert not processor.active_version(undone)['representations']
    assert issues.operation(store,p['id'],receipt['preview_id'])['status']=='undone'


def test_format_group_counts_only_actual_safe_hits_and_occurrences(document):
    store,pid,row,md=document
    text=md+'\n甲。乙。\n\n> 引用。\n\n`代码。` 和 $x。$ 与 [地址。](https://x.test/a。) 与“原话。” 与‘原话。’ 与\'quoted。\' 与\\(x。\\)\n\n$$\n数学。\n$$\n\n```text\n程序。\n```\n'
    p=processor.save_result(store,pid,text)
    group=next(g for g in issues.issues(p)['issues'] if g['kind']=='format')
    assert group['category']=='format' and group['occurrence_count']==2
    assert len(group['occurrences'])==1 and group['examples'][0]['after']=='甲.乙.'
    preview=issues.preview(store,pid,processor.active_version(p)['id'],dict(issue_id='format',action='format'))
    assert preview['markdown']==text.replace('甲。乙。','甲.乙.')


def test_unavailable_saved_image_is_service_failure_not_insert_marker(document,tmp_path):
    store,pid,row,md=document
    # The real blob is unavailable while the existing source/draft index remains.
    store.read_blob=lambda key: (_ for _ in ()).throw(FileNotFoundError())
    p=store.get(pid);listing=issues.issues(p,store=store)
    unavailable=next(g for g in listing['issues'] if g['category']=='service')
    assert row['id'] in unavailable['source_ids'] and unavailable['actions']==['restore_resource']
    assert unavailable['recommended_action']=='restore_resource'
    assert unavailable['restore_requires_file']


@pytest.mark.parametrize('representation',['markdown','html'])
def test_native_rectangular_source_grid_checks_and_repairs_one_numeric_cell(tmp_path,representation):
    source=b'<article><h1>Numbers</h1><table><tr><th>Item</th><th>Value (kg)</th></tr><tr><td>A</td><td>1.25</td></tr><tr><td>B</td><td>-2</td></tr></table></article>'
    store=Store(tmp_path/'data');p=processor.create(store,'Native grid')
    p=processor.prepare(store,p['id'],[('numbers.html',source)])
    md='# Numbers\n\n| Item | Value (kg) |\n| --- | --- |\n| A | 1.25 |\n| B | -2 |\n'
    if representation=='html':md=source.decode()
    p=processor.save_result(store,p['id'],md);v=processor.active_version(p)
    assert not any(g['kind']=='table' for g in issues.issues(p)['issues'])
    assert issues.issues(p)['resolved'][0]['status']=='checked'
    assert processor.compile_result(p,md)['mechanical_pass']
    bad=processor.save_result(store,p['id'],md.replace('1.25','12.5'),base_version=v['id']);bad_v=processor.active_version(bad)
    g=next(g for g in issues.issues(bad)['issues'] if g['kind']=='table')
    assert g['differences'][0]['expected']=='1.25' and g['differences'][0]['current']=='12.5'
    request=dict(issue_id=g['id'],action='repair_table')
    preview=issues.preview(store,p['id'],bad_v['id'],request)
    assert preview['markdown']==md
    repaired=issues.apply(store,p['id'],bad_v['id'],dict(preview_id=preview['preview_id'],request=request))
    assert processor.active_version(repaired)['markdown']==md


def test_duplicate_native_headers_do_not_pick_first_table(tmp_path):
    source=b'<table><tr><th>Item</th><th>A</th></tr><tr><td>one</td><td>1</td></tr></table><table><tr><th>Item</th><th>A</th></tr><tr><td>two</td><td>2</td></tr></table>'
    store=Store(tmp_path/'data');p=processor.create(store,'Different occurrences')
    p=processor.prepare(store,p['id'],[('source.html',source)])
    md='| Item | A |\n| --- | --- |\n| one | 9 |\n\nBreak\n\n| Item | A |\n| --- | --- |\n| two | 2 |\n'
    p=processor.save_result(store,p['id'],md)
    assert not any(g.get('recommended_action')=='repair_table' for g in issues.issues(p)['issues'])


def test_duplicate_row_names_do_not_broadcast_repair(document):
    store,p,v=_broken(document)
    p=processor.save_result(store,p['id'],v['markdown']+'| row | another | no |\n',base_version=v['id'])
    v=processor.active_version(p)
    g=next(g for g in issues.issues(p)['issues'] if g['kind']=='table')
    assert any(d.get('ambiguous') for d in g['differences'])
    assert 'repair_table' not in g['actions'] and 'confirm_manual' not in g['actions']
    with pytest.raises(Conflict):issues.preview(store,p['id'],v['id'],dict(issue_id=g['id'],action='repair_table'))


def test_identical_source_tables_cannot_share_one_unmapped_body_occurrence(tmp_path):
    table='<table><tr><th>Item</th><th>A</th></tr><tr><td>one</td><td>1</td></tr></table>'
    store=Store(tmp_path/'data');p=processor.create(store,'Two actual source occurrences')
    p=processor.prepare(store,p['id'],[('source.html',(table+table).encode())])
    md='| Item | A |\n| --- | --- |\n| one | 1 |\n'
    p=processor.save_result(store,p['id'],md)
    result=processor.compile_result(p,md)
    assert not result['mechanical_pass'] and not result['effective_representations']


@pytest.mark.parametrize('broken',['missing','corrupt'])
def test_restore_exact_missing_file_reuses_operation_and_never_inserts_marker(tmp_path,broken):
    import base64
    png=BytesIO();Image.new('RGB',(24,24),'blue').save(png,'PNG');raw=png.getvalue()
    store=Store(tmp_path/'data');p=processor.create(store,'Missing original file')
    p=processor.prepare(store,p['id'],[('figure.png',raw)])
    row=p['processor']['resources'][0];md='{{source:'+row['id']+'}}\n'
    p=processor.save_result(store,p['id'],md);v=processor.active_version(p);before_inventory=digest(p['inventory'])
    path=store.root/'blobs'/row['sha256']
    if broken=='missing':path.unlink()
    else:path.write_bytes(b'corrupted original image')
    listing=issues.issues(p,store=store);g=next(g for g in listing['issues'] if g['category']=='service')
    assert g['restore_requires_file'] and g['actions']==['restore_resource']
    bad=dict(issue_id=g['id'],action='restore_resource',resource_bytes=base64.b64encode(b'wrong').decode())
    with pytest.raises(Conflict):issues.preview(store,p['id'],v['id'],bad)
    request=dict(issue_id=g['id'],action='restore_resource',resource_bytes=base64.b64encode(raw).decode())
    preview=issues.preview(store,p['id'],v['id'],request)
    assert not preview['body_changed'] and not preview['undo_available']
    assert not path.exists() if broken=='missing' else path.read_bytes()==b'corrupted original image'
    restored=issues.apply(store,p['id'],v['id'],dict(preview_id=preview['preview_id'],request=request))
    assert digest(restored['inventory'])==before_inventory and processor.active_version(restored)['markdown']==md
    assert processor.active_version(restored)['id']==v['id'] and len(restored['processor']['versions'])==1
    assert store.read_blob(row['sha256'])==raw and issues.operation(store,p['id'],preview['preview_id'])['status']=='saved'
    duplicate=issues.apply(store,p['id'],v['id'],dict(preview_id=preview['preview_id'],request=request))
    assert len(duplicate['processor']['versions'])==1
    assert not any(g['category']=='service' for g in issues.issues(duplicate,store=store)['issues'])
    with pytest.raises(Conflict):issues.undo(store,p['id'],v['id'],preview['preview_id'])


def test_conflicting_saved_grids_are_not_automatically_chosen(document):
    store,pid,row,md=document
    def contradictory(p):
        v=p['processor']['versions'][0]
        second=dict(v['representations'][0],source_grid=[['row','contradictory','no']])
        v['representations'].append(second)
    p=store.change(pid,contradictory)
    result=processor.compile_result(p,md,representations=processor.active_version(p)['representations'])
    assert any(c['severity']=='error' and c.get('source_id')==row['id'] for c in result['checks'])
    g=next(g for g in issues.issues(p)['issues'] if g['kind']=='table')
    assert g['source_grid_conflict'] and not g['reliable_mapping'] and g['recommended_action']=='confirm_manual'


def test_saved_response_loss_is_queried_through_existing_operation_identity(document):
    from fastapi.testclient import TestClient
    from sourceloom.app import create_app
    from sourceloom.config import load_config
    store,p,v=_broken(document)
    config=load_config();config.update(data_dir=str(store.root),auth_mode='local',external_worker=True)
    base=f'/api/processor/projects/{p["id"]}'
    with TestClient(create_app(config)) as client:
        g=next(g for g in client.get(base+'/versions/'+v['id']+'/issues').json()['issues'] if g['kind']=='table')
        request=dict(issue_id=g['id'],action='repair_table')
        preview=client.post(base+'/versions/'+v['id']+'/issue-preview',json=request,headers={'X-SourceLoom':'1'}).json()
        token=preview['preview_id']
        assert client.get(base+'/issue-operations/'+token).json()['status']=='not_found'
        response=client.post(base+'/versions/'+v['id']+'/issue-apply',json=dict(preview_id=token,request=request),headers={'X-SourceLoom':'1'})
        assert response.status_code==200
        # Discard the response as a transport-loss simulation. An independent
        # GET finds the original saved operation without a new apply request.
        recovered=client.get(base+'/issue-operations/'+token).json()
        assert recovered['status']=='saved' and recovered['body_changed']
        assert len(store.get(p['id'])['processor']['versions'])==3
        other=processor.create(store,'Unrelated project')
        assert client.get('/api/processor/projects/'+other['id']+'/issue-operations/'+token).json()['status']=='not_found'
        undo=client.post(base+'/versions/'+recovered['version_id']+'/issue-undo',json={'preview_id':token},headers={'X-SourceLoom':'1'})
        assert undo.status_code==200
        assert client.get(base+'/issue-operations/'+token).json()['status']=='undone'


def _page_tables(tmp_path):
    import fitz
    pdf=fitz.open();page=pdf.new_page();page.insert_text((40,80),'A native PDF page with tables for local comparison')
    raw=pdf.tobytes();pdf.close()
    store=Store(tmp_path/'page-data');p=processor.create(store,'Native page table instances')
    p=processor.prepare(store,p['id'],[('page.pdf',raw)])
    sid=next(r['id'] for r in p['processor']['resources'] if r['kind']=='page')
    md='{{source:'+sid+'}}\n\n| Item | Value |\n| --- | --- |\n| one | 1.25 |\n\nOther table\n\n| Item | Value |\n| --- | --- |\n| two | 2 |\n'
    p=processor.save_result(store,p['id'],md)
    return store,p,sid,md


def test_issue_image_reuses_checked_listing_and_invalidates_after_saved_change(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from sourceloom.app import create_app
    store,p,sid,md=_page_tables(tmp_path)
    calls=[];original=issues.issues
    def measured(*args,**kwargs):
        calls.append(args[0]['id'])
        return original(*args,**kwargs)
    monkeypatch.setattr(issues,'issues',measured)
    before=store.get(p['id']);vid=p['processor']['active_version']
    route='/api/processor/projects/'+p['id']+'/versions/'+vid
    with TestClient(create_app(dict(data_dir=str(store.root),auth_mode='local',external_worker=True,writing_skill_dir=''))) as client:
        listing=client.get(route+'/issues').json()
        group=next(g for g in listing['issues'] if g['kind']=='table')
        image=client.get(route+'/issue-image/'+group['id'])
        assert image.status_code==200 and image.content.startswith(b'\x89PNG')
        assert image.headers['x-sourceloom-region-page']=='1'
        assert len(calls)==1
        assert store.get(p['id'])==before
        store.change(p['id'],lambda saved: saved['processor']['versions'][0].update(label='Changed saved metadata'))
        second=client.get(route+'/issue-image/'+group['id'])
        assert second.status_code==200 and len(calls)==2
        assert client.get(route+'/issue-image/not-a-current-table').status_code==404


def test_page_context_tables_without_image_objects_support_scoped_manual_and_add_page(tmp_path):
    store,p,sid,md=_page_tables(tmp_path);v=processor.active_version(p)
    assert not any(r['kind'] in {'image','table'} for r in p['processor']['resources'])
    before=digest(p);listing=issues.issues(p,store=store)
    groups=[g for g in listing['issues'] if g['kind']=='table']
    assert len(groups)==2 and len({g['id'] for g in groups})==2
    assert groups[0]['block_id']==groups[1]['block_id']
    assert [g['table_index'] for g in groups]==[0,1]
    assert all(g['recommended_action']=='confirm_manual' and not g['reliable_mapping'] for g in groups)
    assert digest(store.get(p['id']))==before
    request=dict(issue_id=groups[0]['id'],action='confirm_manual',acknowledged=True)
    preview=issues.preview(store,p['id'],v['id'],request)
    checked=issues.apply(store,p['id'],v['id'],dict(preview_id=preview['preview_id'],request=request))
    assert len(checked['processor']['versions'])==1
    fresh=issues.issues(checked,store=store)
    assert len([g for g in fresh['issues'] if g['kind']=='table'])==1
    assert fresh['resolved'][0]['id']==groups[0]['id']
    # Changing the other instance does not discard the confirmed first table.
    edited=processor.save_result(store,p['id'],md.replace('| two | 2 |','| two | 3 |'),base_version=v['id'])
    assert issues.issues(edited,store=store)['resolved'][0]['id']==groups[0]['id']
    g=next(g for g in issues.issues(edited,store=store)['issues'] if g['kind']=='table')
    current=processor.active_version(edited);request=dict(issue_id=g['id'],action='add_page_reference')
    added=issues.preview(store,p['id'],current['id'],request)
    assert '| one | 1.25 |' in added['markdown'] and '| two | 3 |' in added['markdown']
    assert added['markdown'].count('完整原页参考')==1 and added['derived_resources'][0]['region_precision']=='page'
    new=issues.apply(store,p['id'],current['id'],dict(preview_id=added['preview_id'],request=request))
    assert len(issues.issues(new,store=store)['resolved'])==1
    with pytest.raises(Conflict):issues.preview(store,p['id'],processor.active_version(new)['id'],request)
    undone=issues.undo(store,p['id'],processor.active_version(new)['id'],added['preview_id'])
    assert processor.active_version(undone)['markdown']==current['markdown']
    changed=processor.save_result(store,p['id'],current['markdown'].replace('1.25','1.26'),base_version=current['id'])
    assert not issues.issues(changed,store=store)['resolved']


def test_saved_page_grid_is_rechecked_and_known_decimal_difference_cannot_be_confirmed(tmp_path):
    store,p,sid,md=_page_tables(tmp_path);v=processor.active_version(p)
    group=next(g for g in issues.issues(p,store=store)['issues'] if g.get('table_index')==0)
    def seed(p):
        live=processor.active_version(p)
        preview=issues._preview(p,live['id'],dict(issue_id=group['id'],action='confirm_manual',acknowledged=True),store)
        record=preview['representation'];record.update(id='independent-source-grid',source_grid=[['one','1.25']],source_columns=['Value'],
            draft_digest=live['digest'],resource_snapshot_digest=digest(live['resource_usages']),target_markdown_digest=digest(preview['before'].encode()))
        live['representations']=[record]
    store.change(p['id'],seed);p=store.get(p['id'])
    checked=issues.issues(p,store=store)
    assert next(g for g in checked['resolved'] if g['id']==group['id'])['confirmed_by']=='saved_source_grid_rechecked'
    assert processor.compile_result(p,md)['effective_representations'][0]['source_grid']==[['one','1.25']]
    bad=processor.save_result(store,p['id'],md.replace('1.25','12.5'),base_version=v['id']);version=processor.active_version(bad)
    g=next(g for g in issues.issues(bad,store=store)['issues'] if g['id']==group['id'])
    assert g['category']=='repair' and g['differences'][0]['expected']=='1.25'
    assert 'confirm_manual' not in g['actions'] and not version['mechanical_pass']
    assert any(c['code']=='TABLE_SOURCE_DIFFERENCE' for c in version['checks'])
    request=dict(issue_id=g['id'],action='repair_table',difference_index=0)
    preview=issues.preview(store,p['id'],version['id'],request)
    assert preview['markdown']==md
    repaired=issues.apply(store,p['id'],version['id'],dict(preview_id=preview['preview_id'],request=request))
    assert processor.active_version(repaired)['mechanical_pass']
    assert not any(g['id']==group['id'] for g in issues.issues(repaired,store=store)['issues'])


def test_missing_native_table_has_actual_safe_source_preview(tmp_path):
    raw=b'<article><table><tr><th>Sample</th><th>Mass</th></tr><tr><td>A</td><td>1.25 kg</td></tr></table></article>'
    store=Store(tmp_path/'data');p=processor.create(store,'Native original preview')
    p=processor.prepare(store,p['id'],[('source.html',raw)])
    p=processor.save_result(store,p['id'],'No table has been placed.\n')
    group=next(g for g in issues.issues(p)['issues'] if g.get('source_kind')=='table')
    assert group['category']=='repair' and group['source_preview_url'] is None
    assert '<table>' in group['source_preview_html'] and '1.25 kg' in group['source_preview_html']
    assert group['source_locator'].startswith('source.html')
