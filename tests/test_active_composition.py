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


def test_internal_english_plan_title_does_not_block_generation():
    candidate=plan();candidate['nodes'][0]['title']='Compute Throughput'
    assert validate_plan(candidate,source(),['s1','s2'])['nodes'][0]['title']=='Compute Throughput'


def test_same_node_concepts_are_established_in_dependency_order():
    candidate=plan();candidate['obligations']=candidate['obligations'][:1]
    candidate['nodes'][0].update(source_ids=['s1'],obligation_ids=['f1'],
        requires_concepts=['advanced','base'],establishes_concepts=[])
    common=dict(definition='Source concept',source_ids=['s1'],chinese_name='',english_name='',
        naming_status='unsearched',name_evidence=[],abbreviations=[],naming_note='',
        naming_status_reason='')
    candidate['concepts']=[dict(id='base',name='Base',requires=[],**common),
        dict(id='advanced',name='Advanced',requires=['base'],**common)]
    validated=validate_plan(candidate,source(),['s1'])
    assert validated['nodes'][0]['establishes_concepts']==['base','advanced']
    assert validated['nodes'][0]['requires_concepts']==[]


def test_explicit_same_node_establishment_is_sorted_by_dependency():
    candidate=plan();candidate['obligations']=candidate['obligations'][:1]
    candidate['nodes'][0].update(source_ids=['s1'],obligation_ids=['f1'],
        requires_concepts=[],establishes_concepts=['advanced','base'])
    common=dict(definition='Source concept',source_ids=['s1'],chinese_name='',english_name='',
        naming_status='unsearched',name_evidence=[],abbreviations=[],naming_note='',
        naming_status_reason='')
    candidate['concepts']=[dict(id='base',name='Base',requires=[],**common),
        dict(id='advanced',name='Advanced',requires=['base'],**common)]
    validated=validate_plan(candidate,source(),['s1'])
    assert validated['nodes'][0]['establishes_concepts']==['base','advanced']


def test_repeated_summary_node_does_not_duplicate_source_obligations():
    candidate=plan();summary=copy.deepcopy(candidate['nodes'][0])
    summary.update(id='n2',title='重复汇总',depends_on=['n1'],
        requires_concepts=[],establishes_concepts=[])
    candidate['nodes'].append(summary)
    validated=validate_plan(candidate,source(),['s1','s2'])
    assert [node['id'] for node in validated['nodes']]==['n1']


def test_redundant_effective_name_status_is_removed_only_from_validation_copy():
    from sourceloom.active_composition import discard_known_plan_protocol_extras
    raw={'result':{'concepts':[{'id':'cache','requires':[],'naming_status':'ambiguous',
        'naming_status_effective':'ambiguous'}],
        'nodes':[{'id':'node-1','explanation_placeholder':''}]}}
    cleaned,removed=discard_known_plan_protocol_extras(raw)
    assert removed==['cache','node-1'] and 'naming_status_effective' not in cleaned['result']['concepts'][0]
    assert 'explanation_placeholder' not in cleaned['result']['nodes'][0]
    assert raw['result']['concepts'][0]['naming_status_effective']=='ambiguous'
    assert raw['result']['nodes'][0]['explanation_placeholder']==''


def test_missing_concept_dependency_list_is_normalized_without_another_model_call():
    from sourceloom.active_composition import discard_known_plan_protocol_extras
    raw={'result':{'concepts':[{'id':'faq','name':'FAQ'}],'nodes':[]}}
    cleaned,changed=discard_known_plan_protocol_extras(raw)
    assert changed==['faq'] and cleaned['result']['concepts'][0]['requires']==[]
    assert 'requires' not in raw['result']['concepts'][0]


def test_empty_review_reason_note_is_removed_only_from_validation_copy():
    from sourceloom.active_composition import discard_known_review_protocol_extras
    raw={'result':{'format_decisions':[{'candidate_id':'candidate-1',
        'decision':'dismiss','reason':'The same sentence continues','reason_note':''}]}}
    cleaned,removed=discard_known_review_protocol_extras(raw)
    assert removed==['candidate-1'] and 'reason_note' not in cleaned['result']['format_decisions'][0]
    assert raw['result']['format_decisions'][0]['reason_note']==''


def test_unsupported_model_name_is_removed_instead_of_blocking_document(tmp_path):
    from sourceloom.active_composition import validate_names
    resources=Resources(Store(tmp_path),source());resources.read('s1')
    candidate={'concepts':[dict(id='igpu',name='iGPU',definition='Source term',
        source_ids=['s1'],requires=[],chinese_name='集成图形处理器',
        english_name='Invented Graphics Processing Unit',naming_status='verified',
        name_evidence=[dict(resource_id='s1',quote=source()['objects'][0]['text'])],
        abbreviations=[],naming_note='',naming_status_reason='')]}
    result=validate_names(candidate,resources,allow_unverified_downgrade=True)['concepts'][0]
    assert result['naming_status']=='ambiguous'
    assert not result['english_name'] and not result['abbreviations']
    assert result['naming_status_reason']=='unsupported_model_name_was_removed'


def test_unsearched_model_name_is_removed_without_losing_source_term(tmp_path):
    from sourceloom.active_composition import validate_names
    resources=Resources(Store(tmp_path),source());resources.read('s1')
    candidate={'concepts':[dict(id='fp32',name='FP32',definition='Source term',
        source_ids=['s1'],requires=[],chinese_name='FP32 运算',
        english_name='Unverified Full Name',naming_status='unsearched',
        name_evidence=[],abbreviations=[dict(short='FP32',chinese='单精度',english='Unverified Full Name')],
        naming_note='',naming_status_reason='')]}
    result=validate_names(candidate,resources,allow_unverified_downgrade=True)['concepts'][0]
    assert result['name']=='FP32' and result['naming_status']=='ambiguous'
    assert not result['english_name'] and not result['abbreviations']


def test_incomplete_verified_name_is_downgraded_without_blocking_document(tmp_path):
    from sourceloom.active_composition import validate_names
    resources=Resources(Store(tmp_path),source());resources.read('s1')
    candidate={'concepts':[dict(id='cache',name='cache',definition='Source term',
        source_ids=['s1'],requires=[],chinese_name='缓存',english_name='',
        naming_status='verified',name_evidence=[],abbreviations=[],naming_note='',
        naming_status_reason='')]}
    result=validate_names(candidate,resources,allow_unverified_downgrade=True)['concepts'][0]
    assert result['name']=='cache' and result['naming_status']=='ambiguous'
    assert result['naming_status_reason']=='incomplete_verified_name_was_removed'


def test_invalid_name_evidence_is_downgraded_in_lightweight_mode(tmp_path):
    from sourceloom.active_composition import validate_names
    resources=Resources(Store(tmp_path),source());resources.read('s1')
    candidate={'concepts':[dict(id='cache',name='cache',definition='Source term',
        source_ids=['s1'],requires=[],chinese_name='缓存',english_name='Cache',
        naming_status='verified',name_evidence=[dict(resource_id='missing',quote='Cache')],
        abbreviations=[],naming_note='',naming_status_reason='')]}
    result=validate_names(candidate,resources,allow_unverified_downgrade=True)['concepts'][0]
    assert result['naming_status']=='ambiguous' and not result['english_name']
    assert result['naming_status_reason']=='invalid_name_evidence_was_removed'


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


def test_prerequisites_can_be_established_in_order_within_one_unit():
    p=plan()
    p['concepts']=[dict(id='c1',name='First',definition='First concept',source_ids=['s1'],requires=[]),
                   dict(id='c2',name='Second',definition='Builds on first',source_ids=['s2'],requires=['c1'])]
    p['nodes'][0].update(requires_concepts=['c1','c2'],establishes_concepts=[])
    validated=validate_plan(p,source(),['s1','s2'])
    assert validated['nodes'][0]['establishes_concepts']==['c1','c2']


def test_coordinated_english_name_needs_explicit_shared_modifier():
    from sourceloom.active_composition import coordinated_name_in_quote
    assert coordinated_name_in_quote('Template Inclusion','Template inheritance and inclusion')
    assert not coordinated_name_in_quote('Template Inclusion','Data inheritance and inclusion')
    assert not coordinated_name_in_quote('Template Inclusion','Template inclusion is absent')


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


def test_active_rewrite_keeps_the_configured_v2_pipeline(tmp_path,skill):
    store,_,project,bundle=prepared(tmp_path,skill)
    job=Queue(store,pipeline='active_composition_v2').rewrite_active(project['id'],bundle)
    assert job['pipeline']=='active_composition_v2'
    assert store.job(job['id'])['pipeline']=='active_composition_v2'


def test_visual_card_scalar_lists_are_repaired_without_changing_words():
    from sourceloom.active_composition import normalize_visual_card_lists
    raw={'cards':[dict(source_id='image',visible_content='chart',source_text='',role='diagram',
        relationships='A points to B',uncertainty='',limitations=['cropped'],blocking_uncertainty='motion unseen')]}
    fixed=normalize_visual_card_lists(raw)
    assert fixed['cards'][0]['relationships']==['A points to B']
    assert fixed['cards'][0]['uncertainty']==['motion unseen']
    assert fixed['cards'][0]['limitations']==['cropped']
    assert raw['cards'][0]['relationships']=='A points to B'


def test_visual_card_missing_optional_lists_are_filled_without_another_model_call():
    from sourceloom.active_composition import normalize_visual_card_lists
    raw={'cards':[dict(source_id='image',visible_content='icon',source_text='',role='decorative')]}
    fixed=normalize_visual_card_lists(raw)
    assert {key:fixed['cards'][0][key] for key in
        ('relationships','uncertainty','limitations','blocking_uncertainty')}=={
            'relationships':[],'uncertainty':[],'limitations':[],'blocking_uncertainty':[]}
    assert all(key not in raw['cards'][0] for key in
        ('relationships','uncertainty','limitations','blocking_uncertainty'))


def test_active_rewrite_reuses_content_visuals_without_requiring_chrome_cards(tmp_path,skill):
    store,_,project,bundle=prepared(tmp_path,skill)
    def add_images(p):
        p['inventory']['objects'] += [
            dict(id='content-image',kind='image',locator='source/figure[1]',text='',
                 resource_id='a'*64,source_scope='article_media'),
            dict(id='content-media',kind='media',locator='source/iframe[1]',text='',
                 resource_id='c'*64,target='https://player.vimeo.com/video/123',
                 source_scope='article_media'),
            dict(id='chrome-image',kind='image',locator='source/header/img[1]',text='',
                 resource_id='b'*64,source_scope='site_chrome'),
            dict(id='empty-heading',kind='heading',locator='source/h2[2]',text='',
                 raw='<h2 class="spacer"></h2>'),
        ]
    store.change(project['id'],add_images)
    queue=Queue(store,pipeline='active_composition_v2')
    old=queue.enqueue(project['id'],bundle)
    classified=copy.deepcopy(store.get(project['id'])['inventory'])
    classified['classification_version']=1
    old.update(status='ready_for_review',source=classified,stage='active_plan',
        active_groups=[['s1'],['content-image','content-media']],active_plans=[{'contract':{'purpose':'kept'}}],
        active_partition_index=1,
        visual_cards=[dict(source_id='content-image',visible_content='Chart',source_text='',
                           role='diagram',relationships=[],uncertainty=[],limitations=[],
                           blocking_uncertainty=[]),
                      dict(source_id='content-media',visible_content='Video frame',source_text='',
                           role='video',relationships=[],uncertainty=[],limitations=[],
                           blocking_uncertainty=[]),
                      dict(source_id='chrome-image',visible_content='Logo',source_text='',
                           role='decoration',relationships=[],uncertainty=[],limitations=[],
                           blocking_uncertainty=[])])
    store.put_job(old)
    interrupted=copy.deepcopy(old)
    interrupted.update(id='newer-cancelled-visual',created=old['created']+1,
                       status='cancelled',visual_cards=[],active_plans=[],active_partition_index=0)
    store.put_job(interrupted)
    published=copy.deepcopy(classified)
    next(obj for obj in published['objects'] if obj['id']=='content-media')['text']='模型生成的视频说明'
    published.update(frozen=True,inventory_review={'status':'complete'},
        obligations=[dict(id='derived-obligation',object_id='s1',statement='same source')],
        digest='derived-published-inventory')
    store.change(project['id'],lambda p:p.update(active_job=None,inventory=published))
    rewritten=queue.rewrite_active(project['id'],bundle)
    assert rewritten['reused_visual_job']==old['id']
    assert [card['source_id'] for card in rewritten['visual_cards']]==[
        'content-image','content-media','chrome-image']
    assert next(o for o in rewritten['source']['objects'] if o['id']=='empty-heading')[
        'source_scope']=='layout_decorative'
    assert rewritten['stage']=='active_plan' and rewritten['reused_plan_job']==old['id']
    assert rewritten['active_partition_index']==1


