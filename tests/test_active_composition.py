import copy
import pytest
from sourceloom.active_composition import validate_plan, legacy_artifacts, initialize, ActiveComposition
from sourceloom.active_resources import Resources
from sourceloom.store import Store, digest
from sourceloom.durable import Queue
from sourceloom.production import Production
from sourceloom.ingest import intake
from tests.test_production import skill, prepared


def source():
    return dict(objects=[dict(id='s1', kind='text', locator='input/1', text='We may use three items only if ready.'),
                         dict(id='s2', kind='text', locator='input/2', text='Do not use them otherwise.')],
                obligations=[], resources=[], unknown=[], originals=[], version=1, id='src', frozen=False, digest='old')


def plan():
    return dict(contract=dict(purpose='Preserve the instruction',reader_start='Can read',voice='We',
                              scope_boundary='No invented procedures',depth='clarify',permitted_additions=[]),
        obligations=[dict(id='f'+str(i),source_id='s'+str(i),quote=text,meaning=text,
            conditions=['if ready'] if i==1 else [],quantities=['three'] if i==1 else [],
            negations=[] if i==1 else ['Do not'],narrator='We',referents=['items'])
            for i,text in enumerate([o['text'] for o in source()['objects']],1)],concepts=[],
        nodes=[dict(id='n1',title='适用条件',purpose='Clarify conditions',source_ids=['s1','s2'],
            obligation_ids=['f1','f2'],requires_concepts=[],establishes_concepts=[],depends_on=[],
            transition_from='No preceding content',prepares_for='No next section',depth='clarify',expansion='source_only',
            explanation=dict(known_start='Reader can read',obstacle='The permission is conditional',
                             reasoning_steps=['state condition','state exception'],boundary='Only these items'))])


def test_plan_requires_all_sources_and_preserves_explicit_qualifications():
    result=validate_plan(plan(),source(),['s1','s2'])
    inv, legacy=legacy_artifacts([result],source())
    assert inv['obligations'][0]['conditions']==['if ready']
    assert not inv['inventory_review']
    assert inv['frozen'] and legacy['units'][0]['obligation_ids']==['f1','f2']
    broken=plan();broken['obligations'].pop()
    with pytest.raises(ValueError,match='全部原对象'):validate_plan(broken,source(),['s1','s2'])


@pytest.mark.parametrize('mutate',[
    lambda p:p['nodes'][0].update(depends_on=['future']),
    lambda p:p['nodes'][0].update(requires_concepts=['unlearned']),
    lambda p:p['nodes'][0].update(expansion='authorized_example'),
    lambda p:p['nodes'][0].update(obligation_ids=['f1','f1','f2']),
    lambda p:p['obligations'][0].update(quote='They always use three items'),
])
def test_invalid_planning_cannot_become_default_content(mutate):
    broken=plan();mutate(broken)
    with pytest.raises(ValueError):validate_plan(broken,source(),['s1','s2'])


def test_identical_resources_deduplicate_content_without_losing_addresses(tmp_path):
    src=source();src['objects'][1]['text']=src['objects'][0]['text']
    resources=Resources(Store(tmp_path),src)
    resources.read('s1');resources.read('s2')
    assert len(resources.context())==1
    assert len(resources.context()[0]['addresses'])==2
    resources.execute(dict(kind='release',resource_id='s1'))
    restored=Resources(Store(tmp_path),src,resources.state)
    assert restored.fully_read('s1') and restored.text('s1')==src['objects'][0]['text']
    assert restored.context()[0]['addresses'][0]['id']=='s2'
    restored.read('s1')
    assert len(restored.context())==1 and len(restored.context()[0]['addresses'])==2


def test_resource_partial_reads_cannot_claim_unread_middle(tmp_path):
    resources=Resources(Store(tmp_path),source())
    resources.read('s1',0,3);resources.read('s1',8)
    assert not resources.fully_read('s1')
    resources.read('s1',3,8)
    assert resources.fully_read('s1')


def test_source_changes_invalidate_saved_resource_reads(tmp_path):
    resources=Resources(Store(tmp_path),source());resources.read('s1')
    changed=source();changed['objects'][0]['text']='changed'
    with pytest.raises(ValueError,match='版本发生变化'):Resources(Store(tmp_path),changed,resources.state)


def test_pipeline_identity_is_frozen_at_enqueue(tmp_path,skill):
    store,queue,p,bundle=prepared(tmp_path,skill)
    job=Queue(store,pipeline='active_composition_v1').enqueue(p['id'],bundle)
    assert job['stage']=='active_index' and job['pipeline']=='active_composition_v1'
    claimed=Queue(Store(store.root),pipeline='legacy').claim('new-worker')
    assert claimed['pipeline']==job['pipeline']
    assert claimed['source_snapshot_digest']==digest(store.get(p['id'])['inventory'])


