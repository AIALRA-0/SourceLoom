import json
import pytest
from sourceloom.providers import reasoning_exhausted, ReasoningExhausted


@pytest.mark.parametrize('active',[None,2])
def test_rejected_repair_never_dispatches_a_third_round(tmp_path,monkeypatch,active):
    from sourceloom.production import Production
    from sourceloom.store import Store,Conflict
    store=Store(tmp_path);engine=Production(store,{})
    job={'id':'j','project':'p','role':'production','status':'running','created':1,
         'writing_skill':{'root':'unused','package_digest':'d'},'goal':'rewrite',
         'source':{'objects':[]},'stage':'repair','repair_rounds':2,
         'draft':{'blocks':[{'id':'b','markdown':'unchanged'}]},
         'style':{'findings':[{'block_id':'b'}]},'results':{},'quality_issues':['format']}
    monkeypatch.setattr('sourceloom.production.load_bundle',lambda *a:{})
    calls=[]
    def call(j,key,*args):
        assert store.job('j')['active_repair_round']==2
        if key not in j['results']:calls.append(key);j['results'][key]={'edits':[]}
        return j['results'][key]
    monkeypatch.setattr(engine,'_call',call)
    monkeypatch.setattr('sourceloom.production.repair',lambda *a:(_ for _ in ()).throw(Conflict('invalid patch')))
    if active:
        job.update(repair_rounds=1)
        for _ in range(2):
            assert engine.step(job)=='needs_attention'
        assert calls==['local_repair-2'] and job['active_repair_round']==2
        assert job['repair_rejection']=='invalid patch'
    else:
        assert engine.step(job)=='needs_attention' and calls==[]
    assert job['draft']['blocks'][0]['markdown']=='unchanged' and job['repair_rounds']==2


def test_noop_repair_is_rejected_before_committer_runs(tmp_path):
    from sourceloom.writing import repair,canonical
    from sourceloom.store import digest,Conflict
    draft={'blocks':[{'id':'b','markdown':'unchanged'}]}
    proposal={'document_digest':digest(canonical(draft).encode()),
              'edits':[{'block_id':'b','old_text':'unchanged','new_text':'unchanged','reason':'placeholder'}]}
    with pytest.raises(Conflict,match='没有实际修改'):repair({},draft,proposal,{'b'},tmp_path)
    assert not list(tmp_path.iterdir())


def test_embedded_original_object_binds_its_fact_without_claiming_prose_coverage(tmp_path):
    from sourceloom.writing import compose,protected_objects
    runtime=tmp_path/'runtime';runtime.mkdir()
    (runtime/'composition.py').write_text('def render_document(body, sources):\n return next(iter(sources.values()))\n',encoding='utf-8')
    inv={'objects':[{'id':'code','kind':'code','text':'x = 1\n','locator':'code'},
                    {'id':'prose','kind':'text','text':'A separate condition.','locator':'p'}],
         'obligations':[{'id':'f-code','object_id':'code'},{'id':'f-prose','object_id':'prose'}]}
    body={'blocks':[{'id':'b','unit_id':'u','kind':'explanation','content':[{'type':'source','id':'code'}]}]}
    result=compose({'root':str(tmp_path)},body,inv)['blocks'][0]
    assert result['obligation_ids']==['f-code']
    assert protected_objects(inv)['code'] in result['markdown']
    assert result['evidence']==[{'source_id':'code','quote':'x = 1\n'}]
    assert 'obligation_ids' not in body['blocks'][0]


def response(content='',reason='length',completion=100,thinking=100):
    return {'choices':[{'finish_reason':reason,'message':{'content':content}}],
            'usage':{'completion_tokens':completion,'completion_tokens_details':{'reasoning_tokens':thinking}}}


def test_reasoning_exhaustion_never_classifies_partial_or_unknown_output_as_empty():
    assert reasoning_exhausted(response())
    assert reasoning_exhausted(response(reason='stop'))
    for body in [response('{'),response(reason='content_filter'),response(completion=0,thinking=0),response(thinking=99),{}]:
        assert not reasoning_exhausted(body)
    body=response();body['choices'][0]['message']['tool_calls']=[{'id':'partial'}]
    assert not reasoning_exhausted(body)


