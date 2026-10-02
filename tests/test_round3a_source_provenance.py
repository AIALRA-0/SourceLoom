"""Regressions for classification-before-fetch and the single I3 receipt."""
import pytest

from sourceloom.active_composition import (
    ActiveComposition, authorize_plan_link_actions, integrity_allowed_evidence,
    normalize_first_plan_link_classification, validate_integrity_review,
    compile_integrity_review, integrity_result, prune_reference_link_evidence,
)
from sourceloom.production import Production
from sourceloom.store import Store


def _link_source():
    return dict(objects=[dict(id='body',kind='text',text='The route depends on the map.',
                              locator='article/p'),
                         dict(id='link',kind='link',text='Map',locator='article/a',
                              target='https://example.org/map')])


def _action_turn(role, *, source_id='link'):
    return dict(gaps=['gap-1: the source omits which route the map shows'],
                ready_reason='',result=None,
                link_decisions=[dict(source_id='link',role=role,
                                     missing='the route shown on the linked map'
                                     if role=='semantic_dependency' else '',
                                     source_quote='The route depends on the map.'
                                     if role=='semantic_dependency' else '')],
                actions=[dict(kind='page',gap_id='gap-1',source_id=source_id,
                              url='https://example.org/map')])


def test_reference_link_cannot_be_fetched_before_final_plan():
    with pytest.raises(ValueError,match='先判定'):
        authorize_plan_link_actions(_action_turn('reference'),_link_source(),
                                    ['body','link'],{})
    with pytest.raises(ValueError,match='准确绑定'):
        authorize_plan_link_actions(_action_turn('semantic_dependency',source_id='body'),
                                    _link_source(),['body','link'],{})


def test_semantic_dependency_can_read_only_its_direct_target():
    turn=_action_turn('semantic_dependency')
    assert authorize_plan_link_actions(turn,_link_source(),['body','link'],{})['link']['role']==(
        'semantic_dependency')
    turn['actions'][0]['url']='https://example.org/unrelated'
    with pytest.raises(ValueError,match='精确直接目标'):
        authorize_plan_link_actions(turn,_link_source(),['body','link'],{})


def test_core_plan_turn_blocks_reference_page_before_execute(tmp_path,monkeypatch):
    from pydantic import BaseModel
    from sourceloom.active_resources import Resources

    class Answer(BaseModel):
        value:int
        evidence_gaps:list[dict]=[]

    calls=[]
    monkeypatch.setattr(Resources,'execute',lambda self,action:
        calls.append(action) or {'status':'retrieved'})
    engine=ActiveComposition(Production(Store(tmp_path),{'active_resource_rounds':0}))
    monkeypatch.setattr(engine,'call',lambda *args:_action_turn('reference'))
    job=dict(id='core-plan',project='p',role='active_plan',status='running',created=1,
             pipeline='active_composition_v2',
             core_chain_version=1,results={},calls=[],active_sessions={},
             external_resources={},generated_resources={},verified_terminology=[])
    with pytest.raises(ValueError):
        engine.turn(job,'active-plan-p1','active_plan',Answer,_link_source(),
                    ['body','link'],{},lambda value,_:value)
    assert calls==[]


def test_core_plan_turn_fetches_classified_dependency_once(tmp_path,monkeypatch):
    from pydantic import BaseModel
    from sourceloom.active_resources import Resources

    class Answer(BaseModel):
        value:int
        evidence_gaps:list[dict]=[]

    dispatched=[]
    monkeypatch.setattr(Resources,'execute',lambda self,action:
        dispatched.append(action) or {'status':'retrieved','complete':True})
    engine=ActiveComposition(Production(Store(tmp_path),{'active_resource_rounds':2}))
    turns=iter((dict(gaps=[],ready_reason='Classified from the current claim',
                     actions=[],result=None,
                     link_decisions=_action_turn('semantic_dependency')['link_decisions']),
                _action_turn('semantic_dependency')|{'link_decisions':[]},
                dict(gaps=[],ready_reason='Source link resolved',actions=[],
                     result={'value':1,'evidence_gaps':[{'id':'gap-1'}]},
                     link_decisions=[])))
    monkeypatch.setattr(engine,'call',lambda *args:next(turns))
    job=dict(id='core-dependency',project='p',role='active_plan',status='running',created=1,
             pipeline='active_composition_v2',
             core_chain_version=1,results={},calls=[],active_sessions={},
             external_resources={},generated_resources={},verified_terminology=[])
    result=engine.turn(job,'active-plan-p1','active_plan',Answer,_link_source(),
                       ['body','link'],{},lambda value,_:value)
    assert result=={'value':1,'evidence_gaps':[{'id':'gap-1'}]}
    assert [row['url'] for row in dispatched]==['https://example.org/map']