def test_active_calls_replay_saved_result_without_provider_cost(tmp_path,monkeypatch):
    engine=ActiveComposition(Production(Store(tmp_path),{}))
    monkeypatch.setattr('sourceloom.active_composition.Provider.call',lambda *a:pytest.fail('Repeated paid request'))
    assert engine.call({'results':{'saved':{'answer':'exact'}}},'saved','active_plan',{},None)=={'answer':'exact'}


def test_completed_composition_is_not_an_independent_semantic_review():
    from sourceloom.checks import review_complete
    from sourceloom.writing import canonical
    p=dict(revision=2,draft={'blocks':[{'markdown':'正文'}]},production=dict(
        pipeline='active_composition_v1',automatic=True,status='completed',revision=2,manual_edits=0,issues=[],skill_digest='x'))
    p['production']['canonical_digest']=digest(canonical(p['draft']).encode())
    assert not review_complete(p)
    p['independent_review']=dict(status='passed',revision=2,canonical_digest=p['production']['canonical_digest'])
    assert review_complete(p)
    p['draft']['blocks'][0]['markdown']='changed'
    assert not review_complete(p)


def test_new_pipeline_progress_describes_actual_unit():
    from sourceloom.progress import summary
    result=summary(dict(stage='active_write',status='running',unit_index=2,unit_count=5))
    assert result['current']==3 and '3 / 5' in result['label']


def test_completed_json_can_ignore_only_surplus_closing_delimiters():
    from sourceloom.providers import parse_json
    assert parse_json('{"body":"unchanged"}}}')=={'body':'unchanged'}
    with pytest.raises(ValueError):parse_json('{"body":"unchanged"}{"extra":"lost"}')
    with pytest.raises(ValueError):parse_json('{"body":"unchanged"} arbitrary words')


def test_format_patch_signature_rejects_semantic_word_and_number_changes():
    from sourceloom.active_composition import format_signature
    assert format_signature('如果成立；继续')==format_signature('如果成立\n\n继续')
    assert format_signature('可以继续')!=format_signature('不可以继续')
    assert format_signature('3 个')!=format_signature('4 个')
    assert format_signature('-3')!=format_signature('3')
    assert format_signature('0.1')!=format_signature('01')
    assert format_signature('5-3')!=format_signature('53')


def test_compiler_binds_actual_body_and_never_trusts_model_self_quotation():
    from sourceloom.active_composition import validate_written
    p=plan();inv,_=legacy_artifacts([p],source());node=p['nodes'][0]
    value=dict(blocks=[dict(id='n1-b1',kind='explanation',markdown='只有准备好时，我们才可以使用三个项目\n\n否则不要使用',
        obligation_ids=['f1','f2'],source_ids=['s1','s2'])],
        coverage=[dict(obligation_id='f1',block_id='n1-b1',output_quote='只有准备好时，我们才可以使用三个项目'),
                  dict(obligation_id='f2',block_id='n1-b1',output_quote='否则不要使用')],
        knowledge_delta=dict(established_concepts=[],explained_obligations=['f1','f2'],
            unresolved_prerequisites=[],next_bridge='',concept_evidence=[]))
    draft,coverage,delta=validate_written(value,node,inv,None,{'blocks':[]})
    assert draft['blocks'][0]['evidence'][0]['quote']==source()['objects'][0]['text']
    broken=copy.deepcopy(value);broken['coverage'][1]['output_quote']='invented evidence'
    _,actual,_=validate_written(broken,node,inv,None,{'blocks':[]})
    assert actual[1]['output_quote']==draft['blocks'][0]['markdown']
    assert 'invented evidence' not in actual[1]['output_quote']
    broken['blocks'][0]['obligation_ids']=['f1']
    with pytest.raises(ValueError,match='结构校验'):validate_written(broken,node,inv,None,{'blocks':[]})


