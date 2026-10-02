import copy

import pytest

from sourceloom import active_contracts as A
from sourceloom.active_composition import ActiveComposition
from sourceloom.production import Production
from sourceloom.store import Store


def _source():
    return dict(
        id='source', version=1, frozen=False, digest='source-digest',
        objects=[dict(id='s3', kind='text', locator='p3', text='The exception applies only to linked terms.')],
        resources=[], unknown=[], originals=[], obligations=[],
    )


def _unit(text):
    return dict(blocks=[dict(id='p3-b1', kind='explanation', markdown=text,
        obligation_ids=['f3'], source_ids=['s3'])], coverage=[], knowledge_delta=dict(
            established_concepts=[], explained_obligations=['f3'],
            unresolved_prerequisites=[], next_bridge='', concept_evidence=[]))


def _turn(result):
    return dict(gaps=[], actions=[], ready_reason='ready', result=result)


def _job():
    return dict(id='job', project='project', role='production', status='running', created=1,
        pipeline='active_composition_v2', results={}, calls=[], active_sessions={},
        external_resources={}, generated_resources={}, verified_terminology=[],
        draft={'blocks':[dict(id='p1-b1',markdown='已完成的第一单元'),
                         dict(id='p2-b1',markdown='已完成的第二单元')]},
        active_checkpoints=[{'node_id':'p1','draft_digest':'one'},
                            {'node_id':'p2','draft_digest':'two'}],
        knowledge_memory=[dict(node_id='p1',established=[dict(id='concept-a',
            definition='前面已经解释的概念')])])


def test_failed_glossary_writer_gets_one_contextual_extra_and_keeps_prior_checkpoints(tmp_path, monkeypatch):
    engine=ActiveComposition(Production(Store(tmp_path), {'active_structure_correction_limit':2}))
    job=_job()
    source=_source()
    node=dict(id='p3',title='Link behavior',source_ids=['s3'],obligation_ids=['f3'])
    payload=dict(node=node, established_memory=[],
        link_guides=[dict(source_id='s3', role='content_link', label='Term page')])
    # Model output from the last already-paid attempt is present in results,
    # as it is after Queue.retry_validation re-enters a rejected writer turn.
    key='active-write-p3-turn-2'
    invalid=_turn(_unit('## 术语表\n\n- 已解释概念（Existing concept）：重复定义\n\n链接例外也适用于所有术语'))
    job['results'][key]=invalid
    session=job['active_sessions'].setdefault('active-write-p3', {'round':2,'corrections':2})
    session.update(round=2, corrections=2, correction={'error':'prior failure', 'instruction':'retry'},
                   previous_invalid_result=_unit('上一次已保存的候选'))
    requests=[]

    def call(current_job, call_key, role, request, schema):
        assert call_key=='active-write-p3-turn-2'
        # Simulate production.Engine.call's in-job response cache.
        return current_job['results'][call_key]

    monkeypatch.setattr(engine, 'call', call)
    def validate(value, _resources):
        requests.append(copy.deepcopy(job['active_sessions']['active-write-p3'].get('correction')))
        if value['blocks'][0]['markdown'].startswith('## 术语表'):
            raise ValueError('短篇普通改写被扩成术语表：只为必要概念保留正式定义')
        return value

    # The extra attempt is dispatched in the following turn after the cached
    # rejected response is converted into a precise correction request.
    def corrected_call(current_job, call_key, role, request, schema):
        if call_key=='active-write-p3-turn-2':
            return current_job['results'][call_key]
        assert role=='active_write'
        assert 'previous_invalid_result' in request
        assert request['previous_invalid_result']==invalid['result']
        correction=request['protocol_correction']
        assert 'established_memory' in correction['instruction']
        assert 'do not define them again' in correction['instruction']
        assert correction['scope']['current_node']['id']=='p3'
        assert correction['scope']['current_node']['source_ids']==['s3']
        assert correction['scope']['current_link_roles'][0]['role']=='content_link'
        assert correction['scope']['already_established_memory']['required_prerequisites']==[]
        prior=correction['scope']['already_established_memory']['prior_concept_anchors']
        assert prior==[dict(node_id='p1',concept_id='concept-a',
                            prior_explanation='前面已经解释的概念')]
        request_seen.append(copy.deepcopy(request))
        return _turn(_unit('## 链接适用范围\n\n例外只适用于链接中的术语。'))

    request_seen=[]
    monkeypatch.setattr(engine, 'call', corrected_call)
    result=engine.turn(job, 'active-write-p3', 'active_write', A.WrittenUnit,
        source, ['s3'], payload, validate)

    assert result['blocks'][0]['markdown']=='## 链接适用范围\n\n例外只适用于链接中的术语。'
    assert len(request_seen)==1
    assert session['short_rewrite_glossary_repair_used'] is True
    assert session['short_rewrite_glossary_repair']['attempt']==3
    assert job['active_checkpoints']==[{'node_id':'p1','draft_digest':'one'},
                                      {'node_id':'p2','draft_digest':'two'}]
    assert [block['id'] for block in job['draft']['blocks']]==['p1-b1','p2-b1']
    assert job['knowledge_memory'][0]['established'][0]['id']=='concept-a'


def test_glossary_extra_is_one_shot_and_never_returns_source_text(tmp_path, monkeypatch):
    engine=ActiveComposition(Production(Store(tmp_path), {'active_structure_correction_limit':2}))
    job=_job()
    source=_source()
    node=dict(id='p3',title='Link behavior',source_ids=['s3'],obligation_ids=['f3'])
    payload=dict(node=node, established_memory=[], link_guides=[])
    source_text=source['objects'][0]['text']
    invalid=_turn(_unit('## 术语表\n\n- 重复定义（Repeated definition）：错误扩写'))
    call_count=0

    def call(current_job, call_key, role, request, schema):
        nonlocal call_count
        call_count+=1
        if 'previous_invalid_result' in request:
            assert 'do not define them again' in request['protocol_correction']['instruction']
        return invalid

    monkeypatch.setattr(engine,'call',call)
    def validate(value, _resources):
        raise ValueError('短篇普通改写被扩成术语表：仍未修正')

    with pytest.raises(ValueError, match='当前阶段结构修正后仍不成立'):
        engine.turn(job,'active-write-p3','active_write',A.WrittenUnit,
            source,['s3'],payload,validate)
    assert call_count==4  # two ordinary corrections, one special correction, then stop
    assert job['active_sessions']['active-write-p3']['short_rewrite_glossary_repair_used'] is True
    assert source_text not in invalid['result']['blocks'][0]['markdown']
    assert job['draft']['blocks']==[dict(id='p1-b1',markdown='已完成的第一单元'),
                                    dict(id='p2-b1',markdown='已完成的第二单元')]
