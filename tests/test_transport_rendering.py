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


def test_adjacent_section_continuation_keeps_blocks_and_source_voice():
    payload=flat([dict(type='section',node_id='s',parent_id='',heading='条件'),dict(type='paragraph',node_id='p',parent_id='s',text='我们先设置条件')])
    second=flat([dict(type='paragraph',node_id='q',parent_id='s',text='你可以接着检查结果')])['blocks'][0]
    second['id']='second';payload['blocks'].append(second);before=copy.deepcopy(payload)
    expanded=expand_response(payload)
    assert [b['id'] for b in expanded['blocks']]==['b','second']
    assert expanded['blocks'][1]['content']==[dict(type='paragraph',text='你可以接着检查结果')]
    assert payload==before
    payload['blocks'][1]['unit_id']='another-unit'
    with pytest.raises(ValueError,match='父节点'):expand_response(payload)
    payload=before
    separator=flat([dict(type='section',node_id='new',parent_id='',heading='另一主题')])['blocks'][0]
    payload['blocks'].insert(1,separator)
    with pytest.raises(ValueError,match='父节点'):expand_response(payload)


def test_bare_section_adopts_following_siblings_only_until_next_heading():
    response=flat([dict(type='section',node_id='s',parent_id='',heading='第一部分'),
        dict(type='paragraph',node_id='p',parent_id='',text='我们保留这段'),
        dict(type='section',node_id='t',parent_id='',heading='第二部分'),
        dict(type='paragraph',node_id='q',parent_id='',text='你保留下一段')])
    before=copy.deepcopy(response);out=expand_response(response)['blocks'][0]['content']
    assert [x['heading'] for x in out]==['第一部分','第二部分']
    assert [x['blocks'][0]['text'] for x in out]==['我们保留这段','你保留下一段']
    assert response==before


def test_empty_unordered_term_wrappers_preserve_definition_and_reject_ordered_loss():
    terms=[dict(type='term',node_id='t'+str(i),parent_id='i'+str(i),zh=name,en='',definition=['是什么','怎样用','限制是什么']) for i,name in enumerate(['甲','乙'])]
    nodes=[dict(type='list',node_id='l',parent_id='',ordered=False)]
    for i,t in enumerate(terms):nodes.extend([dict(type='list_item',node_id='i'+str(i),parent_id='l',text=''),t])
    payload=flat(nodes);before=copy.deepcopy(payload)
    assert [n['zh'] for n in expand_response(payload)['blocks'][0]['content']]==['甲','乙']
    assert payload==before
    payload['blocks'][0]['content'][0]['ordered']=True
    with pytest.raises(ValueError):expand_response(payload)


def test_reference_repair_cannot_change_voice_source_nodes_or_block_order():
    from sourceloom.writing import validate_binding_repair
    original=flat([dict(type='paragraph',node_id='p',parent_id='',text='我们保留条件')])
    repaired=copy.deepcopy(original);repaired['blocks'][0]['obligation_ids']=['f1']
    assert validate_binding_repair(original,repaired)==repaired
    repaired['blocks'][0]['content'][0]['text']='原文保留条件'
    with pytest.raises(ValueError,match='不能改写'):validate_binding_repair(original,repaired)
    repaired=copy.deepcopy(original);repaired['blocks'][0]['unit_id']='other'
    with pytest.raises(ValueError,match='身份'):validate_binding_repair(original,repaired)
    repaired=copy.deepcopy(original);repaired['blocks'][0]['evidence']=[dict(source_id='s',quote_source_id='s')]
    inv={'objects':[dict(id='s',text='We keep the condition.') ]}
    result=validate_binding_repair(original,repaired,inv)
    assert result['blocks'][0]['evidence']==[dict(source_id='s',quote='We keep the condition.')]
    assert 'quote' not in repaired['blocks'][0]['evidence'][0]
    repaired['blocks'][0]['evidence'][0]['quote_source_id']='other'
    with pytest.raises(ValueError,match='别名'):validate_binding_repair(original,repaired,inv)


def test_completion_appends_new_blocks_without_replacing_existing_voice():
    from sourceloom.writing import append_unit_completion
    original=flat([dict(type='paragraph',node_id='p',parent_id='',text='我们已有这一段')])
    extra=flat([dict(type='paragraph',node_id='q',parent_id='',text='你还需要这个条件')]);extra['blocks'][0]['id']='new'
    before=copy.deepcopy(original);result=append_unit_completion(original,extra,'u')
    assert result['blocks'][0]==before['blocks'][0] and len(result['blocks'])==2 and original==before
    extra['blocks'][0]['id']='b'
    with pytest.raises(ValueError,match='覆盖'):append_unit_completion(original,extra,'u')
    extra['blocks'][0].update(id='other',unit_id='v')
    with pytest.raises(ValueError,match='其他'):append_unit_completion(original,extra,'u')


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
    from sourceloom.writing import protected_objects
    rendered=safe_html(markdown_renderer().render(protected_objects(inv)[link['id']]))
    assert 'href="https://developer.mozilla.org/en-US/docs/Test"' in rendered
    assert '原始链接目标：/en-US/docs/Test' in rendered
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