def test_protected_resource_is_inserted_by_identity_with_exact_bytes():
    from sourceloom.active_composition import validate_written
    src=source();src['objects']=src['objects'][:1];src['objects'][0].update(kind='code',text='x = 3',fence_raw='```python\nx = 3\n```')
    p=plan();p['obligations']=p['obligations'][:1];p['obligations'][0]['quote']='x = 3'
    p['nodes'][0].update(source_ids=['s1'],obligation_ids=['f1']);inv,_=legacy_artifacts([p],src)
    value=dict(blocks=[dict(id='n1-code',kind='object',markdown='{{source:s1}}',obligation_ids=['f1'],source_ids=['s1'])],
        coverage=[dict(obligation_id='f1',block_id='n1-code',output_quote='{{source:s1}}')],
        knowledge_delta=dict(established_concepts=[],explained_obligations=['f1'],unresolved_prerequisites=[],next_bridge=''))
    draft,coverage,_=validate_written(value,p['nodes'][0],inv,None,{'blocks':[]})
    assert draft['blocks'][0]['markdown']==src['objects'][0]['fence_raw']
    assert coverage[0]['output_quote']==src['objects'][0]['fence_raw']


def test_source_spans_preserve_every_character_and_reject_an_unassigned_tail():
    from sourceloom.active_composition import source_spans
    src=source();src['objects'][0]['text']='A long sentence. '*100+'Final exception is mandatory.'
    spans=source_spans(src,['s1'])
    assert ''.join(src['objects'][0]['text'][s['start']:s['end']] for s in spans)==src['objects'][0]['text']
    p=plan();p['nodes'][0]['obligation_ids']=['f1'];p['nodes'][0]['source_ids']=['s1']
    p['obligations']=p['obligations'][:1]
    p['obligations'][0].update(quote='',source_span_ids=[s['id'] for s in spans])
    validated=validate_plan(p,src,['s1'],require_spans=True)
    assert validated['obligations'][0]['quote']==src['objects'][0]['text']
    p['obligations'][0]['source_span_ids'].pop()
    with pytest.raises(ValueError,match='未分配的原文片段'):validate_plan(p,src,['s1'],require_spans=True)


def test_resource_search_is_discovery_not_verified_source(tmp_path,monkeypatch):
    import sourceloom.network
    xml=b'<rss><channel><item><title>Source</title><link>https://example.org/reference</link><description>Untrusted summary</description></item></channel></rss>'
    monkeypatch.setattr(sourceloom.network,'fetch',lambda *args,**kwargs:(xml,'text/xml',args[0]))
    r=Resources(Store(tmp_path),source());result=r.execute(dict(kind='search',query='term'))
    assert result['evidence_status'].startswith('discovery_only')
    assert not r.context() and 'https://example.org/reference' not in r.state['entries']
    assert r.store.read_blob(result['snapshot_blob'])==xml


def test_background_restart_completes_planning_writing_review_without_repeating_calls(tmp_path,skill,monkeypatch):
    from sourceloom.active_composition import source_spans
    from sourceloom.writing import canonical
    store,_,p,bundle=prepared(tmp_path,skill)
    job=Queue(store,pipeline='active_composition_v1').enqueue(p['id'],bundle)
    calls=[]
    def provider(self,pid,role,payload,schema,job,cancelled):
        calls.append(role)
        if role=='active_plan':
            result=plan();result['obligations']=result['obligations'][:1]
            sid=payload['assigned_source_ids'][0]
            result['obligations'][0].update(source_id=sid,quote='',source_span_ids=[s['id'] for s in payload['source_spans']])
            result['nodes'][0].update(source_ids=[sid],obligation_ids=['f1'])
        elif role=='active_write':
            text='## 适用条件\n\n我们假设 `x` 为正'
            result=dict(blocks=[dict(id='n1-b1',kind='explanation',markdown=text,obligation_ids=['f1'],source_ids=payload['node']['source_ids'])],
                coverage=[dict(obligation_id='f1',block_id='n1-b1',output_quote='我们假设 `x` 为正')],
                knowledge_delta=dict(established_concepts=[],explained_obligations=['f1'],unresolved_prerequisites=[],next_bridge=''))
        elif role=='active_review':
            result=dict(findings=[],checked_obligation_ids=['f1'])
        else:pytest.fail('Unexpected routine review: '+role)
        return dict(gaps=[],actions=[],ready_reason='Required material is read',result=result)
    monkeypatch.setattr('sourceloom.active_composition.Provider.call',provider)
    monkeypatch.setattr('sourceloom.active_composition.scan',lambda bundle,draft,work:dict(
        canonical_digest=digest(canonical(draft).encode()),format=dict(findings=[],candidates=[])))
    for _ in range(8):
        engine=Production(Store(store.root),{})
        assert engine.run_once()
        saved=store.job(job['id'])
        if saved['status']=='completed':break
        assert saved['status']=='queued',saved.get('error')
    final=store.get(p['id'])
    assert calls==['active_plan','active_write','active_review']
    assert final['state']=='completed' and final['production']['manual_edits']==0
    assert final['production']['semantic_status']=='not_independently_reviewed'
    assert store.read_blob(next(iter(saved['generated_resources'].values()))['blob']).decode()==canonical(final['draft'])
    assert saved['active_checkpoints'][0]['draft_digest']==digest(canonical(final['draft']).encode())


