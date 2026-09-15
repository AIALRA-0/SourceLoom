import pytest
from sourceloom.review_context import editable_lines, line_proposal
from sourceloom.store import Conflict


def test_source_code_and_links_are_never_line_repair_targets():
    source={'objects':[{'id':'c','kind':'code','text':'f();','fence_raw':'```js\nf();\n```'},
                       {'id':'l','kind':'link','text':'English','target':'https://example.org'}]}
    draft={'blocks':[{'id':'b','markdown':'Generated first\n```js\nf();\n```\nGenerated second\n[English](https://example.org)'}]}
    lines=editable_lines(draft,source,{'b'})
    assert [x['text'] for x in lines]==['Generated first','Generated second']
    with pytest.raises(Conflict):
        line_proposal({'document_digest':'d','edits':[{'line_id':'b:3','replacement':'changed','reason':'bad'}]},lines)


def test_same_line_cannot_receive_overlapping_staged_edits():
    lines=[{'line_id':'b:1','block_id':'b','text':'original whole sentence'}]
    edit={'line_id':'b:1','replacement':'one combined change','reason':'all findings on this line'}
    with pytest.raises(Conflict):line_proposal({'document_digest':'d','edits':[edit,edit]},lines)
    result=line_proposal({'document_digest':'d','edits':[edit]},lines)
    assert result['edits'][0]['old_text']=='original whole sentence'


def test_process_pass_requires_real_matching_execution_record():
    from sourceloom.production import style_issues
    review={'assessments':[{'rule_ids':['FMT-006'],'status':'pass','block_ids':[],
             'quotes':[],'execution_ids':['limit'],'reason':'counter'}],
            'mechanical_assessments':[],'findings':[]}
    report={'format':{'findings':[],'candidates':[]},
            'execution_evidence':{'limit':{'rule_ids':['FMT-006'],'status':'pass','attempts':1}}}
    assert style_issues(review,{'FMT-006':{}},{'blocks':[]},report)==[]
    report['execution_evidence']['limit']['status']='fail'
    assert style_issues(review,{'FMT-006':{}},{'blocks':[]},report)
    report['execution_evidence']['limit']={'rule_ids':['FMT-009'],'status':'pass'}
    assert style_issues(review,{'FMT-006':{}},{'blocks':[]},report)


def test_prose_rules_cannot_pass_using_execution_evidence():
    from sourceloom.production import style_issues
    review={'assessments':[{'rule_ids':['FMT-055'],'status':'pass','block_ids':[],
             'quotes':[],'execution_ids':['fake'],'reason':'fake'}],
            'mechanical_assessments':[],'findings':[]}
    report={'format':{'findings':[],'candidates':[]},
            'execution_evidence':{'fake':{'rule_ids':['FMT-055'],'status':'pass'}}}
    assert style_issues(review,{'FMT-055':{}},{'blocks':[]},report)


def test_raw_source_language_hint_does_not_change_source_bytes(tmp_path):
    from sourceloom.writing import compose
    runtime=tmp_path/'runtime';runtime.mkdir()
    (runtime/'composition.py').write_text('def render_document(body, sources):\n assert "language" not in body["blocks"][0]\n return sources["s"]\n',encoding='utf8')
    inv={'objects':[{'id':'s','kind':'metadata','text':'title: Example\n','locator':'frontmatter'}]}
    body={'blocks':[{'id':'b','unit_id':'u','kind':'document_info',
                    'content':[{'type':'source','id':'s','presentation':'raw','language':'yaml'}]}]}
    assert compose({'root':str(tmp_path)},body,inv)['blocks'][0]['markdown']=='title: Example\n'
    assert body['blocks'][0]['content'][0]['language']=='yaml'


def test_long_review_references_reconstruct_every_original_character():
    from sourceloom.providers import reference_repeated_text
    import json
    text='A complete source paragraph with numbers 1.25 and punctuation.\n'*25
    original={'source':{'text':text},'facts':[{'quote':text}],'findings':[{'quote':text}],'unique':'unchanged'}
    compact=reference_repeated_text(original);pool=compact['verbatim_texts']
    def resolve(value):
        if isinstance(value,dict):
            if set(value)=={'verbatim_text_ref'}:return pool[value['verbatim_text_ref']]
            return {k:resolve(v) for k,v in value.items() if k not in {'verbatim_texts','verbatim_text_reference_rule'}}
        if isinstance(value,list):return [resolve(v) for v in value]
        return value
    assert resolve(compact)==original
    assert len(json.dumps(compact))<len(json.dumps(original))
    assert original['source']['text']==text
    reserved=original|{'untrusted':{'verbatim_text_ref':'literal user data'}}
    assert reference_repeated_text(reserved)==reserved


def test_review_table_encoding_preserves_ids_order_and_all_values():
    from sourceloom.providers import compact_review_tables
    rows=[{'id':str(i),'location':'line '+str(i),'old_text':'exact\ncharacters',
           'rule_id':'FMT-001','status':'candidate','severity':'unknown','reason':'review','repair_scope':'line'} for i in range(10)]
    result=compact_review_tables({'mechanical_candidates':rows})['mechanical_candidates']
    assert [dict(zip(result['columns'],row)) for row in result['rows']]==rows
    facts=compact_review_tables({'facts':{'facts':rows,'unresolved':['keep']}})['facts']
    assert [dict(zip(facts['facts']['columns'],row)) for row in facts['facts']['rows']]==rows
    assert facts['unresolved']==['keep']
    assert compact_review_tables({'mechanical_candidates':[{'a':1},{'b':2},{'c':3}]})=={'mechanical_candidates':[{'a':1},{'b':2},{'c':3}]}


def test_failed_attempt_is_readable_without_replacing_saved_revision(tmp_path):
    import copy
    from fastapi.testclient import TestClient
    from sourceloom.app import create_app
    from sourceloom.config import load_config
    from sourceloom.demo import create_demo
    from sourceloom.writing import canonical
    app=create_app(load_config()|{'data_dir':str(tmp_path),'provider':'manual'})
    store=app.state.store;project=create_demo(store);other=create_demo(store)
    candidate=copy.deepcopy(project['draft'])
    candidate['blocks'][0]['markdown']='## 本次实际新稿\n\n待核对的完整正文'
    job=dict(id='candidate-attempt',project=project['id'],role='production',status='failed',
             created=1,stage='style',draft=candidate,inventory=project['inventory'],
             plan=project['plan'],calls=[{'request_blob':'private-request-marker'}])
    store.put_job(job)
    with TestClient(app) as client:
        status=client.get(f"/api/projects/{project['id']}/production").json()
        assert 'private-request-marker' not in str(status)
        url=status['candidate_url'];assert url
        download=client.get(url+'?format=markdown')
        assert download.status_code==200 and download.content==canonical(candidate).encode()
        html=client.get(url)
        assert html.status_code==200 and '尚未通过全部核对' in html.text
        assert '本次实际新稿' in html.text and 'private-request-marker' not in html.text
        assert 'sandbox' in html.headers['content-security-policy']
        assert client.get(url.replace(project['id'],other['id'])).status_code==404
        assert client.get(url+'?format=unknown').status_code==400
        assert client.get(f"/api/projects/{project['id']}/output?format=markdown").content==canonical(project['draft']).encode()
    assert store.get(project['id'])['revision']==project['revision']
    assert store.get(project['id'])['draft']==project['draft']