def test_core_reference_classification_turn_never_fetches(tmp_path,monkeypatch):
    from pydantic import BaseModel
    from sourceloom.active_resources import Resources

    class Answer(BaseModel):
        value:int

    monkeypatch.setattr(Resources,'execute',lambda self,action:
        pytest.fail('reference link fetched'))
    engine=ActiveComposition(Production(Store(tmp_path),{'active_resource_rounds':1}))
    seen=[]
    turns=iter((dict(gaps=[],ready_reason='Source already gives the rule',
                     actions=[],result=None,link_decisions=[dict(source_id='link',
                         role='reference',missing='',source_quote='')]),
                dict(gaps=[],ready_reason='Article plan ready',actions=[],
                     result={'value':1},link_decisions=[])))
    def call(job,key,role,payload,schema):
        seen.append(payload)
        return next(turns)
    monkeypatch.setattr(engine,'call',call)
    job=dict(id='core-reference',project='p',role='active_plan',status='running',created=1,
             pipeline='active_composition_v2',core_chain_version=1,results={},calls=[],
             active_sessions={},external_resources={},generated_resources={},
             verified_terminology=[])
    assert engine.turn(job,'active-plan-p1','active_plan',Answer,_link_source(),
                       ['body','link'],{},lambda value,_:value)=={'value':1}
    assert seen[0]['link_classification_required'] is True
    assert seen[0]['link_context'][0]['surrounding_text']=='The route depends on the map.'
    assert seen[1]['prior_link_decisions'][0]['role']=='reference'


def test_core_reference_classification_precedes_final_plan(tmp_path,monkeypatch):
    from pydantic import BaseModel
    from sourceloom.active_resources import Resources

    class Answer(BaseModel):
        value:int

    monkeypatch.setattr(Resources,'execute',lambda self,action:
        pytest.fail('reference link fetched'))
    engine=ActiveComposition(Production(Store(tmp_path),{}))
    calls=[]
    def call(job,key,role,payload,schema):
        calls.append(key)
        return dict(gaps=[],ready_reason='Links classified',actions=[],
                    link_decisions=[dict(source_id='link',role='reference',
                                         missing='',source_quote='')],result=None) if len(calls)==1 else dict(
                    gaps=[],ready_reason='Plan complete',actions=[],
                    link_decisions=[],result={'value':1})
    monkeypatch.setattr(engine,'call',call)
    job=dict(id='one-turn-reference',project='p',role='active_plan',status='running',
             created=1,pipeline='active_composition_v2',core_chain_version=1,
             results={},calls=[],active_sessions={},external_resources={},
             generated_resources={},verified_terminology=[])
    assert engine.turn(job,'active-plan-p1','active_plan',Answer,_link_source(),
                       ['body','link'],{},lambda value,_:value)=={'value':1}
    assert calls==['active-plan-p1-turn-0','active-plan-p1-turn-1']