def test_layout_unwrap_preserves_raw_and_real_data_tables():
    from sourceloom.source_context import classify_layout_tables
    raw='<table><tr><td><img src="x.png"></td><td><p>Actual article</p></td></tr></table>'
    original={'objects':[{'id':'s1','kind':'table','text':'Actual article','raw':raw}]}
    result=classify_layout_tables(original)
    assert result['objects'][0]['kind']=='text'
    assert result['objects'][0]['raw']==raw
    assert original['objects'][0]['kind']=='table'
    original['objects'][0]['raw']='<table><tr><th>Name</th><th>Value</th></tr><tr><td>A</td><td>2</td></tr></table>'
    assert classify_layout_tables(original)['objects'][0]['kind']=='table'


def test_writing_batches_keep_outline_and_every_fact_within_size_limit():
    import copy
    from sourceloom.active_composition import writing_batches
    p=plan();first=p['nodes'][0];first['obligation_ids']=['f1'];first['source_ids']=['s1']
    second=copy.deepcopy(first);second.update(id='n2',obligation_ids=['f2'],source_ids=['s2'],depends_on=['n1'])
    p['nodes'].append(second)
    batches=writing_batches([p],source(),10000)
    assert len(batches)==1
    assert batches[0]['obligation_ids']==['f1','f2']
    assert [n['id'] for n in batches[0]['section_outline']]==['n1','n2']
    assert len(writing_batches([p],source(),1))==2


def test_resource_find_uses_original_unicode_offsets_and_bounded_results(tmp_path):
    src=source();src['objects'][0]['text']='İß abc '+'abc '*105
    resources=Resources(Store(tmp_path),src)
    result=resources.execute(dict(kind='find',resource_id='s1',query='ABC'))
    assert len(result['matches'])==100 and result['has_more']
    assert result['matches'][0]['start']==3
    for match in result['matches']:
        assert src['objects'][0]['text'][match['start']:match['end']]=='abc'


def test_compiled_literal_can_resume_revision_without_duplicate_insertion():
    from sourceloom.active_composition import validate_written
    src=source();src['objects']=src['objects'][:1]
    src['objects'][0].update(kind='code',text='x = 3',fence_raw='```python\nx = 3\n```')
    p=plan();p['obligations']=p['obligations'][:1];p['obligations'][0]['quote']='x = 3'
    node=p['nodes'][0];node.update(source_ids=['s1'],obligation_ids=['f1'])
    inv,_=legacy_artifacts([p],src)
    value=dict(blocks=[dict(id='n1-code',kind='object',markdown=src['objects'][0]['fence_raw'],
        obligation_ids=['f1'],source_ids=['s1'])],knowledge_delta=dict(established_concepts=[],
        explained_obligations=['f1'],unresolved_prerequisites=[],next_bridge=''))
    draft,_,_=validate_written(value,node,inv,None,{'blocks':[]})
    assert len(draft['blocks'])==1
    assert draft['blocks'][0]['embedded_object_ids']==['s1']


@pytest.mark.parametrize('limit',[0,5,True,'4'])
def test_active_revision_limit_cannot_silently_create_unbounded_loops(tmp_path,limit):
    with pytest.raises(ValueError,match='一至四'):
        ActiveComposition(Production(Store(tmp_path),{'active_revision_limit':limit}))