def test_missing_audit_quotes_cannot_pass_and_quotes_must_match_named_blocks():
    from sourceloom.production_contracts import StyleReview
    from sourceloom.production import style_issues
    review={'assessments':[{'rule_ids':['FMT-001'],'status':'fail','block_ids':['a'],'reason':'Missing definition'}],
            'mechanical_assessments':[],'findings':[]}
    parsed=StyleReview.model_validate(review).model_dump()
    draft={'blocks':[{'id':'a','markdown':'first'},{'id':'b','markdown':'elsewhere'}]}
    report={'format':{'findings':[],'candidates':[]}}
    assert style_issues(parsed,{'FMT-001'},draft,report)
    parsed['assessments'][0]['status']='pass'
    assert '写作规则通过声明缺少正文证据' in style_issues(parsed,{'FMT-001'},draft,report)
    parsed['assessments'][0]['quotes']=['elsewhere']
    assert '写作审核引用的成稿原句不匹配' in style_issues(parsed,{'FMT-001'},draft,report)


def test_binding_patch_selects_exact_sources_without_rewriting_any_content():
    from sourceloom.writing import apply_binding_patch
    original={'blocks':[{'id':'b','content':[{'type':'paragraph','text':'我们选择'}],
                         'object_ids':['s'],'evidence':[{'source_id':'s','quote':'wrong'}],'obligation_ids':[]}]}
    inv={'objects':[{'id':'s','text':'We choose.'}],'obligations':[{'id':'f','object_id':'s'}]}
    patch={'assignments':[{'block_id':'b','source_ids':['s'],'obligation_ids':['f']}]}
    result=apply_binding_patch(original,patch,inv)
    assert result['blocks'][0]['content']==original['blocks'][0]['content']
    assert result['blocks'][0]['object_ids']==['s']
    assert result['blocks'][0]['evidence']==[{'source_id':'s','quote':'We choose.'}]
    assert original['blocks'][0]['obligation_ids']==[]
    patch['assignments'][0]['source_ids']=['unknown']
    with pytest.raises(ValueError):apply_binding_patch(original,patch,inv)
    assert original['blocks'][0]['evidence'][0]['quote']=='wrong'


def test_continuation_leaf_reference_keeps_active_section_but_rejects_stale_one():
    from sourceloom.writing import expand_response
    def block(bid,node):return {'id':bid,'unit_id':'u','kind':'explanation','content':[node]}
    heading=block('h',{'type':'section','node_id':'heading','parent_id':'','heading':'参见'})
    leaf=block('a',{'type':'paragraph','node_id':'leaf','parent_id':'heading','text':'已有说明'})
    extra=block('b',{'type':'paragraph','node_id':'extra','parent_id':'leaf','text':'后续说明'})
    draft={'encoding':'flat_nodes_v1','blocks':[heading,leaf,extra]}
    assert expand_response(draft)['blocks'][-1]['content']==[{'type':'paragraph','text':'后续说明'}]
    assert draft['blocks'][-1]['content'][0]['parent_id']=='leaf'
    newer=block('new',{'type':'section','node_id':'new-heading','parent_id':'','heading':'新主题'})
    with pytest.raises(ValueError,match='不存在的父节点'):
        expand_response(draft|{'blocks':[heading,leaf,newer,extra]})


