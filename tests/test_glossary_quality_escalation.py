import json

from pydantic import BaseModel
import pytest

from sourceloom.active_composition import ActiveComposition
from sourceloom.durable import Queue
from sourceloom.production import Production
from tests.test_production import prepared


class Shape(BaseModel):
    answer: str


@pytest.fixture
def skill(tmp_path):
    root = tmp_path / 'writing-skill'
    (root / 'references').mkdir(parents=True)
    (root / 'constitution').mkdir()
    (root / 'assets').mkdir()
    (root / 'SKILL.md').write_text(
        '[format](references/format-rules.md)\n[explain](references/explanation-framework.md)\n'
        '[formula](references/formula-explanation.md)\n`constitution/principles.md`', encoding='utf-8')
    for name in ('format-rules', 'explanation-framework', 'formula-explanation'):
        (root / 'references' / f'{name}.md').write_text('test writing guidance', encoding='utf-8')
    (root / 'constitution' / 'principles.md').write_text('test principles', encoding='utf-8')
    (root / 'assets' / 'asset.bin').write_bytes(b'test asset')
    return root


def _routes():
    return {
        'provider_routes': {
            'kuafu': {'provider': 'openai-compatible', 'provider_id': 'kuafu',
                'model': 'deepseek-v4.1-flash', 'protocol': 'chat_completions',
                'base_url': 'https://api.kuafushe.cc/v1', 'enabled': True},
        },
        'provider_credentials': {'kuafu': 'kuafu-key'},
        'role_providers': {'active_write': {
            'provider': 'openai-compatible', 'provider_id': 'kuafu',
            'model': 'deepseek-v4.1-flash', 'protocol': 'chat_completions',
            'base_url': 'https://api.kuafushe.cc/v1', 'api_key': 'kuafu-key',
            'backup_provider_id': 'opencode-go'}},
        # Deliberately omit provider_id: the route identity is resolved from its host.
        'fallback_providers': {'active_write': {
            'provider': 'openai-compatible', 'model': 'deepseek-v4.1-flash',
            'protocol': 'chat_completions', 'base_url': 'https://opencode.ai/zen/go/v1',
            'api_key': 'go-key'}},
    }