def test_content_correction_routes_through_committer_and_scoped_review(tmp_path,skill,monkeypatch):
    from sourceloom.writing import canonical
    store,_,p,bundle=prepared(tmp_path,skill)
    job=Queue(store,pipeline='active_composition_v1').enqueue(p['id'],bundle)
    calls=[]
    def provider(self,pid,role,payload,schema,job,cancelled):
        calls.append(role)
        if role=='active_plan':
            result=plan();result['obligations']=result['obligations'][:1]
            sid=payload['assigned_source_ids'][0]
            result['obligations'][0].update(source_id=sid,quote='',source_span_ids=[s['id'] for s in payload['source_spans']])
            result['nodes'][0].update(source_ids=[sid],obligation_ids=['f1'])
        elif role=='active_write':
            result=dict(blocks=[dict(id='n1-b1',kind='explanation',markdown='## 适用条件\n\n我们假设 `x` 为负',
                obligation_ids=['f1'],source_ids=payload['node']['source_ids'])],knowledge_delta=dict(
                    established_concepts=[],explained_obligations=['f1'],unresolved_prerequisites=[],next_bridge=''))
        elif role=='active_review':
            negative='为负' in canonical(payload['actual_draft'])
            result=dict(findings=[dict(block_id='n1-b1',output_quote='为负',problem='改变了原条件',required_change='恢复正值条件')] if negative else [],checked_obligation_ids=['f1'])
            if not negative:assert payload['exact_revision']['edits'][0]['new_text']=='为正'
        elif role=='active_patch':
            result=dict(document_digest=payload['document_digest'],edits=[dict(block_id='n1-b1',old_text='为负',new_text='为正',reason='恢复原条件')])
        else:pytest.fail('Unexpected role '+role)
        return dict(gaps=[],actions=[],ready_reason='已读取所需原件',result=result)
    monkeypatch.setattr('sourceloom.active_composition.Provider.call',provider)
    def committer(bundle,draft,proposal,allowed,work):
        assert allowed=={'n1-b1'}
        assert proposal['document_digest']==digest(canonical(draft).encode())
        assert proposal['edits']==[dict(block_id='n1-b1',old_text='为负',new_text='为正',reason='恢复原条件')]
        changed=copy.deepcopy(draft)
        changed['blocks'][0]['markdown']='## 适用条件\n\n我们假设 `x` 为正'
        return changed
    monkeypatch.setattr('sourceloom.active_composition.repair',committer)
    monkeypatch.setattr('sourceloom.active_composition.scan',lambda bundle,draft,work:dict(
        canonical_digest=digest(canonical(draft).encode()),format=dict(findings=[],candidates=[])))
    for _ in range(12):
        assert Production(Store(store.root),{}).run_once()
        saved=store.job(job['id'])
        assert saved['status'] in {'queued','completed'},saved.get('error')
        if saved['status']=='completed':break
    assert saved['status']=='completed'
    assert calls==['active_plan','active_write','active_review','active_patch','active_review']
    assert canonical(saved['draft'])=='## 适用条件\n\n我们假设 `x` 为正\n'
    assert saved['active_checkpoints'][0]['content_patches'][0]['edits'][0]['old_text']=='为负'
    assert saved['delivery_checks']['source_digest']==saved['source_snapshot_digest']
    assert saved['delivery_checks']['source_digest_kind']=='initial_inventory_snapshot'
    assert saved['delivery_checks']['compiled_inventory_digest']==saved['inventory']['digest']


def test_spacing_normalization_and_scan_exemptions_preserve_exact_original_code():
    from sourceloom.active_composition import normalize_authored_spacing,respect_original_format
    from sourceloom.writing import canonical
    original='```python\nx = 3\n```\n'
    inv=source();inv['objects']=[dict(id='code',kind='code',text='x = 3',fence_raw=original)]
    draft={'blocks':[dict(id='b1',kind='explanation',markdown='- 第一项\n\n- 第二项',embedded_object_ids=[]),
        dict(id='b2',kind='object',markdown=original,embedded_object_ids=['code'])]}
    fixed=normalize_authored_spacing(draft,inv)
    assert fixed['blocks'][0]['markdown']=='- 第一项\n- 第二项'
    assert fixed['blocks'][1]['markdown']==original
    rows=canonical(fixed).splitlines();row=rows.index('x = 3')+1
    finding=dict(id='source-code',location=f'LINE-{row:04d}',old_text='x = 3',rule_id='FORMAT_CODE_COMMENT_COVERAGE')
    authored=dict(id='authored',location='LINE-0001',old_text='第一项',rule_id='example')
    report=dict(format=dict(findings=[finding,authored],candidates=[]))
    adapted=respect_original_format(report,fixed,inv)
    assert adapted['format']['findings']==[authored]
    assert adapted['original_exemptions'][0]['source_id']=='code'
    assert report['format']['findings']==[finding,authored]
    fence_report=dict(format=dict(findings=[finding|{'location':f'LINE-{row-1:04d}'}],candidates=[]))
    assert not respect_original_format(fence_report,fixed,inv)['format']['findings']


