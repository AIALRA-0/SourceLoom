import copy

import pytest

from sourceloom.durable import Queue
from sourceloom.store import digest
from sourceloom.writing import canonical
from tests.test_production import prepared, skill


def test_new_v2_enqueue_and_rewrite_are_core_chain_v1(tmp_path, skill):
    store, _, project, bundle = prepared(tmp_path, skill)
    queue = Queue(store, pipeline='active_composition_v2')

    created = queue.enqueue(project['id'], bundle)
    assert created['core_chain_version'] == 1

    claimed = queue.claim('boundary-test-worker')
    queue.finish(claimed, 'boundary-test-worker', 'completed')
    rewritten = queue.rewrite_active(project['id'], bundle)
    assert rewritten['core_chain_version'] == 1


def test_resuming_marked_legacy_zero_call_job_does_not_upgrade_it(tmp_path, skill):
    store, _, project, bundle = prepared(tmp_path, skill)
    complete = copy.deepcopy(store.get(project['id'])['inventory'])
    incomplete = copy.deepcopy(complete)
    incomplete['unknown'] = [dict(id='gap-1', object_id='s1', reason='image unavailable')]
    incomplete['digest'] = 'before-asset-recovery'
    store.change(project['id'], lambda value: value.update(inventory=incomplete))

    queue = Queue(store, pipeline='active_composition_v2')
    old = queue.enqueue(project['id'], bundle)
    old['core_chain_version'] = 0
    old.update(status='failed', stage='active_visual', calls=[])
    store.put_job(old)
    store.change(project['id'], lambda value: value.update(active_job=None, state='failed'))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='failed' WHERE id=?", (old['id'],))
    store.change(project['id'], lambda value: value.update(inventory=complete))

    resumed = queue.continue_preprocessing(old['id'])

    assert resumed['id'] == old['id']
    assert resumed['status'] == 'queued'
    assert resumed['core_chain_version'] == 0


@pytest.mark.parametrize('version,should_reuse', [(None, False), (1, True)])
def test_rewrite_reuses_checkpoints_only_from_the_same_core_chain(
        tmp_path, skill, version, should_reuse):
    store, _, project, bundle = prepared(tmp_path, skill)
    store.change(project['id'], lambda value: value['inventory']['objects'].append(dict(
        id='content-image', kind='image', locator='source/figure[1]', text='',
        resource_id='a' * 64, source_scope='article_media')))
    queue = Queue(store, pipeline='active_composition_v2')
    old = queue.enqueue(project['id'], bundle)
    if version is None:
        old.pop('core_chain_version')
    else:
        old['core_chain_version'] = version

    inventory = copy.deepcopy(store.get(project['id'])['inventory'])
    block = dict(id='done-b1', unit_id='write-1', kind='explanation', markdown='已完成正文',
        obligation_ids=[], object_ids=['s1'], evidence=[], embedded_object_ids=[])
    checkpoint = dict(node_id='write-1',
        draft_digest=digest(canonical({'blocks': [block]}).encode()),
        unresolved_content_findings=[], unresolved_revision={}, unresolved_format={})
    visual_card = dict(source_id='content-image', visible_content='Chart', source_text='',
        role='diagram', relationships=[], uncertainty=[], limitations=[], blocking_uncertainty=[])
    old.update(status='ready_for_review', source=copy.deepcopy(inventory), stage='active_deliver',
        active_groups=[['s1']], active_plans=[{'contract': {'purpose': 'kept'}}],
        active_partition_index=1, writing_batches=[{'id': 'write-1'}], unit_index=1,
        active_checkpoints=[checkpoint], draft={'blocks': [block]}, inventory=copy.deepcopy(inventory),
        plan={'title': 'validated'}, visual_cards=[visual_card],
        knowledge_memory=[{'node_id': 'write-1'}],
        generated_resources={'written-write-1': {'id': 'saved'}})
    store.put_job(old)
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='ready_for_review' WHERE id=?", (old['id'],))
    store.change(project['id'], lambda value: value.update(active_job=None, inventory=inventory))

    rewritten = queue.rewrite_active(project['id'], bundle)

    assert rewritten['core_chain_version'] == 1
    if should_reuse:
        assert rewritten['reused_visual_job'] == old['id']
        assert rewritten['reused_plan_job'] == old['id']
        assert rewritten['reused_writing_checkpoint_job'] == old['id']
        assert rewritten['active_checkpoints'] == [checkpoint]
        assert rewritten['draft']['blocks'] == [block]
    else:
        assert 'reused_visual_job' not in rewritten
        assert 'reused_plan_job' not in rewritten
        assert 'reused_writing_checkpoint_job' not in rewritten
        assert rewritten['unit_index'] == 0
        assert not rewritten.get('active_checkpoints')
        assert not rewritten.get('draft', {}).get('blocks')
