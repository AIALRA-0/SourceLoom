"""Local issue edits must rebuild occurrence-bound PDF compositions."""
import copy
from io import BytesIO

import pytest

from bs4 import BeautifulSoup
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from sourceloom import processor, processor_issues
from sourceloom.store import Store


def document(tmp_path, table=False, third_component=False):
    raw = BytesIO()
    pdf = canvas.Canvas(raw, pagesize=(300, 400))
    pdf.drawImage(ImageReader(Image.new('RGB', (100, 100), 'yellow')),
                  20, 200, width=90, height=90)
    pdf.drawString(20, 170, 'First figure')
    pdf.showPage()
    for index, color in enumerate(['red', 'blue'] + (['green'] if third_component else [])):
        pdf.drawImage(ImageReader(Image.new('RGB', (100, 100), color)),
                      20+index*90, 200, width=80, height=80)
    pdf.save()
    store = Store(tmp_path)
    p = processor.create(store, 'Local edit PDF')
    p = processor.prepare(store, p['id'], [('source.pdf', raw.getvalue())])
    first = next(o for o in p['inventory']['objects'] if o['kind']=='image' and o['placements'][0]['page']==1)
    images = [o for o in p['inventory']['objects'] if o['kind']=='image' and o['placements'][0]['page']==2]
    md = '# Report\n\n图像位置。\n\n'
    if table:
        page = next(o for o in p['inventory']['objects'] if o['kind']=='page' and o['locator'].endswith('/page[1]'))
        md += '{{source:'+page['id']+'}}\n\n| 项目 | 数值 |\n| --- | --- |\n| A | 1 |\n\n'
    md += '## Later composite\n\n'+'\n\n'.join('{{source:'+o['id']+'}}' for o in images[:2])+'\n\n独立图注\n'
    p = processor.save_result(store, p['id'], md)
    return store, p, first, images


def apply(store, p, request):
    old = copy.deepcopy(processor.active_version(p))
    preview = processor_issues.preview(store, p['id'], old['id'], request)
    assert processor.active_version(store.get(p['id'])) == old
    p = processor_issues.apply(store, p['id'], old['id'],
                              dict(preview_id=preview['preview_id'], request=request))
    assert next(v for v in p['processor']['versions'] if v['id']==old['id']) == old
    return p, preview, processor.active_version(p)


def test_insert_before_later_group_rebinds_preview_and_commit_without_losing_coverage(tmp_path):
    store, p, first, images = document(tmp_path)
    old = copy.deepcopy(processor.active_version(p))
    original = copy.deepcopy(p['inventory'])
    listing = processor_issues.issues(p, store=store)
    issue = next(g for g in listing['issues'] if first['id'] in g['source_ids'])
    target = next(b['block_id'] for b in listing['blocks'] if '图像位置' in b['label'])
    request = dict(issue_id=issue['id'], action='insert_resource', block_id=target)
    p, preview, new = apply(store, p, request)
    assert preview['body_changed'] and new['id'] != old['id']
    prior = next(r for r in old['derived_resources'] if r.get('pdf_component_group'))
    rebound = next(r for r in preview['derived_resources'] if r.get('pdf_component_group'))
    assert rebound['sha256'] == prior['sha256']
    assert rebound['pdf_component_group']['source_ids'] == prior['pdf_component_group']['source_ids']
    assert rebound['pdf_component_group']['start_line'] > prior['pdf_component_group']['start_line']
    assert new['derived_resources'] == preview['derived_resources']
    assert p['inventory'] == original
    compiled = processor.compile_result(p, new['markdown'])
    assert compiled['mechanical_pass']
    assert len(BeautifulSoup(compiled['html'], 'html.parser').select('figure[data-source-role="original-page-composition"]')) == 1
    assert set(compiled['inserted_source_ids']) == {first['id'], *(o['id'] for o in images)}


def test_table_page_derivative_survives_rebind_and_following_prose_edit(tmp_path):
    store, p, first, images = document(tmp_path, table=True)
    listing = processor_issues.issues(p, store=store)
    table = next(g for g in listing['issues'] if g['kind']=='table' and g['page']==1)
    p, preview, v = apply(store, p, dict(issue_id=table['id'], action='add_page_reference'))
    page_reference = next(r for r in v['derived_resources'] if not r.get('pdf_component_group'))
    assert page_reference['region_precision'] == 'page'
    assert store.read_blob(page_reference['sha256']).startswith(b'\x89PNG')
    assert len([r for r in v['derived_resources'] if r.get('pdf_component_group')]) == 1
    p, _, updated = apply(store, p, dict(issue_id='format', action='format'))
    assert '图像位置.' in updated['markdown']
    assert page_reference in updated['derived_resources']
    assert len([r for r in updated['derived_resources'] if r.get('pdf_component_group')]) == 1
    assert any(m['mapping']=='original_page_composition' for m in processor.compile_result(p, updated['markdown'])['source_map'])


@pytest.mark.parametrize('invalid', ['separated', 'reordered', 'foreign_source'])
def test_rebuilt_group_does_not_relax_current_occurrence_or_source_binding(tmp_path, invalid):
    store, p, first, images = document(tmp_path, third_component=True)
    # The two body markers render the whole page, so the third image is covered.
    old = processor.active_version(p)
    assert not any(c.get('source_id')==images[2]['id'] for c in old['checks'])
    listing = processor_issues.issues(p, store=store)
    group = next(g for g in listing['issues'] if first['id'] in g['source_ids'])
    p, _, new = apply(store, p, dict(issue_id=group['id'], action='insert_resource',
                                   block_id=listing['blocks'][0]['block_id']))
    md = new['markdown']
    markers = ['{{source:'+o['id']+'}}' for o in images[:2]]
    if invalid == 'separated':
        md = md.replace(markers[0], markers[0]+'\n\nIndependent intervening prose')
    elif invalid == 'reordered':
        md = md.replace(markers[0], '{{temporary}}').replace(markers[1], markers[0]).replace('{{temporary}}', markers[1])
    else:
        p = copy.deepcopy(p)
        p['processor']['source_digest'] = '0'*64
    compiled = processor.compile_result(p, md, derived_resources=new['derived_resources'])
    assert not any(m['mapping']=='original_page_composition' for m in compiled['source_map'])
    assert any(c['code']=='OMITTED_RESOURCE' and c['source_id']==images[2]['id'] for c in compiled['checks'])