@pytest.mark.parametrize('reference_id',['link','term-reference'])
def test_intact_content_link_still_requires_its_explanation(tmp_path,skill,monkeypatch,reference_id):
    from sourceloom.writing import protected_objects
    store,_,project,bundle=prepared(tmp_path,skill)
    src=source();src['objects']=[dict(id='link',kind='link',text='Reference',
        locator='input/a',target='https://example.org/topic')]
    node=plan()['nodes'][0]|dict(source_ids=['link'],obligation_ids=['f1'])
    obligation=plan()['obligations'][0]|dict(source_id='link',quote='Reference')
    literal=protected_objects(src)['link']
    draft={'blocks':[dict(id='n1-b1',kind='explanation',markdown=literal+'\n需要修正的标点。',
                         obligation_ids=['f1'],object_ids=['link'])]}
    job=dict(id='review-fixture',calls=[],stage='active_review',writing_skill=bundle,verified_terminology=[],source=src,
        inventory=src,unit_index=0,draft={'blocks':[]},writing_batches=[node],
        active_plans=[plan()|dict(obligations=[obligation],concepts=[])],
        visual_cards=[dict(source_id='link',source_text='Reference',visible_content='A visible diagram')],
        active_candidate=dict(draft=draft))
    review=dict(findings=[dict(block_id='n1-b1',output_quote=literal,source_id=reference_id,
        source_quote='Reference',problem='Missing destination explanation',
        required_change='Explain the destination beside the unchanged link')],checked_obligation_ids=['f1'])
    engine=ActiveComposition(Production(store,{}))
    mechanical=dict(id='format-first',rule_id='FMT-017',location='LINE-2',
                    old_text='需要修正的标点。',message='Fixture format finding')
    monkeypatch.setattr('sourceloom.active_composition.scan',lambda *a:dict(format=dict(findings=[mechanical],candidates=[])))
    resources=Resources(store,src)
    resources.add('term-reference','Reference',kind='external',locator='https://example.org/name')
    def review_turn(job,key,role,schema,source,ids,payload,validate):
        assert payload['visual_cards']==[dict(source_id='link',visible_content='A visible diagram')]
        return validate(review,resources)
    monkeypatch.setattr(engine,'turn',review_turn)
    assert engine.step(job)=='queued'
    assert job['stage']=='active_revision'
    assert job['active_candidate']['revision_issues']['findings'][0]['output_quote']==literal
    assert job['active_candidate']['revision_issues']['protected_reference_notes']==[]
    assert job['active_candidate']['revision_issues']['findings'][1]['output_quote']=='需要修正的标点。'


def test_joint_review_maps_format_lines_to_actual_blocks():
    from sourceloom.active_composition import located_format_issues
    draft={'blocks':[dict(id='first',markdown='## Heading\nFirst paragraph'),
                     dict(id='second',markdown='Second paragraph\nLast line') ]}
    report={'format':{'findings':[dict(location='LINE-4',old_text='stale scanner text')],
                      'candidates':[dict(location='LINE-5',old_text='Last line')]}}
    result=located_format_issues(report,draft)
    assert result['findings'][0]['block_id']=='second'
    assert result['findings'][0]['output_quote']=='Second paragraph'
    assert result['candidates'][0]['block_id']=='second'
    assert result['candidates'][0]['output_quote']=='Last line'


def test_candidate_dismissal_is_bound_to_the_actual_unchanged_block():
    from sourceloom.active_composition import candidate_key
    draft={'blocks':[dict(id='a',markdown='First'),dict(id='b',markdown='Other') ]}
    issue=dict(id='c1',rule_id='review',location='LINE-0003',old_text='Other')
    key=candidate_key(issue,draft)
    moved=copy.deepcopy(draft);moved['blocks'][0]['markdown']='First\n\nMore'
    assert candidate_key(issue|{'id':'c8','location':'LINE-0005'},moved)==key
    moved['blocks'][1]['markdown']='Changed other'
    assert candidate_key(issue|{'location':'LINE-0005'},moved)!=key


def test_annotated_revision_id_preserves_reason_without_loosening_identity():
    from sourceloom.active_composition import normalize_revision_ids
    value=dict(requires_revision=['candidates-2: missing official English name'],reasons=[])
    result=normalize_revision_ids(value,{'candidates-2'})
    assert result['requires_revision']==['candidates-2']
    assert result['reasons']==value['requires_revision']
    assert value['reasons']==[]
    with pytest.raises(ValueError,match='不存在'):
        normalize_revision_ids(value,{'candidates-3'})