def test_active_rewrite_reuses_complete_planning_before_first_writer(tmp_path,skill):
    store,_,project,bundle=prepared(tmp_path,skill)
    store.change(project['id'],lambda p:p['inventory']['objects'].append(dict(
        id='content-image',kind='image',locator='source/figure[1]',text='',
        resource_id='a'*64,source_scope='article_media')))
    queue=Queue(store,pipeline='active_composition_v2')
    old=queue.enqueue(project['id'],bundle)
    classified=copy.deepcopy(store.get(project['id'])['inventory'])
    old.update(status='ready_for_review',source=classified,stage='active_write',
        active_groups=[['s1']],active_plans=[{'contract':{'purpose':'kept'}}],
        active_partition_index=1,writing_batches=[{'id':'write-1'}],
        inventory=copy.deepcopy(classified),plan={'title':'validated'},
        visual_cards=[dict(source_id='content-image',visible_content='Chart',source_text='',
                           role='diagram',relationships=[],uncertainty=[],limitations=[],
                           blocking_uncertainty=[])])
    store.put_job(old)
    newer=copy.deepcopy(old)
    newer.update(id='newer-partial-plan',created=old['created']+1,status='cancelled',
        stage='active_plan',active_groups=[['s1'],['s2']],active_plans=[{'contract':{'purpose':'partial'}}],
        active_partition_index=1,writing_batches=[],inventory=None,plan=None)
    store.put_job(newer)
    published=copy.deepcopy(classified)
    published.update(frozen=True,inventory_review={'status':'complete'},
        obligations=[dict(id='derived',object_id='s1',statement='same source')],digest='derived')
    store.change(project['id'],lambda p:p.update(active_job=None,inventory=published))
    rewritten=queue.rewrite_active(project['id'],bundle)
    assert rewritten['stage']=='active_plan' and rewritten['unit_index']==0
    assert rewritten['reused_visual_job']==newer['id']
    assert rewritten['reused_plan_job']==old['id']
    assert rewritten['reused_writing_preparation_job']==old['id']
    assert rewritten.get('writing_batches',[])==[]


def test_active_rewrite_resumes_from_last_completed_writing_batch(tmp_path,skill):
    from sourceloom.writing import canonical
    store,_,project,bundle=prepared(tmp_path,skill)
    store.change(project['id'],lambda p:p['inventory']['objects'].append(dict(
        id='content-image',kind='image',locator='source/figure[1]',text='',
        resource_id='a'*64,source_scope='article_media')))
    queue=Queue(store,pipeline='active_composition_v2')
    old=queue.enqueue(project['id'],bundle)
    classified=copy.deepcopy(store.get(project['id'])['inventory'])
    block=dict(id='done-b1',unit_id='write-1',kind='explanation',markdown='已完成正文',
        obligation_ids=[],object_ids=['s1'],evidence=[],embedded_object_ids=[])
    checkpoint=dict(node_id='write-1',draft_digest=digest(canonical({'blocks':[block]}).encode()),unresolved_content_findings=[],
        unresolved_revision={},unresolved_format={})
    recovered=dict(id='recovered-s1',unit_id='recovered-source',kind='source',markdown='旧兜底原件块',
        obligation_ids=[],object_ids=['s1'],evidence=[],embedded_object_ids=['s1'])
    old.update(status='ready_for_review',source=classified,stage='active_write',
        active_groups=[['s1']],active_plans=[{'contract':{'purpose':'kept'}}],
        active_partition_index=1,writing_batches=[{'id':'write-1'},{'id':'write-2'}],
        unit_index=1,active_checkpoints=[checkpoint],draft={'blocks':[block,recovered]},
        inventory=copy.deepcopy(classified),plan={'title':'validated'},
        knowledge_memory=[{'node_id':'write-1'}],generated_resources={'written-write-1':{'id':'saved'}},
        visual_cards=[dict(source_id='content-image',visible_content='Chart',source_text='',
            role='diagram',relationships=[],uncertainty=[],limitations=[],blocking_uncertainty=[])])
    store.put_job(old)
    published=copy.deepcopy(classified);published.update(digest='derived',frozen=True)
    store.change(project['id'],lambda p:p.update(active_job=None,inventory=published))
    rewritten=queue.rewrite_active(project['id'],bundle)
    assert rewritten['stage']=='active_write' and rewritten['unit_index']==1
    assert rewritten['reused_writing_checkpoint_job']==old['id']
    assert rewritten['draft']['blocks']==[block]
    assert rewritten['active_checkpoints']==[checkpoint]


def test_active_rewrite_reuses_all_clean_batches_without_recovering_fallback_blocks(tmp_path,skill):
    from sourceloom.writing import canonical
    store,_,project,bundle=prepared(tmp_path,skill)
    store.change(project['id'],lambda p:p['inventory']['objects'].append(dict(
        id='content-image',kind='image',locator='source/figure[1]',text='',
        resource_id='a'*64,source_scope='article_media')))
    queue=Queue(store,pipeline='active_composition_v2')
    old=queue.enqueue(project['id'],bundle)
    classified=copy.deepcopy(store.get(project['id'])['inventory'])
    block=dict(id='done-b1',unit_id='write-1',kind='explanation',markdown='已完成正文',
        obligation_ids=[],object_ids=['s1'],evidence=[],embedded_object_ids=[])
    recovered=dict(id='recovered-s1',unit_id='recovered-source',kind='source',markdown='旧兜底原件块',
        obligation_ids=[],object_ids=['s1'],evidence=[],embedded_object_ids=['s1'])
    checkpoint=dict(node_id='write-1',
        draft_digest=digest(canonical({'blocks':[block]}).encode()),
        unresolved_content_findings=[],unresolved_revision={},unresolved_format={})
    old.update(status='ready_for_review',source=classified,stage='active_deliver',
        active_groups=[['s1']],active_plans=[{'contract':{'purpose':'kept'}}],
        active_partition_index=1,writing_batches=[{'id':'write-1'}],unit_index=1,
        active_checkpoints=[checkpoint],draft={'blocks':[block,recovered]},
        inventory=copy.deepcopy(classified),plan={'title':'validated'},
        knowledge_memory=[{'node_id':'write-1'}],generated_resources={'written-write-1':{'id':'saved'}},
        visual_cards=[dict(source_id='content-image',visible_content='Chart',source_text='',
            role='diagram',relationships=[],uncertainty=[],limitations=[],blocking_uncertainty=[])])
    store.put_job(old)
    published=copy.deepcopy(classified);published.update(digest='derived',frozen=True)
    store.change(project['id'],lambda p:p.update(active_job=None,inventory=published))
    rewritten=queue.rewrite_active(project['id'],bundle)
    assert rewritten['stage']=='active_deliver' and rewritten['unit_index']==1
    assert rewritten['draft']['blocks']==[block]
    assert rewritten['reused_writing_checkpoint_job']==old['id']


def test_active_call_honors_the_whole_job_deadline_before_dispatch(tmp_path,skill,monkeypatch):
    import time
    from sourceloom import active_contracts as A
    from sourceloom.store import Conflict
    store,_,project,bundle=prepared(tmp_path,skill)
    Queue(store,pipeline='active_composition_v2').enqueue(project['id'],bundle)
    engine=Production(store,{'generation_pipeline':'active_composition_v2','job_timeout':300})
    job=engine.queue.claim(engine.owner,project=project['id'])
    job['started']=time.time()-301
    monkeypatch.setattr('sourceloom.active_composition.Provider.call',
        lambda *args,**kwargs:pytest.fail('deadline must stop dispatch'))
    with pytest.raises(Conflict,match='处理时间上限'):
        ActiveComposition(engine).call(job,'deadline-test','active_plan',{},A.VisualCards)


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


def test_local_repair_keeps_valid_edits_and_filters_inline_code_deletion():
    from sourceloom.active_composition import normalize_local_proposal
    draft={'blocks':[dict(id='b1',markdown='Use `<title>` for the name。\n\nOther sentence。')]}
    proposal=dict(edits=[dict(block_id='b1',old_text='Use `<title>` for the name。',
                              new_text='Use a title for the name',reason='rewrite'),
                         dict(block_id='b1',old_text='Other sentence',
                              new_text='Other sentence',reason='no-op'),
                         dict(block_id='b1',old_text='Other sentence。',
                              new_text='Other sentence',reason='punctuation')])
    result,rejected=normalize_local_proposal(proposal,draft,[])
    assert len(result['edits'])==1 and result['edits'][0]['new_text']=='Other sentence'
    assert rejected==[dict(block_id='b1',reason='authored_inline_code_removed'),
                      dict(block_id='b1',reason='no_change')]


def test_confirmed_format_finding_does_not_need_a_second_candidate_decision():
    from sourceloom.active_composition import confirmed_format_issues
    issues=dict(findings=[dict(id='findings-1',old_text='x')],
                candidates=[dict(id='candidates-1',old_text='y')])
    review=dict(format_decisions=[dict(candidate_id='findings-1',decision='fix',reason='Definite'),
                                  dict(candidate_id='candidates-1',decision='dismiss',reason='Context')])
    assert confirmed_format_issues(issues,review,required=True)==issues['findings']
    assert [d['candidate_id'] for d in review['format_decisions']]==['candidates-1']


def test_heading_jump_guard_ignores_code_but_catches_new_skip():
    from sourceloom.active_composition import heading_level_skips
    normal={'blocks':[dict(markdown='# Article\n\n## Section\n\n```python\n# code\n```')]}
    broken={'blocks':[dict(markdown='# Article\n\n#### Section\n\n```python\n# code\n```')]}
    assert heading_level_skips(normal)==0
    assert heading_level_skips(broken)==1


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


def test_patch_and_delivery_reject_empty_block_that_still_claims_source_coverage():
    from sourceloom.active_composition import reject_empty_claimed_blocks
    with pytest.raises(ValueError,match='段落不能为空'):
        reject_empty_claimed_blocks({'blocks':[dict(id='n1-empty',markdown='',
            obligation_ids=['f1','f2'])]})
    reject_empty_claimed_blocks({'blocks':[dict(id='n1-spacer',markdown='',
        obligation_ids=[])]})


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


def test_document_information_discards_plain_text_marker_and_keeps_explanation():
    from sourceloom.active_composition import validate_written
    src=source();src['objects']=src['objects'][:1]
    src['objects'][0]['text']='Created Date: 2002-04-18'
    p=plan();p['obligations']=p['obligations'][:1]
    p['obligations'][0].update(quote=src['objects'][0]['text'],meaning='Date created')
    p['nodes'][0].update(source_ids=['s1'],obligation_ids=['f1'])
    inv,_=legacy_artifacts([p],src)
    value=dict(blocks=[dict(id='n1-info',kind='document_info',
        markdown='## 页面信息\n\n{{source:s1}}\n\n创建日期为 2002 年 4 月 18 日',
        obligation_ids=['f1'],source_ids=['s1'])],
        coverage=[dict(obligation_id='f1',block_id='n1-info',output_quote='创建日期为 2002 年 4 月 18 日')],
        knowledge_delta=dict(established_concepts=[],explained_obligations=['f1'],
            unresolved_prerequisites=[],next_bridge=''))
    draft,_,_=validate_written(value,p['nodes'][0],inv,None,{'blocks':[]})
    assert '{{source:' not in draft['blocks'][0]['markdown']
    assert '创建日期为 2002 年 4 月 18 日' in draft['blocks'][0]['markdown']


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


def test_plan_expands_a_contiguous_model_composed_span_range():
    from sourceloom.active_composition import source_spans
    src=source();src['objects'][0]['text']='A long sentence. '*100+'Final exception is mandatory.'
    spans=source_spans(src,['s1'])
    assert len(spans)>1
    p=plan();p['nodes'][0]['obligation_ids']=['f1'];p['nodes'][0]['source_ids']=['s1']
    p['obligations']=p['obligations'][:1]
    composed=f"s1:{spans[0]['start']}:{spans[-1]['end']}"
    p['obligations'][0].update(quote='',source_span_ids=[composed])
    validated=validate_plan(p,src,['s1'],require_spans=True)
    assert validated['obligations'][0]['source_span_ids']==[s['id'] for s in spans]
    assert validated['obligations'][0]['quote']==src['objects'][0]['text']


def test_resource_search_is_discovery_not_verified_source(tmp_path,monkeypatch):
    import sourceloom.network
    xml=b'<rss><channel><item><title>Source</title><link>https://example.org/reference</link><description>Untrusted summary</description></item></channel></rss>'
    monkeypatch.setattr(sourceloom.network,'fetch',lambda *args,**kwargs:(xml,'text/xml',args[0]))
    r=Resources(Store(tmp_path),source());result=r.execute(dict(kind='search',query='term'))
    assert result['evidence_status'].startswith('discovery_only')
    assert not r.context() and 'https://example.org/reference' not in r.state['entries']
    assert r.store.read_blob(result['snapshot_blob'])==xml


def test_content_link_plan_requires_direct_destination_evidence(tmp_path):
    src=source();src['objects']=[dict(id='link',kind='link',text='Read more',
        locator='input/a',target='https://example.org/topic')]
    p=plan();p['obligations']=p['obligations'][:1]
    p['obligations'][0].update(source_id='link',quote='Read more')
    p['nodes'][0].update(source_ids=['link'],obligation_ids=['f1'])
    resources=Resources(Store(tmp_path),src)
    resources.add('external-page','This example distinguishes counting from proving difficulty.',
        kind='external',locator='https://example.org/topic',original_url='https://example.org/topic')
    p['link_briefs']=[dict(source_id='link',role='content',topic='Route counting',
        connection='Explains why checking every route is expensive',
        destination='Counts candidate routes but does not prove hardness',
        limitation='Counting alone is not a hardness proof',
        evidence=[dict(resource_id='external-page',quote='distinguishes counting from proving difficulty')])]
    assert validate_plan(p,src,['link'],resources=resources,require_link_briefs=True)['link_briefs'][0]['role']=='content'
    p['link_briefs'][0]['evidence'][0]['quote']='Invented destination sentence'
    with pytest.raises(ValueError,match='目标证据'):
        validate_plan(p,src,['link'],resources=resources,require_link_briefs=True)
    p['link_briefs']=[]
    with pytest.raises(ValueError,match='每个原文链接'):
        validate_plan(p,src,['link'],resources=resources,require_link_briefs=True)