def test_layout_repair_cannot_smuggle_changed_prose_or_source_bindings():
    from sourceloom.writing import validate_layout_repair
    original=flat([dict(type='list_item',node_id='i',parent_id='',text='我们保留两个条件')])
    candidate=copy.deepcopy(original)
    candidate['blocks'][0]['content'].insert(0,dict(type='list',node_id='list',parent_id='',ordered=False))
    candidate['blocks'][0]['content'][1]['parent_id']='list'
    assert validate_layout_repair(original,candidate)==candidate
    changed=copy.deepcopy(candidate);changed['blocks'][0]['content'][1]['text']='他们保留一个条件'
    with pytest.raises(ValueError,match='重写'):validate_layout_repair(original,changed)
    changed=copy.deepcopy(candidate);changed['blocks'][0]['obligation_ids']=['other']
    with pytest.raises(ValueError,match='来源'):validate_layout_repair(original,changed)


def test_paragraph_list_decode_never_renumbers_source_numbers():
    out=expand_response(flat([dict(type='paragraph',node_id='p',parent_id='',text='42. 我保留这个编号')]))
    assert out['blocks'][0]['content'][0]['text']=='42. 我保留这个编号'
    out=expand_response(flat([dict(type='paragraph',node_id='p',parent_id='',text='- 我们保留两个条件')]))
    assert out['blocks'][0]['content'][0]['items'][0]['text']=='我们保留两个条件'


@pytest.mark.parametrize('newline',['\n','\r\n'])
def test_quoted_original_keeps_blank_lines_and_changed_pronoun_is_rejected(newline):
    from sourceloom.checks import inspect_draft
    inv={'frozen':True,'objects':[dict(id='s',kind='text',text='We use it.\n\nYou keep it.')], 'obligations':[],'resources':[]}
    b=dict(id='b',unit_id='u',kind='source',markdown='> We use it.\n> \n> You keep it.',obligation_ids=[],object_ids=['s'],embedded_object_ids=['s'],evidence=[])
    inv['objects'][0]['text']=inv['objects'][0]['text'].replace('\n',newline)
    b['markdown']=b['markdown'].replace('\n',newline)
    assert not inspect_draft(inv,{'blocks':[b]})
    b['markdown']=b['markdown'].replace('We','They')
    assert any(x['code']=='embedded_bytes' for x in inspect_draft(inv,{'blocks':[b]}))

def test_native_import_link_rewriting_cannot_change_literal_code_or_prose():
    import re
    from bs4 import BeautifulSoup
    from sourceloom.export import editor_storage
    raw='<p>Literal src="diagram.png" and href="chapter.html"</p><pre><code>&lt;script src="index.js"&gt;\n&lt;/script&gt;\n</code></pre><img src="real.png" alt="real"><a href="chapter.html">Read</a>'
    encoded=editor_storage(raw)
    # Match the upstream importer's serialized attribute rewrite boundary.
    imported=re.sub(r'(src|href)="([^"]*)"',lambda m:m[1]+'="api/imported/'+m[2]+'"',encoded)
    before=BeautifulSoup(raw,'html.parser');after=BeautifulSoup(imported,'html.parser')
    assert before.pre.get_text()==after.pre.get_text()
    assert before.p.get_text()==after.p.get_text()
    assert after.img['src']=='api/imported/real.png'
    assert after.a['href']=='api/imported/chapter.html'

def test_native_candidate_identity_tracks_export_bytes_not_zip_timestamps(tmp_path,monkeypatch):
    import json,zipfile
    from io import BytesIO
    from scripts.rich_fixture import create
    from sourceloom.store import Store
    import sourceloom.export as export
    s=Store(tmp_path);p=create(s)
    def key():
        with zipfile.ZipFile(BytesIO(export.export_zip(s,p))) as z:
            return json.loads(z.read('!!!meta.json'))['files'][0]['attributes'][0]['value']
    first=key();assert key()==first
    original=export.editor_storage
    monkeypatch.setattr(export,'editor_storage',lambda raw:original(raw)+'<!-- transport revision -->')
    second=key();assert second!=first
    assert first.startswith(p['id']+':'+str(p['revision'])+':')
    assert second.startswith(p['id']+':'+str(p['revision'])+':')

@pytest.mark.parametrize('legacy',[False,True])
def test_uncertain_native_import_is_not_replayed_after_transport_changes(tmp_path,monkeypatch,legacy):
    import json,zipfile,httpx
    from io import BytesIO
    from scripts.rich_fixture import create
    from sourceloom.store import Store,Conflict
    from sourceloom.export import export_zip
    from sourceloom.readweave import import_candidate
    s=Store(tmp_path);p=create(s)
    with zipfile.ZipFile(BytesIO(export_zip(s,p))) as z:
        key=json.loads(z.read('!!!meta.json'))['files'][0]['attributes'][0]['value']
    folder=s.root/'readweave';folder.mkdir()
    suffix='' if legacy else '-'+key.rsplit(':',1)[-1]
    (folder/(p['id']+'-'+str(p['revision'])+suffix+'.json')).write_text(json.dumps({'status':'submitted'}))
    calls=[]
    def handler(request):
        calls.append(request.method)
        assert request.method=='GET'
        return httpx.Response(200,json={'results':[]})
    client=httpx.Client
    monkeypatch.setattr(httpx,'Client',lambda **kwargs:client(**kwargs,transport=httpx.MockTransport(handler)))
    with pytest.raises(Conflict,match='不确定'):
        import_candidate(s,{'readweave_url':'https://reader.example','readweave_token':'synthetic','readweave_parent':'parent'},p['id'])
    assert calls==['GET']
