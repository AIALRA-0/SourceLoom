import copy
import json

import pytest

from sourceloom.active_composition import prompt_active_plan_catalog
from sourceloom.active_resources import Resources
from sourceloom.checks import freeze
from sourceloom.durable import Queue, _split_active_plan_group_once
from sourceloom.store import Conflict, Store, digest, identity
from tests.test_production import prepared, skill


def test_old_external_image_lists_are_omitted_but_opened_and_source_lists_remain(tmp_path):
    source_refs = [{'url': 'https://example.org/source.png', 'alt': 'Source diagram'}]
    opened_refs = [{'url': 'https://example.org/current.png', 'alt': 'Current diagram'}]
    historical_refs = [{'url': f'https://example.org/old-{i}.png', 'alt': 'Historical'}
                       for i in range(20)]
    entries = [
        {'id': 'src-link', 'kind': 'link', 'image_refs': source_refs, 'resource_id': 'source-hash'},
        {'id': 'external-open', 'kind': 'external', 'image_refs': opened_refs, 'blob': 'opened-hash'},
        {'id': 'external-old', 'kind': 'external', 'image_refs': historical_refs,
         'blob': 'old-hash', 'snapshot_blob': 'snapshot-hash'},
        {'id': 'external-no-images', 'kind': 'external', 'locator': 'https://example.org/plain'},
    ]
    opened_resources = [{'text': 'Current target text',
                         'addresses': [{'id': 'external-open', 'start': 0, 'end': 20}]}]

    result = prompt_active_plan_catalog(entries, {'src-link'}, opened_resources)
    by_id = {entry['id']: entry for entry in result}

    assert by_id['src-link']['image_refs'] == source_refs
    assert by_id['external-open']['image_refs'] == opened_refs
    assert 'image_refs' not in by_id['external-old']
    assert 'image_refs' not in by_id['external-no-images']
    assert not {'blob', 'snapshot_blob', 'resource_id'} & set(by_id['external-old'])
    # Prompt compaction must not erase the archive's full metadata.
    assert entries[2]['image_refs'] == historical_refs


def test_reading_a_historical_page_restores_its_image_refs_for_the_current_turn(tmp_path):
    store = Store(tmp_path)
    url = 'https://example.org/shared-target'
    image_refs = [{'url': 'https://example.org/figure.png', 'alt': 'Figure'}]
    source = {'objects': [{'id': 'link-current', 'kind': 'link', 'locator': 'input/link[1]',
                          'text': 'Shared target', 'target': url}]}
    state = {'entries': {'external-target': {
        'id': 'external-target', 'blob': store.blob(b'Exact page text'), 'chars': 15,
        'kind': 'external', 'locator': url, 'original_url': url,
        'image_refs': image_refs,
    }}, 'opened': {}, 'reads': [], 'spans': {}, 'url_index': {}, 'search_index': {}}
    resources = Resources(store, source, state)

    compact_before_open = prompt_active_plan_catalog(resources.catalog(), {'link-current'}, [])
    assert 'image_refs' not in compact_before_open[1]
    assert resources.state['entries']['external-target']['image_refs'] == image_refs

    result = resources.execute({'kind': 'page', 'url': url})
    assert result['image_refs'] == image_refs
    compact_after_open = prompt_active_plan_catalog(resources.catalog(), {'link-current'},
                                                    resources.context())
    by_id = {entry['id']: entry for entry in compact_after_open}
    assert by_id['external-target']['image_refs'] == image_refs


