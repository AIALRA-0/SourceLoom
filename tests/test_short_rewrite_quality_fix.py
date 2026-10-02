import copy
import json

from pydantic import BaseModel
import pytest

from sourceloom.active_composition import (
    ActiveComposition,
    coalesce_current_short_rewrite_concepts,
    concept_presence,
    insert_verified_name_at_unique_first_use,
    missing_concept_names,
    short_rewrite_name_completions,
    writer_node_context,
)
from sourceloom.durable import Queue
from sourceloom.production import Production
from sourceloom.store import digest
from tests.test_production import prepared


class Shape(BaseModel):
    answer: str


def _concept(cid, chinese, english, source_ids):
    return dict(id=cid, name='', chinese_name=chinese, english_name=english,
                definition='只用于测试概念身份', source_ids=source_ids, requires=[])


def _planned_node(node_id,source_id,fact_id,requires=(),establishes=(),section_outline=None):
    return dict(id=node_id,title='标题 '+node_id,purpose='保留原文命题',
        source_ids=[source_id],obligation_ids=[fact_id],requires_concepts=list(requires),
        establishes_concepts=list(establishes),depends_on=[],transition_from='',
        prepares_for='',depth='faithful',expansion='source_only',heading_level=2,
        explanation=dict(known_start='',obstacle='',reasoning_steps=['检查原文命题'],boundary=''),
        section_outline=section_outline)