def test_review_quote_uses_actual_pdf_characters_without_fuzzy_matching():
    from sourceloom.active_composition import exact_source_quote
    assert exact_source_quote('We refer to this workflow.','We refer\nto this workflow.')=='We refer\nto this workflow.'
    assert exact_source_quote('We refer to this workflow.','We refer\nto a workflow.') is None
    assert exact_source_quote('value 30','value 3.0') is None
    assert exact_source_quote('a b','a\nb a\tb') is None
    assert exact_source_quote('','arbitrary source') is None
    quote='Such executable descriptions preserve the original sequence.'
    source='Such execut-\nable descriptions preserve the original sequence.'
    assert exact_source_quote(quote,source) is None
    assert exact_source_quote(quote,source,pdf_wrap=True)==source
    assert exact_source_quote('recreation','re-\ncreation',pdf_wrap=True) is None


def test_quota_rejection_can_resume_after_fallback_budget_preflight(tmp_path,skill):
    import json
    from sourceloom.store import Conflict
    store,queue,p,bundle=prepared(tmp_path,skill)
    job=queue.enqueue(p['id'],bundle)
    call=dict(id='quota-stop',role='active_review',status='rejected',
        error_code='subscription_limit_exceeded',step_key='review-1')
    job.update(status='failed',pending='review-1',calls=[call])
    store.put_job(job);store.change(p['id'],lambda p:p.update(active_job=None))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='failed' WHERE id=?",(job['id'],))
        cx.execute('INSERT INTO spending(id,project,reserved,actual,status,body) VALUES(?,?,?,?,?,?)',
            ('quota-stop',p['id'],0,0,'settled',json.dumps({'status':'quota_rejected'})))
    resumed=queue.retry_validation(job['id'])
    assert resumed['status']=='queued' and not resumed.get('pending')
    assert resumed['calls']==[call]
    resumed.update(status='failed',pending='review-1');store.put_job(resumed)
    store.change(p['id'],lambda p:p.update(active_job=None))
    with pytest.raises(Conflict,match='只允许'):
        queue.retry_validation(job['id'])

def test_confirmed_existing_term_pair_is_a_bounded_format_patch():
    from sourceloom.active_composition import compiled_term_format_proposal
    from sourceloom.writing import canonical
    draft={'blocks':[{'id':'a','kind':'explanation','markdown':'- 工作流（Workflow）：原有定义\n- 构建文件（Makefile）：用于描述工作流'}]}
    issue={'id':'candidates-1','rule_id':'FORMAT_NESTED_DEFINED_TERM_REVIEW','old_text':'工作流','location':'LINE-0002'}
    request={'findings':[issue],'format_response':{'requires_revision':['candidates-1'],'document_digest':digest(canonical(draft).encode()),'edits':[]}}
    patch=compiled_term_format_proposal(draft,request)
    assert patch['edits'][0]['old_text']=='- 构建文件（Makefile）：用于描述工作流'
    assert patch['edits'][0]['new_text']=='- 构建文件（Makefile）：用于描述工作流（Workflow）'
    assert draft['blocks'][0]['markdown'].endswith('描述工作流')
    request['format_response']['requires_revision']=[]
    assert compiled_term_format_proposal(draft,request) is None


def test_term_pair_compiler_rejects_ambiguous_unsupported_or_stale_evidence():
    from sourceloom.active_composition import compiled_term_format_proposal
    from sourceloom.writing import canonical
    for declaration in ['- 工作流（Workflow）：定义\n- 工作流（Process）：另一含义', '```text\n- 工作流（Workflow）：只是代码\n```', '- 未知词（Workflow）：不对应']:
        draft={'blocks':[{'id':'a','kind':'explanation','markdown':declaration},{'id':'b','kind':'explanation','markdown':'- 构建文件（Makefile）：用于描述工作流'}]}
        line=declaration.count('\n')+3
        issue={'id':'candidates-1','rule_id':'FORMAT_NESTED_DEFINED_TERM_REVIEW','old_text':'工作流','location':f'LINE-{line:04d}'}
        request={'findings':[issue],'format_response':{'requires_revision':['candidates-1'],'document_digest':digest(canonical(draft).encode()),'edits':[]}}
        assert compiled_term_format_proposal(draft,request) is None
    draft['blocks'][0]['markdown']='- 工作流（Workflow）：定义'
    assert compiled_term_format_proposal(draft,request) is None


def test_term_pair_compiler_does_not_drop_other_confirmed_defects():
    from sourceloom.active_composition import compiled_term_format_proposal
    from sourceloom.writing import canonical
    draft={'blocks':[{'id':'a','kind':'explanation','markdown':'- 工作流（Workflow）：定义\n- 构建文件（Makefile）：描述工作流'}]}
    issues=[{'id':'candidates-1','rule_id':'FORMAT_NESTED_DEFINED_TERM_REVIEW','old_text':'工作流','location':'LINE-0002'},
            {'id':'findings-1','rule_id':'OTHER','old_text':'未处理的另一问题','location':'LINE-0001'}]
    request={'findings':issues,'format_response':{'requires_revision':['candidates-1'],'document_digest':digest(canonical(draft).encode()),'edits':[]}}
    assert compiled_term_format_proposal(draft,request) is None