def test_site_chrome_and_author_links_get_structural_roles_without_model_research():
    import copy
    src=source();src['objects']=[
        dict(id='nav',kind='link',text='Home',locator='input/nav/a',
             target='https://example.org/',source_scope='site_chrome'),
        dict(id='author',kind='link',text='Contributed by A',locator='input/address/a',
             target='https://example.org/author',source_scope='source_metadata'),
    ]
    p=plan();first=p['obligations'][0];first.update(source_id='nav',quote='Home')
    second=copy.deepcopy(first);second.update(id='f2',source_id='author',quote='Contributed by A')
    p['obligations']=[first,second]
    p['nodes'][0].update(source_ids=['nav','author'],obligation_ids=['f1','f2'])
    p['link_briefs']=[]
    result=validate_plan(p,src,['nav','author'],require_link_briefs=True)
    assert {(b['source_id'],b['role']) for b in result['link_briefs']}=={
        ('nav','navigation'),('author','administrative')}


def test_duplicate_avatar_and_author_profile_links_are_administrative():
    import copy
    src=source();src['source_url']='https://example.org/article'
    src['objects']=[
        dict(id='avatar',kind='link',text='',locator='input/article/header/a[1]',
             target='https://profiles.example/@writer'),
        dict(id='author',kind='link',text='Writer Name',locator='input/article/header/a[2]',
             target='https://profiles.example/@writer'),
    ]
    p=plan();first=p['obligations'][0];first.update(source_id='avatar',quote='')
    second=copy.deepcopy(first);second.update(id='f2',source_id='author',quote='Writer Name')
    p['obligations']=[first,second]
    p['nodes'][0].update(source_ids=['avatar','author'],obligation_ids=['f1','f2'])
    p['link_briefs']=[dict(source_id=sid,role='navigation') for sid in ('avatar','author')]
    result=validate_plan(p,src,['avatar','author'],require_link_briefs=True)
    assert {(b['source_id'],b['role']) for b in result['link_briefs']}=={
        ('avatar','administrative'),('author','administrative')}


def test_empty_figure_link_to_full_size_image_is_administrative():
    src=source();src['source_url']='https://example.org/article'
    src['objects']=[dict(id='figure-link',kind='link',text='',
        locator='input/article/div[3]/figure[1]/a[1]',
        target='https://cdn.example/image/fetch/https%3A%2F%2Forigin.example%2Fchart.png')]
    p=plan();p['obligations']=p['obligations'][:1]
    p['obligations'][0].update(source_id='figure-link',quote='')
    p['nodes'][0].update(source_ids=['figure-link'],obligation_ids=['f1'])
    p['link_briefs']=[dict(source_id='figure-link',role='content',topic='Image',
        connection='Figure',destination='',limitation='',evidence=[],
        unavailable_reason='网页类型不在当前接入范围')]
    result=validate_plan(p,src,['figure-link'],require_link_briefs=True)
    assert result['link_briefs'][0]['role']=='administrative'


def test_omitted_empty_image_wrapper_gets_a_deterministic_noncontent_role():
    src=source();src['source_url']='https://example.org/article'
    src['objects']=[dict(id='figure-link',kind='link',text='',locator='input/article/a',
        target='https://substackcdn.com/image/fetch/example.png')]
    p=plan();p['obligations']=p['obligations'][:1]
    p['obligations'][0].update(source_id='figure-link',quote='')
    p['nodes'][0].update(source_ids=['figure-link'],obligation_ids=['f1'])
    p['link_briefs']=[]
    result=validate_plan(p,src,['figure-link'],require_link_briefs=True)
    assert result['link_briefs']==[dict(source_id='figure-link',role='administrative',
        topic='',connection='',destination='',limitation='',evidence=[],unavailable_reason='')]


def test_article_reference_cannot_be_declared_navigation_to_skip_research():
    src=source();src['source_url']='https://example.org/article'
    src['objects']=[dict(id='link',kind='link',text='Research background',
        locator='input/p/a',target='https://example.org/background')]
    p=plan();p['obligations']=p['obligations'][:1]
    p['obligations'][0].update(source_id='link',quote='Research background')
    p['nodes'][0].update(source_ids=['link'],obligation_ids=['f1'])
    p['link_briefs']=[dict(source_id='link',role='navigation')]
    with pytest.raises(ValueError,match='正文知识链接不能仅按导航'):
        validate_plan(p,src,['link'],require_link_briefs=True)


def test_same_page_back_to_top_is_navigation_even_inside_article():
    src=source();src['source_url']='https://example.org/article/'
    src['objects']=[dict(id='link',kind='link',text=' Back to Top',
        locator='input/p/a',target='https://example.org/article/#top')]
    p=plan();p['obligations']=p['obligations'][:1]
    p['obligations'][0].update(source_id='link',quote=' Back to Top')
    p['nodes'][0].update(source_ids=['link'],obligation_ids=['f1'])
    p['link_briefs']=[dict(source_id='link',role='navigation')]
    assert validate_plan(p,src,['link'],require_link_briefs=True)['link_briefs'][0]['role']=='navigation'


def test_same_page_topic_anchor_is_navigation_even_inside_article():
    src=source();src['source_url']='https://example.org/article/'
    src['objects']=[dict(id='link',kind='link',text='Languages',
        locator='input/p/a',target='https://example.org/article/#translations')]
    p=plan();p['obligations']=p['obligations'][:1]
    p['obligations'][0].update(source_id='link',quote='Languages')
    p['nodes'][0].update(source_ids=['link'],obligation_ids=['f1'])
    p['link_briefs']=[dict(source_id='link',role='navigation')]
    assert validate_plan(p,src,['link'],require_link_briefs=True)['link_briefs'][0]['role']=='navigation'


def test_first_use_moves_a_planned_concept_before_its_later_introduction():
    from sourceloom.active_composition import align_unplaced_concepts
    p={'concepts':[dict(id='base',requires=[]),dict(id='term',requires=['base'])],
       'nodes':[dict(id='n1',source_ids=['s1'],requires_concepts=[],
                     establishes_concepts=['base']),
                dict(id='n2',source_ids=['s2'],requires_concepts=['term'],
                     establishes_concepts=[]),
                dict(id='n3',source_ids=['s3'],requires_concepts=[],
                     establishes_concepts=['term'])]}
    repairs=align_unplaced_concepts(p)
    assert p['nodes'][1]['establishes_concepts']==['term']
    assert p['nodes'][1]['requires_concepts']==[]
    assert p['nodes'][2]['establishes_concepts']==[]
    assert p['nodes'][2]['requires_concepts']==['term']
    assert repairs[0]['reason']=='first_use_precedes_planned_introduction'


def test_prerequisite_is_hoisted_before_first_dependent_explanation():
    from sourceloom.active_composition import align_unplaced_concepts
    p={'concepts':[dict(id='css',source_ids=['s2'],requires=[]),
                   dict(id='semantics',source_ids=['s1'],requires=['css'])],
       'nodes':[dict(id='n1',source_ids=['s1'],requires_concepts=[],
                     establishes_concepts=['semantics']),
                dict(id='n2',source_ids=['s2'],requires_concepts=['css'],
                     establishes_concepts=['css'])]}
    repairs=align_unplaced_concepts(p)
    assert p['nodes'][0]['establishes_concepts']==['css','semantics']
    assert p['nodes'][1]['establishes_concepts']==[]
    assert repairs==[dict(concept_id='css',node_id='n1',previous_node_id='n2',
                          reason='prerequisite_before_dependent_concept')]


def test_json_syntax_repair_inserts_only_missing_node_closer():
    import json
    from sourceloom.active_composition import repair_one_missing_json_object_closer
    broken='{"nodes":[{"explanation":{"boundary":"原文有 ] 和 } 字符"}],"links":[]}'
    repaired=repair_one_missing_json_object_closer(broken)
    assert json.loads(repaired)=={'nodes':[{'explanation':{'boundary':'原文有 ] 和 } 字符'}}],
                                  'links':[]}
    assert repair_one_missing_json_object_closer('{"nodes":[{"value":1}]') is None


def test_formal_acronym_requires_a_cited_original_mention():
    from sourceloom.active_composition import retire_unanchored_abbreviations
    source={'objects':[dict(id='s1',text='A document type declaration starts the page.'),
                       dict(id='s2',text='The W3C reference is provided later.') ]}
    parts=[dict(concepts=[dict(id='w3c',source_ids=['s1'],abbreviations=[dict(short='W3C')]),
                          dict(id='html',source_ids=['s2'],abbreviations=[dict(short='W3C')])],
                nodes=[dict(establishes_concepts=['w3c','html'],requires_concepts=['w3c'])])]
    assert retire_unanchored_abbreviations(parts,source)==['w3c']
    assert [c['id'] for c in parts[0]['concepts']]==['html']
    assert parts[0]['nodes'][0]['establishes_concepts']==['html']


def test_lowercase_import_path_does_not_certify_an_added_formal_acronym():
    from sourceloom.active_composition import retire_unanchored_abbreviations
    source={'objects':[dict(id='readme',text='from sqlalchemy.orm import DeclarativeBase')]}
    parts=[dict(concepts=[dict(id='orm',source_ids=['readme'],
                               abbreviations=[dict(short='ORM')])],
                nodes=[dict(establishes_concepts=['orm'],requires_concepts=['orm'])])]
    assert retire_unanchored_abbreviations(parts,source)==['orm']
    assert parts[0]['concepts']==[]
    assert parts[0]['nodes']==[dict(establishes_concepts=[],requires_concepts=[])]


def test_whitespace_only_source_text_is_preserved_without_an_explanation_obligation():
    from sourceloom.visual_sources import decorative_resource
    assert decorative_resource(dict(kind='text',text=' \n\t'))
    assert not decorative_resource(dict(kind='text',text='A real sentence'))


def test_empty_figure_image_link_is_archived_without_hiding_the_image():
    from sourceloom.visual_sources import decorative_resource
    wrapper=dict(kind='link',text='',locator='snapshot.html/article[1]/figure[2]/a[1]',
        target='https://substackcdn.com/image/fetch/example/photo.png')
    image=dict(kind='image',text='Cache bandwidth chart',
        locator='snapshot.html/article[1]/figure[2]/a[1]/img[1]',target='photo.png')
    assert decorative_resource(wrapper)
    assert not decorative_resource(image)


def test_prefetched_body_link_cannot_be_dropped_as_navigation(tmp_path):
    from sourceloom.active_composition import validate_plan
    from sourceloom.active_resources import Resources
    src=source();src['source_url']='https://example.org/article'
    src['objects']=[dict(id='link',kind='link',text='defined in advance',
                         locator='snapshot.html/article/p/a',target='https://docs.example.org/')]
    p=plan();p['obligations']=p['obligations'][:1]
    p['obligations'][0].update(source_id='link',quote='defined in advance')
    p['nodes'][0].update(source_ids=['link'],obligation_ids=['f1'])
    p['link_briefs']=[dict(source_id='link',role='navigation',topic='',connection='',destination='',
                           limitation='',evidence=[],unavailable_reason='')]
    resources=Resources(Store(tmp_path),src)
    resources.add('external-doc','Introduction\nThe option is fixed before execution',kind='external',
                  locator='https://docs.example.org/',original_url='https://docs.example.org/')
    resources.read('external-doc')
    result=validate_plan(p,src,['link'],resources=resources,require_link_briefs=True)
    brief=result['link_briefs'][0]
    assert brief['role']=='content' and brief['evidence'][0]['resource_id']=='external-doc'


def test_visual_card_archives_a_confirmed_textless_decorative_icon():
    from sourceloom.visual_sources import decorative_resource
    icon=dict(kind='image',text='A chain icon',visual_card=dict(
        role='decorative',source_text='',visible_content='A chain icon'))
    chart=dict(kind='image',text='Cache bandwidth chart',visual_card=dict(
        role='diagram',source_text='',visible_content='Cache bandwidth chart'))
    labelled=dict(kind='image',text='OPEN',visual_card=dict(
        role='decorative',source_text='OPEN',visible_content='OPEN'))
    assert decorative_resource(icon)
    assert not decorative_resource(chart)
    assert not decorative_resource(labelled)


def test_writer_concept_labels_can_be_aligned_only_from_exact_cited_ids():
    from sourceloom.active_composition import align_reported_concept_ids
    delta=dict(established_concepts=['the mission and its launch date'],
               concept_evidence=[dict(concept_id='p1-con-1')])
    assert align_reported_concept_ids(delta,['p1-con-1'])
    assert delta['established_concepts']==['p1-con-1']
    assert delta['reported_concept_labels']==['the mission and its launch date']
    wrong=dict(established_concepts=['the mission'],concept_evidence=[dict(concept_id='p1-con-2')])
    assert not align_reported_concept_ids(wrong,['p1-con-1'])
    assert wrong['established_concepts']==['the mission']