def test_complete_reference_plan_finishes_in_first_turn(tmp_path,monkeypatch):
    from pydantic import BaseModel
    from sourceloom.active_resources import Resources
    class Answer(BaseModel):
        value:int
    monkeypatch.setattr(Resources,'execute',lambda self,action:
        pytest.fail('reference link fetched'))
    engine=ActiveComposition(Production(Store(tmp_path),{}))
    calls=[]
    def call(job,key,role,payload,schema):
        calls.append(key)
        return dict(gaps=[],actions=[],ready_reason='Complete plan',
            link_decisions=[dict(source_id='link',role='reference',
                                 missing='',source_quote='')],result={'value':1})
    monkeypatch.setattr(engine,'call',call)
    job=dict(id='one-turn-plan',project='p',role='active_plan',status='running',
        created=1,pipeline='active_composition_v2',core_chain_version=1,
        results={},calls=[],active_sessions={},external_resources={},
        generated_resources={},verified_terminology=[])
    assert engine.turn(job,'active-plan-p1','active_plan',Answer,_link_source(),
                       ['body','link'],{},lambda value,_:value)=={'value':1}
    assert calls==['active-plan-p1-turn-0']


def test_default_rewrite_discards_model_dependency_and_target_read(tmp_path,monkeypatch):
    from pydantic import BaseModel
    from sourceloom.active_resources import Resources

    class Answer(BaseModel):
        value:int

    monkeypatch.setattr(Resources,'execute',lambda self,action:
        pytest.fail('default rewrite fetched an original link target'))
    engine=ActiveComposition(Production(Store(tmp_path),{'active_resource_rounds':2}))
    calls=[]
    def call(job,key,role,payload,schema):
        calls.append(payload)
        if len(calls)==1:return _action_turn('semantic_dependency')
        return dict(gaps=[],actions=[],ready_reason='Plan ready',
            link_decisions=[],result={'value':1})
    monkeypatch.setattr(engine,'call',call)
    job=dict(id='rewrite-reference',project='p',role='active_plan',status='running',
        created=1,pipeline='active_composition_v2',core_chain_version=1,
        transformation_mode='rewrite',results={},calls=[],active_sessions={},
        external_resources={},generated_resources={},verified_terminology=[])
    result=engine.turn(job,'active-plan-p1','active_plan',Answer,_link_source(),
        ['body','link'],{},lambda value,_:value)
    assert result=={'value':1}
    assert len(calls)==2
    assert calls[0]['prior_link_decisions'][0]['role']=='reference'
    assert calls[0]['link_classification_required'] is False
    assert job['active_sessions']['active-plan-p1']['link_decisions']['link']['role']=='reference'
    assert job['plan_protocol_normalizations'][0]['suppressed_action_count']==1


def test_reference_gap_prune_preserves_unrelated_fact_evidence():
    plan=dict(obligations=[dict(id='link-duty',source_id='link'),
                           dict(id='fact-duty',source_id='fact')],
        evidence_gaps=[dict(id='link-gap',source_id='link',obligation_id='link-duty'),
                       dict(id='fact-gap',source_id='fact',obligation_id='fact-duty')],
        evidence_resolutions=[dict(gap_id='link-gap'),dict(gap_id='fact-gap')],
        evidence_bindings=[dict(id='link-binding',gap_id='link-gap'),
                           dict(id='fact-binding',gap_id='fact-gap')])
    cleaned,receipt=prune_reference_link_evidence(plan,{'link'})
    assert receipt==dict(gap_ids=['link-gap'],resolution_gap_ids=['link-gap'],
                         binding_ids=['link-binding'])
    assert [row['id'] for row in cleaned['evidence_gaps']]==['fact-gap']
    assert [row['gap_id'] for row in cleaned['evidence_resolutions']]==['fact-gap']
    assert [row['id'] for row in cleaned['evidence_bindings']]==['fact-binding']
    assert len(plan['evidence_gaps'])==2


