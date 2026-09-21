import copy

import pytest
from pydantic import BaseModel

from sourceloom.active_composition import (
    PIPELINE_V2,
    bounded_prior_context,
    initialize,
    validate_evidence_plan,
)
from sourceloom.active_resources import Resources
from sourceloom.checks import can_publish, transition_delivery
from sourceloom.store import Conflict, Store, digest
from sourceloom.writing import canonical


def _source():
    return dict(objects=[dict(id='s1', kind='text', locator='input/1',
                              text='The original condition is preserved.')],
                obligations=[], resources=[], unknown=[], originals=[], version=1,
                id='src', frozen=False, digest='old')


@pytest.fixture
def skill(tmp_path):
    root=tmp_path/'writing-skill'
    (root/'references').mkdir(parents=True)
    (root/'constitution').mkdir()
    (root/'assets').mkdir()
    (root/'SKILL.md').write_text('[format](references/format-rules.md)\n[explain](references/explanation-framework.md)\n[formula](references/formula-explanation.md)\n`constitution/principles.md`', encoding='utf-8')
    for name in ('format-rules','explanation-framework','formula-explanation'):
        (root/'references'/f'{name}.md').write_text('FULL-'+name, encoding='utf-8')
    (root/'constitution'/'principles.md').write_text('FULL-CONSTITUTION', encoding='utf-8')
    return root


def _obligation():
    return dict(id='o1', source_id='s1', quote='The original condition is preserved.',
                source_span_ids=[], meaning='Preserve the condition', conditions=['if ready'],
                quantities=[], negations=[], narrator='The author', referents=[])


def test_evidence_gap_must_bind_one_assigned_obligation():
    plan=dict(evidence_gaps=[dict(id='gap-1', obligation_id='o1', source_id='s1',
        missing='official name', reason='The source uses an unexplained name',
        allowed_sources=['primary'], stop_condition='exact name quote', status='required')],
        evidence_bindings=[], evidence_resolutions=[])
    assert validate_evidence_plan(plan, {'o1': _obligation()}, {'s1': _source()['objects'][0]}, {'s1'}) == plan
    bad=copy.deepcopy(plan)
    bad['evidence_gaps'][0]['obligation_id']='missing'
    with pytest.raises(ValueError, match='绑定当前原文义务'):
        validate_evidence_plan(bad, {'o1': _obligation()}, {'s1': _source()['objects'][0]}, {'s1'})


def test_evidence_binding_requires_saved_external_quote(tmp_path):
    resources=Resources(Store(tmp_path), _source())
    resources.add('external-page', 'Official name appears here.', kind='external',
                  locator='https://example.org/name', original_url='https://example.org/name')
    plan=dict(
        evidence_gaps=[dict(id='gap-1', obligation_id='o1', source_id='s1',
            missing='official name', reason='The source uses an unexplained name',
            allowed_sources=['primary'], stop_condition='exact name quote', status='page_opened')],
        evidence_bindings=[dict(id='binding-1', gap_id='gap-1', obligation_id='o1',
            resource_id='external-page', quote='Official name appears here.',
            source_url='https://example.org/name')],
        evidence_resolutions=[dict(gap_id='gap-1', status='resolved',
            binding_ids=['binding-1'], attempts=1, stop_reason='primary quote found')])
    assert validate_evidence_plan(plan, {'o1': _obligation()},
                                  {'s1': _source()['objects'][0]}, {'s1'}, resources)==plan


def test_external_url_and_search_are_deduplicated(tmp_path, monkeypatch):
    import sourceloom.network
    seen=[]
    xml=b'<rss><channel><item><title>Source</title><link>https://example.org/topic</link><description>snippet</description></item></channel></rss>'
    def fetch(url, *args, **kwargs):
        seen.append(url)
        if 'bing.com' in url:
            return xml, 'text/xml', url
        return b'<html><body>Primary evidence.</body></html>', 'text/html', url
    monkeypatch.setattr(sourceloom.network, 'fetch', fetch)
    resources=Resources(Store(tmp_path), _source())
    first=resources.execute(dict(kind='page', url='https://example.org/topic'))
    second=resources.execute(dict(kind='page', url='https://example.org/topic#section'))
    assert first['complete'] and second['reused_external']
    assert len([url for url in seen if 'example.org' in url]) == 1
    search=resources.execute(dict(kind='search', query='official name'))
    repeated=resources.execute(dict(kind='search', query='  official   name '))
    assert search['evidence_status'].startswith('discovery_only')
    assert repeated['reused_search']
    assert len([url for url in seen if 'bing.com' in url]) == 1


def test_configured_search_routes_stop_in_order_and_prioritize_openalex(tmp_path, monkeypatch):
    import sourceloom.network
    seen=[]
    def api_request(url, *args, **kwargs):
        seen.append((url,kwargs))
        if 'openalex' in url:
            return b'{"results":[{"display_name":"Paper","id":"https://doi.org/10/x"}]}', 'application/json', url
        if 'tinyfish' in url:
            return b'{"results":[{"title":"Primary","url":"https://example.org/primary"}]}', 'application/json', url
        raise AssertionError('later search route should not be called')
    monkeypatch.setattr(sourceloom.network, 'api_request', api_request)
    routes=[dict(provider_id='parallel', search_url='https://parallel.test/search'),
            dict(provider_id='tinyfish', search_url='https://tinyfish.test/search',api_key='secret'),
            dict(provider_id='openalex', search_url='https://openalex.test/works')]
    resources=Resources(Store(tmp_path), _source(), search_routes=routes)
    normal=resources.execute(dict(kind='search', query='primary', search_profile='general'))
    assert normal['search_provider']=='tinyfish'
    academic=resources.execute(dict(kind='search', query='paper', search_profile='academic'))
    assert academic['search_provider']=='openalex'
    assert all('parallel.test' not in url for url,_ in seen)
    assert 'query=primary' in seen[0][0]
    assert seen[0][1]['headers']=={'X-API-Key':'secret'}


