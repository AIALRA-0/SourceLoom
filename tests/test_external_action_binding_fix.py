import json

import pytest
from pydantic import BaseModel

from sourceloom.active_composition import ActiveComposition, PIPELINE_V2
from sourceloom.active_resources import Resources
from sourceloom.durable import Queue
from sourceloom.production import Production
from sourceloom.store import Store


class _TurnResult(BaseModel):
    value: int
    evidence_gaps: list[dict] = []


def _linked_source(*objects):
    return dict(objects=list(objects), obligations=[], resources=[], unknown=[], originals=[],
                version=1, id='source', frozen=False, digest='source-digest',
                source_url='https://origin.example/article')


def _job(job_id='binding-test'):
    return dict(id=job_id, project='project', role='active_plan', status='running',
        created=1, pipeline=PIPELINE_V2, link_contract_version=1, results={}, calls=[],
        active_sessions={}, external_resources={}, generated_resources={},
        verified_terminology=[])


def _link(source_id='src-00004', label='WSGI', target='https://wsgi.readthedocs.io/'):
    return dict(id=source_id, kind='link', text=label, locator='input/article/link[1]', target=target)


def test_gap_prefix_and_explicit_source_id_align_missing_page_action(tmp_path, monkeypatch):
    import sourceloom.network

    fetched = []

    def fetch(url, *args, **kwargs):
        fetched.append(url)
        return (b'<main><p>WSGI (Web Server Gateway Interface) is specified here.</p></main>',
                'text/html', url)

    monkeypatch.setattr(sourceloom.network, 'fetch', fetch)
    engine = ActiveComposition(Production(Store(tmp_path), {}))
    source = _linked_source(_link())
    job = _job()
    saved_action = dict(kind='page', gap_id='g1', source_id='',
                        url='https://wsgi.readthedocs.io/en/latest/what.html')
    saved_response = dict(gaps=['g1：src-00004 指向的 WSGI 需要核实正式英文全称'],
        actions=[saved_action], ready_reason='', result=None)
    calls = []

    def call(job, key, role, payload, schema):
        calls.append(key)
        if len(calls) == 1:
            return saved_response
        return dict(gaps=[], actions=[], ready_reason='Saved page is available',
                    result={'value': 1, 'evidence_gaps':[{'id':'g1'}]})

    engine.call = call
    result = engine.turn(job, 'active-plan-p1', 'active_plan', _TurnResult, source,
                         ['src-00004'], {}, lambda value, _: value)

    session = job['active_sessions']['active-plan-p1']
    receipt = session['source_binding_receipts'][0]
    action_history = next(item for item in session['action_history']
                          if item.get('source_binding_receipt'))
    assert result == {'value': 1, 'evidence_gaps':[{'id':'g1'}]}
    assert saved_response['actions'][0]['source_id'] == ''
    assert action_history['action']['source_id'] == 'src-00004'
    assert action_history['action']['url'] == saved_action['url']
    assert action_history['source_binding_receipt'] == receipt['id']
    assert receipt['gap_texts'] == saved_response['gaps']
    assert receipt['alignment']['methods'][0]['method'] == 'explicit_source_id_in_gap'
    assert fetched == ['https://wsgi.readthedocs.io/', saved_action['url']]