def test_dependency_fetches_after_same_turn_classification(tmp_path,monkeypatch):
    from pydantic import BaseModel
    from sourceloom.active_resources import Resources
    class Answer(BaseModel):
        value:int
        evidence_gaps:list[dict]=[]
    dispatched=[]
    monkeypatch.setattr(Resources,'execute',lambda self,action:
        dispatched.append(action) or {'status':'retrieved','complete':True})
    engine=ActiveComposition(Production(Store(tmp_path),{'active_resource_rounds':2}))
    calls=[]
    def call(job,key,role,payload,schema):
        calls.append(key)
        if len(calls)==1:return _action_turn('semantic_dependency')
        assert dispatched and job['active_sessions']['active-plan-p1']['link_decisions']['link'][
            'role']=='semantic_dependency'
        return dict(gaps=[],actions=[],ready_reason='Page read',link_decisions=[],
                    result={'value':1,'evidence_gaps':[{'id':'gap-1'}]})
    monkeypatch.setattr(engine,'call',call)
    job=dict(id='same-turn-dependency',project='p',role='active_plan',status='running',
        created=1,pipeline='active_composition_v2',core_chain_version=1,
        results={},calls=[],active_sessions={},external_resources={},
        generated_resources={},verified_terminology=[])
    assert engine.turn(job,'active-plan-p1','active_plan',Answer,_link_source(),
                       ['body','link'],{},lambda value,_:value)['value']==1
    assert len(dispatched)==1 and len(calls)==2


def test_complete_first_classification_suppresses_premature_actions(tmp_path,monkeypatch):
    from pydantic import BaseModel
    from sourceloom.active_resources import Resources

    class Answer(BaseModel):
        value:int

    actions=[]
    monkeypatch.setattr(Resources,'execute',lambda self,action:
        actions.append(action) or pytest.fail('premature page read'))
    engine=ActiveComposition(Production(Store(tmp_path),{'active_resource_rounds':2}))
    calls=[]
    first=_action_turn('reference')
    def call(job,key,role,payload,schema):
        calls.append(key)
        return first if len(calls)==1 else dict(gaps=[],ready_reason='Plan complete',
            actions=[],result={'value':1},link_decisions=[])
    monkeypatch.setattr(engine,'call',call)
    job=dict(id='classified-before-action',project='p',role='active_plan',
             status='running',created=1,pipeline='active_composition_v2',
             core_chain_version=1,results={},calls=[],active_sessions={},
             external_resources={},generated_resources={},verified_terminology=[])
    assert engine.turn(job,'active-plan-p1','active_plan',Answer,_link_source(),
                       ['body','link'],{},lambda value,_:value)=={'value':1}
    assert actions==[]
    assert calls==['active-plan-p1-turn-0','active-plan-p1-turn-1']
    assert job['active_sessions']['active-plan-p1']['link_decisions']['link']['role']=='reference'
    assert job['plan_protocol_normalizations']==[dict(step='active-plan-p1',turn=0,
        retained_link_decision_ids=['link'],suppressed_action_count=1,
        suppressed_gap_count=1,suppressed_result=False,
        reason='classification_complete_before_external_actions')]


def test_incomplete_or_invalid_classification_never_suppresses_actions():
    raw=_action_turn('reference')
    raw['link_decisions']=[]
    assert normalize_first_plan_link_classification(raw,_link_source(),
        ['body','link'])==(raw,None)
    raw=_action_turn('semantic_dependency')
    raw['link_decisions'][0]['missing']=''
    with pytest.raises(ValueError,match='语义依赖'):
        normalize_first_plan_link_classification(raw,_link_source(),['body','link'])
    raw=_action_turn('reference')
    raw['link_decisions'][0]['source_id']='wrong'
    with pytest.raises(ValueError,match='唯一对应'):
        normalize_first_plan_link_classification(raw,_link_source(),['body','link'])


def _review(claims=()):
    return dict(checked_source_ids=['s'],checked_block_ids=['b'],findings=[],
                i3_provenance_checked=False,
                i3_block_assessments=[dict(block_id='b',status=(
                    'ADDED_FACTS_PRESENT' if claims else 'NO_ADDED_FACTS'))],
                added_fact_claims=list(claims))


def _draft_source():
    return (dict(blocks=[dict(id='b',markdown='The package installs a method in old environments.')]),
            dict(objects=[dict(id='s',text='The package is a Polyfill link.')]))