def test_paid_search_fallbacks_use_readweave_request_shapes(tmp_path, monkeypatch):
    import sourceloom.network
    calls=[]
    def api_request(url, *args, **kwargs):
        calls.append((url,kwargs))
        if 'tinyfish' in url:return b'{"results":[]}', 'application/json', url
        if 'octen' in url:return b'{"data":{"results":[]}}', 'application/json', url
        return b'{"results":[{"title":"Result","url":"https://example.org/result","excerpts":["Exact excerpt"]}]}', 'application/json', url
    monkeypatch.setattr(sourceloom.network,'api_request',api_request)
    routes=[
        dict(provider_id='tinyfish',base_url='https://tinyfish.test',endpoint='/',api_key='tiny'),
        dict(provider_id='octen',base_url='https://octen.test',endpoint='/search',api_key='octen',model_parameters={'count':6}),
        dict(provider_id='parallel',base_url='https://parallel.test',endpoint='/v1/search',api_key='parallel',model_parameters={'mode':'turbo','maxResults':4}),
    ]
    result=Resources(Store(tmp_path),_source(),search_routes=routes).execute(
        dict(kind='search',query='bounded evidence',search_profile='general'))
    assert result['search_provider']=='parallel'
    assert result['matches'][0]['snippet']=='Exact excerpt'
    assert calls[1][1]['method']=='POST'
    assert calls[1][1]['json_body']=={'query':'bounded evidence','count':6}
    assert calls[2][1]['method']=='POST'
    assert calls[2][1]['json_body']['advanced_settings']['max_results']==4
    assert all('secret' not in str(item) for item in result['attempts'])


def test_v2_initialization_preserves_pipeline_and_review_limits():
    job={'pipeline': PIPELINE_V2}
    initialize(job)
    assert job['pipeline']==PIPELINE_V2
    assert job['delivery_state']=='draft'
    assert job['content_patch_default']==1
    assert job['content_patch_hard_limit']==2
    assert job['format_patch_hard_limit']==2


def test_queue_records_v2_pipeline_before_initialization(tmp_path):
    from sourceloom.durable import Queue
    store=Store(tmp_path)
    project=store.create('v2')
    store.change(project['id'],lambda value:value.update(inventory=_source()))
    job=Queue(store,pipeline=PIPELINE_V2).enqueue(project['id'],{
        'root':'skill','package_digest':'package','instruction_digest':'instruction'})
    assert job['pipeline']==PIPELINE_V2


def test_v2_estimate_batches_visuals_instead_of_counting_every_page_as_a_call():
    from sourceloom.durable import Queue
    inventory={'objects':[{'kind':'page','resource_id':f'r{i}','text':'page'} for i in range(13)]}
    estimate=Queue.estimate(inventory,pipeline=PIPELINE_V2)
    assert estimate['visual_batches']==3
    assert estimate['minimum_calls']==6


def test_cross_batch_context_is_bounded_and_keeps_latest_blocks():
    blocks=[{'markdown':'a'*4000},{'markdown':'b'*4000},{'markdown':'c'*4000}]
    assert bounded_prior_context(blocks,8000)==['b'*4000,'c'*4000]


def test_delivery_cannot_skip_review_ready_acceptance_and_publish():
    draft={'blocks':[{'markdown':'candidate'}]}
    project=dict(revision=1, draft=draft, accepted_revision=0,
                 production=dict(pipeline=PIPELINE_V2, delivery_state='draft'))
    assert transition_delivery(project, 'review_ready')=='ready_for_review'
    with pytest.raises(Conflict, match='独立语义审核'):
        transition_delivery(project, 'accept')
    project['independent_review']=dict(
        status='passed', revision=1,
        canonical_digest=digest(canonical(draft).encode()))
    assert transition_delivery(project, 'accept')=='accepted'
    assert not can_publish({**project, 'accepted_revision':0})
    assert can_publish(project)
    assert transition_delivery(project, 'publish')=='published'
    assert project['production']['delivery_state']=='published'


def test_existing_acceptance_receipt_can_be_published():
    draft={'blocks':[{'markdown':'candidate'}]}
    project=dict(revision=4, draft=draft, accepted_revision=4,
                 independent_review=dict(status='passed', revision=4,
                     canonical_digest=digest(canonical(draft).encode())),
                 production=dict(pipeline=PIPELINE_V2, delivery_state='ready_for_review'))
    assert transition_delivery(project, 'publish')=='published'


def test_v2_ready_for_review_saves_candidate_without_acceptance(tmp_path, skill):
    from sourceloom.durable import Queue
    from sourceloom.production import Production
    from tests.test_production import prepared

    store, _, project, bundle=prepared(tmp_path, skill)
    queue=Queue(store, pipeline=PIPELINE_V2)
    job=queue.enqueue(project['id'], bundle)
    engine=Production(store, {})
    engine.queue=queue

    def ready(candidate):
        candidate['draft']={'blocks':[dict(id='b1',unit_id='n1',kind='explanation',
            markdown='candidate',obligation_ids=[],object_ids=[],evidence=[])]}
        candidate['inventory']=project['inventory']
        candidate['plan']=project.get('plan')
        candidate['delivery_state']='ready_for_review'
        return 'ready_for_review'

    engine.step=ready
    assert engine.run_once()
    saved=store.get(project['id'])
    assert saved['draft']['blocks'][0]['markdown']=='candidate'
    assert saved['accepted_revision'] is None
    assert saved['production']['delivery_state']=='ready_for_review'