def test_client_challenge_is_recorded_as_unavailable_page_not_project_evidence(tmp_path,monkeypatch):
    from sourceloom.active_resources import Resources
    from sourceloom.store import Store
    from sourceloom import network
    raw=(b'<html><title>Client Challenge</title><body>'
         b'A required part of this site could not load. Please enable JavaScript.'
         b'</body></html>')
    monkeypatch.setattr(network,'fetch',lambda *args,**kwargs:(raw,'text/html',args[0]))
    resource=Resources(Store(tmp_path),{'objects':[]})
    result=resource.execute(dict(kind='page',url='https://example.invalid/project',resource_id=''))
    assert result['status']=='unavailable'
    assert result['snapshot_blob']
    assert not any(entry.get('kind')=='external' for entry in resource.state['entries'].values())


def test_redirected_login_page_is_not_mistaken_for_image_led_link_content(tmp_path,monkeypatch):
    from sourceloom.active_resources import Resources
    from sourceloom.store import Store
    from sourceloom import network
    raw=(b'<html><body><main><img src="/transparent-square.png">'
         b'<form>Continue with account</form></main></body></html>')
    monkeypatch.setattr(network,'fetch',lambda *args,**kwargs:(
        raw,'text/html','https://example.org/login?returnTo=%2Fshared%2F123'))
    resource=Resources(Store(tmp_path),{'objects':[]})
    result=resource.execute(dict(kind='page',url='https://example.org/shared/123',resource_id=''))
    assert result['status']=='unavailable'
    assert result['resolved_url'].startswith('https://example.org/login')
    assert result['snapshot_blob']
    assert not any(entry.get('kind')=='external' for entry in resource.state['entries'].values())


def test_empty_heading_is_archived_as_layout_without_discarding_source_markup():
    from sourceloom.source_context import classify_inert_markup
    from sourceloom.visual_sources import decorative_resource
    src={'objects':[dict(id='empty-heading',kind='heading',text='',
        raw='<h2 class="section-spacer"><!-- placeholder --></h2>',locator='page/h2[3]')]}
    classified=classify_inert_markup(src)
    heading=classified['objects'][0]
    assert decorative_resource(heading)
    assert heading['raw']==src['objects'][0]['raw']
    assert heading['visual_classification']['method']=='source_dom_empty_heading'


def test_standalone_heading_merges_into_its_following_code_unit():
    from sourceloom.active_composition import merge_adjacent_heading_only_nodes
    heading=dict(id='h',title='简单示例',source_ids=['s1'],obligation_ids=['f1'],
                 requires_concepts=[],establishes_concepts=[],depends_on=['before'],
                 transition_from='承接前文')
    code=dict(id='code',title='示例代码',source_ids=['s2'],obligation_ids=['f2'],
              requires_concepts=[],establishes_concepts=['c1'],depends_on=['h'],
              transition_from='解释示例')
    later=dict(id='later',depends_on=['h','code'])
    plan={'nodes':[heading,code,later]}
    repairs=merge_adjacent_heading_only_nodes(plan,{'s1':{'kind':'heading'},'s2':{'kind':'code'}})
    assert repairs==[dict(heading_id='h',content_id='code')]
    assert plan['nodes'][0]['source_ids']==['s1','s2']
    assert plan['nodes'][0]['depends_on']==['before']
    assert plan['nodes'][0]['title']=='简单示例'
    assert later['depends_on']==['code']


def test_verified_name_can_be_inserted_at_unique_authored_first_use():
    from sourceloom.active_composition import insert_verified_name_at_unique_first_use
    draft={'blocks':[dict(id='b1',kind='prose',markdown='Pallets 社区生态包含相关项目',
                           embedded_object_ids=[])]}
    delta={'concept_evidence':[dict(block_id='b1',concept_id='c1',
                                    output_quote='Pallets 社区生态包含相关项目')]}
    concepts=[dict(id='c1',chinese_name='Pallets 社区生态',
                   english_name='Pallets Community Ecosystem',naming_status='verified')]
    assert insert_verified_name_at_unique_first_use(draft,delta,concepts)
    assert 'Pallets 社区生态（Pallets Community Ecosystem）' in draft['blocks'][0]['markdown']
    assert delta['concept_evidence'][0]['output_quote'] in draft['blocks'][0]['markdown']


def test_short_plain_rewrite_rejects_glossary_detour_without_counting_source_code():
    from sourceloom.active_composition import overgrown_short_rewrite_glossary
    objects=[dict(kind='heading',text='Care With Font Size'),
             dict(kind='text',text='Small type may look sleek, but it can impair reading.'),
             dict(kind='text',text='People use screens of different sizes.')]
    definitions='\n'.join('- '+name+'（English Name）：'+'说明'*80
                          for name in ['网页','平台','字号'])
    draft={'blocks':[dict(markdown=definitions)]}
    assert overgrown_short_rewrite_glossary(draft,objects)
    assert overgrown_short_rewrite_glossary(draft,objects+[dict(kind='code',text='x=1')])
    assert not overgrown_short_rewrite_glossary(draft,objects+[dict(kind='code',text='x=1\n'*100)])


def test_screened_patch_cannot_clear_an_unchanged_source_fidelity_finding():
    from sourceloom.active_composition import carry_unchanged_findings
    previous=[dict(block_id='note',output_quote='原来的提示仍在',problem='原文事实丢失')]
    draft={'blocks':[dict(id='intro',markdown='已修订的开头'),
                     dict(id='note',markdown='原来的提示仍在')]}
    patch={'edits':[dict(block_id='intro',old_text='旧开头',new_text='已修订的开头')]}
    assert carry_unchanged_findings([],previous,patch,draft)==previous
    assert carry_unchanged_findings([],previous,
        {'edits':[dict(block_id='note',old_text='旧提示',new_text='新提示')]},draft)==[]


def test_passed_format_replay_retires_only_the_previous_same_unit_failure():
    from sourceloom.active_composition import clear_resolved_format_issue
    candidate={'unresolved_format':{'findings':[{'rule_id':'FORMAT_EXCESSIVE_BLANK_LINES'}]}}
    job={'quality_issues':['unit-1：两轮局部修补后仍有 1 项确定格式问题，正文与检查记录均已保留',
                           'unit-2：仍有来源事实问题']}
    clear_resolved_format_issue(job,candidate,'unit-1')
    assert 'unresolved_format' not in candidate
    assert job['quality_issues']==['unit-2：仍有来源事实问题']
    assert len(job['resolved_quality_issues'])==1


def test_checkpoint_format_replay_needs_same_bytes_and_all_candidates_adjudicated():
    from sourceloom.active_composition import checkpoint_format_clearable
    from sourceloom.writing import canonical,digest
    unit={'blocks':[dict(id='b1',kind='explanation',markdown='正文')]}
    identity=digest(canonical(unit).encode())
    point=dict(draft_digest=identity,format_digest=identity,
               unresolved_format={'findings':[{'rule_id':'FORMAT_EXCESSIVE_BLANK_LINES'}]},
               format_records=[dict(digest=identity,dismissed=['c1'])])
    report={'canonical_digest':identity,'format':{'findings':[],'candidates':[{'id':'c1'}]}}
    assert checkpoint_format_clearable(point,unit,report)
    assert not checkpoint_format_clearable(point,unit,report|{'format':{'findings':[],'candidates':[{'id':'c2'}]}})
    assert not checkpoint_format_clearable(point,unit,report|{'canonical_digest':'old'})


def test_plan_preview_shows_future_heading_without_its_source_id():
    from sourceloom.active_composition import plan_document_preview
    import json
    src={'objects':[dict(id='first-id',kind='text',text='Opening'),
                    dict(id='future-secret-id',kind='heading',text='Later section')],
         'originals':[dict(name='snapshot.html'),dict(name='web-assets/image.png')]}
    preview=plan_document_preview(src,['first-id'],[])
    assert preview['future_headings']==['Later section']
    assert preview['whole_document_files']==['snapshot.html']
    assert 'future-secret-id' not in json.dumps(preview)


def test_html_inline_wrap_quote_rebinds_exact_saved_punctuation():
    from sourceloom.active_composition import exact_source_quote
    source_text='Cascading Style Sheets is used on the open web platform\n, and adds style.'
    quote='Cascading Style Sheets is used on the open web platform, and adds style.'
    assert exact_source_quote(quote,source_text)==source_text
    assert exact_source_quote('CSS is used on the open web platform, and adds style.',source_text) is None


def test_unique_ellipsis_source_quote_expands_to_actual_contiguous_bytes():
    from sourceloom.active_composition import exact_source_quote
    source=('Human activities, primarily from burning fossil fuels that release carbon dioxide, '
            'have disrupted Earth\'s energy balance.')
    quote="Human activities, primarily from burning fossil fuels…have disrupted Earth's energy balance."
    assert exact_source_quote(quote,source)==source
    assert exact_source_quote('Human activities…balance.',source) is None


def test_historical_http_link_is_fetched_at_same_https_address(tmp_path,monkeypatch):
    import sourceloom.network
    seen=[]
    def fetch(url,*args,**kwargs):
        seen.append(url)
        return b'<html><body>Archived technical discussion</body></html>','text/html',url
    monkeypatch.setattr(sourceloom.network,'fetch',fetch)
    r=Resources(Store(tmp_path),source())
    item=r.execute(dict(kind='page',url='http://example.org/archive#thread'))
    assert seen==['https://example.org/archive#thread']
    assert item['kind']=='read' and item['complete']
    assert r.state['entries']['external-'+digest('http://example.org/archive#thread'.encode())[:20]]['original_url']=='http://example.org/archive#thread'


def test_direct_link_failure_is_recorded_and_cannot_be_invented(tmp_path,monkeypatch):
    src=source();src['objects']=[dict(id='link',kind='link',text='Related research',
        locator='input/a',target='https://example.org/research')]
    p=plan();p['obligations']=p['obligations'][:1]
    p['obligations'][0].update(source_id='link',quote='Related research')
    p['nodes'][0].update(source_ids=['link'],obligation_ids=['f1'])
    resources=Resources(Store(tmp_path),src)
    def rejected(*args,**kwargs):raise ValueError('网页返回 429，未取得完整材料')
    monkeypatch.setattr('sourceloom.network.fetch',rejected)
    receipt=resources.execute(dict(kind='page',url='https://example.org/research'))
    assert receipt['status']=='unavailable'
    p['link_briefs']=[dict(source_id='link',role='content',topic='Related research',
        connection='Background for this paragraph',destination='',evidence=[],
        unavailable_reason=receipt['reason'])]
    assert validate_plan(p,src,['link'],resources=resources,require_link_briefs=True)['link_briefs'][0]['unavailable_reason']==receipt['reason']
    p['link_briefs'][0]['unavailable_reason']='The source says something else'
    with pytest.raises(ValueError,match='访问失败记录'):
        validate_plan(p,src,['link'],resources=resources,require_link_briefs=True)


def test_image_led_link_requires_actual_visual_evidence(tmp_path):
    src=source();src['objects']=[dict(id='link',kind='link',text='Illustrated article',
        locator='input/a',target='https://example.org/article')]
    p=plan();p['obligations']=p['obligations'][:1]
    p['obligations'][0].update(source_id='link',quote='Illustrated article')
    p['nodes'][0].update(source_ids=['link'],obligation_ids=['f1'])
    resources=Resources(Store(tmp_path),src)
    resources.add('page','Title and publication date',kind='external',
        locator='https://example.org/article',original_url='https://example.org/article',
        image_refs=[dict(url='https://example.org/article.png',alt='Scanned article')])
    p['link_briefs']=[dict(source_id='link',role='content',topic='Illustrated account',
        connection='Explains the article mentioned here',destination='An illustrated account',
        evidence=[dict(resource_id='page',quote='Title and publication date')])]
    with pytest.raises(ValueError,match='须读取图片'):
        validate_plan(p,src,['link'],resources=resources,require_link_briefs=True)
    resources.add('image','The illustration shows two labeled routes',kind='external',
        scope='linked_image',locator='https://example.org/article.png',
        original_url='https://example.org/article.png',parent_page_url='https://example.org/article')
    p['link_briefs'][0]['evidence'].append(dict(resource_id='image',quote='two labeled routes'))
    assert validate_plan(p,src,['link'],resources=resources,require_link_briefs=True)['link_briefs'][0]['evidence'][1]['resource_id']=='image'


def test_administrative_donation_link_does_not_trigger_article_research(tmp_path):
    src=source();src['objects']=[dict(id='link',kind='link',text='Please donate today',
        locator='input/a',target='https://example.org/donate')]
    p=plan();p['obligations']=p['obligations'][:1]
    p['obligations'][0].update(source_id='link',quote='Please donate today')
    p['nodes'][0].update(source_ids=['link'],obligation_ids=['f1'])
    p['link_briefs']=[dict(source_id='link',role='content',topic='Support',
        connection='Donation',destination='Donation form',evidence=[])]
    validated=validate_plan(p,src,['link'],resources=Resources(Store(tmp_path),src),require_link_briefs=True)
    assert validated['link_briefs'][0]['role']=='administrative'


