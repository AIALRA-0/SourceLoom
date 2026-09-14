import copy
import pytest
from sourceloom.store import Store, Conflict
from sourceloom.durable import Queue
from sourceloom.pedagogy import validate_teaching_plan, unmark_nonmetadata_document_info, mark_known_document_metadata, teaching_issues, validate_replan, arrange_document_info


def lesson():
    inventory={'objects':[{'id':'s','kind':'text'},{'id':'m','kind':'metadata'}],
               'obligations':[{'id':'f','object_id':'s'}]}
    unit=dict(id='u',title='选择取消以后',objective='观察变化',obligation_ids=['f'],object_ids=['s','m'],
              stages=['选择与取消','解释新状态'],proof_questions=[],prerequisites=[],
              reader_question='取消以后记住什么',entry_knowledge=['日常选择行为'],example_thread='选择，取消，重新选择',
              learning_result='分辨是否已经选定',follows_units=[],bridge_reason='',document_info_ids=['m'])
    plan=dict(title='选择',objective='理解状态',units=[unit],research_gaps=[],teaching_functions=[
        dict(kind=k,treatment='included',unit_ids=['u'],reason='对应实际选择过程') for k in
        ['problem','foundations','concepts','mechanism','example','transfer','extension']])
    return inventory,plan


def test_adaptive_plan_preserves_functions_without_seven_section_template():
    inv,plan=lesson()
    assert len(validate_teaching_plan(plan,inv)['units'][0]['stages'])==2
    plan['units'][0]['follows_units']=['future']
    with pytest.raises(ValueError,match='前提引用'):validate_teaching_plan(plan,inv)


def test_rewrite_can_omit_invented_teaching_problem_without_losing_source():
    inv,plan=lesson()
    plan['teaching_functions'][0].update(treatment='not_needed',unit_ids=[],reason='原文没有教学问题，保留原文目的')
    original=copy.deepcopy(plan)
    with pytest.raises(ValueError,match='必要前提'):validate_teaching_plan(plan,inv)
    validated=validate_teaching_plan(plan,inv,'rewrite')
    assert validated==original and plan==original
    assert validate_replan(validated,validated,['u'],inv,'rewrite')==validated
    extra_source=copy.deepcopy(inv);extra_source['obligations'].append({'id':'unassigned','object_id':'s'})
    with pytest.raises(ValueError,match='冻结义务'):validate_teaching_plan(plan,extra_source,'rewrite')
    invalid=copy.deepcopy(plan);invalid['teaching_functions'][0]['reason']=' '
    with pytest.raises(ValueError,match='说明原因'):validate_teaching_plan(invalid,inv,'rewrite')
    plain=copy.deepcopy(plan);plain['teaching_functions']=[]
    assert validate_teaching_plan(plain,inv,'rewrite')==plain
    with pytest.raises(ValueError,match='七类教学'):validate_teaching_plan(plain,inv)
    with pytest.raises(ValueError,match='冻结义务'):validate_teaching_plan(plain,extra_source,'rewrite')


def test_technical_facts_cannot_be_reclassified_as_document_info():
    inv,plan=lesson();plan['units'][0]['document_info_ids']=['s']
    with pytest.raises(ValueError,match='主题原信息'):validate_teaching_plan(plan,inv)


def test_production_binds_original_objects_from_facts_before_review(tmp_path):
    from sourceloom.production import Production
    inv,plan=lesson();plan['units'][0]['object_ids']=['m']
    original=copy.deepcopy(plan)
    job={'teaching_version':2,'transformation_mode':'rewrite','inventory':inv}
    engine=Production(Store(tmp_path),{})
    validated=engine.validate_plan(job,plan)
    assert validated['units'][0]['object_ids']==['m','s']
    assert validated['units'][0]['stages']==original['units'][0]['stages']
    assert validated['units'][0]['obligation_ids']==['f']
    assert plan==original
    assert engine.validate_plan(job,validated)==validated