def test_failed_glossary_fallback_queues_one_fresh_kuafu_primary_turn(
        tmp_path, skill, monkeypatch):
    store, _, project, bundle = prepared(tmp_path, skill)
    queue = Queue(store, pipeline='active_composition_v2')
    job = queue.enqueue(project['id'], bundle)
    session_key = 'active-write-p3'
    failed_key = session_key + '-turn-3'
    invalid = {'blocks': [{'id': 'p3-b1', 'markdown': '## 术语表\n\n- repeated definition'}]}
    failed_response = {'result': invalid, 'gaps': [], 'actions': [], 'ready_reason': 'ready'}
    session = {'round': 3, 'corrections': 3, 'short_rewrite_glossary_repair_used': True,
        'correction': {'error': 'short rewrite became glossary', 'instruction': 'Preserve this correction'},
        'resources': {'entries': {'saved': {'id': 'saved'}}}}
    checkpoints = [{'node_id': 'p1', 'draft_digest': 'checkpoint-p1'},
                   {'node_id': 'p2', 'draft_digest': 'checkpoint-p2'}]
    call = {'id': 'go-p3', 'role': 'active_write__fallback', 'status': 'completed',
        'step_key': failed_key, 'provider_id': 'opencode-go', 'channel': 'openai-compatible',
        'protocol': 'chat_completions', 'upstream_base': 'https://opencode.ai/zen/go/v1',
        'response_blob': store.blob(json.dumps({'choices': []}).encode())}
    job.update(core_chain_version=0, status='failed', stage='active_write', pending=None,
        error='短篇普通改写被扩成术语表：校验失败', calls=[call],
        results={failed_key: failed_response}, active_sessions={session_key: session},
        active_checkpoints=checkpoints, draft={'blocks': [{'id': 'p1-b1'}, {'id': 'p2-b1'}]})
    store.put_job(job)
    store.change(project['id'], lambda p: p.update(active_job=None))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='failed' WHERE id=?", (job['id'],))

    config = _routes()
    resumed = queue.retry_validation(job['id'], config)
    state = resumed['active_glossary_quality_escalations'][session_key]
    next_key = session_key + '-turn-4'
    assert resumed['status'] == 'queued'
    assert state['status'] == 'queued' and state['attempts'] == 0
    assert next_key not in resumed['results']
    assert resumed['active_sessions'][session_key]['round'] == 4
    assert resumed['active_sessions'][session_key]['previous_invalid_result'] == invalid
    assert resumed['active_sessions'][session_key]['correction'] == session['correction']
    assert resumed['active_checkpoints'] == checkpoints
    assert resumed['results'][failed_key] == failed_response

    observed = {}

    def dispatch(provider, pid, role, payload, schema, claimed, cancelled):
        observed.update(role=role, provider_id=provider.config['provider_id'],
            backup=provider.config.get('backup_provider_id'), key=claimed['current_step_key'])
        claimed['calls'].append({'id': 'kuafu-quality-attempt', 'role': role,
            'status': 'completed', 'step_key': claimed['current_step_key'],
            'provider_id': provider.config['provider_id'], 'channel': 'openai-compatible',
            'upstream_base': provider.config['base_url']})
        return {'answer': 'accepted'}

    monkeypatch.setattr('sourceloom.active_composition.Provider.call', dispatch)
    engine = ActiveComposition(Production(store, config))
    monkeypatch.setattr(engine.queue, 'cancelled', lambda *args: False)
    assert engine.call(resumed, next_key, 'active_write', {}, Shape) == {'answer': 'accepted'}
    assert observed == {'role': 'active_write', 'provider_id': 'kuafu',
        'backup': None, 'key': next_key}
    assert state['status'] == 'completed' and state['attempts'] == 1
    assert state['attempt_call_id'] == 'kuafu-quality-attempt'
    assert resumed['results'][failed_key] == failed_response
    assert resumed['active_checkpoints'] == checkpoints
    # A second lookup returns the cached result and cannot dispatch another attempt.
    assert engine.call(resumed, next_key, 'active_write', {}, Shape) == {'answer': 'accepted'}
    assert len([row for row in resumed['calls'] if row.get('id') == 'kuafu-quality-attempt']) == 1

    # Even when a later validation failure is resumed, the existing marker
    # prevents Queue.retry_validation from creating a second escalation.
    resumed.update(status='failed', error='短篇普通改写被扩成术语表：仍未修正', pending=None)
    store.put_job(resumed)
    store.change(project['id'], lambda p: p.update(active_job=None))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='failed' WHERE id=?", (job['id'],))
    resumed_again = queue.retry_validation(job['id'], config)
    assert len(resumed_again['quality_escalation_history']) == 1
    assert resumed_again['active_glossary_quality_escalations'][session_key]['attempts'] == 1


def test_unrelated_uncertain_kuafu_call_does_not_degrade_later_writer_step(
        tmp_path, skill, monkeypatch):
    store, _, project, bundle = prepared(tmp_path, skill)
    job = Queue(store, pipeline='active_composition_v2').enqueue(project['id'], bundle)
    job['calls'] = [{'id': 'earlier-uncertain', 'role': 'active_write',
        'status': 'uncertain', 'step_key': 'active-write-p1-turn-0',
        'upstream_base': 'https://api.kuafushe.cc/v1'}]
    job['transport_fallback_steps'] = {'active-write-p1-turn-0': 'earlier-uncertain'}
    observed = {}

    def dispatch(provider, pid, role, payload, schema, claimed, cancelled):
        observed.update(role=role, provider_id=provider.config.get('provider_id'))
        return {'answer': 'ok'}

    monkeypatch.setattr('sourceloom.active_composition.Provider.call', dispatch)
    engine = ActiveComposition(Production(store, _routes()))
    monkeypatch.setattr(engine.queue, 'cancelled', lambda *args: False)
    assert engine.call(job, 'active-write-p2-turn-0', 'active_write', {}, Shape) == {'answer': 'ok'}
    assert observed == {'role': 'active_write', 'provider_id': 'kuafu'}
