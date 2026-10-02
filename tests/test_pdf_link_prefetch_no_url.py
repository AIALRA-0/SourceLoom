from pydantic import BaseModel, Field

from sourceloom.active_composition import ActiveComposition
from sourceloom.active_resources import Resources
from sourceloom.production import Production
from sourceloom.store import Store


class _PlanResult(BaseModel):
    value: int
    evidence_gaps: list[dict] = Field(default_factory=list)


def test_pdf_link_prefetch_keeps_hostless_http_target_unavailable(tmp_path, monkeypatch):
    target = 'http:/reference.example/document'
    source = dict(source_url=None, objects=[dict(
        id='pdf-link', kind='link', text='Reference',
        locator='source.pdf/page[3]/annotation', target=target)],
        resources=[], unknown=[], originals=[], version=1, id='pdf-source',
        frozen=False, digest='pdf-source-digest')
    job = dict(id='pdf-link-prefetch-test', project='project', role='active_plan',
        status='running', created=1, pipeline='active_composition_v2',
        link_contract_version=1, results={}, calls=[], active_sessions={},
        external_resources={}, generated_resources={}, verified_terminology=[])
    store = Store(tmp_path)
    engine = ActiveComposition(Production(store, {'evidence_open_limit': 1}))
    fetched = []

    def record_unavailable(resources, action, allow_external=True):
        fetched.append(action)
        return dict(kind='page', url=action['url'], status='unavailable',
                    reason='HTTP target has no hostname; no page was fetched')

    monkeypatch.setattr(Resources, 'execute', record_unavailable)
    calls = []

    def saved_turn(job, key, role, payload, schema):
        calls.append(payload)
        assert payload['previous_action_results'][0]['status'] == 'unavailable'
        return dict(gaps=[], actions=[], ready_reason='source link retained',
                    result=dict(value=1, evidence_gaps=[]))

    monkeypatch.setattr(engine, 'call', saved_turn)

    result = engine.turn(job, 'active-plan-p3', 'active_plan', _PlanResult,
        source, ['pdf-link'], {}, lambda value, _: value)

    assert result == dict(value=1, evidence_gaps=[])
    assert source['objects'][0]['target'] == target
    assert fetched == [dict(kind='page', resource_id='', url=target)]
    assert len(calls) == 1
    prefetch = job['active_sessions']['active-plan-p3']['direct_link_prefetch'][0]
    assert prefetch['status'] == 'unavailable'
    assert prefetch['url'] == target
    assert not any(entry.get('kind') == 'external'
                   for entry in job['active_sessions']['active-plan-p3']['resources']['entries'].values())
