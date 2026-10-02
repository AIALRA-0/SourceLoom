import json
import time

import pytest

from sourceloom.active_composition import (
    claim_unknown_patch_continuation,
    patchable_block_ids,
    retarget_protected_findings,
    writing_batch_contract,
)
from sourceloom.durable import Queue
from sourceloom.store import Conflict, digest
from sourceloom.writing import canonical, repair
from tests.test_production import prepared, skill as writing_skill


def _draft(source_id):
    return {'blocks': [
        {'id': 'object', 'kind': 'object', 'source_ids': [source_id],
         'markdown': '<!-- <li class="dropdown education"> -->'},
        {'id': 'explanation', 'kind': 'explanation', 'source_ids': [source_id],
         'markdown': '菜单项用于访问课程内容。'},
    ]}


def test_protected_review_is_retargeted_to_bound_authored_prose():
    draft = _draft('src-1')
    review = {'findings': [dict(block_id='object', output_quote=draft['blocks'][0]['markdown'],
        source_id='src-1', source_quote='original object', problem='缺少必要解释',
        required_change='补充解释') ]}

    retarget_protected_findings(review, draft)

    finding = review['findings'][0]
    assert finding['block_id'] == 'explanation'
    assert finding['output_quote'] == '菜单项用于访问课程内容。'
    assert '保留关联的原始材料对象' in finding['required_change']
    assert patchable_block_ids(draft) == {'explanation'}
    assert review['protected_finding_retargets'][0]['from_block_id'] == 'object'


def test_skill_committer_rejects_new_code_fence_inside_local_patch(tmp_path):
    skill_path = writing_skill.__wrapped__(tmp_path)
    store, _, project, bundle = prepared(tmp_path, skill_path)
    draft = _draft('src-1')
    old = draft['blocks'][1]['markdown']
    new = old + '\n\n```html\n<li class="dropdown education">课程入口说明</li>\n```'
    proposal = dict(document_digest=digest(canonical(draft).encode()), edits=[dict(
        block_id='explanation', old_text=old, new_text=new,
        reason='Add the required annotated copy as authored material')])

    with pytest.raises(Conflict, match='技能的精确提交器拒绝补丁'):
        repair(bundle, draft, proposal, {'explanation'}, tmp_path / 'repair')


def _uncertain_patch_job(tmp_path):
    skill_path = writing_skill.__wrapped__(tmp_path)
    store, _, project, bundle = prepared(tmp_path, skill_path)
    queue = Queue(store, pipeline='active_composition_v2')
    queued = queue.enqueue(project['id'], bundle)
    job = store.job(queued['id'])
    source_id = job['source']['objects'][0]['id']
    node = dict(id='unit-1', source_ids=[source_id], obligation_ids=[])
    draft = _draft(source_id)
    candidate = dict(draft=draft, patch_history=[], revision_blocks=['object'],
        revision_issues={'findings': [dict(block_id='object',
            output_quote=draft['blocks'][0]['markdown'], source_id=source_id,
            source_quote=job['source']['objects'][0]['text'], problem='需解释该材料',
            required_change='增加必要说明')]})
    job.update(stage='active_revision', status='uncertain', unit_index=0,
        writing_batches=[node], active_plans=[{'contract': {}, 'nodes': [node]}],
        active_candidate=candidate)
    base='active-patch-unit-1-0-'+digest([
        canonical(draft), writing_batch_contract({}, node, job.get('goal', ''))])[:16]
    pending=base+'-turn-2'
    job['pending']=pending
    job['calls']=[dict(id='unknown-call', role='active_patch', status='uncertain',
        step_key=pending, channel='openai-compatible', protocol='responses', streaming=True,
        http_status=200, dispatch_started=True, deadline_at=time.time()-2,
        upstream_base='https://api.kuafushe.cc/v1')]
    store.put_job(job)
    # A terminal job has released its project reservation and worker control.
    p = store.get(project['id'])
    p['active_job'] = None
    with store.connect() as cx:
        cx.execute('UPDATE projects SET body=? WHERE id=?',
                   (json.dumps(p, ensure_ascii=False), project['id']))
        cx.execute("UPDATE production_control SET status='uncertain',owner=NULL,lease_until=0 WHERE id=?",
                   (job['id'],))
    return store, queue, job, pending


def test_queue_allows_one_frozen_same_job_distinct_step_continuation(tmp_path):
    store, queue, job, pending = _uncertain_patch_job(tmp_path)
    original_call = dict(job['calls'][0])

    outcome = queue.continue_active_patch_unknown_sse(job['id'])

    resumed = store.job(job['id'])
    marker = resumed['active_candidate']['unknown_patch_continuations'][0]
    assert outcome['queued'] is True
    assert outcome['original_call_preserved'] == original_call['id']
    assert resumed['calls'][0] == original_call
    assert 'pending' not in resumed
    assert marker['original_step_key'] == pending
    assert marker['continuation_session_key'] != pending
    assert marker['original_result'] == 'unknown_not_replayed'
    assert resumed['active_candidate']['revision_blocks'] == ['explanation']
    assert resumed['status'] == 'queued'


def test_queue_refuses_a_second_continuation_for_same_unknown_call(tmp_path):
    store, queue, job, pending = _uncertain_patch_job(tmp_path)
    marker = dict(original_step_key=pending, continuation_session_key='fresh-v1',
                  version='active-patch-unknown-sse-v1', status='queued')
    job['active_candidate']['unknown_patch_continuations'] = [marker]
    store.put_job(job)

    with pytest.raises(Conflict):
        queue.continue_active_patch_unknown_sse(job['id'])

    unchanged = store.job(job['id'])
    assert unchanged['pending'] == pending
    assert unchanged['calls'][0]['id'] == 'unknown-call'
    assert len(unchanged['active_candidate']['unknown_patch_continuations']) == 1


def test_unknown_patch_claim_preserves_old_result_and_rejects_duplicate():
    job = {'pending': 'patch-turn-2', 'results': {}, 'calls': [dict(
        id='unknown', step_key='patch-turn-2', role='active_patch', status='uncertain',
        protocol='responses', streaming=True, http_status=200, response_blob=None,
        upstream_id=None, dispatch_started=True, deadline_at=10,
        upstream_base='https://api.kuafushe.cc/v1')],
        'active_candidate': {'draft': {'blocks': []}, 'revision_issues': {'findings': []}}}
    marker = claim_unknown_patch_continuation(job, 'patch-turn-2', 'patch-fresh-v1', now=11)
    assert marker and job.get('pending') is None
    assert job['calls'][0]['status'] == 'uncertain'
    assert claim_unknown_patch_continuation(job, 'patch-turn-2', 'patch-fresh-v2', now=12) is None
