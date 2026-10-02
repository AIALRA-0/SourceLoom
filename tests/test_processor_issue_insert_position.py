"""A missing figure must not be silently moved away from its saved caption."""
from io import BytesIO
import zipfile

import pytest
from PIL import Image
from bs4 import BeautifulSoup

from sourceloom import processor, processor_issues
from sourceloom.store import Store, Conflict, digest
from sourceloom.readweave import compare_html


def prepared(tmp_path):
    image = BytesIO()
    Image.new('RGB', (16, 12), 'black').save(image, 'PNG')
    store = Store(tmp_path / 'isolated')
    p = processor.create(store, 'Image occurrence')
    p = processor.prepare(store, p['id'], [
        ('source.html', b'<article><h1>Report</h1><img src="figure.png"><p>Actual caption</p><p>Later body</p></article>'),
        ('figure.png', image.getvalue()),
    ])
    sid = next(r['id'] for r in p['processor']['resources'] if r['kind'] == 'image')
    return store, p, sid


def test_exact_saved_figure_slot_restores_caption_relation_without_other_changes(tmp_path):
    store, p, sid = prepared(tmp_path)
    marker = '{{source:'+sid+'}}'
    original = '# Report\n\n'+marker+'\n\n*Actual caption*\n\nLater independent body.\n'
    p = processor.save_result(store, p['id'], original)
    old = processor.active_version(p)
    p = processor.save_result(store, p['id'], original.replace(marker+'\n', ''), base_version=old['id'])
    v = processor.active_version(p)
    source_digest = digest(p['inventory'])
    group = next(g for g in processor_issues.issues(p, store=store)['issues'] if sid in g['source_ids'])
    assert group['previous_position_available']
    request = dict(issue_id=group['id'], action='insert_resource')
    preview = processor_issues.preview(store, p['id'], v['id'], request)
    assert preview['markdown'] == original
    assert marker in preview['after'] and 'Actual caption' in preview['after']
    assert processor.active_version(store.get(p['id']))['markdown'] == v['markdown']
    updated = processor_issues.apply(store, p['id'], v['id'], dict(preview_id=preview['preview_id'], request=request))
    assert processor.active_version(updated)['markdown'] == original
    assert digest(updated['inventory']) == source_digest


@pytest.mark.parametrize('history', [False, True])
def test_no_unique_unchanged_saved_slot_requires_explicit_position(tmp_path, history):
    store, p, sid = prepared(tmp_path)
    marker = '{{source:'+sid+'}}'
    current = '# Report\n\nActual caption.\n\nLater body.\n'
    if history:
        p = processor.save_result(store, p['id'], '# Report\n\n'+marker+'\n\nEarlier caption.\n')
    p = processor.save_result(store, p['id'], current)
    v = processor.active_version(p)
    listing = processor_issues.issues(p, store=store)
    group = next(g for g in listing['issues'] if sid in g['source_ids'])
    assert not group['previous_position_available']
    with pytest.raises(Conflict, match='明确选择位置'):
        processor_issues.preview(store, p['id'], v['id'], dict(issue_id=group['id'], action='insert_resource'))
    target = listing['blocks'][0]['block_id']
    explicit = processor_issues.preview(store, p['id'], v['id'], dict(issue_id=group['id'], action='insert_resource', block_id=target))
    assert explicit['markdown'].count(marker) == 1
    assert current.replace('\n\n', '\n') in explicit['markdown'].replace(marker+'\n', '').replace('\n\n', '\n')


def test_native_editor_code_save_keeps_block_identity_and_exact_code(tmp_path):
    store = Store(tmp_path / 'isolated')
    p = processor.create(store, 'Code delivery')
    p = processor.prepare(store, p['id'], [('original.md', b'# Example\n\n```js\nconst value = "quoted";\n```\n')])
    source = next(r for r in p['processor']['resources'] if r['kind'] == 'code')
    p = processor.save_result(store, p['id'], '# Example\n\n{{source:'+source['id']+'}}\n')
    with zipfile.ZipFile(BytesIO(processor.export_package(store, p))) as package:
        original = package.read('material.html').decode()
    doc = BeautifulSoup(original, 'html.parser')
    codes = [n.get_text() for n in doc.find_all('pre')]
    anchors = [n.get('data-readweave-anchor-id') for n in doc.select('[data-readweave-anchor-id]')]
    assert any(a.startswith('resource-') for a in anchors)
    # Real CKEditor drops custom attributes from pre, but supports the existing
    # paragraph/heading anchor nodes. This must not erase a code occurrence.
    for node in doc.find_all('pre'):
        node.attrs.pop('data-readweave-anchor-id', None)
    assert [n.get_text() for n in doc.find_all('pre')] == codes
    assert all(compare_html(original, str(doc)).values())