def test_linked_page_image_is_read_once_and_reused_in_planning(tmp_path,monkeypatch):
    from pydantic import BaseModel
    from sourceloom.store import digest
    import sourceloom.network
    from io import BytesIO
    from PIL import Image
    class Answer(BaseModel):
        value:int
    picture=BytesIO();Image.new('RGB',(20,20),'white').save(picture,'PNG')
    fetched=[]
    def fetch(url,*args,**kwargs):
        fetched.append(url)
        if url.endswith('topic'):
            return b'<main><h1>Topic</h1><img src="/article.png" alt="Scanned article"></main>','text/html',url
        return picture.getvalue(),'image/png',url
    monkeypatch.setattr(sourceloom.network,'fetch',fetch)
    store=Store(tmp_path);engine=ActiveComposition(Production(store,{}))
    src=source();src['objects']=[dict(id='link',kind='link',text='Topic',locator='input/a',
        target='https://example.org/topic')]
    job=dict(id='linked-image-test',project='project',role='active_plan',status='running',created=1,
             results={},calls=[],active_sessions={},external_resources={},
             generated_resources={},verified_terminology=[])
    requests=[]
    page_id='external-'+digest('https://example.org/topic'.encode())[:20]
    def fake_call(job,key,role,payload,schema):
        requests.append((role,payload))
        if role=='active_visual':
            return dict(cards=[dict(source_id=payload['pages'][0]['id'],
                visible_content='Article says the route count is not a proof',
                source_text='Article says the route count is not a proof',role='text',
                relationships=[],uncertainty=[],limitations=[],blocking_uncertainty=[])])
        if len([r for r,_ in requests if r=='active_plan'])==1:
            return dict(gaps=['Need direct page'],ready_reason='',result=None,
                actions=[dict(kind='page',url='https://example.org/topic')])
        if len([r for r,_ in requests if r=='active_plan'])==2:
            assert payload['previous_action_results'][0]['image_refs'][0]['url']=='https://example.org/article.png'
            return dict(gaps=['Need scanned article'],ready_reason='',result=None,
                actions=[dict(kind='image',resource_id=page_id,url='https://example.org/article.png')])
        assert any('route count is not a proof' in entry['text'] for entry in payload['opened_resources'])
        return dict(gaps=[],actions=[],ready_reason='Image read',result={'value':1})
    monkeypatch.setattr(engine,'call',fake_call)
    assert engine.turn(job,'linked','active_plan',Answer,src,['link'],{},lambda value,_:value)=={'value':1}
    assert fetched==['https://example.org/topic','https://example.org/article.png']
    assert len([r for r,_ in requests if r=='active_visual'])==1
    assert len(job['external_resources'])==2


def test_writer_structure_corrections_edit_the_previous_candidate_incrementally(tmp_path,monkeypatch):
    from sourceloom import active_contracts as A
    store=Store(tmp_path);engine=ActiveComposition(Production(store,{}))
    src=source();requests=[]
    job=dict(id='incremental-writer',project='project',role='production',status='running',
        created=1,pipeline='active_composition_v2',results={},calls=[],active_sessions={},
        external_resources={},generated_resources={},verified_terminology=[],
        archived_layout_source_ids=[],active_plans=[])
    node=dict(id='n1',title='English Source Title',section_outline=[])
    def unit(title,body):
        return dict(blocks=[dict(id='n1-b1',kind='explanation',markdown='## '+title+'\n\n'+body,
            obligation_ids=[],source_ids=['s1'])],coverage=[],knowledge_delta=dict(
            established_concepts=[],explained_obligations=[],unresolved_prerequisites=[],
            next_bridge='',concept_evidence=[]))
    answers=[unit('English Source Title','正文'),unit('中文标题','正文'),unit('中文标题','正文\n\n图片说明')]
    def call(job,key,role,payload,schema):
        requests.append(payload)
        if schema is A.HeadingRepairs:
            return {'edits':[{'block_id':'n1-b1','old_heading':'## English Source Title',
                'new_heading':'## 中文标题'}]}
        assert 'previous_invalid_result' in payload or len(requests)==1
        if len(requests)==3:
            assert payload['previous_invalid_result']==answers[1]
            assert 'Preserve the existing Chinese headings' in payload['protocol_correction']['instruction']
        return dict(gaps=[],actions=[],ready_reason='ready',result=answers[len(requests)-1])
    monkeypatch.setattr(engine,'call',call)
    def validate(value,_):
        markdown=value['blocks'][0]['markdown']
        if len(requests)==1:raise ValueError('正文标题照搬了未解释的英文，需要按完整写作技能改写')
        if len(requests)==2:raise ValueError('正文图片缺少与原图绑定的说明：image-1')
        return value
    result=engine.turn(job,'writer','active_write',A.WrittenUnit,src,['s1'],{'node':node},validate)
    assert result['blocks'][0]['markdown'].endswith('图片说明')
    assert len(requests)==3


def test_review_quote_correction_uses_the_previous_review(tmp_path,monkeypatch):
    from pydantic import BaseModel
    class Review(BaseModel):
        quote:str
    store=Store(tmp_path);engine=ActiveComposition(Production(store,{}));requests=[]
    src=source();job=dict(id='review-correction',project='project',role='production',status='running',
        created=1,pipeline='active_composition_v2',results={},calls=[],active_sessions={},
        external_resources={},generated_resources={},verified_terminology=[])
    def call(job,key,role,payload,schema):
        requests.append(payload)
        if len(requests)==2:
            assert payload['previous_invalid_result']=={'quote':'wrong'}
            assert 'exact non-empty substring' in payload['protocol_correction']['instruction']
        return dict(gaps=[],actions=[],ready_reason='ready',result={'quote':'wrong' if len(requests)==1 else 'exact'})
    monkeypatch.setattr(engine,'call',call)
    def validate(value,_):
        if value['quote']!='exact':raise ValueError('核对意见未准确引用实际正文')
        return value
    assert engine.turn(job,'review','active_review',Review,src,['s1'],{},validate)=={'quote':'exact'}


def test_link_target_quote_rebinds_to_saved_destination_evidence(tmp_path):
    from sourceloom.active_composition import rebind_link_finding_evidence
    resources=Resources(Store(tmp_path),source())
    resources.add('external-page','Heading\nExact destination statement',kind='external',
                  locator='https://example.org/topic',original_url='https://example.org/topic')
    finding=dict(source_id='link',source_quote='Exact destination statement')
    guides=[dict(source_id='link',evidence=[dict(
        resource_id='external-page',quote='Exact destination statement')])]
    alignment=rebind_link_finding_evidence(finding,guides,resources)
    assert finding==dict(source_id='external-page',source_quote='Exact destination statement')
    assert alignment['operation']=='content_link_destination_evidence_rebind'


def test_v2_prefetches_bounded_direct_link_before_first_planning_call(tmp_path,monkeypatch):
    from pydantic import BaseModel
    import sourceloom.network
    class Answer(BaseModel):
        value:int
    monkeypatch.setattr(sourceloom.network,'fetch',lambda url,*args,**kwargs:
        (b'<main><h1>Direct topic</h1><p>Verified target detail.</p></main>','text/html',url))
    store=Store(tmp_path)
    engine=ActiveComposition(Production(store,{'evidence_open_limit':1}))
    src=source()|{'source_url':'https://example.org/article'}
    src['objects']=[dict(id='link',kind='link',text='Direct topic',locator='input/a',
        target='https://reference.example/topic')]
    job=dict(id='v2-prefetch-test',project='project',role='active_plan',status='running',created=1,
             pipeline='active_composition_v2',link_contract_version=1,
             results={},calls=[],active_sessions={},external_resources={},
             generated_resources={},verified_terminology=[])
    calls=[]
    def fake_call(job,key,role,payload,schema):
        calls.append(payload)
        assert payload['previous_action_results'][0]['id'].startswith('external-')
        assert any('Verified target detail.' in entry['text'] for entry in payload['opened_resources'])
        return dict(gaps=[],actions=[],ready_reason='Direct target already available',result={'value':1})
    monkeypatch.setattr(engine,'call',fake_call)
    assert engine.turn(job,'prefetched','active_plan',Answer,src,['link'],{},lambda value,_:value)=={'value':1}
    assert len(calls)==1
    history=job['active_sessions']['prefetched']['action_history']
    assert history[0]['operation']=='direct_link_prefetch_before_planning'


def test_missing_header_brand_asset_does_not_block_article_image(tmp_path):
    from sourceloom.source_context import classify_web_chrome
    from sourceloom.visual_sources import decorative_resource
    store=Store(tmp_path)
    html=b'<h1><a href="https://example.org/"><img src="/logo.svg" alt="Site"/></a> Article</h1><nav><a href="/other">Other</a></nav><main><img src="/diagram.png" alt="Diagram"/></main>'
    original=store.blob(html)
    src=dict(source_url='https://example.org/article',
        originals=[dict(name='snapshot.html',sha256=original,size=len(html))],
        objects=[dict(id='logo',kind='image',text='Site',target='https://example.org/logo.svg',
                      locator='snapshot.html/h1/img',raw='<img src="/logo.svg" alt="Site"/>',resource_id=None),
                 dict(id='diagram',kind='image',text='Diagram',target='https://example.org/diagram.png',
                      locator='snapshot.html/main/img',raw='<img src="/diagram.png" alt="Diagram"/>',resource_id=None),
                 dict(id='menu-link',kind='link',text='Other',target='https://example.org/other',
                      locator='snapshot.html/node[2]/a[1]')],
        unknown=[dict(id='gap-1',object_id='logo',reason='unavailable'),
                 dict(id='gap-2',object_id='diagram',reason='unavailable')])
    classified=classify_web_chrome(store,src)
    assert [gap['object_id'] for gap in classified['unknown']]==['diagram']
    assert decorative_resource(classified['objects'][0])
    assert not decorative_resource(classified['objects'][1])
    assert decorative_resource(classified['objects'][2])
    assert classified['objects'][0]['raw']==src['objects'][0]['raw']


def test_html_main_region_excludes_navigation_from_rewrite_but_keeps_original(tmp_path):
    from sourceloom.ingest import intake
    from sourceloom.source_context import classify_web_chrome
    raw=(b'<html><body><div><nav><a href="/all">All pages</a></nav></div>'
         b'<div><main><article><h1>Water</h1><p>Water moves.</p></article></main></div>'
         b'<footer><p>Site footer</p></footer></body></html>')
    store=Store(tmp_path)
    src=intake(store,[('snapshot.html',raw)],source_url='https://example.org/water')
    classified=classify_web_chrome(store,src)
    assert store.read_blob(src['originals'][0]['sha256'])==raw
    assert {o['text'] for o in classified['objects'] if o.get('source_scope')!='site_chrome'}=={
        'Water','Water moves.'}
    assert all(o.get('source_scope')=='site_chrome' for o in classified['objects']
               if o['text'] in {'All pages','Site footer'})


def test_article_header_avatar_is_archived_as_source_metadata(tmp_path):
    from sourceloom.ingest import intake
    from sourceloom.source_context import classify_web_chrome
    raw=(b'<html><body><main><article><header><h1>Processor report</h1>'
         b'<a href="/author"><img src="avatar.png" alt="Ada\'s avatar"></a><p>Ada</p></header>'
         b'<p>Measured result.</p><figure><img src="chart.png" alt="Throughput chart"></figure>'
         b'</article></main></body></html>')
    store=Store(tmp_path)
    src=intake(store,[('snapshot.html',raw)],source_url='https://example.org/report')
    classified=classify_web_chrome(store,src)
    avatar=next(o for o in classified['objects'] if o.get('text')=="Ada's avatar")
    chart=next(o for o in classified['objects'] if o.get('text')=='Throughput chart')
    assert avatar['source_scope']=='source_metadata'
    assert avatar['visual_classification']['method']=='source_dom_profile_avatar'
    assert chart['source_scope']=='article_media'


def test_explicit_site_navigation_banner_excluded_without_main_region(tmp_path):
    from sourceloom.ingest import intake
    from sourceloom.source_context import classify_web_chrome
    raw=(b'<html><body><div><h1>CSS and XSL</h1><p>Article body.</p></div>'
         b'<div id="banner"><h2>Site navigation</h2><form><ul><li>'
         b'<a href="/"></a><a href="/Style/CSS/">CSS home</a></li></ul></form>'
         b'</div></body></html>')
    store=Store(tmp_path)
    src=intake(store,[('snapshot.html',raw)],source_url='https://example.org/article')
    classified=classify_web_chrome(store,src)
    assert {o['text'] for o in classified['objects'] if o.get('source_scope')!='site_chrome'}=={
        'CSS and XSL','Article body.'}
    assert store.read_blob(src['originals'][0]['sha256'])==raw


def test_inline_svg_button_icon_is_chrome_but_article_svg_remains_unresolved(tmp_path):
    from sourceloom.ingest import intake
    from sourceloom.source_context import classify_web_chrome

    raw=(b'<html><body><main><h1>Shapes</h1>'
         b'<button><span>Open menu</span><svg><path d="M0 0"/></svg></button>'
         b'<p>Article diagram follows.</p><svg><circle cx="5" cy="5" r="4"/></svg>'
         b'</main></body></html>')
    store=Store(tmp_path)
    src=intake(store,[('snapshot.html',raw)],source_url='https://example.org/shapes')
    classified=classify_web_chrome(store,src)
    svgs=[o for o in classified['objects'] if '<svg' in o.get('raw','')]
    assert len(svgs)==2
    assert svgs[0]['source_scope']=='site_chrome'
    assert svgs[0]['visual_classification']['method']=='source_dom_interactive_control_icon'
    assert svgs[1]['kind']=='image' and svgs[1]['resource_id']
    assert not svgs[1].get('source_scope')
    assert not classified['unknown']
    assert store.read_blob(src['originals'][0]['sha256'])==raw