def test_live_w3c_short_rewrite_completes_verified_name_without_redefining():
    # Live W3C p1 accepted the Chinese term without an English pairing.
    accepted_success=_concept('p1-con-001','成功准则','',['s1'])
    accepted_w3c=_concept('p1-con-003','万维网联盟','World Wide Web Consortium',['s1'])
    accepted_w3c['requires']=['p1-con-002']
    duplicate_success=_concept('p3-con-001','成功准则','Success Criterion',['s3'])
    duplicate_success.update(naming_status='verified',name_evidence=[
        dict(resource_id='s3',quote='Success Criterion (SC)')])
    duplicate_success['abbreviations']=[dict(short='SC',chinese='成功准则',
        english='Success Criterion')]
    new_concept=_concept('p3-con-002','对比度','Contrast',['s3'])
    text_image=_concept('p3-con-003','文字图像','Text and images',['s3'])
    plan1_concepts=[accepted_success,accepted_w3c]
    plan1=dict(contract={'purpose':'rewrite'},obligations=[dict(id='f1',source_id='s1',meaning='事实一',
        conditions=[],quantities=[],negations=[],narrator='',referents=[])],
        concepts=plan1_concepts,nodes=[_planned_node('p1-node-001','s1','f1',
            establishes=['p1-con-001','p1-con-003'])],
        link_briefs=[dict(source_id='l1',role='navigation')])
    plan2=dict(contract={'purpose':'rewrite'},obligations=[dict(id='f2',source_id='s2',meaning='事实二',
        conditions=[],quantities=[],negations=[],narrator='',referents=[])],
        concepts=[],nodes=[_planned_node('p2-node-001','s2','f2')],
        link_briefs=[])
    current_plan_node=_planned_node('p3-node-001','s3','f3',
        requires=['p1-con-001','p1-con-003'],
        establishes=['p3-con-001','p3-con-003','p3-con-002'],section_outline=None)
    plan3=dict(contract={'purpose':'rewrite'},obligations=[dict(id='f3',source_id='s3',meaning='规范句',
        conditions=['仅在符合条件时'],quantities=[],negations=[],narrator='',referents=[])],
        concepts=[duplicate_success,text_image,new_concept],
        nodes=[current_plan_node],
        link_briefs=[dict(source_id='l3',role='navigation')])
    plan4=dict(contract={'purpose':'rewrite'},obligations=[dict(id='f4',source_id='s4',meaning='事实四',
        conditions=[],quantities=[],negations=[],narrator='',referents=[])],
        concepts=[],nodes=[_planned_node('p4-node-001','s4','f4',requires=['p3-con-001'])],
        link_briefs=[])
    checkpoints=[dict(node_id='p1',draft_digest='accepted-p1'),dict(node_id='p2',draft_digest='accepted-p2')]
    current=copy.deepcopy(current_plan_node)
    later=_planned_node('p4-node-001','s4','f4',requires=['p3-con-001'],
        section_outline=[dict(id='p4-node-001-a',requires_concepts=['p3-con-001'],establishes_concepts=[])])
    objects=[dict(id=sid,kind='text',locator=sid,text='source text') for sid in ('s1','s2','s3','s4')]
    objects.append(dict(id='l3',kind='link',locator='l3',text='More information'))
    job=dict(transformation_mode='rewrite',active_plans=[plan1,plan2,plan3,plan4],
        knowledge_memory=[dict(node_id='p1-node-001',established=[dict(id='p1-con-001',definition='已接受'),
                                                         dict(id='p1-con-003',definition='已接受')]),
                          dict(node_id='p2-node-001',established=[])],
        writing_batches=[_planned_node('p1-node-001','s1','f1',establishes=['p1-con-001','p1-con-003']),
            _planned_node('p2-node-001','s2','f2'),current,later],unit_index=2,
        active_checkpoints=copy.deepcopy(checkpoints),
        source=dict(objects=objects,resources=[],unknown=[],originals=[],obligations=[]))
    obligations_before=copy.deepcopy([plan['obligations'] for plan in job['active_plans']])
    links_before=copy.deepcopy([plan['link_briefs'] for plan in job['active_plans']])
    accepted_concepts_before=copy.deepcopy([plan['concepts'] for plan in (plan1,plan2)])
    original_job=copy.deepcopy(job)
    short_source=[dict(id='s3',kind='text',text='A short normative sentence of about 210 characters. Success Criterion (SC) appears in the source. '*2),
                  dict(id='l3',kind='link',text='More information')]

    receipts=coalesce_current_short_rewrite_concepts(job,current,short_source)

    assert {(row['source_concept_id'],row['accepted_concept_id']) for row in receipts} == {
        ('p3-con-001','p1-con-001')}
    completion=short_rewrite_name_completions(job,current)
    assert len(completion)==1
    assert completion[0]['id']=='p3-con-001'
    assert completion[0]['accepted_concept_id']=='p1-con-001'
    assert completion[0]['chinese_name']=='成功准则'
    assert completion[0]['english_name']=='Success Criterion'
    assert completion[0]['name_evidence']==[dict(resource_id='s3',quote='Success Criterion (SC)')]
    assert completion[0]['abbreviations']==[dict(short='SC',chinese='成功准则',english='Success Criterion')]
    assert completion[0]['first_use_display']=='SC 成功准则（Success Criterion）'
    assert completion[0]['requires_first_use_pair']
    assert '不要重讲定义' in completion[0]['instruction']
    completion_receipt=next(row for row in receipts if row['source_concept_id']=='p3-con-001')
    assert completion_receipt['reason']=='accepted_chinese_concept_had_no_english_name; verified_current_source_name_completion'
    assert completion_receipt['name_completion']['source_ids']==['s3']
    assert current['requires_concepts']==['p1-con-001','p1-con-003']
    assert current['establishes_concepts']==['p3-con-003','p3-con-002']
    assert current['section_outline'] is None
    assert writer_node_context(current)['section_outline']==[]
    assert current['id']=='p3-node-001'
    assert current['source_ids']==['s3'] and current['obligation_ids']==['f3']
    assert later['requires_concepts']==['p1-con-001']
    assert later['establishes_concepts']==[]
    assert later['section_outline'][0]['requires_concepts']==['p1-con-001']
    assert later['section_outline'][0]['establishes_concepts']==[]
    assert {item['id'] for item in plan3['concepts']}=={'p3-con-003','p3-con-002'}
    assert [plan['obligations'] for plan in job['active_plans']]==obligations_before
    assert [plan['link_briefs'] for plan in job['active_plans']]==links_before
    assert [plan['concepts'] for plan in (plan1,plan2)]==accepted_concepts_before
    assert plan1['concepts'][1]['requires']==['p1-con-002']
    assert job['knowledge_memory'][0]['established'][0]['id']=='p1-con-001'
    assert job['active_checkpoints']==checkpoints
    assert job['plan']['units'][2]['prerequisites']==['p1-con-001','p1-con-003']
    assert all(row['duplicate_source_ids']==['s3'] for row in receipts)
    assert plan1['concepts'][0]['english_name']==''

    def candidate(markdown):
        return {'blocks':[dict(id='p3-b1',kind='explanation',markdown=markdown,
            obligation_ids=['f3'],object_ids=[],embedded_object_ids=[],evidence=[])]}
    # Even if SC and the English name occur later, the first use must follow
    # the verified order and keep the full-width parentheses English-only.
    for initial,expected,missing_first_use in (
        ('成功准则先前已提及。 Success Criterion and SC later.',
         'SC 成功准则（Success Criterion）先前已提及。 Success Criterion and SC later.',True),
        ('SC 成功准则先前已提及。 Success Criterion and SC later.',
         'SC 成功准则（Success Criterion）先前已提及。 Success Criterion and SC later.',True),
        ('SC 成功准则（Success Criterion）先前已提及。 Success Criterion and SC later.',
         'SC 成功准则（Success Criterion）先前已提及。 Success Criterion and SC later.',False),
        ('理解成功准则先前已提及。 Success Criterion and SC later.',
         '理解 SC 成功准则（Success Criterion）先前已提及。 Success Criterion and SC later.',True),
    ):
        draft=candidate(initial)
        omissions=missing_concept_names(completion,draft)
        assert bool(omissions)==missing_first_use
        if omissions:assert omissions[0]['first_use_missing']
        alignments=insert_verified_name_at_unique_first_use(draft,{'concept_evidence':[]},completion)
        assert draft['blocks'][0]['markdown']==expected
        if not missing_first_use:
            assert alignments==[]
        else:
            assert alignments[0]['concept_id']=='p3-con-001'
        concept_presence(completion,draft)