def test_unique_link_label_allows_name_page_and_discovery_after_saved_direct_target(tmp_path, monkeypatch):
    import sourceloom.network

    fetched = []

    def fetch(url, *args, **kwargs):
        fetched.append(url)
        if 'bing.com' in url:
            return (b'<rss><channel><item><title>PEP 333</title>'
                    b'<link>https://peps.python.org/pep-3333/</link>'
                    b'<description>Web Server Gateway Interface</description></item></channel></rss>',
                    'application/rss+xml', url)
        return (b'<main><p>Web Server Gateway Interface (WSGI) is the standard.</p></main>',
                'text/html', url)

    monkeypatch.setattr(sourceloom.network, 'fetch', fetch)
    store = Store(tmp_path)
    engine = ActiveComposition(Production(store, {}))
    source = _linked_source(_link())
    job = _job('label-binding-test')
    saved_resources = Resources(store, source)
    saved_resources.read('src-00004')
    direct_result = saved_resources.execute(dict(kind='page', url=source['objects'][0]['target']))
    fetched.clear()
    job['active_sessions']['active-plan-p1'] = dict(round=0, corrections=0,
        direct_link_prefetch_complete=True,
        direct_link_prefetch=[dict(source_id='src-00004', url=source['objects'][0]['target'],
                                   status='retrieved', result=direct_result)],
        action_history=[dict(action=dict(kind='page', url=source['objects'][0]['target']),
                             result=direct_result,
                             operation='direct_link_prefetch_before_planning')],
        action_results=[direct_result], resources=saved_resources.state)
    saved_response = dict(gaps=['gap-p1-wsgi-name'],
        actions=[
            dict(kind='page', gap_id='gap-p1-wsgi-name', source_id='',
                 url='https://peps.python.org/pep-3333/'),
            dict(kind='search', gap_id='gap-p1-wsgi-name', source_id='',
                 query='WSGI Web Server Gateway Interface'),
        ], ready_reason='', result=None)
    responses = [saved_response,
        dict(gaps=[], actions=[], ready_reason='Name evidence saved',
             result={'value': 2, 'evidence_gaps':[{'id':'gap-p1-wsgi-name'}]})]
    calls = []

    def call(job, key, role, payload, schema):
        calls.append(key)
        return responses[len(calls)-1]

    engine.call = call
    result = engine.turn(job, 'active-plan-p1', 'active_plan', _TurnResult, source,
                         ['src-00004'], {}, lambda value, _: value)

    session = job['active_sessions']['active-plan-p1']
    supplemental = [item for item in session['action_history'] if item.get('source_binding_receipt')]
    assert result == {'value': 2, 'evidence_gaps':[{'id':'gap-p1-wsgi-name'}]}
    assert [item['action']['source_id'] for item in supplemental] == ['src-00004', 'src-00004']
    assert all(item['source_binding_receipt'] for item in supplemental)
    assert session['external_action_counts'] == {'search': 1, 'open': 1}
    assert 'https://wsgi.readthedocs.io/' not in fetched
    assert 'https://peps.python.org/pep-3333/' in fetched
    assert any('bing.com/search' in url for url in fetched)
    assert all(item['source_id'] == '' for item in saved_response['actions'])


def test_ambiguous_link_label_does_not_bind_or_fetch(tmp_path, monkeypatch):
    import sourceloom.network

    monkeypatch.setattr(sourceloom.network, 'fetch',
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('must not fetch')))
    engine = ActiveComposition(Production(Store(tmp_path), {}))
    source = _linked_source(
        _link('src-00004', 'WSGI', 'https://one.example/'),
        _link('src-00005', 'WSGI', 'https://two.example/'))
    job = _job('ambiguous-binding-test')
    job['link_contract_version'] = 0
    engine.call = lambda *args: dict(gaps=['g1：WSGI 的正式英文全称'], actions=[
        dict(kind='page', gap_id='g1', source_id='', url='https://peps.python.org/pep-3333/')],
        ready_reason='', result=None)

    with pytest.raises(ValueError, match='证据不足或存在歧义'):
        engine.turn(job, 'active-plan-p1', 'active_plan', _TurnResult, source,
                    ['src-00004', 'src-00005'], {}, lambda value, _: value)


@pytest.mark.parametrize('gap,url', [
    ('g1：src-00004 的 WSGI 正式英文名称', 'https://peps.python.org/pep-3333/'),
    ('g1：WSGI 的正式英文名称', 'https://peps.python.org/pep-3333/'),
    ('g1：核实正式名称', 'https://wsgi.readthedocs.io/'),
])
def test_supplied_source_id_conflicting_with_gap_label_or_direct_target_is_rejected(
        tmp_path, monkeypatch, gap, url):
    import sourceloom.network

    monkeypatch.setattr(sourceloom.network, 'fetch',
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('must not fetch')))
    engine = ActiveComposition(Production(Store(tmp_path), {}))
    source = _linked_source(
        _link('src-00004', 'WSGI', 'https://wsgi.readthedocs.io/'),
        _link('src-00005', 'Werkzeug', 'https://werkzeug.palletsprojects.com/'))
    job = _job('contradictory-binding-test')
    job['link_contract_version'] = 0
    action = dict(kind='page', gap_id='g1', source_id='src-00005', url=url)
    engine.call = lambda *args: dict(gaps=[gap], actions=[action], ready_reason='', result=None)

    with pytest.raises(ValueError, match='独立来源线索冲突'):
        engine.turn(job, 'active-plan-p1', 'active_plan', _TurnResult, source,
                    ['src-00004', 'src-00005'], {}, lambda value, _: value)