def test_invalid_end_matter_flag_is_removed_but_original_fact_remains_assigned():
    inv,plan=lesson()
    plan['units'][0]['document_info_ids']=['s','m']
    plan['units'][0]['object_ids'].remove('s')
    plan['units'][0]['obligation_ids'].remove('f')
    repaired,removed=unmark_nonmetadata_document_info(plan,inv)
    assert removed==['s']
    assert repaired['units'][0]['document_info_ids']==['m']
    assert 's' in repaired['units'][0]['object_ids']
    assert 'f' in repaired['units'][0]['obligation_ids']
    assert validate_teaching_plan(repaired,inv)
    assert plan['units'][0]['document_info_ids']==['s','m']


def test_page_metadata_is_routed_to_end_matter_even_if_planner_forgets_flag():
    inv,plan=lesson()
    plan['units'][0]['document_info_ids']=[]
    repaired,marked=mark_known_document_metadata(plan,inv)
    assert marked==['m']
    assert repaired['units'][0]['document_info_ids']==['m']
    assert validate_teaching_plan(repaired,inv)
    assert plan['units'][0]['document_info_ids']==[]


def test_end_matter_selection_binds_all_metadata_facts_without_dropping_them():
    inv,plan=lesson();inv['obligations'].append(dict(id='metadata-fact',object_id='m'))
    plan['units'][0]['object_ids'].remove('m')
    validated=validate_teaching_plan(plan,inv)
    assert 'metadata-fact' in validated['units'][0]['obligation_ids']
    assert 'm' in validated['units'][0]['object_ids']
    assert plan['units'][0]['object_ids']==['s']


def test_info_move_keeps_all_actual_text_and_rejects_unassigned_source():
    _,plan=lesson();draft={'blocks':[dict(id='i',kind='document_info',markdown='原路径 /example',evidence=[{'source_id':'m'}]),
                                   dict(id='b',kind='explanation',markdown='我们取消选择',evidence=[])]}
    result=arrange_document_info(draft,plan)
    assert result['blocks']==list(reversed(draft['blocks']))
    draft['blocks'][0]['evidence']=[{'source_id':'s'}]
    with pytest.raises(ValueError,match='未批准'):arrange_document_info(draft,plan)


def test_review_cannot_pass_on_connector_words_or_skip_last_transition():
    _,plan=lesson();draft={'blocks':[dict(id='a',unit_id='u',kind='explanation',markdown='已取消旧地址'),dict(id='b',unit_id='u',kind='explanation',markdown='接下来，页面路径') ]}
    review=dict(assessed_block_ids=['a','b'],checks=[dict(category=k,status='pass',block_ids=['a'],quotes=['已取消旧地址'],reason='具体证据') for k in
        ['prerequisites','concrete_example','progression','term_use','scope','objects','document_info']],transitions=[],findings=[])
    assert '连接' in ' '.join(teaching_issues(review,draft,plan))
    review['transitions']=[dict(before_id='a',after_id='b',before_quote='伪造原句',after_quote='接下来',relationship='连接词不能建立含义',status='connected')]
    assert '缺口' in ' '.join(teaching_issues(review,draft,plan))


def test_structural_repair_cannot_change_an_unrelated_unit():
    inv,plan=lesson();other=copy.deepcopy(plan['units'][0]);other['id']='v';plan['units'].append(other)
    plan=validate_teaching_plan(plan,inv);candidate=copy.deepcopy(plan);candidate['units'][1]['title']='顺手改写'
    with pytest.raises(ValueError,match='无关单元'):validate_replan(candidate,plan,['u'],inv)


def test_required_teaching_checks_cannot_be_waived_as_not_applicable():
    _,plan=lesson()
    draft={'blocks':[dict(id='a',unit_id='u',kind='explanation',markdown='选择取消以后') ]}
    review=dict(assessed_block_ids=['a'],checks=[dict(category=k,status='not_applicable',block_ids=[],quotes=[],reason='不适用') for k in
        ['prerequisites','concrete_example','progression','term_use','scope','objects','document_info']],transitions=[],findings=[])
    issues=teaching_issues(review,draft,plan)
    assert len(issues)==4
    assert all('不能用不适用跳过' in issue for issue in issues)