def test_short_rewrite_does_not_coalesce_same_chinese_name_without_verified_source_evidence():
    accepted=_concept('p1-con-001','成功准则','',['s1'])
    current_concept=_concept('p3-con-001','成功准则','Success Criterion',['s3'])
    current_concept.update(naming_status='ambiguous',name_evidence=[])
    accepted_node=_planned_node('p1','s1','f1',establishes=['p1-con-001'])
    current_node=_planned_node('p3','s3','f3',establishes=['p3-con-001'])
    job=dict(transformation_mode='rewrite',active_plans=[
        dict(concepts=[accepted],nodes=[accepted_node]),
        dict(concepts=[current_concept],nodes=[current_node])],
        knowledge_memory=[dict(node_id='p1',established=[dict(id='p1-con-001',definition='已接受')])],
        writing_batches=[accepted_node,current_node],unit_index=1)
    before=copy.deepcopy(job)

    receipts=coalesce_current_short_rewrite_concepts(
        job,current_node,[dict(id='s3',kind='text',text='A short plain sentence.')])

    assert receipts==[]
    assert job['active_plans'][1]['concepts']==before['active_plans'][1]['concepts']
    assert current_node['establishes_concepts']==['p3-con-001']
    assert not job.get('short_rewrite_name_completions')


def test_short_rewrite_still_coalesces_exact_normalized_bilingual_names():
    accepted=_concept('p1-con-002','对比度','Contrast',['s1'])
    duplicate=_concept('p3-con-002','对比度','Con-trast',['s3'])
    prior=_planned_node('p1-node-001','s1','f1',establishes=['p1-con-002'])
    current=_planned_node('p3-node-001','s3','f3',requires=['p3-con-002'])
    job=dict(transformation_mode='rewrite',active_plans=[
        dict(concepts=[accepted],nodes=[prior]),dict(concepts=[duplicate],nodes=[current])],
        knowledge_memory=[dict(node_id='p1-node-001',established=[dict(id='p1-con-002')])],
        writing_batches=[prior,current],unit_index=1)

    receipts=coalesce_current_short_rewrite_concepts(
        job,current,[dict(id='s3',kind='text',text='A short plain rewrite.')])

    assert [(row['source_concept_id'],row['accepted_concept_id']) for row in receipts]==[
        ('p3-con-002','p1-con-002')]
    assert current['requires_concepts']==['p1-con-002']
    assert job['active_plans'][1]['concepts']==[]
    assert job['active_plans'][0]['concepts']==[accepted]
    assert not job.get('short_rewrite_name_completions')