def test_valid_supplied_source_id_is_kept_without_independent_identity_hint(tmp_path, monkeypatch):
    import sourceloom.network

    fetched = []

    def fetch(url, *args, **kwargs):
        fetched.append(url)
        return b'<main><p>Saved supplemental page.</p></main>', 'text/html', url

    monkeypatch.setattr(sourceloom.network, 'fetch', fetch)
    engine = ActiveComposition(Production(Store(tmp_path), {}))
    source = _linked_source(dict(id='src-00005', kind='text', text='Assigned text',
        locator='input/article/text[1]'))
    job = _job('explicit-binding-test')
    action = dict(kind='page', gap_id='g1', source_id='src-00005',
                  url='https://example.org/supplement')
    responses = [dict(gaps=['g1：补充缺失背景'], actions=[action], ready_reason='', result=None),
        dict(gaps=[], actions=[], ready_reason='Supplement saved',
             result={'value': 4, 'evidence_gaps':[{'id':'g1'}]})]
    engine.call = lambda job, key, role, payload, schema: responses.pop(0)

    result = engine.turn(job, 'active-plan-p1', 'active_plan', _TurnResult, source,
                         ['src-00005'], {}, lambda value, _: value)

    history = job['active_sessions']['active-plan-p1']['action_history']
    assert result == {'value': 4, 'evidence_gaps':[{'id':'g1'}]}
    assert history[-1]['action']['source_id'] == 'src-00005'
    assert history[-1]['source_binding_receipt'] is None
    assert 'source_binding_receipts' not in job['active_sessions']['active-plan-p1']
    assert fetched == ['https://example.org/supplement']


def test_missing_plan_gap_is_repaired_with_saved_declaration_and_exact_id(tmp_path, monkeypatch):
    engine = ActiveComposition(Production(Store(tmp_path), {}))
    source = _linked_source(dict(id='src-00005', kind='text', locator='input/article/text[1]',
                                 text='A framework is mentioned.'))
    job = _job('missing-plan-gap-test')
    responses = [
        dict(gaps=['gap-p1-framework：verify the framework name'], actions=[
            dict(kind='search', gap_id='gap-p1-framework', source_id='src-00005',
                 query='official framework name')], ready_reason='', result=None),
        dict(gaps=[], actions=[], ready_reason='ready',
             result=dict(value=1, evidence_gaps=[])),
        dict(gaps=[], actions=[], ready_reason='ready',
             result=dict(value=1, evidence_gaps=[dict(id='gap-p1-framework',
                 obligation_id='p1-o1', source_id='src-00005', missing='framework name',
                 reason='The source leaves the name unexplained', allowed_sources=['primary'],
                 stop_condition='one exact official name quote', status='unresolved')]))]
    requests = []

    def execute(self, action):
        return dict(status='retrieved', action=action)

    monkeypatch.setattr(Resources, 'execute', execute)

    def call(job, key, role, payload, schema):
        requests.append(payload)
        return responses.pop(0)

    engine.call = call
    result = engine.turn(job, 'active-plan-p1', 'active_plan', _TurnResult, source,
                         ['src-00005'], {}, lambda value, _: value)

    declaration = dict(id='gap-p1-framework',
        text='gap-p1-framework\uff1averify the framework name',
        description='verify the framework name', source_id='src-00005')
    assert result['evidence_gaps'][0]['id'] == 'gap-p1-framework'
    assert requests[1]['declared_evidence_gaps'] == [declaration]
    assert requests[2]['declared_evidence_gaps'] == [declaration]
    correction = requests[2]['protocol_correction']
    assert 'gap-p1-framework' in correction['error']
    assert correction['error'].startswith('规划没有保存本轮声明的 EvidenceGap ID：')
    assert 'matching current SourceObligation' in correction['instruction'], correction


