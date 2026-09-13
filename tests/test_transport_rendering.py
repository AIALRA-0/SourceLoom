import copy

import pytest

from sourceloom.providers import strict_schema, deepseek_schema, transport_schema, impossible_closed_schema
from sourceloom.writing import expand_response
from sourceloom.math_render import markdown_renderer
from sourceloom.export import safe_html


def test_schema_metadata_removal_never_deletes_named_properties_or_literal_values():
    literal={'title':'a','default':0,'minItems':3}
    schema={'type':'object','additionalProperties':False,'title':'Model',
            'properties':{name:{'type':'string','title':'Label','default':''}
                          for name in ('title','default','minItems')},
            'required':['title','default','minItems'],'examples':[literal]}
    original=copy.deepcopy(schema)
    for transform in (strict_schema,deepseek_schema,transport_schema):
        out=transform(schema)
        assert set(out['properties'])==set(schema['properties'])
        assert out['examples']==[literal]
        assert not impossible_closed_schema(out)
    assert schema==original
    assert impossible_closed_schema({'type':'object','properties':{},'required':['title'],'additionalProperties':False})


def flat(nodes):
    return {'encoding':'flat_nodes_v1','blocks':[dict(id='b',unit_id='u',kind='explanation',
        content=nodes,obligation_ids=[],object_ids=[],evidence=[])]}


def test_flat_hierarchy_keeps_exact_source_identity_text_and_list_order():
    text='我保留这个条件；你也不应改动它'
    nodes=[dict(type='section',node_id='s',parent_id='',heading='标题'),
           dict(type='list',node_id='l',parent_id='s',ordered=True),
           dict(type='list_item',node_id='i',parent_id='l',text=text),
           dict(type='list',node_id='n',parent_id='i',ordered=False),
           dict(type='list_item',node_id='j',parent_id='n',text='第二层'),
           dict(type='source',node_id='r',parent_id='s',id='source-1',presentation='code',language='python')]
    payload=flat(nodes);before=copy.deepcopy(payload)
    out=expand_response(payload)['blocks'][0]['content'][0]['blocks']
    assert out[0]['items'][0]['text']==text
    assert out[0]['items'][0]['children'][0]['text']=='第二层'
    assert out[1]['id']=='source-1' and out[1]['presentation']=='code'
    assert payload==before


@pytest.mark.parametrize('nodes',[
    [dict(type='section',node_id='s',parent_id='s',heading='cycle')],
    [dict(type='paragraph',node_id='p',parent_id='missing',text='orphan')],
    [dict(type='paragraph',node_id='p',parent_id='',text='one'),dict(type='paragraph',node_id='p',parent_id='',text='two')],
    [dict(type='list_item',node_id='i',parent_id='',text='unowned')],
])
def test_invalid_flat_structure_is_rejected_without_dropping_nodes(nodes):
    with pytest.raises(ValueError):expand_response(flat(nodes))


def test_math_preview_keeps_tex_and_readweave_uses_native_editor_storage():
    source=r'$x^2$'+'\n\n'+r'$$\frac{a}{b}$$'
    preview=safe_html(markdown_renderer().render(source))
    assert '<math' in preview and '<msup>' in preview and '<mfrac>' in preview
    assert 'application/x-tex' in preview and r'\frac{a}{b}' in preview
    native=safe_html(markdown_renderer('readweave').render(source))
    assert 'class="math-tex"' in native and r'\(x^2\)' in native
    assert r'\[\frac{a}{b}\]' in native


def test_alignment_survives_markdown_style_spelling_without_allowing_active_css():
    assert 'text-align: center;' in safe_html('<td style="text-align:center">x</td>')
    raw='<span style="text-align:center;background:url(https://example.invalid)">x</span><script>alert(1)</script>'
    assert safe_html(raw)=='<span>x</span>'