def test_single_integrity_review_flags_unsupported_added_mechanism():
    draft,source=_draft_source()
    claim=dict(block_id='b',output_quote='installs a method in old environments',
               support='UNSUPPORTED')
    review=validate_integrity_review(_review([claim]),draft,source,[])
    assert [(f['invariant'],f['verdict']) for f in review['findings']]==[('I3','FAIL')]
    assert review['findings'][0]['output_quote']==claim['output_quote']


def test_i3_coverage_is_derived_from_each_authored_block():
    draft,source=_draft_source()
    draft['blocks'].append(dict(id='b2',kind='explanation',markdown='A second paragraph.'))
    partial=_review()
    partial['i3_provenance_checked']=True
    with pytest.raises(ValueError,match='恰好覆盖'):
        validate_integrity_review(partial,draft,source,[])
    complete=partial|dict(i3_block_assessments=[
        dict(block_id='b',status='NO_ADDED_FACTS'),
        dict(block_id='b2',status='NO_ADDED_FACTS')])
    assert validate_integrity_review(complete,draft,source,[])['i3_provenance_checked']
    duplicate=complete|dict(i3_block_assessments=complete['i3_block_assessments']+[
        dict(block_id='b',status='NO_ADDED_FACTS')])
    with pytest.raises(ValueError,match='恰好覆盖'):
        validate_integrity_review(duplicate,draft,source,[])
    unknown=complete|dict(i3_block_assessments=[
        dict(block_id='b',status='NO_ADDED_FACTS'),
        dict(block_id='missing',status='NO_ADDED_FACTS')])
    with pytest.raises(ValueError,match='恰好覆盖'):
        validate_integrity_review(unknown,draft,source,[])


def test_i3_added_fact_status_requires_claim_and_consistent_block():
    draft,source=_draft_source()
    absent=_review()
    absent['i3_block_assessments'][0]['status']='ADDED_FACTS_PRESENT'
    with pytest.raises(ValueError,match='声明与实际 claim'):
        validate_integrity_review(absent,draft,source,[])
    claim=dict(block_id='b',output_quote='installs a method in old environments',
               support='UNSUPPORTED')
    contradictory=_review([claim])
    contradictory['i3_block_assessments'][0]['status']='NO_ADDED_FACTS'
    with pytest.raises(ValueError,match='声明与实际 claim'):
        validate_integrity_review(contradictory,draft,source,[])
    no_receipt=_review([claim])
    no_receipt['i3_block_assessments']=[]
    no_receipt['i3_provenance_checked']=True
    with pytest.raises(ValueError,match='恰好覆盖'):
        validate_integrity_review(no_receipt,draft,source,[])


def test_i3_only_assesses_authored_prose_including_mixed_object_caption():
    source=dict(objects=[dict(id='s',text='Original chart')])
    literal='![source image](assets/chart.png)'
    draft=dict(blocks=[dict(id='original',kind='object',markdown=literal),
                       dict(id='caption',kind='object',markdown=literal+'\n\nA new caption.'),
                       dict(id='prose',kind='explanation',markdown='Author prose.')])
    review=dict(checked_source_ids=['s'],
                checked_block_ids=['original','caption','prose'],findings=[],
                i3_provenance_checked=True,added_fact_claims=[],
                i3_block_assessments=[dict(block_id='caption',status='NO_ADDED_FACTS'),
                                      dict(block_id='prose',status='NO_ADDED_FACTS')])
    checked=validate_integrity_review(review,draft,source,[],
                                      protected_literals=[literal])
    assert checked['i3_provenance_checked']
    review['i3_block_assessments'].append(dict(block_id='original',status='NO_ADDED_FACTS'))
    with pytest.raises(ValueError,match='恰好覆盖'):
        validate_integrity_review(review,draft,source,[],
                                  protected_literals=[literal])


def test_name_evidence_cannot_license_added_mechanism():
    draft,source=_draft_source()
    claim=dict(block_id='b',output_quote='installs a method in old environments',
               support='EXTERNAL_SUPPORTED',evidence_id='page')
    with pytest.raises(ValueError,match='名称查证'):
        validate_integrity_review(_review([claim]),draft,source,
            [dict(id='page',scope='name_only',resource_id='page',quote='Polyfill')])