def library(tmp_path):
    store=Store(tmp_path/'data');q=Queue(store);a=q.folder('父目录');b=q.folder('子目录',a['id'])
    doc=store.create('原文');q.edit_document(doc['id'],0,folder=b['id'],move=True)
    return store,q,a,b,doc


def test_folder_trash_restore_preserves_hierarchy_and_preexisting_trash(tmp_path):
    store,q,a,b,doc=library(tmp_path)
    old=store.create('之前删除的材料');q.edit_document(old['id'],0,folder=b['id'],move=True,trashed=True)
    q.library.apply('trash',[dict(kind='folder',id=a['id'],revision=0)])
    assert not q.tree()['folders'] and store.get(doc['id'])['trashed']
    q=Queue(Store(store.root));q.library.apply('restore',[dict(kind='folder',id=a['id'],revision=1)])
    assert store.get(doc['id'])['folder']==b['id'] and not store.get(doc['id'])['trashed']
    assert store.get(old['id'])['trashed']
    assert next(f for f in q.tree()['folders'] if f['id']==b['id'])['parent']==a['id']


def test_batch_version_conflict_rolls_back_every_change_and_cycles_rejected(tmp_path):
    store,q,a,b,doc=library(tmp_path)
    with pytest.raises(Conflict,match='另一页面'):
        q.library.apply('trash',[dict(kind='folder',id=a['id'],revision=0),dict(kind='document',id=doc['id'],revision=0)])
    assert not store.get(doc['id']).get('trashed')
    assert len(q.tree()['folders'])==2
    with pytest.raises(Conflict,match='子文件夹'):
        q.library.apply('move',[dict(kind='folder',id=a['id'],revision=0)],b['id'])


def test_permanent_deletion_requires_explicit_confirmation_and_keeps_billing(tmp_path):
    store,q,a,b,doc=library(tmp_path)
    q.library.apply('trash',[dict(kind='folder',id=a['id'],revision=0)])
    items=[dict(kind='folder',id=a['id'],revision=1)]
    with pytest.raises(Conflict,match='明确确认'):q.library.apply('purge',items)
    with store.connect() as cx:
        cx.execute('INSERT INTO spending(id,project,reserved,actual,status,body,created) VALUES(?,?,?,?,?,?,?)',('cost',doc['id'],.1,.1,'done','{}',1))
    q.library.apply('purge',items,confirm=True)
    with pytest.raises(KeyError):store.get(doc['id'])
    with store.connect() as cx:assert cx.execute('SELECT actual FROM spending WHERE id=?',('cost',)).fetchone()[0]==.1


def test_review_contract_rejects_invented_quotes_and_false_unit_scope():
    from sourceloom.pedagogy import teaching_review_contract
    inv,plan=lesson()
    draft={'blocks':[dict(id='a',unit_id='u',kind='explanation',markdown='我们先取消选择'),dict(id='b',unit_id='u',kind='explanation',markdown='再观察结果')]}
    review=dict(assessed_block_ids=['a','b'],checks=[],transitions=[dict(before_id='a',after_id='b',before_quote='我们先取消选择',after_quote='再观察结果',relationship='同一操作的结果',status='connected')],findings=[])
    assert not teaching_review_contract(review,draft,plan)
    review['transitions'][0]['before_quote']='他们先取消选择'
    assert any('non-verbatim' in e for e in teaching_review_contract(review,draft,plan))
    review['findings']=[dict(unit_ids=['other'],block_ids=['b'],quotes=['再观察结果'],missing_understanding='没有展示结果',repair_direction='具体展示')]
    assert any('unit_ids' in e for e in teaching_review_contract(review,draft,plan))