def test_mdn_frontmatter_is_not_a_heading_and_links_keep_original_and_published_targets(tmp_path):
    from sourceloom.store import Store
    from sourceloom.ingest import intake
    source=b'---\ntitle: Test\nslug: Glossary/Test\n---\n\nWe use [a reference](/en-US/docs/Test).'
    s=Store(tmp_path)
    inv=intake(s,[('test.md',source)],source_url='https://raw.githubusercontent.com/mdn/content/main/test.md')
    assert inv['objects'][0]['kind']=='metadata'
    link=next(o for o in inv['objects'] if o['kind']=='link')
    assert link['original_target']=='/en-US/docs/Test'
    assert link['target']=='https://developer.mozilla.org/en-US/docs/Test'
    assert s.read_blob(inv['originals'][0]['sha256'])==source


def test_teaching_inventory_retains_resources_needed_to_validate_actual_output(tmp_path):
    from sourceloom.production import Production
    from sourceloom.store import Store
    from sourceloom.ingest import intake
    from sourceloom.checks import inspect_draft
    s=Store(tmp_path);inv=intake(s,[('a.txt',b'We need both conditions.')])
    transferred=Production(s,{}).teaching_inventory(inv)
    transferred['frozen']=True
    findings=inspect_draft(transferred,{'blocks':[dict(id='b',unit_id='u',kind='explanation',
        markdown='已生成但遗漏条件',obligation_ids=[],object_ids=[],evidence=[])]})
    assert any(f['code']=='omission' for f in findings)


def test_authored_line_breaks_preserve_words_and_parentage():
    payload=flat([dict(type='paragraph',node_id='p',parent_id='',text='我保留条件\n你保留数量')])
    nodes=expand_response(payload)['blocks'][0]['content']
    assert [n['text'] for n in nodes]==['我保留条件','你保留数量']


def test_nested_source_binding_survives_expansion():
    payload=flat([dict(type='list',node_id='l',parent_id='',ordered=False),
                  dict(type='list_item',node_id='i',parent_id='l',text='原始资料'),
                  dict(type='source',node_id='s',parent_id='i',id='source-1',presentation='raw')])
    out=expand_response(payload)['blocks'][0]['content']
    assert out[0]['items'][0]['source_children'][0]['id']=='source-1'


def test_display_image_and_downloadable_original_never_share_native_identity(tmp_path):
    from scripts.rich_fixture import create
    from sourceloom.store import Store
    from sourceloom.export import export_zip
    from io import BytesIO
    import zipfile,json
    s=Store(tmp_path);p=create(s)
    with zipfile.ZipFile(BytesIO(export_zip(s,p))) as z:
        items=json.loads(z.read('!!!meta.json'))['files'][0]['attachments']
        assert len({a['attachmentId'] for a in items})==len(items)
        same_name=[a for a in items if a['title']=='plot.png']
        assert {a['role'] for a in same_name}=={'image','file'}
        assert len({z.read(a['dataFileName']) for a in same_name})==1
        from bs4 import BeautifulSoup
        doc=BeautifulSoup(z.read('material.html'),'html.parser')
        assert not doc.select('section[data-readweave-anchor-id]')
        assert len(doc.select('[data-readweave-anchor-id]'))==len(p['draft']['blocks'])
        assert doc.select('a[id^="loom-source-"]')
        assert all(doc.find(id=a['href'][1:]) for a in doc.select('a[href^="#"]'))
        assert not doc.select('table caption')


def test_visual_checks_do_not_turn_passing_observations_into_failures():
    from sourceloom.production import visual_decision
    categories=['text','reading_order','tables','formulas','figures','captions','footnotes']
    response={'pages':[{'source_id':'p','checks':[dict(category=c,status='preserved' if c in categories[:2] else 'not_applicable',
                region='whole page',observation='Compared exactly' if c in categories[:2] else 'This category is absent',transcript_excerpt='') for c in categories]}]}
    assert visual_decision(response)['pages'][0]['status']=='verified'
    response['pages'][0]['checks'][0].update(status='changed',observation='Original says we; transcript says they')
    result=visual_decision(response)['pages'][0]
    assert result['status']=='needs_correction' and len(result['discrepancies'])==1
    response['pages'][0]['checks'].pop()
    with pytest.raises(ValueError):visual_decision(response)