def test_svg_use_inherits_only_a_proven_decorative_definition(tmp_path):
    from sourceloom.source_context import classify_web_chrome
    store=Store(tmp_path);raw=b'<html><body><main><article><h1>Report</h1></article></main></body></html>'
    key=store.blob(raw)
    source=dict(source_url='https://example.org/report',
        originals=[dict(name='snapshot.html',sha256=key,size=len(raw))],resources=[],
        objects=[
            dict(id='use',kind='unknown',text='原始 svg 对象',locator='snapshot.html/main/svg[1]',
                 raw='<svg><use href="#chevron"></use></svg>'),
            dict(id='definition',kind='image',text='',locator='snapshot.html/footer/svg[1]',
                 raw='<svg id="chevron"><path d="M0 0L1 1"></path></svg>',source_scope='site_chrome'),
            dict(id='content-use',kind='unknown',text='原始 svg 对象',locator='snapshot.html/main/svg[2]',
                 raw='<svg><use href="#content-diagram"></use></svg>'),
            dict(id='content-definition',kind='image',text='Architecture',locator='snapshot.html/main/svg[3]',
                 raw='<svg id="content-diagram"><path d="M0 0L5 5"></path></svg>')],
        unknown=[dict(id='g1',object_id='use',reason='svg 未执行，需要独立解释或安全转换'),
                 dict(id='g2',object_id='content-use',reason='svg 未执行，需要独立解释或安全转换')])
    result=classify_web_chrome(store,source);objects={o['id']:o for o in result['objects']}
    assert objects['use']['source_scope']=='site_chrome'
    assert objects['use']['visual_classification']['definition_source_id']=='definition'
    assert [gap['object_id'] for gap in result['unknown']]==['content-use']


def test_article_breadcrumb_excludes_only_parent_link_not_body_reference(tmp_path):
    from sourceloom.ingest import intake
    from sourceloom.source_context import classify_web_chrome

    raw=(b'<html><body><main><header class="in-resource"><h1>Decision Tree</h1>'
         b'<p>in <a href="/guides/">Guides</a></p></header>'
         b'<p>Read the <a href="/guides/">Guides</a> for details.</p></main></body></html>')
    store=Store(tmp_path)
    src=intake(store,[('snapshot.html',raw)],source_url='https://example.org/guides/decision-tree/')
    classified=classify_web_chrome(store,src)
    links=[o for o in classified['objects'] if o['kind']=='link' and o['text']=='Guides']
    assert len(links)==2
    assert [o.get('source_scope') for o in links]==['site_chrome',None]
    assert next(o for o in classified['objects'] if o['text']=='Decision Tree').get('source_scope') is None
    assert store.read_blob(src['originals'][0]['sha256'])==raw


def test_heading_status_badges_remain_archived_metadata_not_article_images(tmp_path):
    from sourceloom.ingest import intake
    from sourceloom.source_context import classify_web_chrome
    from sourceloom.visual_sources import decorative_resource

    raw=(b'# Flask Babel ![Tests](https://github.com/org/repo/workflows/Test/badge.svg) '
         b'[![PyPI](https://img.shields.io/pypi/v/project.svg)](https://pypi.org/project/project/)\n\n'
         b'Implements translation support.\n')
    store=Store(tmp_path)
    src=intake(store,[('snapshot.md',raw)],source_url='https://example.org/readme.md')
    classified=classify_web_chrome(store,src)
    badges=[o for o in classified['objects'] if o['kind']=='image']
    assert len(badges)==2 and all(o.get('source_scope')=='source_metadata' for o in badges)
    assert all(decorative_resource(o) for o in badges)
    assert all(o.get('source_scope')=='source_metadata' for o in classified['objects']
               if o['kind']=='link' and not o.get('text','').strip())
    assert any(o['text']=='Implements translation support.' and not decorative_resource(o)
               for o in classified['objects'])
    assert store.read_blob(src['originals'][0]['sha256'])==raw


def test_separate_badge_paragraph_after_heading_is_source_metadata(tmp_path):
    from sourceloom.ingest import intake
    from sourceloom.source_context import classify_web_chrome

    raw=(b'# Package\n\n![Tests](https://github.com/org/repo/workflows/Test/badge.svg)\n'
         b'[![PyPI](https://img.shields.io/pypi/v/project.svg)](https://pypi.org/project/project/)\n\n'
         b'Article starts here.\n')
    store=Store(tmp_path)
    src=intake(store,[('snapshot.md',raw)],source_url='https://example.org/readme.md')
    classified=classify_web_chrome(store,src)
    assert all(o.get('source_scope')=='source_metadata' for o in classified['objects']
               if o['kind']=='image')
    assert all(o.get('source_scope')=='source_metadata' for o in classified['objects']
               if o['kind']=='link' and not o.get('text','').strip())
    assert any(o['text']=='Article starts here.' and not o.get('source_scope')
               for o in classified['objects'])


def test_centered_empty_alt_markdown_brand_stays_in_original(tmp_path):
    from sourceloom.source_context import classify_web_chrome
    from sourceloom.visual_sources import decorative_resource
    store=Store(tmp_path)
    markdown=b'<div align="center"><img src="https://example.org/logo.svg" alt=""></div>\n\n# Article\n\nUseful text'
    original=store.blob(markdown)
    src=dict(source_url='https://example.org/README.md',
        originals=[dict(name='snapshot.md',sha256=original,size=len(markdown))],
        objects=[dict(id='logo',kind='image',text='',target='https://example.org/logo.svg',
                      locator='snapshot.md/node[1]/img[1]',resource_id=None,raw='<img src="https://example.org/logo.svg" alt=""/>'),
                 dict(id='body',kind='text',text='Useful text',locator='snapshot.md/node[3]')],
        unknown=[dict(id='gap-1',object_id='logo',reason='unavailable')])
    classified=classify_web_chrome(store,src)
    assert not classified['unknown'] and decorative_resource(classified['objects'][0])
    assert not decorative_resource(classified['objects'][1])
    assert store.read_blob(original)==markdown


def test_exhausted_go_subscription_is_skipped_for_later_task_calls(tmp_path,monkeypatch):
    from sourceloom.providers import Provider
    class PreflightReached(Exception):pass
    seen=[]
    def check(_store,configuration):
        seen.append(configuration['base_url'])
        raise PreflightReached
    monkeypatch.setattr('sourceloom.trials.check',check)
    config=dict(base_url='https://opencode.ai/zen/go/v1',provider='openai-compatible',
        quota_fallback=dict(base_url='https://api.deepseek.com/v1',provider='openai-compatible'))
    job=dict(calls=[dict(status='rejected',error_code='subscription_limit_exceeded',
        upstream_base='https://opencode.ai/zen/go/v1')])
    with pytest.raises(PreflightReached):Provider(Store(tmp_path),config).call('project','active_plan',{}, {},job)
    assert seen==['https://api.deepseek.com/v1']


@pytest.mark.parametrize('residual',['','content','format'])
def test_background_restart_completes_planning_writing_review_without_repeating_calls(tmp_path,skill,monkeypatch,residual):
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
            if residual:
                job['active_candidate']['local_patch_attempts']=2
            if residual=='content':
                result['findings']=[dict(block_id='n1-b1',output_quote='我们假设 `x` 为正',
                    source_id='',source_quote='',problem='A remaining meaning issue',required_change='Preserve for inspection')]
            if residual=='format':
                result['format_decisions']=[dict(candidate_id='candidate-1',decision='fix',reason='Confirmed in this context')]
        else:pytest.fail('Unexpected routine review: '+role)
        return dict(gaps=[],actions=[],ready_reason='Required material is read',result=result)
    monkeypatch.setattr('sourceloom.active_composition.Provider.call',provider)
    monkeypatch.setattr('sourceloom.active_composition.scan',lambda bundle,draft,work:dict(
        canonical_digest=digest(canonical(draft).encode()),format=dict(findings=[],candidates=[dict(
            id='candidate-1',location='LINE-3',old_text='我们假设 `x` 为正',rule_id='FORMAT_REVIEW',reason='Still undecided')]
            if residual=='format' else [])))
    for _ in range(8):
        engine=Production(Store(store.root),{})
        assert engine.run_once()
        saved=store.job(job['id'])
        if saved['status'] in {'completed','failed'}:break
        assert saved['status']=='queued',saved.get('error')
    final=store.get(p['id'])
    assert calls==['active_plan','active_write','active_review']
    assert saved['status']==('failed' if residual else 'completed')
    if residual:
        # A checkpoint with unresolved meaning or confirmed format errors
        # cannot become a user-facing article merely because it is the last unit.
        assert final['state']!='completed'
        assert final.get('draft') is None
    else:
        assert final['state']=='completed' and final['production']['manual_edits']==0
    if residual:
        assert saved['quality_issues']
        if residual=='content':
            assert saved['active_candidate']['unresolved_content_findings']
    else:
        assert final['production']['semantic_status']=='not_independently_reviewed'
        assert store.read_blob(next(iter(saved['generated_resources'].values()))['blob']).decode()==canonical(final['draft'])
        assert saved['active_checkpoints'][0]['draft_digest']==digest(canonical(final['draft']).encode())


def test_missing_asset_can_resume_same_zero_call_job_after_inventory_is_completed(tmp_path,skill):
    store,_,project,bundle=prepared(tmp_path,skill)
    complete=copy.deepcopy(store.get(project['id'])['inventory'])
    incomplete=copy.deepcopy(complete)
    incomplete['unknown']=[dict(id='gap-1',object_id='s1',reason='image unavailable')]
    incomplete['digest']='before-asset-recovery'
    store.change(project['id'],lambda p:p.update(inventory=incomplete))
    queue=Queue(store,pipeline='active_composition_v1')
    job=queue.enqueue(project['id'],bundle)
    claimed=queue.claim('test-worker')
    claimed.update(stage='active_visual',error='original image unavailable')
    queue.finish(claimed,'test-worker','failed')
    store.change(project['id'],lambda p:p.update(inventory=complete))
    resumed=queue.continue_preprocessing(job['id'])
    assert resumed['id']==job['id'] and resumed['stage']=='active_index'
    assert resumed['status']=='queued' and resumed['calls']==[]
    assert not resumed['inventory']['unknown']
    assert store.get(project['id'])['active_job']==job['id']


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
    first['establishes_concepts']=['c1']
    second['establishes_concepts']=['c2']
    assert len(writing_batches([p],source(),10000,concept_limit=1))==2
    first['establishes_concepts']=['c'+str(n) for n in range(1,5)]
    second['establishes_concepts']=['c'+str(n) for n in range(5,9)]
    assert len(writing_batches([p],source(),10000))==1
    assert len(writing_batches([p],source(),10000,concept_limit=7))==2
    assert len(writing_batches([p],source(),10000,object_limit=1))==2


def test_local_patch_keeps_valid_edits_without_changing_protected_code():
    from sourceloom.active_composition import normalize_local_proposal
    draft={'blocks':[dict(id='b',markdown='A\nB\n```code```')]}
    proposal=dict(edits=[dict(block_id='b',old_text='A\nB',new_text='AB',reason='join'),
                         dict(block_id='b',old_text='```code```',new_text='```edited```',reason='bad')])
    normalized,rejected=normalize_local_proposal(proposal,draft,['```code```'])
    assert [(e['old_text'],e['new_text']) for e in normalized['edits']]==[('A','AB'),('B','')]
    assert rejected==[dict(block_id='b',reason='protected_original_changed')]
    paragraph,_=normalize_local_proposal(dict(edits=[dict(block_id='b',
        old_text='A\n\nB',new_text='New paragraph',reason='merge')]),
        {'blocks':[dict(id='b',markdown='A\n\nB')]},[])
    assert len(paragraph['edits'])==2


def test_english_source_heading_uses_the_approved_chinese_section_title():
    from sourceloom.active_composition import bind_planned_headings
    result={'blocks':[dict(id='n1-a',markdown='## Examples\n\nA paragraph'),
                      dict(id='n2-a',markdown='## 已经是中文标题\n') ]}
    node=dict(section_outline=[dict(id='n1',title='示例'),dict(id='n2',title='下一节')])
    changed=bind_planned_headings(result,node)
    assert result['blocks'][0]['markdown']=='## 示例\n\nA paragraph'
    assert result['blocks'][1]['markdown']=='## 已经是中文标题\n'
    assert changed[0]['planned_section_id']=='n1'
    english_plan={'blocks':[dict(id='n3-a',markdown='## 中文改写标题\n')]}
    assert bind_planned_headings(english_plan,dict(id='n3',title='English Source Title'))==[]
    assert english_plan['blocks'][0]['markdown']=='## 中文改写标题\n'


def test_single_source_span_can_align_a_wrong_model_end_offset():
    from sourceloom.active_composition import rebind_single_source_spans
    source_document={'objects':[dict(id='s1',kind='text',text='A short heading')]}
    value={'obligations':[dict(id='f1',source_id='s1',source_span_ids=['s1:0:20'])]}
    aligned,repairs=rebind_single_source_spans(value,source_document,['s1'])
    assert aligned['obligations'][0]['source_span_ids']==['s1:0:15']
    assert repairs[0]['submitted_span_id']=='s1:0:20'
    assert value['obligations'][0]['source_span_ids']==['s1:0:20']


def test_single_source_span_can_bind_omitted_id_without_guessing_multi_span():
    from sourceloom.active_composition import rebind_single_source_spans
    src={'objects':[dict(id='h',kind='heading',text='A short heading'),
                    dict(id='long',kind='text',text='A'*1200)]}
    value={'obligations':[dict(id='f1',source_id='h'),dict(id='f2',source_id='long')]}
    aligned,repairs=rebind_single_source_spans(value,src,['h','long'])
    assert aligned['obligations'][0]['source_span_ids']==['h:0:15']
    assert 'source_span_ids' not in aligned['obligations'][1]
    assert repairs[0]['submitted_span_id'] is None


