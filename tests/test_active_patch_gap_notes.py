import pytest

from sourceloom.active_composition import ActiveComposition
from sourceloom.durable import Queue
from sourceloom.production import Production
from sourceloom.production_contracts import LocalRepair
from sourceloom.store import Store
from tests.test_production import prepared, skill as writing_skill
from tests.test_active_composition import saved_legacy_v1_job


def _patch_job(tmp_path):
    skill_path = writing_skill.__wrapped__(tmp_path)
    store, _, project, bundle = prepared(tmp_path, skill_path)
    queue = Queue(store, pipeline='active_composition_v1')
    queued = saved_legacy_v1_job(store, project['id'], bundle)
    job = store.job(queued['id'])
    return store, queue, job, ActiveComposition(Production(store, {'active_revision_limit': 2}))


def _source():
    return {'objects': [{'id': 'source-1', 'kind': 'text', 'locator': 'input/1',
                         'text': 'Source text.'}]}


def test_active_patch_applies_valid_edit_and_records_explicit_out_of_scope_gaps(
        tmp_path, monkeypatch):
    store, queue, job, composition = _patch_job(tmp_path)
    calls = []

    def unexpected_provider_call(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError('A saved complete reply must be reused without another model call')

    monkeypatch.setattr('sourceloom.active_composition.Provider.call', unexpected_provider_call)
    key = 'active-patch-node-0-fixed'
    job['results'][key + '-turn-0'] = {
        'result': {
            'document_digest': 'saved-digest',
            'edits': [{'block_id': 'b1', 'old_text': 'old', 'new_text': 'new',
                       'reason': 'Correct the reviewed wording'}],
        },
        'gaps': ['Block b10 is outside the editable scope',
                 'Block b5 is outside the editable scope'],
        'actions': [],
        'ready_reason': 'The in-scope patch is complete',
    }
    store.put_job(job)
    payload = {
        'original_draft': {'blocks': [{'id': bid} for bid in ('b1', 'b4', 'b5', 'b10')]},
        'editable_block_ids': ['b1', 'b4'],
    }
    committed = []

    def validate(result, resources):
        assert result['edits'][0]['block_id'] == 'b1'
        committed.append(result)
        return 'transaction committed'

    result = composition.turn(job, key, 'active_patch', LocalRepair, _source(),
                              ['source-1'], payload, validate)

    assert result == 'transaction committed'
    assert len(committed) == 1
    assert calls == []
    assert job['active_sessions'][key]['complete'] is True
    assert any('b10' in note for note in job['quality_issues'])
    assert any('b5' in note for note in job['quality_issues'])
    assert job['nonblocking_protocol_notes'][-1]['gaps'] == [
        'Block b10 is outside the editable scope',
        'Block b5 is outside the editable scope',
    ]


def test_active_patch_rejects_gap_note_that_points_into_editable_scope(
        tmp_path, monkeypatch):
    store, queue, job, composition = _patch_job(tmp_path)
    composition.owner = None
    key = 'active-patch-node-0-in-scope-gap'
    job['results'][key + '-turn-0'] = {
        'result': {
            'document_digest': 'saved-digest',
            'edits': [{'block_id': 'b1', 'old_text': 'old', 'new_text': 'new',
                       'reason': 'Correct the reviewed wording'}],
        },
        'gaps': ['Block b1 still lacks a required correction'],
        'actions': [],
        'ready_reason': 'A patch was proposed',
    }
    store.put_job(job)
    provider_calls = []

    def no_more_gaps(self, provider_id, role, payload, schema, job, cancelled):
        provider_calls.append(role)
        return {'result': None, 'gaps': ['No valid in-scope repair is available'],
                'actions': [], 'ready_reason': ''}

    monkeypatch.setattr('sourceloom.active_composition.Provider.call', no_more_gaps)
    payload = {
        'original_draft': {'blocks': [{'id': bid} for bid in ('b1', 'b4', 'b5', 'b10')]},
        'editable_block_ids': ['b1', 'b4'],
    }

    with pytest.raises(ValueError, match='结构修正后仍不成立'):
        composition.turn(job, key, 'active_patch', LocalRepair, _source(),
                         ['source-1'], payload, lambda result, resources: result)

    assert provider_calls == ['active_patch', 'active_patch']
    assert key not in job['active_sessions'] or not job['active_sessions'][key].get('complete')
    assert not job.get('quality_issues')