def test_metadata_source_is_assessed_but_excluded_from_lesson_transitions():
    from sourceloom.pedagogy import teaching_review_contract,teaching_issues
    inv,plan=lesson()
    draft={'blocks':[
        dict(id='a',unit_id='u',kind='explanation',markdown='先看一次操作',object_ids=[],evidence=[]),
        dict(id='m',unit_id='u',kind='source',markdown='页面登记信息',object_ids=['m'],
             evidence=[{'source_id':'m','quote':'页面登记信息'}]),
        dict(id='b',unit_id='u',kind='explanation',markdown='再观察结果',object_ids=[],evidence=[])]}
    review=dict(assessed_block_ids=['a','m','b'],checks=[],transitions=[
        dict(before_id='a',after_id='b',before_quote='先看一次操作',after_quote='再观察结果',
             relationship='同一操作的结果',status='connected')],findings=[])
    assert not teaching_review_contract(review,draft,plan,inv)
    assert '逐一检查前后内容' not in ' '.join(teaching_issues(review,draft,plan,inv))
    assert teaching_review_contract(review,draft,plan)


def test_disjoint_teaching_findings_select_only_cited_units():
    from sourceloom.pedagogy import repair_units
    plan={'units':[{'id':'u1'},{'id':'u2'},{'id':'u3'}]}
    review={'findings':[{'unit_ids':['u1']},{'unit_ids':['u3']}]}
    assert repair_units(review,plan)==['u1','u3']
    assert 'u2' not in repair_units(review,plan)


def test_partial_draft_displays_approved_page_info_after_the_lesson():
    from sourceloom.writing import available_draft
    info={'id':'page','unit_id':'u','kind':'document_info','markdown':'页面路径',
          'evidence':[{'source_id':'m','quote':'页面路径'}]}
    lesson={'id':'body','unit_id':'u','kind':'explanation','markdown':'先读主要概念','evidence':[]}
    job={'plan':{'units':[{'id':'u','document_info_ids':['m']}]},
         'draft':{'blocks':[info,lesson]}}
    assert [b['id'] for b in available_draft(job)['blocks']]==['body','page']
    assert [b['id'] for b in job['draft']['blocks']]==['page','body']


@pytest.mark.parametrize('password',['','required-password'])
def test_public_pdf_with_empty_open_password_keeps_original_bytes(tmp_path,password):
    from io import BytesIO
    from pypdf import PdfWriter
    from sourceloom.ingest import intake
    writer=PdfWriter();writer.add_blank_page(width=72,height=72)
    writer.encrypt(password,owner_password='owner-only')
    data=BytesIO();writer.write(data);raw=data.getvalue();store=Store(tmp_path/'pdf')
    if password:
        with pytest.raises(ValueError,match='打开密码'):intake(store,[('source.pdf',raw)])
    else:
        result=intake(store,[('source.pdf',raw)])
        assert store.read_blob(result['originals'][0]['sha256'])==raw
        assert len([o for o in result['objects'] if o['kind']=='page'])==1
        assert result['unknown'] # Opening the file is not proof of visual fidelity


@pytest.mark.parametrize('finish',['stop','tool_calls','length',None])
def test_known_finished_malformed_artifact_has_one_bounded_fallback(tmp_path,finish):
    from sourceloom.production import Production
    store=Store(tmp_path/'fallback');engine=Production(store,{'fallback_providers':{'planner':{'provider':'manual'}}})
    job={'id':'job','calls':[dict(id='original',finish_reason=finish,response_blob='saved')], 'pending':'planner'}
    store.put_job=lambda j:None
    engine._call=lambda *args:'fallback-result'
    if finish in {'stop','tool_calls'}:
        assert engine._invalid_response_fallback(job,'planner','planner',{},None)=='fallback-result'
        assert job['fallbacks']['planner']['original_call']=='original'
        with pytest.raises(ValueError):engine._invalid_response_fallback(job,'planner__fallback','planner__fallback',{},None)
    else:
        with pytest.raises(ValueError):engine._invalid_response_fallback(job,'planner','planner',{},None)
        assert job['pending']=='planner' and 'fallbacks' not in job