def test_term_pair_compiler_leaves_word_changing_proposals_for_recheck():
    from sourceloom.active_composition import compiled_term_format_proposal
    from sourceloom.writing import canonical
    draft={'blocks':[{'id':'a','kind':'explanation','markdown':'- 工作流（Workflow）：定义\n- 构建文件（Makefile）：描述工作流\n可选择甲，也可选择乙'}]}
    issues=[{'id':'candidates-1','rule_id':'FORMAT_NESTED_DEFINED_TERM_REVIEW','old_text':'工作流','location':'LINE-0002'},
            {'id':'candidates-2','rule_id':'PARALLEL','old_text':'可选择甲，也可选择乙','location':'LINE-0003'}]
    request={'findings':issues,'format_response':{'requires_revision':['candidates-1'],'document_digest':digest(canonical(draft).encode()),
        'edits':[{'block_id':'a','old_text':'可选择甲，也可选择乙','new_text':'- 甲\n- 乙','rule':'candidates-2'}]}}
    proposal=compiled_term_format_proposal(draft,request)
    assert len(proposal['edits'])==1
    assert proposal['edits'][0]['new_text'].endswith('工作流（Workflow）')
    assert draft['blocks'][0]['markdown'].endswith('可选择甲，也可选择乙')

def test_active_composition_loads_verified_names_once_before_generation(tmp_path,skill,monkeypatch):
    store,queue,p,bundle=prepared(tmp_path,skill)
    job=Queue(store,pipeline='active_composition_v1').enqueue(p['id'],bundle)
    calls=[]
    def lookup(store,source):
        calls.append(source['id']);return [{'abbr':'EX','quote':'Synthetic exact evidence'}]
    monkeypatch.setattr('sourceloom.terminology.verified_terms',lookup)
    engine=ActiveComposition(Production(store,{}))
    assert engine.step(job)=='queued'
    assert job['verified_terminology'][0]['quote']=='Synthetic exact evidence'
    assert engine.step(job)=='queued'
    assert len(calls)==1


def test_resource_rounds_keep_prior_queries_and_negative_results(tmp_path,skill,monkeypatch):
    from pydantic import BaseModel
    class Answer(BaseModel):
        value:int
    store,queue,p,bundle=prepared(tmp_path,skill)
    job=Queue(store,pipeline='active_composition_v1').enqueue(p['id'],bundle)
    engine=ActiveComposition(Production(store,{}));requests=[]
    def call(job,key,role,payload,schema):
        requests.append(copy.deepcopy(payload))
        if len(requests)<3:
            return dict(gaps=['Need exact location'],actions=[dict(kind='find',resource_id='s1',query='absent' if len(requests)==1 else 'ready')],result=None,ready_reason='')
        return dict(gaps=[],actions=[],result={'value':1},ready_reason='Compared source and search results')
    monkeypatch.setattr(engine,'call',call)
    assert engine.turn(job,'test','active_review',Answer,source(),['s1'],{},lambda value,r:value)=={'value':1}
    assert len(requests[-1]['action_history'])==2
    assert requests[-1]['action_history'][0]['result']['matches']==[]
    assert requests[-1]['action_history'][1]['result']['matches']

def test_grouped_review_findings_expand_only_exact_individual_quotes():
    from sourceloom.active_composition import expand_exact_grouped_findings
    draft={'blocks':[{'id':'a','markdown':'## First\nBody A'},{'id':'b','markdown':'## Second\nBody B'}]}
    issue={'block_id':'a, b','output_quote':'## First\n## Second','problem':'Same heading defect','source_quote':'unchanged'}
    result=expand_exact_grouped_findings({'findings':[issue]},draft)
    assert [f['block_id'] for f in result['findings']]==['a','b']
    assert [f['output_quote'] for f in result['findings']]==['## First','## Second']
    assert all(f['source_quote']=='unchanged' for f in result['findings'])
    assert result['grouped_finding_expansions'][0]['submitted']==issue
    for bad in [issue|{'block_id':'a, missing'},issue|{'output_quote':'## Second\n## First'},issue|{'output_quote':'## First'},issue|{'block_id':'a, a'}]:
        assert expand_exact_grouped_findings({'findings':[bad]},draft)['findings']==[bad]