def _routes():
    return {
        'provider_routes': {'kuafu': {'provider':'openai-compatible','provider_id':'kuafu',
            'model':'deepseek-v4.1-flash','protocol':'chat_completions',
            'base_url':'https://api.kuafushe.cc/v1','enabled':True}},
        'provider_credentials': {'kuafu':'kuafu-key'},
        'role_providers': {'active_write': {'provider':'openai-compatible','provider_id':'kuafu',
            'model':'deepseek-v4.1-flash','protocol':'chat_completions',
            'base_url':'https://api.kuafushe.cc/v1','api_key':'kuafu-key',
            'backup_provider_id':'opencode-go'}},
        'fallback_providers': {'active_write': {'provider':'openai-compatible',
            'model':'deepseek-v4.1-flash','protocol':'chat_completions',
            'base_url':'https://opencode.ai/zen/go/v1','api_key':'go-key'}},
    }


@pytest.fixture
def skill(tmp_path):
    root=tmp_path/'writing-skill'
    (root/'references').mkdir(parents=True)
    (root/'constitution').mkdir()
    (root/'assets').mkdir()
    (root/'SKILL.md').write_text(
        '[format](references/format-rules.md)\n[explain](references/explanation-framework.md)\n'
        '[formula](references/formula-explanation.md)\n`constitution/principles.md`',encoding='utf-8')
    for name in ('format-rules','explanation-framework','formula-explanation'):
        (root/'references'/f'{name}.md').write_text('test writing guidance',encoding='utf-8')
    (root/'constitution'/'principles.md').write_text('test principles',encoding='utf-8')
    (root/'assets'/'asset.bin').write_bytes(b'test asset')
    return root


