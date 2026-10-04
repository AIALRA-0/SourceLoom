"""Layout containers do not acquire original data-table insertion duties."""
import copy
from io import BytesIO

import pytest
from PIL import Image

from sourceloom import processor
from sourceloom.source_context import classify_layout_tables
from sourceloom.store import Store, digest


LAYOUT = ('<table cellpadding="0" cellspacing="0"><tr>'
          '<td width="2"><img src="spacer.png" alt="" width="2" height="1"></td>'
          '<td><img src="figure.png" alt="Research diagram"></td>'
          '<td width="25"><img src="spacer.png" alt="" width="25" height="1"></td>'
          '<td valign="top"><p>Original article first paragraph.</p>'
          '<p>Original article next paragraph with <a href="https://example.invalid/evidence">Evidence</a>.</p>'
          '<ul><li>Original related resource</li></ul></td></tr></table>')


def picture():
    output=BytesIO()
    Image.new('RGB',(8,8),'blue').save(output,'PNG')
    return output.getvalue()


def test_processor_layout_container_stays_prose_without_restored_english_duplicate(tmp_path):
    store=Store(tmp_path)
    original=('<article><h1>Original article</h1>'+LAYOUT+'</article>').encode()
    project=processor.create(store,'Synthetic layout article')
    prepared=processor.prepare(store,project['id'],[('source.html',original),
        ('figure.png',picture()),('spacer.png',picture())])
    objects=prepared['inventory']['objects']
    container=next(o for o in objects if o.get('original_kind')=='table')
    assert container['kind']=='text'
    assert container['layout_classification']=='single-row-one-text-cell-with-image-layout'
    assert '<table' in container['raw'] and 'Original article first paragraph.' in container['raw']
    original_entry=next(o for o in prepared['inventory']['originals'] if o['name']=='source.html')
    assert store.read_blob(original_entry['sha256'])==original
    assert original_entry['sha256']==digest(original)
    pack=processor.task_pack(store,project['id'])
    assert not any(r['kind']=='table' for r in pack['resources'])
    assert '{{source:'+container['id']+'}}' not in pack['source_text']
    assert pack['source_text'].count('Original article first paragraph.')==1
    assert '[Evidence](https://example.invalid/evidence)' in pack['source_text']
    images=[r for r in pack['resources'] if r['kind']=='image']
    assert len(images)==3
    assert all(r['parent_id']==container['id'] and r['parent_kind']=='text' for r in images)
    assert not any('表格' in r.get('purpose_hint','') for r in images)
    refs=[o for o in objects if o['kind']=='link']
    assert len(refs)==1 and refs[0]['parent_id']==container['id']
    source_before=copy.deepcopy(prepared['inventory'])
    markdown='# 合成中文稿\n\n此稿只用于测试资源恢复，不是模型质量证明\n\n'+'\n\n'.join(r['marker'] for r in images)
    saved=processor.save_result(store,project['id'],markdown)
    assert processor.active_version(saved)['markdown']==markdown
    compiled=processor.compile_result(saved,markdown)
    assert '<table' not in compiled['html']
    assert 'Original article first paragraph.' not in compiled['html']
    assert not any(c['code']=='OMITTED_RESOURCE' and c.get('source_id')==container['id'] for c in compiled['checks'])
    assert saved['inventory']==source_before


@pytest.mark.parametrize('raw',[
    '<table><tr><th>Item</th><th>Value</th></tr><tr><td>A</td><td>2</td></tr></table>',
    '<table><caption>Observations</caption><tr><td><img src="x.png"></td><td><p>Value</p></td></tr></table>',
    '<table><tr><td rowspan="2"><img src="x.png"></td><td><p>Group A</p></td></tr><tr><td>2</td></tr></table>',
    '<table><tr><td><img src="x.png"></td><td colspan="2"><p>Group value</p></td></tr></table>',
    '<table><tr><td><img src="x.png"></td><td rowspan="0"><p>Group value</p></td></tr></table>',
    '<table role="table"><tr><td><img src="x.png"></td><td><p>Value</p></td></tr></table>',
    '<table role="grid"><tr><td><img src="x.png"></td><td><p>Value</p></td></tr></table>',
    '<table role="treegrid"><tr><td><img src="x.png"></td><td><p>Value</p></td></tr></table>',
    '<table><tr><td><img src="x.png"></td><td scope="col"><p>Value</p></td></tr></table>',
    '<table><tr><td><img src="x.png"></td><td headers="h1"><p>Value</p></td></tr></table>',
    '<table><tr><td>24</td><td>70%</td></tr></table>',
    '<table><tr><td><p>Row label</p><img src="x.png"></td><td><p>Value</p></td></tr></table>',
])
def test_explicit_and_ambiguous_data_tables_keep_original_cells_and_marker(tmp_path,raw):
    source={'objects':[{'id':'s1','kind':'table','raw':raw,'text':'Original data'}]}
    before=copy.deepcopy(source)
    assert classify_layout_tables(source)==before
    assert source==before
    store=Store(tmp_path)
    project=processor.create(store,'Synthetic data table')
    prepared=processor.prepare(store,project['id'],[('source.html',raw.encode())])
    table=next(o for o in prepared['inventory']['objects'] if o['kind']=='table')
    assert table['raw'] and 'layout_classification' not in table
    pack=processor.task_pack(store,project['id'])
    assert any(r['id']==table['id'] and r['kind']=='table' for r in pack['resources'])
    assert '{{source:'+table['id']+'}}' in pack['source_text']
    original_cells=copy.deepcopy(table['cells'])
    compiled=processor.compile_result(prepared,'# 合成稿\n\n{{source:'+table['id']+'}}')
    assert '<table' in compiled['html']
    assert table['cells']==original_cells
