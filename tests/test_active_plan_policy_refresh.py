import json

from pydantic import BaseModel
import pytest

from sourceloom.active_composition import ActiveComposition, PIPELINE_V2
from sourceloom.active_policy import EVIDENCE_GAP_POLICY_REFRESH_VERSION
from sourceloom.durable import Queue
from sourceloom.production import Production
from sourceloom.store import Conflict
from tests.test_external_action_binding_fix import _make_skill
from tests.test_production import prepared


class _PlanShape(BaseModel):
    value: int
    evidence_gaps: list[dict] = []


def _failed_plan(tmp_path):
    store, _, project, bundle = prepared(tmp_path, _make_skill(tmp_path / 'skill'))
    queue = Queue(store, pipeline=PIPELINE_V2)
    job = queue.enqueue(project['id'], bundle)
    session_key = 'active-plan-p1'
    failed_key = session_key + '-turn-3'
    gap_id = 'gap-p1-wsgi-name'
    calls = []
    results = {}
    for turn in range(4):
        step = f'{session_key}-turn-{turn}'
        response = dict(gaps=[], actions=[], ready_reason='ready',
                        result={'value': turn, 'evidence_gaps': []})
        results[step] = response
        calls.append(dict(id=f'old-call-{turn}', role='active_plan', status='completed',
            step_key=step, response_blob=store.blob(json.dumps(response).encode('utf-8'))))
    calls[-1]['step_key'] = failed_key
    checkpoints = [{'partition': 0, 'digest': 'plan-checkpoint'}]
    session = dict(round=3, corrections=2, declared_gap_ids=[gap_id],
        action_history=[dict(action=dict(kind='page', gap_id=gap_id, source_id='src-00004',
            url='https://peps.python.org/pep-3333/'), result={'status': 'retrieved'})],
        resources={'entries': {}, 'opened': {}, 'reads': [], 'spans': {},
                   'url_index': {}, 'search_index': {}})
    error = '当前阶段结构修正后仍不成立：规划没有保存本轮声明的 EvidenceGap ID：' + gap_id
    job.update(status='failed', stage='active_plan', pipeline=PIPELINE_V2,
        core_chain_version=0, pending=None,
        active_partition_index=0, active_partition_count=1, active_sessions={session_key: session},
        active_checkpoints=checkpoints, calls=calls, results=results, error=error, finished=1,
        active_correction_receipts=[
            dict(step=session_key, role='active_plan',
                 error='v2 外部检索必须绑定当前 Turn 明确声明的 EvidenceGap',
                 received_digest=f'older-receipt-{turn}') for turn in (1, 2)])
    store.put_job(job)
    store.change(project['id'], lambda value: value.update(active_job=None, state='failed'))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='failed' WHERE id=?", (job['id'],))
    return store, queue, project, job, checkpoints, calls, results, session_key, gap_id, error


def test_failed_plan_uses_one_fresh_turn_with_actionable_gap_policy(tmp_path):
    (store, queue, project, job, checkpoints, calls, results,
     session_key, gap_id, error) = _failed_plan(tmp_path)
    resumed = queue.retry_validation(job['id'], {'max_plan_repairs': 2})
    marker = resumed['active_plan_policy_refreshes'][session_key]
    next_key = session_key + '-turn-4'

    assert resumed['status'] == 'queued'
    assert marker['version'] == EVIDENCE_GAP_POLICY_REFRESH_VERSION
    assert marker['status'] == 'queued' and marker['attempts'] == 0
    assert marker['previous_step_key'] == session_key + '-turn-3'
    assert marker['next_step_key'] == next_key
    assert marker['gap_ids'] == [gap_id]
    assert resumed['active_sessions'][session_key]['round'] == 4
    assert resumed['active_sessions'][session_key]['corrections'] == 2
    assert resumed['active_sessions'][session_key]['correction'] == {
        'error': error.removeprefix('当前阶段结构修正后仍不成立：'),
        'instruction': resumed['active_sessions'][session_key]['correction']['instruction']}
    assert 'exact ID' in resumed['active_sessions'][session_key]['correction']['instruction']
    assert resumed['active_sessions'][session_key]['declared_gap_ids'] == [gap_id]
    assert resumed['calls'] == calls
    assert resumed['results'] == results
    assert resumed['active_checkpoints'] == checkpoints

    observed = {}
    engine = ActiveComposition(Production(store, {}))

    def fresh_turn(claimed, key, role, payload, schema):
        observed.update(key=key, role=role, correction=payload['protocol_correction'],
                        declared_evidence_gaps=payload['declared_evidence_gaps'],
                        action_history=payload['action_history'])
        return dict(gaps=[], actions=[], ready_reason='repaired',
                    result={'value': 4, 'evidence_gaps':[{'id': gap_id}]})

    engine.call = fresh_turn
    source = dict(objects=[], resources=[], unknown=[], originals=[], version=1,
                  id='saved-source', frozen=False, digest='saved-source-digest')
    value = engine.turn(resumed, session_key, 'active_plan', _PlanShape, source, [], {},
                        lambda result, _: result)

    assert value == {'value': 4, 'evidence_gaps':[{'id': gap_id}]}
    assert observed['key'] == next_key
    assert observed['role'] == 'active_plan'
    assert observed['declared_evidence_gaps'] == []
    assert observed['action_history'][0]['action']['gap_id'] == gap_id
    assert 'action_history' in observed['correction']['instruction']
    assert resumed['results'][session_key + '-turn-3'] == results[session_key + '-turn-3']
    assert resumed['active_checkpoints'] == checkpoints


def test_policy_refreshed_plan_still_requires_the_declared_gap_and_cannot_refresh_twice(tmp_path):
    (store, queue, project, job, _, _, _,
     session_key, gap_id, error) = _failed_plan(tmp_path)
    resumed = queue.retry_validation(job['id'], {'max_plan_repairs': 2})
    engine = ActiveComposition(Production(store, {}))
    invalid = dict(gaps=[], actions=[], ready_reason='ready',
                   result={'value': 4, 'evidence_gaps': []})
    engine.call = lambda *args: invalid
    source = dict(objects=[], resources=[], unknown=[], originals=[], version=1,
                  id='saved-source', frozen=False, digest='saved-source-digest')

    with pytest.raises(ValueError, match='当前阶段结构修正后仍不成立：规划没有保存本轮声明的 EvidenceGap ID'):
        engine.turn(resumed, session_key, 'active_plan', _PlanShape, source, [], {},
                    lambda result, _: result)

    next_key = session_key + '-turn-4'
    resumed.update(status='failed', stage='active_plan', pending=None,
        error='当前阶段结构修正后仍不成立：规划没有保存本轮声明的 EvidenceGap ID：' + gap_id,
        finished=2)
    resumed['calls'].append(dict(id='new-call-4', role='active_plan', status='completed',
        step_key=next_key, response_blob=store.blob(json.dumps(invalid).encode('utf-8'))))
    resumed['results'][next_key] = invalid
    store.put_job(resumed)
    store.change(project['id'], lambda value: value.update(active_job=None, state='failed'))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='failed' WHERE id=?", (job['id'],))

    with pytest.raises(Conflict, match='一次性 EvidenceGap 策略刷新已用完'):
        queue.retry_validation(job['id'], {'max_plan_repairs': 2})
    saved = store.job(job['id'])
    assert saved['active_sessions'][session_key]['round'] == 4
    assert saved['active_plan_policy_refreshes'][session_key]['attempts'] == 0
    assert len(saved['active_policy_refresh_history']) == 1
    assert [call['id'] for call in saved['calls']][-1] == 'new-call-4'
