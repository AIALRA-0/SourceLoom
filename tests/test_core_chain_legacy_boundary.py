import json

import pytest

from sourceloom.active_composition import ActiveComposition
from sourceloom.durable import Queue
from sourceloom.production import Production
from sourceloom.store import Conflict
from tests.test_production import prepared, skill


LEGACY_BATCH_STAGES = ('active_review', 'active_revision', 'active_format')


def _new_v2_job(tmp_path, skill):
    store, _, project, bundle = prepared(tmp_path, skill)
    queue = Queue(store, pipeline='active_composition_v2')
    job = queue.enqueue(project['id'], bundle)
    engine = ActiveComposition(Production(store, {}))
    return store, queue, project, bundle, job, engine


@pytest.mark.parametrize('stage', LEGACY_BATCH_STAGES)
@pytest.mark.parametrize(('pipeline', 'marker'), [
    ('active_composition_v2', 1),
    ('active_composition_v2', None),
    ('active_composition_v1', None),
], ids=['core-chain-v1', 'v2-missing-marker', 'v1-unmarked'])
def test_unmarked_or_core_chain_job_cannot_enter_legacy_batch_stages(
        tmp_path, skill, stage, pipeline, marker):
    _, _, _, _, job, engine = _new_v2_job(tmp_path, skill)
    job['pipeline'] = pipeline
    if marker is None:
        job.pop('core_chain_version')
    else:
        job['core_chain_version'] = marker
    job['stage'] = stage

    with pytest.raises(Conflict, match='内部错误'):
        engine.step(job)


@pytest.mark.parametrize(('pipeline', 'version'), [
    ('active_composition_v2', 0),
    ('active_composition_v1', 0),
], ids=['explicit-v2-legacy-marker', 'explicit-v1-legacy-marker'])
def test_explicit_legacy_job_can_continue_legacy_revision_path(
        tmp_path, skill, pipeline, version):
    _, _, _, _, job, engine = _new_v2_job(tmp_path, skill)
    job['pipeline'] = pipeline
    if version is None:
        job.pop('core_chain_version', None)
    else:
        job['core_chain_version'] = version
    job.update(
        stage='active_revision',
        unit_index=0,
        writing_batches=[dict(id='unit-1', source_ids=[], obligation_ids=[])],
        active_candidate=dict(
            draft={'blocks': []},
            revision_issues={'findings': []},
            patch_limit=1,
            patch_history=[dict(edits=[dict(block_id='b1')])],
        ),
    )

    assert engine.step(job) == 'queued'
    assert job['stage'] == 'active_format'
    assert job['active_candidate']['unresolved_revision'] == {'findings': []}


@pytest.mark.parametrize('operation', ['enqueue', 'rewrite_active'])
def test_new_entry_cannot_create_v1_legacy_jobs(tmp_path, skill, operation):
    store, _, project, bundle = prepared(tmp_path, skill)
    queue = Queue(store, pipeline='active_composition_v1')

    with pytest.raises(Conflict):
        if operation == 'enqueue':
            queue.enqueue(project['id'], bundle)
        else:
            queue.rewrite_active(project['id'], bundle)


def test_pre_cutover_migration_marks_existing_jobs_once(tmp_path):
    from sourceloom.store import Store

    store = Store(tmp_path)
    old_jobs = [
        dict(id='old-v1', project='project-v1', role='production', status='queued',
             created=1, pipeline='active_composition_v1'),
        dict(id='old-v2', project='project-v2', role='production', status='queued',
             created=2, pipeline='active_composition_v2'),
    ]
    with store.connect() as cx:
        for job in old_jobs:
            cx.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)', (
                job['id'], job['project'], job['role'], job['status'], job['created'],
                json.dumps(job),
            ))
        cx.execute('PRAGMA user_version=0')

    migrated = Store(store.root)
    assert [migrated.job(job['id'])['core_chain_version'] for job in old_jobs] == [0, 0]
    with migrated.connect() as cx:
        assert cx.execute('PRAGMA user_version').fetchone()[0] == 1

    post_cutover_missing = dict(
        id='post-cutover-unmarked', project='project-v2', role='production',
        status='queued', created=3, pipeline='active_composition_v2', stage='active_review')
    migrated.put_job(post_cutover_missing)
    reopened = Store(store.root)
    assert 'core_chain_version' not in reopened.job(post_cutover_missing['id'])

    engine = ActiveComposition(Production(reopened, {}))
    with pytest.raises(Conflict, match='内部错误'):
        engine.step(reopened.job(post_cutover_missing['id']))