def test_unrelated_repeated_icons_cannot_claim_another_pages_unique_table():
    doc = BeautifulSoup('<section data-block-id="block-0117"><table><tr><td>0.768</td></tr></table></section>', 'html.parser')
    compiled = {'source_map': [{'block_id': 'block-0117', 'locator': 'report.pdf/page[10]'}]}
    assert processor_issues._table_node(doc, compiled, 4, None, 'report.pdf') is None
    assert processor_issues._table_node(doc, compiled, 11, None, 'report.pdf') is None
    assert processor_issues._table_node(doc, compiled, 10, None, 'report.pdf')['data-block-id'] == 'block-0117'


def test_add_html_page_reference_preserves_all_following_tables_and_body(tmp_path):
    import fitz
    from difflib import SequenceMatcher
    pdf=fitz.open();page=pdf.new_page();page.insert_text((40,80),'Source page')
    raw=pdf.tobytes();pdf.close()
    store=Store(tmp_path/'data');p=processor.create(store,'Long HTML table')
    p=processor.prepare(store,p['id'],[('report.pdf',raw)])
    sid=next(r['id'] for r in p['processor']['resources'] if r['kind']=='page')
    table='<table><tr><th>Item</th><th>Value</th></tr><tr><td>A</td><td>1</td></tr></table>'
    original='{{source:'+sid+'}}\n\n'+table+'\n\n'+'Later paragraph.\n\n'*80+table+'\n\nFinal paragraph.\n'
    p=processor.save_result(store,p['id'],original);v=processor.active_version(p)
    group=next(g for g in processor_issues.issues(p,store=store)['issues'] if g.get('table_index')==0)
    request=dict(issue_id=group['id'],action='add_page_reference')
    preview=processor_issues.preview(store,p['id'],v['id'],request)
    changes=[op for op in SequenceMatcher(None,original,preview['markdown'],autojunk=False).get_opcodes() if op[0]!='equal']
    assert changes and all(op[0]=='insert' for op in changes)
    assert preview['markdown'].count(table)==2 and preview['markdown'].endswith('Final paragraph.\n')
    applied=processor_issues.apply(store,p['id'],v['id'],dict(preview_id=preview['preview_id'],request=request))
    assert processor.active_version(applied)['markdown']==preview['markdown']


def test_native_html_cell_repair_never_uses_character_offsets_to_cut_document(tmp_path):
    raw='<table><tr><th>Item</th><th>Value</th></tr><tr><td>A</td><td>1.25</td></tr></table>'
    store=Store(tmp_path/'data');p=processor.create(store,'Native local repair')
    p=processor.prepare(store,p['id'],[('source.html',raw.encode())])
    original=raw.replace('1.25','12.5')+'\n\n'+'Untouched later body.\n\n'*100
    p=processor.save_result(store,p['id'],original);v=processor.active_version(p)
    group=next(g for g in processor_issues.issues(p,store=store)['issues'] if g.get('recommended_action')=='repair_table')
    preview=processor_issues.preview(store,p['id'],v['id'],dict(issue_id=group['id'],action='repair_table'))
    assert preview['markdown']==original.replace('12.5','1.25')


def test_repeat_image_is_two_distinct_occurrences_but_code_duplicate_is_still_error(tmp_path):
    store,p,sid=prepared(tmp_path)
    md='{{source:'+sid+'}}\n\nSecond actual mention.\n\n{{source:'+sid+'}}\n'
    compiled=processor.compile_result(p,md)
    assert not any(c['code']=='DUPLICATE_RESOURCE' for c in compiled['checks'])
    occurrences=[x for x in compiled['source_map'] if x['mapping']=='explicit_resource']
    assert len(occurrences)==2 and len({x['block_id'] for x in occurrences})==2
    other=processor.create(store,'Code');other=processor.prepare(store,other['id'],[('code.md',b'```js\nconst a = 1;\n```')])
    code=next(r['id'] for r in other['processor']['resources'] if r['kind']=='code')
    assert any(c['code']=='DUPLICATE_RESOURCE' for c in processor.compile_result(other,('{{source:'+code+'}}\n\n')*2)['checks'])