def test_merged_writer_scope_overrides_first_planning_part_scope():
    from sourceloom.active_composition import writing_batch_contract
    planned={'purpose':'First group only','scope_boundary':'only src-001 through src-010',
             'voice':'first person'}
    node={'source_ids':['src-001','src-011']}
    actual=writing_batch_contract(planned,node,'Full source rewrite')
    assert actual['purpose']=='Full source rewrite'
    assert 'node.source_ids' in actual['scope_boundary']
    assert 'src-010' not in actual['scope_boundary']
    assert actual['voice']=='first person'
    assert planned['purpose']=='First group only'


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


def test_v2_failure_preserves_work_as_a_reviewable_result(tmp_path,skill,monkeypatch):
    store,_,project,bundle=prepared(tmp_path,skill)
    job=Queue(store,pipeline='active_composition_v2').enqueue(project['id'],bundle)
    engine=Production(store,{'generation_pipeline':'active_composition_v2'})

    def fail_after_partial_writer(claimed):
        first=claimed['source']['objects'][0]
        claimed.update(stage='active_review',inventory=claimed['source'],draft={'blocks':[dict(
            id='written-first',unit_id='u1',kind='explanation',markdown='已生成的正文',
            obligation_ids=[],object_ids=[first['id']],evidence=[],embedded_object_ids=[])]})
        raise ValueError('模拟检查失败')

    monkeypatch.setattr(engine,'step',fail_after_partial_writer)
    assert engine.run_once()
    saved=store.job(job['id']);published=store.get(project['id'])
    assert saved['status']=='ready_for_review' and saved.get('error')=='模拟检查失败'
    assert published['state']=='ready_for_review'
    assert published['delivery_state']=='recovered_ready_for_review'
    represented={sid for block in published['draft']['blocks'] for sid in block.get('object_ids',[])}
    assert represented=={obj['id'] for obj in published['inventory']['objects']}
    assert published['draft']['blocks'][0]['markdown']=='已生成的正文'
    assert saved['internal_failures'][0]['detail']=='模拟检查失败'


def test_v2_recovery_keeps_uncommitted_candidate_and_uses_all_evidence_links(tmp_path,skill,monkeypatch):
    from sourceloom.production import recoverable_delivery
    store,_,project,bundle=prepared(tmp_path,skill)
    job=Queue(store,pipeline='active_composition_v2').enqueue(project['id'],bundle)
    claimed=store.job(job['id'])
    first=claimed['source']['objects'][0]
    second=copy.deepcopy(first)|dict(id='source-second',locator='input/2',text='The second source object.')
    claimed['source']['objects'].append(second)
    claimed.update(inventory=claimed['source'],draft={'blocks':[dict(
        id='written-first',unit_id='u1',kind='explanation',markdown='已生成的正文',
        obligation_ids=[],object_ids=[first['id']],evidence=[],embedded_object_ids=[])]},
        active_candidate={'draft':{'blocks':[dict(
            id='written-second',unit_id='u2',kind='explanation',markdown='尚未提交的正文',
            obligation_ids=[],source_ids=[second['id']],evidence=[],embedded_object_ids=[])]}})
    inventory,_,draft=recoverable_delivery(claimed)
    blocks={block['id']:block for block in draft['blocks']}
    assert set(blocks)=={'written-first','written-second'}
    represented={sid for block in draft['blocks'] for sid in (
        block.get('object_ids',[])+block.get('source_ids',[])+
        [e['source_id'] for e in block.get('evidence',[]) if e.get('source_id')])}
    assert represented=={first['id'],second['id']}
    assert all('原件没有' not in block['markdown'] for block in draft['blocks'])


def test_v2_explicit_gateway_timeout_retries_current_saved_stage_once(tmp_path,skill,monkeypatch):
    from sourceloom.providers import Uncertain
    store,_,project,bundle=prepared(tmp_path,skill)
    job=Queue(store,pipeline='active_composition_v2').enqueue(project['id'],bundle)
    engine=Production(store,{'generation_pipeline':'active_composition_v2'})

    def gateway_timeout(claimed):
        key='active-plan-p1'
        claimed.update(stage='active_plan',pending=key)
        claimed['calls'].append(dict(id='gateway-call',role='active_plan',status='uncertain',
            step_key=key,http_status=524,response_blob=store.blob(b'gateway timeout')))
        raise Uncertain('模型请求返回 524，本次未自动重发')

    monkeypatch.setattr(engine,'step',gateway_timeout)
    assert engine.run_once()
    saved=store.job(job['id']);published=store.get(project['id'])
    assert saved['status']=='queued' and published['active_job']==job['id']
    assert 'pending' not in saved
    assert saved['transient_gateway_retries']['active-plan-p1']['attempts']==1
    assert saved['internal_recoveries'][0]['http_status']==524

    # A second explicit timeout preserves source and checkpoints while still
    # exposing the best saved result for review
    assert engine.run_once()
    saved=store.job(job['id']);published=store.get(project['id'])
    assert saved['status']=='ready_for_review'
    assert published['delivery_state']=='recovered_ready_for_review'


def test_v2_stateless_transport_uncertainty_retries_once_before_delivery(tmp_path,skill,monkeypatch):
    from sourceloom.providers import Uncertain
    store,_,project,bundle=prepared(tmp_path,skill)
    job=Queue(store,pipeline='active_composition_v2').enqueue(project['id'],bundle)
    engine=Production(store,{'generation_pipeline':'active_composition_v2'})
    def timeout(claimed):
        key='active-plan-p1-turn-1';claimed.update(stage='active_plan',pending=key)
        claimed['calls'].append(dict(id='transport-call',role='active_plan',status='uncertain',
            step_key=key,channel='openai-compatible'))
        raise Uncertain('上游结果不确定，原调用及预留费用保留')
    monkeypatch.setattr(engine,'step',timeout)
    assert engine.run_once()
    saved=store.job(job['id'])
    assert saved['status']=='queued' and 'pending' not in saved
    assert saved['transient_gateway_retries']['active-plan-p1-turn-1']['attempts']==1
    assert saved['internal_recoveries'][0]['type']=='unqueryable_transport'
    assert saved['transport_fallback_steps']['active-plan-p1-turn-1']=='transport-call'
    assert engine.run_once()
    saved=store.job(job['id']);published=store.get(project['id'])
    assert saved['status']=='ready_for_review'
    assert published['delivery_state']=='recovered_ready_for_review'


def test_active_retry_uses_the_configured_transport_fallback(tmp_path,skill,monkeypatch):
    from pydantic import BaseModel
    class Shape(BaseModel):
        answer:str
    store,_,project,bundle=prepared(tmp_path,skill)
    job=Queue(store,pipeline='active_composition_v2').enqueue(project['id'],bundle)
    job['transport_fallback_steps']={'step-1':'uncertain-call'}
    observed={}
    def call(provider,pid,role,payload,schema,claimed,cancelled):
        observed.update(role=role,model=provider.config.get('model'))
        return {'answer':'ok'}
    monkeypatch.setattr('sourceloom.active_composition.Provider.call',call)
    engine=ActiveComposition(Production(store,{
        'role_providers':{'active_plan':{'model':'primary'}},
        'fallback_providers':{'active_plan':{'model':'fallback'}}}))
    monkeypatch.setattr(engine.queue,'cancelled',lambda *args:False)
    assert engine.call(job,'step-1','active_plan',{},Shape)=={'answer':'ok'}
    assert observed=={'role':'active_plan__fallback','model':'fallback'}


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
    contextual=dict(id='candidate-previous',rule_id='PARALLEL_REVIEW',location='LINE-2',old_text='需要修正的标点。')
    from sourceloom.active_composition import candidate_key
    job['joint_review_contract_version']=1
    job['active_candidate']['dismissed_keys']=[candidate_key(contextual,draft)]
    monkeypatch.setattr('sourceloom.active_composition.scan',lambda *a:dict(format=dict(findings=[mechanical],candidates=[contextual])))
    resources=Resources(store,src)
    resources.add('term-reference','Reference',kind='external',locator='https://example.org/name')
    def review_turn(job,key,role,schema,source,ids,payload,validate):
        assert payload['visual_cards']==[dict(source_id='link',visible_content='A visible diagram')]
        assert payload['format_preflight']['candidates']==[]
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


def test_billed_active_plan_truncation_can_resume_once_on_different_model(tmp_path,skill):
    import json
    from sourceloom.store import Conflict

    store,queue,p,bundle=prepared(tmp_path,skill)
    job=queue.enqueue(p['id'],bundle)
    blob=store.blob(b'{"partial":true}')
    call=dict(id='plan-cut',role='active_plan',status='truncated',
        finish_reason='length',response_blob=blob,step_key='active-plan-p1-turn-0')
    job.update(status='failed',pending=call['step_key'],calls=[call])
    store.put_job(job);store.change(p['id'],lambda p:p.update(active_job=None))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='failed' WHERE id=?",(job['id'],))
        cx.execute('INSERT INTO spending(id,project,reserved,actual,created,body) VALUES(?,?,?,?,?,?)',
            (call['id'],p['id'],0,.15,1,json.dumps({'model':'deepseek-flash'})))
    config={'role_providers':{'active_plan':{'model':'deepseek-v4-pro'}}}
    resumed=queue.retry_validation(job['id'],config)
    assert resumed['status']=='queued' and not resumed.get('pending')
    assert resumed['known_plan_truncation_retries'][call['step_key']]['original_call']==call['id']
    resumed.update(status='failed',pending=call['step_key']);store.put_job(resumed)
    store.change(p['id'],lambda p:p.update(active_job=None))
    with pytest.raises(Conflict,match='不能重新发送'):
        queue.retry_validation(job['id'],config)


def test_active_plan_partitions_many_small_source_objects_before_output_overflow():
    from sourceloom.source_context import inventory_groups

    objects=[{'id':f'src-{n:03d}','text':'one short source object'} for n in range(53)]
    groups=inventory_groups(objects,limit=12000,max_objects=18)
    assert [len(group) for group in groups]==[18,18,17]
    assert [item['id'] for group in groups for item in group]==[
        item['id'] for item in objects]


def test_active_plan_keeps_list_item_text_and_its_link_in_one_partition():
    from sourceloom.source_context import inventory_groups

    objects=[{'id':f'src-{n}','text':'short','locator':f'page/node[1]/p[{n}]'}
             for n in range(17)]
    objects += [
        {'id':'item-text','text':'A linked reading','locator':'page/node[1]/ul[1]/li[1]'},
        {'id':'item-link','text':'A linked reading','locator':'page/node[1]/ul[1]/li[1]/a[1]'},
        {'id':'next-item','text':'Next','locator':'page/node[1]/ul[1]/li[2]'},
    ]
    groups=inventory_groups(objects,limit=12000,max_objects=18)
    assert [len(group) for group in groups]==[19,1]
    assert [item['id'] for item in groups[0][-2:]]==['item-text','item-link']


def test_future_partition_links_are_deferred_without_hiding_missing_current_links():
    from sourceloom.active_composition import filter_future_partition_link_briefs

    candidate={'link_briefs':[{'source_id':'src-1','role':'content'},
                              {'source_id':'src-2','role':'navigation'}]}
    scoped,deferred=filter_future_partition_link_briefs(candidate,['src-1'])
    assert [item['source_id'] for item in scoped['link_briefs']]==['src-1']
    assert deferred==['src-2']
    assert len(candidate['link_briefs'])==2
    empty,_=filter_future_partition_link_briefs({'link_briefs':[candidate['link_briefs'][1]]},['src-1'])
    assert empty['link_briefs']==[]


def test_html_element_name_evidence_keeps_literal_tag_boundary():
    from sourceloom.active_composition import markup_element_name_in_quote

    assert markup_element_name_in_quote('font element','the <font> element in HTML')
    assert not markup_element_name_in_quote('font element','the font system')
    assert not markup_element_name_in_quote('color property','the <color> element')


def test_coordinated_color_evidence_requires_each_component_in_one_sentence():
    from sourceloom.active_composition import coordinated_color_components_in_quote

    assert coordinated_color_components_in_quote(
        'foreground and background colors',
        'The background, foreground and link colors are all specified.')
    assert not coordinated_color_components_in_quote(
        'foreground and background colors','The foreground is dark. The background is light.')


def test_markup_property_pair_evidence_keeps_each_identifier_literal():
    from sourceloom.active_composition import markup_property_pair_in_quote

    assert markup_property_pair_in_quote('color and background-color properties',
        'Use the CSS properties <color> and <background-color> or its shorthand <background>.')
    assert not markup_property_pair_in_quote('color and background-color properties',
        'Use the CSS properties <color> and <border-color>.')


def test_css_inline_comments_are_not_reported_as_missing_by_python_only_scanner():
    from sourceloom.active_composition import respect_original_format

    def report():
        return {'format':{'findings':[dict(rule_id='FORMAT_CODE_COMMENT_COVERAGE',
            location='LINE-0002',old_text='a:link { /* link state */')],
            'candidates':[]}}
    good={'blocks':[dict(id='b',kind='explanation',markdown=(
        '```css\na:link { /* link state */\n color: red; /* red text */\n}\n```'))]}
    bad={'blocks':[dict(id='b',kind='explanation',markdown=(
        '```css\na:link { /* link state */\n color: red;\n}\n```'))]}
    assert not respect_original_format(report(),good,{'objects':[]})['format']['findings']
    assert respect_original_format(report(),bad,{'objects':[]})['format']['findings']


