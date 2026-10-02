import copy
from types import SimpleNamespace

import pytest

from sourceloom.active_composition import (
    downgrade_original_only_evidence_bindings,
    validate_evidence_plan,
)


def _plan():
    return dict(
        evidence_gaps=[dict(id='gap-1', obligation_id='ob-1', source_id='src-1',
                            status='resolved')],
        evidence_bindings=[dict(id='binding-1', gap_id='gap-1',
                                obligation_id='ob-1', resource_id='src-1',
                                quote='salinity changes density')],
        evidence_resolutions=[dict(gap_id='gap-1', status='resolved',
                                   binding_ids=['binding-1'], attempts=1,
                                   stop_reason='original text proves external fact', error='')],
    )


def _source():
    return dict(objects=[dict(id='src-1', kind='text',
                              text='Ocean salinity changes density over time.')])


def test_exact_original_binding_becomes_explicitly_unresolved_without_losing_gap():
    original = _plan()
    revised, receipts = downgrade_original_only_evidence_bindings(
        original, _source(), ['src-1'])

    assert original == _plan()
    assert revised['evidence_bindings'] == []
    assert revised['evidence_gaps'][0]['id'] == 'gap-1'
    assert revised['evidence_gaps'][0]['status'] == 'unresolved'
    assert revised['evidence_resolutions'][0]['status'] == 'unresolved'
    assert revised['evidence_resolutions'][0]['binding_ids'] == []
    assert receipts == [dict(gap_id='gap-1', binding_ids=['binding-1'],
                             source_ids=['src-1'],
                             reason='exact_original_quote_is_not_external_evidence')]
    validate_evidence_plan(revised, {'ob-1': {'source_id': 'src-1'}},
                           {'src-1': _source()['objects'][0]}, {'src-1'},
                           SimpleNamespace(state={'entries': {}}, text=lambda _id: ''))


def test_external_binding_remains_and_resolved_gap_keeps_its_real_evidence():
    plan = _plan()
    plan['evidence_bindings'].append(dict(id='binding-2', gap_id='gap-1',
        obligation_id='ob-1', resource_id='ext-1', quote='Salinity affects density.'))
    plan['evidence_resolutions'][0]['binding_ids'].append('binding-2')

    revised, receipts = downgrade_original_only_evidence_bindings(
        plan, _source(), ['src-1'])

    assert len(receipts) == 1
    assert [entry['id'] for entry in revised['evidence_bindings']] == ['binding-2']
    assert revised['evidence_resolutions'][0]['binding_ids'] == ['binding-2']
    assert revised['evidence_resolutions'][0]['status'] == 'resolved'
    resources = SimpleNamespace(state={'entries': {'ext-1': {'kind': 'external'}}},
                                text=lambda _id: 'Salinity affects density.')
    validate_evidence_plan(revised, {'ob-1': {'source_id': 'src-1'}},
                           {'src-1': _source()['objects'][0]}, {'src-1'}, resources)


def test_nonmatching_original_quote_is_not_silently_downgraded():
    plan = _plan()
    plan['evidence_bindings'][0]['quote'] = 'a claim absent from the original'
    untouched = copy.deepcopy(plan)

    revised, receipts = downgrade_original_only_evidence_bindings(
        plan, _source(), ['src-1'])

    assert revised == untouched
    assert receipts == []
    with pytest.raises(ValueError, match='外部资源'):
        validate_evidence_plan(revised, {'ob-1': {'source_id': 'src-1'}},
                               {'src-1': _source()['objects'][0]}, {'src-1'},
                               SimpleNamespace(state={'entries': {}}, text=lambda _id: ''))