def test_complete_native_merged_tables_and_footnotes_remain_deliverable(tmp_path):
    from sourceloom.export import safe_html
    raw='<table><tr><th colspan="2">Costs</th></tr><tr><td>A</td><td>1</td></tr></table>'
    second='<table><tr><th>Name</th><th>Value</th></tr><tr><td>B</td><td>2</td></tr></table>'
    store=Store(tmp_path/'data');p=processor.create(store,'Merged native source')
    p=processor.prepare(store,p['id'],[('source.html',(raw+second).encode())])
    md=raw+'\n'+second+'\n<p><a href="#loom-source-footnote-1">1</a></p>\n<div id="loom-source-footnote-1"><p>Actual note</p></div>\n'
    p=processor.save_result(store,p['id'],md);v=processor.active_version(p)
    assert v['mechanical_pass']
    groups=processor_issues.issues(p,store=store)
    assert len([g for g in groups['resolved'] if g['kind']=='table'])==2
    assert not any(g['category']=='repair' for g in groups['issues'])
    compiled=processor.compile_result(p,md)
    doc=BeautifulSoup(compiled['html'],'html.parser')
    assert doc.select_one('#loom-source-footnote-1') and doc.select_one('a[href="#loom-source-footnote-1"]')
    with zipfile.ZipFile(BytesIO(processor.export_package(store,p))) as package:
        exported=BeautifulSoup(package.read('material.html'),'html.parser')
    assert exported.select_one('#loom-source-footnote-1') and exported.select_one('a[href="#loom-source-footnote-1"]')
    assert 'id=' not in safe_html('<p id="loom-source-one">Default legacy sanitizer</p>')
    assert 'id=' not in processor.safe_html('<p id="arbitrary-ui-id" onclick="alert(1)">Unsafe</p>')
    bad=processor.compile_result(p,md.replace('colspan="2"','colspan="1"'))
    assert any(c['code']=='OMITTED_RESOURCE' for c in bad['checks'])


def test_native_pdf_web_protocol_is_not_reinterpreted_as_attachment_on_export(tmp_path):
    store=Store(tmp_path/'data');p=processor.create(store,'Original web link')
    raw=b'<p><a href="http:www.example.org/report?q=1#part">Original web reference</a></p>'
    p=processor.prepare(store,p['id'],[('source.html',raw)])
    md=raw.decode();p=processor.save_result(store,p['id'],md)
    before=digest(p);original_digest=p['inventory']['originals'][0]['sha256']
    with zipfile.ZipFile(BytesIO(processor.export_package(store,p))) as package:
        doc=BeautifulSoup(package.read('material.html'),'html.parser')
        assert doc.select_one('a')['href']=='http://www.example.org/report?q=1#part'
        assert package.read(next(n for n in package.namelist() if n.endswith('.md')))==raw
    assert digest(store.get(p['id']))==before and store.read_blob(original_digest)==raw


def test_present_image_with_unverified_reuse_is_not_a_missing_image_repair(tmp_path):
    store,p,sid=prepared(tmp_path)
    def reuse(project):
        row=next(r for r in project['processor']['resources'] if r['id']==sid)
        row.update(placement_count=2,placements=[dict(page=1,bbox=[0,0,16,12]),dict(page=2,bbox=[0,0,16,12])])
    store.change(p['id'],reuse)
    p=processor.save_result(store,p['id'],'# Draft\n\n{{source:'+sid+'}}\n')
    original=digest(p);v=processor.active_version(p)
    assert any(c['code']=='PLACEMENT_RELATION_UNVERIFIED' for c in v['checks'])
    g=next(g for g in processor_issues.issues(p,store=store)['issues'] if sid in g['source_ids'])
    assert g['category']=='confirm' and '缺少' not in g['title']
    assert 'insert_resource' not in g['actions'] and g['recommended_action'] is None
    assert '<img' in g['draft_preview'] and g['block_id']=='resource-'+sid
    assert not v['mechanical_pass'] and digest(store.get(p['id']))==original