def _failed_sixth_partition(tmp_path, skill):
    store, _, project, bundle = prepared(tmp_path, skill)

    def prepare_inventory(current):
        for index in range(2, 9):
            current['inventory']['objects'].append({
                'id': f'src-{index:05d}', 'kind': 'text', 'locator': f'input/p[{index}]',
                'text': f'Planning source paragraph {index}.'})
        current['inventory']['objects'].append({
            'id': 'source-figure', 'kind': 'image', 'locator': 'input/figure[1]',
            'text': '', 'resource_id': 'a' * 64, 'source_scope': 'article_media'})
        current['inventory'] = freeze(current['inventory'])
        current['inventory']['inventory_review'] = {'status': 'complete'}
        current['state'] = 'inventoried'

    project = store.change(project['id'], prepare_inventory)
    source = copy.deepcopy(project['inventory'])
    queue = Queue(store, pipeline='active_composition_v2')
    job = queue.enqueue(project['id'], bundle)
    job['core_chain_version'] = 0
    ids = [obj['id'] for obj in source['objects']]
    job['source'] = source
    job['stage'] = 'active_plan'
    job['status'] = 'failed'
    job['error'] = 'Responses response.failed upstream_error'
    job['active_groups'] = [[source_id] for source_id in ids]
    job['active_group_prefixes'] = [f'p{index + 1}' for index in range(len(ids))]
    job['active_partition_index'] = 5
    job['active_partition_count'] = len(ids)
    # Live failed jobs can retain the exact dispatched step as pending. The
    # recovery may clear only this matching marker after validating the saved
    # Responses failure receipt.
    job['pending'] = 'active-plan-p6-turn-0'
    job['active_plans'] = [
        {'contract': {'purpose': 'frozen whole-document contract'},
         'concepts': [{'id': f'p{index + 1}-concept'}],
         'nodes': [{'id': f'p{index + 1}-node'}]}
        for index in range(5)
    ]
    resource_state = Resources(store, source).state
    job['active_sessions'] = {
        'active-plan-p6': {'round': 0, 'corrections': 0,
            'resources': resource_state, 'direct_link_prefetch_complete': True,
            'action_results': [], 'action_history': []}}

    prior_calls = []
    for _ in range(5):
        call_id = identity()
        call = {'id': call_id, 'step_key': 'active-plan-p6-turn-0', 'role': 'active_plan',
                'status': 'uncertain', 'channel': 'openai-compatible',
                'protocol': 'responses', 'streaming': False, 'dispatch_started': True,
                'http_status': 524, 'upstream_base': 'https://api.kuafushe.cc/v1'}
        store.reserve(project['id'], call_id, .001,
                      {'channel': 'openai-compatible', 'reserved_cny': .001})
        store.settle(call_id, None, {'status': 'upstream_service_error', 'http_status': 524})
        prior_calls.append(call)
    failed_id = identity()
    failed_body = {'status': 'failed', 'error': {'code': 'upstream_error'}}
    failed_call = {'id': failed_id, 'step_key': 'active-plan-p6-turn-0',
        'role': 'active_plan', 'status': 'invalid', 'channel': 'openai-compatible',
        'protocol': 'responses', 'streaming': True, 'dispatch_started': True,
        # The production Responses parser saves a completed HTTP response
        # without recording 200 on the call; the body is the durable evidence.
        'http_status': None, 'finish_reason': 'failed',
        'upstream_base': 'https://api.kuafushe.cc/v1',
        'response_blob': store.blob(json.dumps(failed_body).encode())}
    store.reserve(project['id'], failed_id, .001,
                  {'channel': 'openai-compatible', 'reserved_cny': .001})
    store.settle(failed_id, 0, {'status': 'upstream_error', 'actual_cny': 0})
    job['calls'] = prior_calls + [failed_call]
    store.put_job(job)
    store.change(project['id'], lambda current: current.update(active_job=None, state='failed'))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='failed',owner=NULL,lease_until=0 WHERE id=?",
                   (job['id'],))
    return store, queue, project, job


def _three_call_failed_partition(tmp_path, skill, *, split_group=True):
    store, queue, project, failed = _failed_sixth_partition(tmp_path, skill)
    source_ids=[obj['id'] for obj in failed['source']['objects']]
    if split_group:
        # Preserve p7 as an existing suffix while giving p6 a safe boundary.
        failed['active_groups']=failed['active_groups'][:5]+[source_ids[5:8],source_ids[8:]]
        failed['active_group_prefixes']=[f'p{index+1}' for index in range(len(failed['active_groups']))]
    failed['calls']=[failed['calls'][0],failed['calls'][1],failed['calls'][-1]]
    first,second,third=failed['calls']
    gateway=store.blob(json.dumps({'error':{'code':'upstream_service_error'}}).encode())
    first.update(provider_id='kuafu-chat',protocol='chat_completions',status='uncertain',
        streaming=True,http_status=524,error_code='upstream_service_error',response_blob=gateway)
    second.update(provider_id='kuafu-responses',protocol='responses',status='uncertain',
        streaming=False,http_status=524,error_code='upstream_service_error',response_blob=gateway)
    third.update(provider_id='kuafu-responses',protocol='responses',status='invalid',
        streaming=True,http_status=None,finish_reason='failed')
    store.put_job(failed)
    return store,queue,project,failed