def test_saved_fact_evidence_and_source_support_are_distinct():
    draft,source=_draft_source()
    factual=dict(block_id='b',output_quote='installs a method in old environments',
                 support='EXTERNAL_SUPPORTED',evidence_id='fact')
    assert validate_integrity_review(_review([factual]),draft,source,
        [dict(id='fact',scope='verified_fact',resource_id='fact',
              quote='The package installs a method in old environments.')])['findings']==[]
    source_claim=dict(block_id='b',output_quote='The package',
                      support='SOURCE_SUPPORTED',source_id='s',source_quote='The package')
    assert validate_integrity_review(_review([source_claim]),draft,source,[])['findings']==[]
    with pytest.raises(ValueError,match='原文引文'):
        validate_integrity_review(_review([source_claim|{'source_quote':'invented'}]),
                                  draft,source,[])


def test_only_saved_authorized_evidence_is_offered_to_integrity(tmp_path):
    store=Store(tmp_path)
    job=dict(external_resources={key:dict(blob=store.blob(value.encode()))
                                 for key,value in [('fact','Exact external fact'),
                                                   ('name','Official Name'),
                                                   ('unused','Unrelated claim')]},
             object_responsibilities={'link':dict(explain=False)},
             active_plans=[dict(evidence_bindings=[dict(id='e1',resource_id='fact',
                            quote='Exact external fact')],
                                link_briefs=[dict(source_id='link',evidence=[dict(
                                    resource_id='unused',quote='Unrelated claim')])],
                                concepts=[dict(name_evidence=[dict(resource_id='name',
                                    quote='Official Name')])])])
    allowed=integrity_allowed_evidence(job,store)
    assert {(item['resource_id'],item['scope']) for item in allowed}=={
        ('fact','verified_fact'),('name','name_only')}
    assert len({item['id'] for item in allowed})==len(allowed)


def test_one_shot_compiler_retains_supported_failure_despite_other_unknown():
    draft,source=_draft_source()
    draft['blocks'].append(dict(id='b2',kind='explanation',markdown='Another line.'))
    claim=dict(block_id='b',output_quote='installs a method in old environments',
               support='UNSUPPORTED')
    raw=_review([claim]);raw['checked_block_ids']=['b','b2']
    compiled=compile_integrity_review(raw,draft,source,[])
    assert ('I3','FAIL','b') in {(f['invariant'],f['verdict'],f['block_id'])
                                for f in compiled['findings']}
    assert ('I3','UNKNOWN','b2') in {(f['invariant'],f['verdict'],f['block_id'])
                                   for f in compiled['findings']}
    job=dict(active_groups=[['s']],draft=draft,source_digest='source')
    assert integrity_result(job,compiled['findings'],
        reviewed_source_ids=compiled['checked_source_ids'],
        reviewed_block_ids=compiled['checked_block_ids'])['invariants']['I3']=='FAIL'


@pytest.mark.parametrize('claim',[
    dict(block_id='b',output_quote='installs a method in old environments',
         support='EXTERNAL_SUPPORTED',evidence_id='missing'),
    dict(block_id='b',output_quote='installs a method in old environments',
         support='SOURCE_SUPPORTED',source_id='s',source_quote='not in source'),
])
def test_unverifiable_support_is_unknown_without_reviewer_retry(claim):
    draft,source=_draft_source()
    compiled=compile_integrity_review(_review([claim]),draft,source,[])
    assert any(row['invariant']=='I3' and row['verdict']=='UNKNOWN'
               for row in compiled['findings'])
    assert not any(row['verdict']=='FAIL' for row in compiled['findings'])


def test_unparseable_review_is_unknown():
    draft,source=_draft_source()
    compiled=compile_integrity_review(None,draft,source,[])
    assert compiled['checked_block_ids']==[]
    assert any(row['verdict']=='UNKNOWN' for row in compiled['findings'])