def test_completed_reasoning_can_use_only_one_explicit_fallback(tmp_path,monkeypatch):
    from sourceloom.production import Production
    from sourceloom.store import Store
    store=Store(tmp_path);engine=Production(store,{'fallback_providers':{'writer':{'reasoning_effort':'low'}}})
    call={'id':'paid','status':'reasoning_exhausted','finish_reason':'length','response_blob':store.blob(json.dumps(response()).encode())}
    job={'id':'job','project':'project','role':'production','status':'running','created':1,'calls':[call],'results':{},'pending':'writer-u'}
    calls=[]
    monkeypatch.setattr(engine,'_call',lambda j,key,role,payload,schema:calls.append((key,role)) or {'blocks':[]})
    result=engine._invalid_response_fallback(job,'writer-u','writer',{},dict)
    assert result=={'blocks':[]} and calls==[('writer-u','writer')]
    assert job['fallbacks']['writer-u']['original_call']=='paid' and job['calls']==[call]
    with pytest.raises(ValueError):engine._invalid_response_fallback(job,'writer-u__fallback','writer__fallback',{},dict)
    call.update(status='uncertain',finish_reason=None)
    with pytest.raises(ValueError):engine._invalid_response_fallback(job,'writer-u','writer',{},dict)


@pytest.mark.parametrize('protected',['','original\n'])
def test_composer_removes_only_its_own_terminal_newline(tmp_path,protected):
    from sourceloom.writing import compose,canonical
    runtime=tmp_path/'runtime';runtime.mkdir()
    (runtime/'composition.py').write_text('def render_document(body, sources):\n return next(iter(sources.values())) if sources else "说明\\n"\n',encoding='utf-8')
    inv={'objects':[{'id':'s','kind':'quote','text':protected,'locator':'s'}] if protected else []}
    content=[{'type':'source','id':'s'}] if protected else [{'type':'paragraph','text':'说明'}]
    draft=compose({'root':str(tmp_path)},{'blocks':[{'id':'b','unit_id':'u','kind':'explanation','content':content}]},inv)
    assert draft['blocks'][0]['markdown']==(protected or '说明')
    if protected:assert protected in canonical(draft)


@pytest.mark.parametrize('changed',[None,'goal','skill','partial','policy'])
def test_explicit_new_rewrite_reuses_only_unchanged_empty_writer_preparation(tmp_path,changed):
    from sourceloom.store import Store
    from sourceloom.durable import Queue,planning_policy_digest
    store=Store(tmp_path);queue=Queue(store);p=store.create('A source')
    inv={'digest':'source','frozen':True,'inventory_review':{'passed':True},
         'objects':[{'id':'s','kind':'text','text':'We choose.'}],
         'obligations':[{'id':'f','object_id':'s'}]}
    plan={'title':'选择','objective':'理解原句','research_gaps':[],'teaching_functions':[],
          'units':[dict(id='u',title='选择',objective='理解原句',obligation_ids=['f'],object_ids=['s'],
              stages=['理解选择'],proof_questions=[],prerequisites=[],reader_question='发生什么',
              entry_knowledge=['日常选择'],example_thread='原文选择',learning_result='理解原句',
              follows_units=[],bridge_reason='',document_info_ids=[])]}
    bundle={'root':'saved','package_digest':'same','instruction_digest':'same'}
    goal='保持原文主旨、作者意图与人称，完整保留信息，改善逻辑与可读性，不自行设计课程、情境、练习或拓展'
    old={'id':'old','created':1,'project':p['id'],'role':'production','status':'failed','stage':'writer',
         'unit_index':0,'draft':{'blocks':[]},'inventory':inv,'source':inv,'facts':{'facts':[{'id':'f'}]},
         'plan':plan,'goal':goal,'project_goal':p['goal'],'transformation_mode':'rewrite',
         'writing_skill':dict(bundle),'planning_policy_digest':planning_policy_digest(),
         'calls':[{'id':'paid','status':'reasoning_exhausted'}]}
    if changed=='goal':old['project_goal']='different'
    if changed=='skill':old['writing_skill']['package_digest']='different'
    if changed=='partial':old['draft']['blocks']=[{'id':'partial'}]
    if changed=='policy':old['planning_policy_digest']='obsolete'
    store.put_job(old);store.change(p['id'],lambda p:p.update(inventory=inv,draft={'blocks':[{'id':'saved'}]}))
    job=queue.rewrite_existing(p['id'],bundle)
    assert (job.get('reused_plan_job')=='old')==(changed is None)
    assert job['stage']==('writer' if changed is None else 'planner')
    assert store.job('old')==old and store.get(p['id'])['draft']=={'blocks':[{'id':'saved'}]}