def test_three_call_kuafu_failure_splits_only_current_source_group_and_keeps_ledgers(tmp_path, skill):
    store,queue,project,failed=_three_call_failed_partition(tmp_path,skill)
    calls_before=copy.deepcopy(failed['calls'])
    source_before=copy.deepcopy(failed['source'])
    plans_before=copy.deepcopy(failed['active_plans'])
    session_before=copy.deepcopy(failed['active_sessions']['active-plan-p6'])
    costs_before=[(row['id'],row['status'],row['actual']) for row in store.costs(project['id'])]

    resumed=queue.retry_validation(failed['id'],config={'preflight':True})

    assert resumed['id']==failed['id'] and resumed['status']=='queued'
    assert resumed['active_partition_index']==5
    assert resumed['active_plans']==plans_before
    assert resumed['source']==source_before
    assert resumed['active_groups'][:5]==failed['active_groups'][:5]
    assert resumed['active_groups'][5:7]==[[source_before['objects'][5]['id']],
                                             [source_before['objects'][6]['id'],source_before['objects'][7]['id']]]
    assert resumed['active_group_prefixes'][5:]==['p6-split1a','p6-split1b','p7']
    assert resumed['active_partition_count']==len(resumed['active_groups'])
    assert resumed['active_sessions']['active-plan-p6']==session_before
    assert 'active-plan-p6-split1a' not in resumed['active_sessions']
    assert 'active-plan-p6-split1b' not in resumed['active_sessions']
    assert resumed['calls']==calls_before
    assert 'pending' not in resumed
    assert [(row['id'],row['status'],row['actual']) for row in store.costs(project['id'])]==costs_before
    marker=resumed['active_plan_split_recoveries'][0]
    assert marker['version']=='active-plan-single-split-v1' and marker['status']=='queued'
    assert marker['previous_call_ids']==[call['id'] for call in calls_before]
    flattened=[sid for group in resumed['active_groups'] for sid in group]
    expected=[obj['id'] for obj in source_before['objects']]
    assert flattened==expected and len(flattened)==len(set(flattened))
    assert store.get(project['id'])['active_job']==failed['id']


def test_three_call_kuafu_failure_without_safe_object_boundary_stays_failed(tmp_path, skill):
    store,queue,project,failed=_three_call_failed_partition(tmp_path,skill,split_group=False)
    # The current group contains one indivisible original object.
    failed['active_groups'][5]=[failed['source']['objects'][5]['id']]
    store.put_job(failed)
    project_before=store.get(project['id'])
    job_before=store.job(failed['id'])
    costs_before=[(row['id'],row['status'],row['actual']) for row in store.costs(project['id'])]

    with pytest.raises(Conflict,match='没有安全的原对象边界'):
        queue.retry_validation(failed['id'],config={'preflight':True})

    assert store.job(failed['id'])==job_before
    assert store.get(project['id'])==project_before
    assert [(row['id'],row['status'],row['actual']) for row in store.costs(project['id'])]==costs_before


def test_uncertain_three_call_context_retry_splits_once_without_touching_unknown_costs(tmp_path, skill):
    store,queue,project,failed=_three_call_failed_partition(tmp_path,skill)
    old_prefix='p6'
    recovery_prefix='p6-ctx1'
    failed['active_group_prefixes'][5]=recovery_prefix
    failed['active_sessions']['active-plan-'+recovery_prefix]=failed['active_sessions'].pop(
        'active-plan-'+old_prefix)
    failed['active_sessions']['active-plan-'+recovery_prefix]['round']=1
    failed['active_plan_context_recoveries']=[dict(partition_index=5,status='failed',
        original_prefix=old_prefix,recovery_prefix=recovery_prefix)]
    step='active-plan-'+recovery_prefix+'-turn-1'
    first,second,third=failed['calls']
    for call in failed['calls']:
        call['step_key']=step
    first.update(status='uncertain',streaming=True)
    first.pop('http_status',None);first.pop('error_code',None);first.pop('response_blob',None)
    third.update(status='uncertain',streaming=True)
    third.pop('http_status',None);third.pop('finish_reason',None);third.pop('response_blob',None)
    failed.update(status='uncertain',pending=step,current_step_key=step,error='响应投递状态未知')
    store.put_job(failed)
    with store.connect() as cx:
        for call in (first,third):
            cx.execute("UPDATE spending SET actual=NULL,status='unknown',body=? WHERE id=?",
                (json.dumps({'status':'unknown','call_id':call['id']}),call['id']))
        cx.execute("UPDATE production_control SET status='uncertain',owner=NULL,lease_until=0 WHERE id=?",
                   (failed['id'],))
    calls_before=copy.deepcopy(failed['calls'])
    source_before=copy.deepcopy(failed['source'])
    costs_before=[(row['id'],row['status'],row['actual'],row['body'])
                  for row in store.costs(project['id'])]

    resumed=queue.retry_validation(failed['id'],config={'preflight':True})

    assert resumed['status']=='queued' and 'pending' not in resumed
    assert resumed['active_group_prefixes'][5:]==['p6-ctx1-split1a','p6-ctx1-split1b','p7']
    assert resumed['active_sessions']['active-plan-p6-ctx1']['round']==1
    assert resumed['calls']==calls_before
    assert [(row['id'],row['status'],row['actual'],row['body'])
            for row in store.costs(project['id'])]==costs_before
    marker=resumed['active_plan_split_recoveries'][0]
    assert marker['previous_call_ids']==[call['id'] for call in calls_before]
    assert marker['reason'].endswith('unknown delivery')