def test_search_cannot_skip_an_unsaved_direct_target(tmp_path, monkeypatch):
    import sourceloom.network

    monkeypatch.setattr(sourceloom.network, 'fetch',
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('must not fetch')))
    engine = ActiveComposition(Production(Store(tmp_path), {}))
    source = _linked_source(_link())
    job = _job('direct-first-test')
    job['link_contract_version'] = 0
    engine.call = lambda *args: dict(gaps=['g1：WSGI 正式英文全称'], actions=[
        dict(kind='search', gap_id='g1', source_id='', query='WSGI official full name')],
        ready_reason='', result=None)

    with pytest.raises(ValueError, match='先读取并保存目标页'):
        engine.turn(job, 'active-plan-p1', 'active_plan', _TurnResult, source,
                    ['src-00004'], {}, lambda value, _: value)


def _make_skill(root):
    (root / 'references').mkdir(parents=True)
    (root / 'constitution').mkdir()
    (root / 'SKILL.md').write_text(
        '[format](references/format-rules.md)\n'
        '[explain](references/explanation-framework.md)\n'
        '[formula](references/formula-explanation.md)\n'
        '`constitution/principles.md`', encoding='utf-8')
    for name in ('format-rules', 'explanation-framework', 'formula-explanation'):
        (root / 'references' / f'{name}.md').write_text('FULL-' + name, encoding='utf-8')
    (root / 'constitution' / 'principles.md').write_text('FULL-CONSTITUTION', encoding='utf-8')
    return root


def test_retry_validation_reuses_saved_action_response_without_new_model_call(tmp_path, monkeypatch):
    import sourceloom.network
    from tests.test_production import prepared

    fetched = []

    def fetch(url, *args, **kwargs):
        fetched.append(url)
        return b'<main><p>WSGI direct and supplemental evidence.</p></main>', 'text/html', url

    monkeypatch.setattr(sourceloom.network, 'fetch', fetch)
    store, queue, project, bundle = prepared(tmp_path, _make_skill(tmp_path / 'skill'))
    job = queue.enqueue(project['id'], bundle)
    source = _linked_source(_link())
    step_key = 'active-plan-p1-turn-0'
    saved_response = dict(gaps=['gap-p1-wsgi-name：src-00004 WSGI 的正式英文名称待核实'],
        actions=[dict(kind='page', gap_id='gap-p1-wsgi-name', source_id='',
                      url='https://peps.python.org/pep-3333/')],
        ready_reason='', result=None)
    response_blob = store.blob(json.dumps(saved_response, ensure_ascii=False).encode())
    job.update(status='failed', stage='active_plan', pipeline=PIPELINE_V2,
        link_contract_version=1, source=source, active_sessions={}, results={step_key: saved_response},
        calls=[dict(id='saved-plan-call', role='active_plan', status='completed',
                    step_key=step_key, response_blob=response_blob)], pending=None,
        error='v2 外部检索必须绑定当前分组的原文义务来源', finished=1)
    store.put_job(job)
    store.change(project['id'], lambda value: value.update(active_job=None))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='failed' WHERE id=?", (job['id'],))

    resumed = queue.retry_validation(job['id'])
    assert resumed['status'] == 'queued'
    assert [call['id'] for call in resumed['calls']] == ['saved-plan-call']

    engine = ActiveComposition(Production(store, {}))
    cached = []
    original_call = engine.call

    def call(job, key, role, payload, schema):
        if key in job['results']:
            cached.append(key)
            return original_call(job, key, role, payload, schema)
        return dict(gaps=[], actions=[], ready_reason='cached response was processed',
                    result={'value': 3, 'evidence_gaps':[{'id':'gap-p1-wsgi-name'}]})

    engine.call = call
    value = engine.turn(resumed, 'active-plan-p1', 'active_plan', _TurnResult, source,
                        ['src-00004'], {}, lambda result, _: result)
    assert value == {'value': 3, 'evidence_gaps':[{'id':'gap-p1-wsgi-name'}]}
    assert cached == [step_key]
    assert len(resumed['calls']) == 1
    assert len(resumed['active_sessions']['active-plan-p1']['source_binding_receipts']) == 1
    assert fetched == ['https://wsgi.readthedocs.io/', 'https://peps.python.org/pep-3333/']