def test_inline_code_img_tag_is_not_a_rendered_uncentered_image():
    from sourceloom.active_composition import respect_original_format
    line='页面说明了 `<img>` 标签的用途'
    report={'format':{'findings':[dict(rule_id='FORMAT_IMAGE_NOT_CENTERED',
        location='LINE-0001',old_text=line)],'candidates':[]}}
    draft={'blocks':[dict(id='b',kind='explanation',markdown=line)]}
    result=respect_original_format(report,draft,{'objects':[]})
    assert not result['format']['findings']
    actual='页面显示 <img src="diagram.png">'
    report['format']['findings'][0]['old_text']=actual
    assert respect_original_format(report,{'blocks':[dict(id='b',kind='explanation',markdown=actual)]},
                                   {'objects':[]})['format']['findings']


def test_reading_projection_fences_unfenced_html_code_without_changing_saved_draft():
    from sourceloom.media import reading_draft

    raw='a:link {\n color: red;\n}'
    project={'inventory':{'objects':[{'id':'code','kind':'code','text':raw}]},
             'draft':{'blocks':[{'id':'b','kind':'explanation','markdown':'Example\n\n'+raw,
                                  'embedded_object_ids':['code']}]}}
    shown=reading_draft(project)['blocks'][0]['markdown']
    assert '```text\n'+raw+'\n```' in shown
    assert project['draft']['blocks'][0]['markdown']=='Example\n\n'+raw


def test_missing_official_name_can_use_saved_primary_page_evidence(tmp_path,monkeypatch):
    from sourceloom.active_composition import validate_names

    resources=Resources(Store(tmp_path),source())
    resources.add('code-example','a:link { color: red }',kind='code',locator='original')
    official_url='https://www.w3.org/TR/CSS22/selector.html'
    def saved_official(action):
        assert action['url']==official_url
        resources.add('official-css','The link pseudo-classes: :link and :visited',
                      kind='external',locator=official_url,original_url=official_url)
        return {'kind':'read','id':'official-css'}
    monkeypatch.setattr(resources,'execute',saved_official)
    concept=dict(id='link-class',chinese_name='链接伪类',english_name='Link Pseudo-Classes',
                 naming_status='verified',name_evidence=[dict(resource_id='code-example',quote='a:link')],
                 abbreviations=[],source_ids=['code-example'])
    checked=validate_names({'concepts':[concept]},resources)
    assert checked['concepts'][0]['name_evidence'][-1]['resource_id']=='official-css'


def test_plural_source_name_uses_verified_singular_w3c_name(tmp_path,monkeypatch):
    from sourceloom.active_composition import validate_names

    resources=Resources(Store(tmp_path),source())
    resources.add('cited','Some images are ignored by assistive technologies.',
                  kind='external',locator='original')
    official_url='https://www.w3.org/WAI/WCAG22/Understanding/page-titled.html'
    def saved_official(action):
        assert action['url']==official_url
        resources.add('official-assistive','Key Terms: assistive technology is hardware and/or software.',
                      kind='external',locator=official_url,original_url=official_url)
        return {'kind':'read','id':'official-assistive'}
    monkeypatch.setattr(resources,'execute',saved_official)
    concept=dict(id='assistive-tech',chinese_name='辅助技术',english_name='assistive technology',
                 naming_status='verified',name_evidence=[dict(resource_id='cited',
                 quote='assistive technologies')],abbreviations=[],source_ids=[])
    checked=validate_names({'concepts':[concept]},resources)
    assert checked['concepts'][0]['name_evidence'][-1]['resource_id']=='official-assistive'


def test_grouped_html_element_names_support_each_member():
    from sourceloom.active_composition import markup_element_name_in_quote

    quote='The HTML5 <figure> and <figcaption> elements provide captions.'
    assert markup_element_name_in_quote('figure element',quote)
    assert markup_element_name_in_quote('figcaption element',quote)
    assert not markup_element_name_in_quote('table element',quote)


def test_name_quote_rebinds_only_to_unique_exact_original(tmp_path):
    from sourceloom.active_composition import validate_names

    resources=Resources(Store(tmp_path),source())
    resources.add('original-line','Use the alt attribute to describe the action.',
                  kind='text',locator='original')
    resources.add('related-page','A related page discusses image links.',
                  kind='external',locator='related')
    concept=dict(id='alt',chinese_name='替代文本属性',english_name='alt attribute',
                 naming_status='verified',name_evidence=[dict(resource_id='related-page',
                 quote='Use the alt attribute to describe the action.')],
                 abbreviations=[],source_ids=['original-line'])
    checked=validate_names({'concepts':[concept]},resources)
    assert checked['concepts'][0]['name_evidence'][0]['resource_id']=='original-line'
    resources.add('second-original','Use the alt attribute to describe the action.',
                  kind='text',locator='other')
    concept['source_ids']=['original-line','second-original']
    concept['name_evidence'][0]['resource_id']='related-page'
    with pytest.raises(ValueError,match='术语名称证据不在已保存来源中'):
        validate_names({'concepts':[concept]},resources)


def test_post_patch_link_review_only_rechecks_touched_obligations():
    from sourceloom.active_composition import reviewed_link_guides

    job={'active_plans':[{'link_briefs':[
        {'source_id':'link-a','role':'content'},
        {'source_id':'link-b','role':'content'}]}]}
    obligations=[{'id':'a','source_id':'link-a'},{'id':'b','source_id':'link-b'}]
    assert [g['source_id'] for g in reviewed_link_guides(
        job,['link-a','link-b'],obligations,{'a','b'})]==['link-a','link-b']
    assert [g['source_id'] for g in reviewed_link_guides(
        job,['link-a','link-b'],obligations,{'b'})]==['link-b']


def test_ready_result_drops_only_already_opened_read_actions(tmp_path):
    from sourceloom.active_composition import discard_redundant_reads_with_result

    resources=Resources(Store(tmp_path),source())
    resources.add('original','Complete original paragraph.',kind='text',locator='original')
    resources.read('original')
    ready=dict(result={'edits':[]},gaps=[],actions=[dict(kind='read',resource_id='original')])
    fixed,reused=discard_redundant_reads_with_result(ready,resources)
    assert fixed['actions']==[] and reused==['original']
    assert ready['actions']
    needed=dict(result={'edits':[]},gaps=[],actions=[dict(kind='page',url='https://example.org/')])
    assert discard_redundant_reads_with_result(needed,resources)==(needed,[])


def test_editorial_link_scope_is_not_mistaken_for_evidence_limit():
    from sourceloom.active_composition import strip_editorial_link_limits

    plan={'link_briefs':[
        {'source_id':'a','limitation':'当前节点只需说明该目标页主题，不搬入全部示例'},
        {'source_id':'b','limitation':'该图只给出估计用时，不能证明路线最优'}]}
    checked,removed=strip_editorial_link_limits(plan)
    assert checked['link_briefs'][0]['limitation']==''
    assert checked['link_briefs'][1]['limitation']==plan['link_briefs'][1]['limitation']
    assert [r['source_id'] for r in removed]==['a']
    assert plan['link_briefs'][0]['limitation']


def test_catalogued_numeronyms_need_name_plan_before_writer():
    from sourceloom.active_composition import missing_catalogued_numeronyms

    src={'objects':[{'id':'text','kind':'text','text':'Supports i18n and l10n.'},
                    {'id':'code','kind':'code','text':'a1b = 3'}]}
    assert missing_catalogued_numeronyms(src,['text','code'],[])==['i18n','l10n']
    declared=[{'abbreviations':[{'short':'I18N'},{'short':'L10N'}]}]
    assert missing_catalogued_numeronyms(src,['text','code'],declared)==[]

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


def test_prefetched_link_repair_binds_actual_target_and_keeps_destination_for_review(tmp_path):
    from sourceloom.active_composition import bind_prefetched_link_evidence
    src={'objects':[dict(id='link',kind='link',text='Details',locator='input/a',
                         target='https://example.org/details')]}
    resources=Resources(Store(tmp_path),src)
    resources.add('target','Details about date formats and locale ambiguity.',kind='external',
                  locator='https://example.org/details',original_url='https://example.org/details')
    p={'link_briefs':[dict(source_id='link',role='content',destination='Claim still needs review',
                            evidence=[dict(resource_id='link',quote='Details')],unavailable_reason='')]}
    repairs=bind_prefetched_link_evidence(p,src,resources,[{'source_id':'link'}])
    assert repairs[0]['resource_id']=='target'
    assert p['link_briefs'][0]['evidence']==[dict(resource_id='target',
        quote='Details about date formats and locale ambiguity.')]
    assert p['link_briefs'][0]['destination']=='Claim still needs review'


def test_unplaced_concepts_follow_source_unit_and_prerequisite_order():
    from sourceloom.active_composition import align_unplaced_concepts
    p={'concepts':[dict(id='base',source_ids=['s1'],requires=[]),
                   dict(id='advanced',source_ids=['s1'],requires=['base'])],
       'nodes':[dict(id='n1',source_ids=['s1'],establishes_concepts=['advanced'])]}
    assert align_unplaced_concepts(p)==[dict(concept_id='base',node_id='n1',
                                           reason='planned_concept_unplaced')]
    assert p['nodes'][0]['establishes_concepts']==['base','advanced']


def test_reviewed_false_name_pair_can_retire_exactly_deleted_definition():
    from sourceloom.active_composition import retire_refuted_formal_concepts
    concept=dict(id='web',chinese_name='网页',english_name='Web',requires=[])
    later=dict(id='later',chinese_name='后续',english_name='Later',requires=['web'])
    node=dict(requires_concepts=[],establishes_concepts=['web','later'])
    job=dict(active_plans=[dict(concepts=[concept,later],nodes=[node])],
             writing_batches=[dict(requires_concepts=['web'],establishes_concepts=['web','later'])])
    candidate=dict(unresolved_content_findings=[dict(problem='已查证术语在实际正文中缺少名称：Web')],
        delta=dict(established_concepts=['web','later'],
                              concept_evidence=[dict(concept_id='web'),dict(concept_id='later')]),
        content_reviews=[dict(findings=[dict(block_id='b1',problem='网页与 Web 名称范围不对应',
                                              required_change='删除错误的正式定义')])],
        patch_history=[dict(edits=[dict(block_id='b1',old_text='- 网页（Web）：错误定义',new_text='')])])
    assert retire_refuted_formal_concepts(job,candidate)[0]['concept_id']=='web'
    assert [c['id'] for c in job['active_plans'][0]['concepts']]==['later']
    assert later['requires']==[]
    assert candidate['delta']['established_concepts']==['later']
    assert candidate['unresolved_content_findings']==[]


def test_unresolved_checkpoint_stops_later_paid_units(tmp_path):
    engine=ActiveComposition(Production(Store(tmp_path),{}))
    job=dict(stage='active_write',active_checkpoints=[dict(
        unresolved_content_findings=[],unresolved_revision={},
        unresolved_format=dict(findings=[dict(rule_id='FMT-001')],candidates=[]))])
    with pytest.raises(ValueError,match='停止后续付费生成'):
        engine.step(job)


def test_authored_spacing_removes_deletion_gap_without_touching_original():
    from sourceloom.active_composition import normalize_authored_spacing
    draft=dict(blocks=[dict(id='authored',kind='explanation',markdown='## 日期\n\n\n\n正文'),
                       dict(id='original',kind='object',markdown='A\n\n\nB')])
    result=normalize_authored_spacing(draft,dict(objects=[]))
    assert result['blocks'][0]['markdown']=='## 日期\n\n正文'
    assert result['blocks'][1]['markdown']=='A\n\n\nB'
    code=dict(blocks=[dict(id='authored-code',kind='explanation',
                           markdown='```python\na = 1\n\n\nb = 2\n```')])
    assert normalize_authored_spacing(code,dict(objects=[]))==code
    empty=dict(blocks=[dict(id='empty',kind='explanation',markdown='',
                            obligation_ids=[],object_ids=[],embedded_object_ids=[]),
                       dict(id='still-bound',kind='explanation',markdown='',
                            obligation_ids=['f1'],object_ids=[],embedded_object_ids=[])])
    assert [block['id'] for block in normalize_authored_spacing(empty,dict(objects=[]))['blocks']]==['still-bound']


def test_scoped_heading_repair_changes_only_named_heading():
    from sourceloom.active_composition import apply_heading_repairs, heading_repair_context
    result={
        'blocks':[
            {'id':'n1-b1','kind':'explanation','markdown':'## GPU Core IP',
             'obligation_ids':['o1'],'source_ids':['s1']},
            {'id':'n1-b2','kind':'explanation','markdown':'正文保持不变',
             'obligation_ids':['o1'],'source_ids':['s1']}],
        'coverage':[],
        'knowledge_delta':{'concept_evidence':[]},
    }
    node={'id':'n1','title':'GPU Core IP','purpose':'进入图形处理器核心主题'}
    expected=heading_repair_context(result,node)
    changed=apply_heading_repairs(result,expected,{'edits':[
        {'block_id':'n1-b1','old_heading':'## GPU Core IP',
         'new_heading':'## 图形处理器核心知识产权（GPU Core IP）'}]})
    assert changed[0]['block_id']=='n1-b1'
    assert result['blocks'][0]['markdown']=='## 图形处理器核心知识产权（GPU Core IP）'
    assert result['blocks'][1]['markdown']=='正文保持不变'