def test_adaptive_plan_split_balances_object_count_before_text_bytes():
    source_ids=[f'object-{index}' for index in range(29)]
    source={'objects':[dict(id=source_id,locator=f'page/p[{index+1}]',
                            text=('long source text '*70 if index<5 else 'x'))
                       for index,source_id in enumerate(source_ids)]}

    groups=_split_active_plan_group_once(source,source_ids)

    assert groups is not None
    assert groups[0]+groups[1]==source_ids
    assert len(groups[0])==14 and len(groups[1])==15


def test_failed_plan_resumes_same_job_once_without_replanning_neighbors(tmp_path, skill):
    store, queue, project, failed = _failed_sixth_partition(tmp_path, skill)
    calls_before = copy.deepcopy(failed['calls'])
    plans_before = copy.deepcopy(failed['active_plans'])
    groups_before = copy.deepcopy(failed['active_groups'])
    ledger_before = [(row['id'], row['status']) for row in store.costs(project['id'])]

    resumed = queue.retry_validation(failed['id'], config={'role_providers': {'active_plan': {}}})

    assert resumed['id'] == failed['id']
    assert resumed['status'] == 'queued' and resumed['stage'] == 'active_plan'
    assert resumed['active_partition_index'] == 5
    assert resumed['active_plans'] == plans_before
    assert resumed['active_groups'] == groups_before
    assert resumed['active_group_prefixes'][:5] == ['p1', 'p2', 'p3', 'p4', 'p5']
    assert resumed['active_group_prefixes'][5] == 'p6-ctx1'
    assert resumed['active_group_prefixes'][6:] == [f'p{i}' for i in range(7, len(groups_before) + 1)]
    assert resumed['active_sessions']['active-plan-p6'] == failed['active_sessions']['active-plan-p6']
    assert resumed['active_sessions']['active-plan-p6-ctx1'] == failed['active_sessions']['active-plan-p6']
    assert resumed['calls'] == calls_before
    assert 'pending' not in resumed
    assert [(row['id'], row['status']) for row in store.costs(project['id'])] == ledger_before
    assert len(store.list()) == 1
    marker = resumed['active_plan_context_recoveries'][0]
    assert marker['status'] == 'queued' and marker['version'] == 'active-plan-historical-catalog-compaction-v1'
    assert marker['reused_validated_partition_count'] == 5
    assert store.get(project['id'])['active_job'] == failed['id']

    with pytest.raises(Conflict):
        queue.continue_failed_active_plan_context(failed['id'])


def test_failed_plan_resume_accepts_derived_visual_text_but_rejects_changed_source_address(
        tmp_path, skill):
    store, queue, project, failed = _failed_sixth_partition(tmp_path, skill)
    visual=next(obj for obj in failed['source']['objects'] if obj['id']=='source-figure')
    visual['text']='Visible image content confirmed during the saved visual stage'
    store.put_job(failed)
    assert queue.retry_validation(failed['id'],config={'preflight':True})['status']=='queued'

    store, queue, project, failed = _failed_sixth_partition(tmp_path/'changed', skill)
    failed['source']['objects'][0]['locator']='other/input/p[1]'
    store.put_job(failed)
    with pytest.raises(Conflict,match='Saved source no longer matches'):
        queue.retry_validation(failed['id'],config={'preflight':True})


def test_failed_plan_resume_rejects_incomplete_partition_coverage_atomically(tmp_path, skill):
    store, queue, project, failed = _failed_sixth_partition(tmp_path, skill)
    failed['active_groups'][-1] = []
    store.put_job(failed)
    before_project = store.get(project['id'])
    before_call_count = len(store.job(failed['id'])['calls'])

    with pytest.raises(Conflict):
        queue.retry_validation(failed['id'], config={'role_providers': {'active_plan': {}}})

    assert store.job(failed['id'])['status'] == 'failed'
    assert len(store.job(failed['id'])['calls']) == before_call_count
    assert store.get(project['id']) == before_project


def test_failed_plan_resume_rejects_mismatched_pending_step_atomically(tmp_path, skill):
    store, queue, project, failed = _failed_sixth_partition(tmp_path, skill)
    failed['pending'] = 'active-plan-p6-turn-1'
    store.put_job(failed)
    before_project = store.get(project['id'])
    before_calls = copy.deepcopy(store.job(failed['id'])['calls'])

    with pytest.raises(Conflict):
        queue.retry_validation(failed['id'], config={'role_providers': {'active_plan': {}}})

    unchanged = store.job(failed['id'])
    assert unchanged['status'] == 'failed'
    assert unchanged['pending'] == 'active-plan-p6-turn-1'
    assert unchanged['calls'] == before_calls
    assert store.get(project['id']) == before_project