def test_completed_glossary_escalation_gets_one_fresh_primary_turn(tmp_path, skill, monkeypatch):
    store,_,project,bundle=prepared(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    job=queue.enqueue(project['id'],bundle)
    session_key='active-write-p3'
    failed_key=session_key+'-turn-4'
    invalid={'blocks':[{'id':'p3-b1','markdown':'## 术语表\n\n- 已重复定义'}]}
    result={'result':invalid,'gaps':[],'actions':[],'ready_reason':'ready'}
    escalation=dict(version='active-short-rewrite-glossary-kuafu-v1',role='active_write',
        logical_step=session_key,status='completed',attempts=1,attempt_step=failed_key,
        attempt_call_id='primary-quality-call',primary_provider_id='kuafu')
    session={'round':4,'corrections':3,'short_rewrite_glossary_repair_used':True,
        'correction':{'error':'glossary quality failure','instruction':'preserve source coverage'},
        'resources':{'entries':{'saved':{'id':'saved'}}}}
    job.update(core_chain_version=0,status='failed',stage='active_write',pending=None,
        error='短篇普通改写被扩成术语表：校验失败',
        calls=[dict(id='fallback-call',role='active_write__fallback',status='completed',
                    step_key=session_key+'-turn-3',provider_id='opencode-go'),
               dict(id='primary-quality-call',role='active_write',status='completed',
                    step_key=failed_key,provider_id='kuafu',response_blob=store.blob(
                        json.dumps({'choices':[{'message':{'content':'saved invalid artifact'}}]}).encode()))],
        results={failed_key:result},active_sessions={session_key:session},
        active_glossary_quality_escalations={session_key:escalation},
        quality_escalation_history=[copy.deepcopy(escalation)],
        active_checkpoints=[{'node_id':'p1','draft_digest':'p1'},
                            {'node_id':'p2','draft_digest':'p2'}],
        draft={'blocks':[{'id':'p1-b1'},{'id':'p2-b1'}]})
    old_role='Essential terms MUST use a Markdown definition list for every item.'
    job['role_policy']={'active_write':old_role,'rewrite_scope':'immutable scope'}
    job['role_policy_digest']=digest(job['role_policy'])
    store.put_job(job)
    store.change(project['id'],lambda p:p.update(active_job=None))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='failed' WHERE id=?",(job['id'],))

    config=_routes()
    resumed=queue.retry_validation(job['id'],config)
    retry=resumed['active_glossary_quality_retries'][session_key]
    next_key=session_key+'-turn-5'
    assert retry['version']=='active-short-rewrite-glossary-fresh-turn-v1'
    assert retry['status']=='queued' and retry['attempts']==0
    assert resumed['active_sessions'][session_key]['round']==5
    assert resumed['active_sessions'][session_key]['previous_invalid_result']==invalid
    assert resumed['results'][failed_key]==result
    assert resumed['active_glossary_quality_escalations'][session_key]==escalation
    assert 'the concept ledger as identity and continuity' in \
        resumed['role_policy']['active_write']
    assert 'Put a verified short abbreviation before its natural Chinese name' in \
        resumed['role_policy']['active_write']
    assert resumed['role_policy']['rewrite_scope']=='immutable scope'
    assert resumed['role_policy_digest']==digest(resumed['role_policy'])
    policy_refresh=resumed['versioned_role_policy_refreshes'][0]
    assert policy_refresh['version']=='active-write-short-rewrite-coverage-v1'
    assert policy_refresh['previous_text']==old_role
    assert policy_refresh['step']==next_key
    assert policy_refresh['previous_policy_digest']==digest({'active_write':old_role,
                                                              'rewrite_scope':'immutable scope'})
    assert policy_refresh['updated_policy_digest']==resumed['role_policy_digest']
    assert retry['next_step_key']==next_key
    assert resumed['active_checkpoints']==[{'node_id':'p1','draft_digest':'p1'},
                                           {'node_id':'p2','draft_digest':'p2'}]
    assert len(resumed['calls'])==2
    assert next_key not in resumed['results']

    observed={}
    def dispatch(provider,pid,role,payload,schema,claimed,cancelled):
        observed.update(role=role,provider_id=provider.config['provider_id'],
                        backup=provider.config.get('backup_provider_id'),
                        fallback=provider.config.get('quota_fallback'),
                        step=claimed['current_step_key'])
        claimed['calls'].append(dict(id='fresh-primary-call',role=role,status='completed',
            step_key=claimed['current_step_key'],provider_id=provider.config['provider_id'],
            channel='openai-compatible',upstream_base=provider.config['base_url']))
        return {'answer':'accepted'}
    monkeypatch.setattr('sourceloom.active_composition.Provider.call',dispatch)
    engine=ActiveComposition(Production(store,config))
    monkeypatch.setattr(engine.queue,'cancelled',lambda *args:False)
    assert engine.call(resumed,next_key,'active_write',{},Shape)=={'answer':'accepted'}
    assert observed=={'role':'active_write','provider_id':'kuafu','backup':None,
                      'fallback':None,'step':next_key}
    assert retry['status']=='completed' and retry['attempts']==1
    assert retry['attempt_call_id']=='fresh-primary-call'

    # A second validation resume cannot queue another model attempt for this unit.
    latest=resumed['calls'][-1]
    latest['response_blob']=store.blob(json.dumps({'choices':[{'message':{'content':'failed'}}]}).encode())
    resumed['results'][next_key]=result
    resumed.update(status='failed',error='短篇普通改写被扩成术语表：仍未修正',pending=None)
    store.put_job(resumed)
    store.change(project['id'],lambda p:p.update(active_job=None))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='failed' WHERE id=?",(job['id'],))
    resumed_again=queue.retry_validation(job['id'],config)
    assert len(resumed_again['active_glossary_quality_retries'])==1
    assert resumed_again['active_glossary_quality_retries'][session_key]['attempts']==1
    assert len(resumed_again['versioned_role_policy_refreshes'])==1
    assert len([call for call in resumed_again['calls'] if call.get('id')=='fresh-primary-call'])==1
