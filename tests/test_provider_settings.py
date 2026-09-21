import json

import httpx
import pytest
from fastapi.testclient import TestClient

from sourceloom.config import load_config
from sourceloom.money import no_cache_upper_bound, usage_cost
from sourceloom.provider_settings import (
    PROTOCOL_REST_SEARCH,
    PROTOCOL_RESPONSES,
    activate_staged_route,
    import_readweave_registry,
    normalize_usage,
    probe_staged_route,
    public_provider_metadata,
    save_private_settings,
    stage_route,
    stage_imported_routes,
)
from sourceloom.providers import Provider
from sourceloom.providers import apply_route_override
from sourceloom.app import create_app
from tests.test_production import prepared


@pytest.fixture
def skill(tmp_path):
    root = tmp_path / "writing-skill"
    (root / "references").mkdir(parents=True)
    (root / "constitution").mkdir()
    (root / "assets").mkdir()
    (root / "SKILL.md").write_text(
        "[format](references/format-rules.md)\n[explain](references/explanation-framework.md)\n"
        "[formula](references/formula-explanation.md)\n`constitution/principles.md`",
        encoding="utf-8",
    )
    for name in ("format-rules", "explanation-framework", "formula-explanation"):
        (root / "references" / f"{name}.md").write_text(f"FULL-{name}\nTAIL-{name}", encoding="utf-8")
    (root / "constitution/principles.md").write_text("FULL-CONSTITUTION", encoding="utf-8")
    return root


def test_usage_contract_separates_cached_and_uncached_input():
    usage = normalize_usage({
        "input_tokens": 100,
        "input_tokens_details": {"cached_tokens": 25},
        "output_tokens": 10,
    })
    assert usage.complete
    assert usage.cache_hit_input_tokens == 25
    assert usage.cache_miss_input_tokens == 75
    rates = {"input": 1.62, "cached_input": .054, "output": 4.86}
    assert usage_cost(usage.as_dict(), rates) == pytest.approx(.00017145)
    assert no_cache_upper_bound(usage.as_dict(), rates) == pytest.approx(.0002106)


def test_usage_contract_accepts_model_router_camel_case_counts():
    usage = normalize_usage({"inputTokens": 80, "cacheHitInputTokens": 20, "outputTokens": 12})
    assert usage.as_dict() == {
        "input_tokens": 80,
        "cache_hit_input_tokens": 20,
        "cache_miss_input_tokens": 60,
        "output_tokens": 12,
        "complete": True,
    }


def test_role_route_does_not_inherit_another_provider_protocol_or_identity():
    active={"provider_id":"kuafu","protocol":"responses","endpoint":"/responses",
            "base_url":"https://api.kuafushe.cc/v1","model":"deepseek-v4.1-flash"}
    visual={"provider":"openai-compatible","base_url":"https://opencode.ai/zen/go/v1",
            "model":"deepseek-v4-flash-vision-exp","api_key":"synthetic-key"}
    route=apply_route_override(active,visual)
    assert route["provider_id"]=="opencode-go"
    assert route["protocol"]=="chat_completions"
    assert "endpoint" not in route


def test_load_config_restores_activated_route_for_detached_worker(tmp_path, monkeypatch):
    config_file=tmp_path/'config.json'
    config_file.write_text('{}',encoding='utf-8')
    data=tmp_path/'data'
    monkeypatch.setenv('SOURCELOOM_CONFIG',str(config_file))
    monkeypatch.setenv('SOURCELOOM_DATA',str(data))
    config=load_config()
    config=stage_route(config,{
        'provider_id':'kuafu','provider':'openai-compatible','model':'deepseek-test',
        'protocol':'responses','base_url':'https://api.example/v1','endpoint':'/responses',
        'pricing_currency':'CNY','pricing_cny':{'cached_input':.1,'input':1,'output':2},
    },'synthetic-key')
    config=probe_staged_route(config,'kuafu')
    config=activate_staged_route(config,'kuafu')
    save_private_settings(data,config)
    reloaded=load_config()
    assert reloaded['provider_id']=='kuafu'
    assert reloaded['protocol']=='responses'
    assert reloaded['role_providers']['active_plan']['provider_id']=='kuafu'
    assert reloaded['api_key']=='synthetic-key'


def test_activated_route_drops_stale_role_credential_and_transport():
    config=load_config()|{
        'provider_routes':{'kuafu':{
            'provider_id':'kuafu','provider':'openai-compatible','model':'deepseek-test',
            'protocol':'chat_completions','base_url':'https://api.kuafushe.cc/v1',
        }},
        'provider_credentials':{'kuafu':'kuafu-key'},
        'role_providers':{'active_plan':{
            'provider_id':'old-router','model':'old-model','protocol':'responses',
            'base_url':'http://127.0.0.1:13210/v1','api_key':'old-router-key',
            'billing_mode':'subscription','max_output_tokens':4321,
        }},
    }
    activated=activate_staged_route(config,'kuafu')
    role=activated['role_providers']['active_plan']
    assert role['provider_id']=='kuafu' and role['protocol']=='chat_completions'
    assert role['max_output_tokens']==4321
    assert 'api_key' not in role and 'billing_mode' not in role
    effective=activated|role
    assert effective['api_key']=='kuafu-key'


def test_explicit_role_overrides_survive_activated_settings_route(tmp_path, monkeypatch):
    config_file=tmp_path/'config.json'
    config_file.write_text(json.dumps({
        'role_provider_overrides': {
            'active_plan': {
                'provider':'openai-compatible','provider_id':'subscription-router',
                'model':'chatgpt-web.auto','protocol':'responses',
                'base_url':'http://127.0.0.1:13210/v1','endpoint':'/responses',
                'billing_mode':'subscription',
            }
        }
    }),encoding='utf-8')
    data=tmp_path/'data'
    monkeypatch.setenv('SOURCELOOM_CONFIG',str(config_file))
    monkeypatch.setenv('SOURCELOOM_DATA',str(data))
    config=load_config()
    config=stage_route(config,{
        'provider_id':'kuafu','provider':'openai-compatible','model':'deepseek-test',
        'protocol':'responses','base_url':'https://api.example/v1','endpoint':'/responses',
    },'synthetic-key')
    config=activate_staged_route(config,'kuafu')
    assert config['role_providers']['active_plan']['provider_id']=='subscription-router'
    assert config['role_providers']['active_write']['provider_id']=='kuafu'
    save_private_settings(data,config)
    reloaded=load_config()
    assert reloaded['provider_id']=='kuafu'
    assert reloaded['role_providers']['active_write']['provider_id']=='kuafu'
    assert reloaded['role_providers']['active_plan']['provider_id']=='subscription-router'
    assert reloaded['role_providers']['active_plan']['billing_mode']=='subscription'


def test_display_multiplier_is_not_used_by_cost_calculation():
    rates = {"input": 1.62, "cached_input": .054, "output": 4.86, "display_multiplier": .15}
    usage = {"input_tokens": 100, "output_tokens": 10}
    assert usage_cost(usage, rates) == pytest.approx(.0002106)


def test_readweave_registry_import_is_file_or_payload_only():
    routes = import_readweave_registry([{
        "id": "kuafu",
        "baseUrl": "https://api.example/v1",
        "endpoint": "/responses",
        "requestProtocol": "responses",
        "model": "deepseek-v4.1-flash",
        "enabled": False,
        "pricing": {
            "currency": "CNY",
            "cacheHitInputPerMillion": .054,
            "cacheMissInputPerMillion": 1.62,
            "outputPerMillion": 4.86,
        },
    }])
    assert routes["kuafu"].protocol == PROTOCOL_RESPONSES
    assert routes["kuafu"].price_snapshot.snapshot_id
    assert "api_key" not in json.dumps(routes["kuafu"].public())


def test_readweave_search_routes_keep_rest_shape_and_private_credentials():
    routes = import_readweave_registry([{
        "id": "tinyfish",
        "kind": "search",
        "baseUrl": "https://search.example",
        "endpoint": "/v1/search",
        "requestProtocol": "rest-search",
        "authType": "bearer",
        "modelParameters": {"mode": "turbo", "maxResults": 5},
        "searchPerRequest": 2,
    }])
    route = routes["tinyfish"]
    assert route.protocol == PROTOCOL_REST_SEARCH
    assert route.route_kind == "search"
    assert route.auth_type == "bearer"
    assert route.model_parameters == {"mode": "turbo", "maxResults": 5}
    assert route.search_per_request == 2
    draft = stage_imported_routes({}, routes, {"tinyfish": "search-secret"})
    assert draft["provider_routes"]["tinyfish"]["protocol"] == "rest_search"
    assert draft["provider_routes"]["tinyfish"]["endpoint"] == "/v1/search"
    assert draft["search_routes"]["tinyfish"]["base_url"] == "https://search.example"
    assert draft["search_routes"]["tinyfish"]["model_parameters"]["maxResults"] == 5
    assert "api_key" not in json.dumps(draft["search_routes"])
    assert draft["provider_credentials"]["tinyfish"] == "search-secret"


def test_public_provider_metadata_never_returns_server_key():
    metadata = public_provider_metadata({
        "provider": "openai-compatible",
        "provider_id": "kuafu",
        "model": "deepseek-v4.1-flash",
        "protocol": "responses",
        "base_url": "https://api.example/v1",
        "api_key": "server-only-secret",
        "pricing_cny": {"input": 1.62, "cached_input": .054, "output": 4.86},
        "role_providers": {"writer": {"api_key": "writer-secret", "model": "other"}},
    })
    serialized = json.dumps(metadata)
    assert "server-only-secret" not in serialized
    assert "writer-secret" not in serialized
    assert metadata["active"]["has_credentials"] is True


def test_imported_route_uses_a_private_credential_and_explicit_activation_window():
    routes = import_readweave_registry([{
        "id": "kuafu", "baseUrl": "https://api.example/v1", "requestProtocol": "responses",
        "endpoint": "/responses", "model": "deepseek-v4.1-flash", "pricing": {
            "currency": "CNY", "cacheHitInputPerMillion": .054,
            "cacheMissInputPerMillion": 1.62, "outputPerMillion": 4.86,
        },
    }])
    draft = stage_imported_routes({"provider": "manual", "api_key": "old-secret"}, routes, {"kuafu": "new-secret"})
    assert draft["provider_settings_state"] == "draft"
    assert draft["provider_routes"]["kuafu"].get("api_key") is None
    assert draft["provider_credentials"]["kuafu"] == "new-secret"
    active = activate_staged_route(draft, "kuafu")
    assert active["provider_id"] == "kuafu"
    assert active["protocol"] == "responses"
    assert active["api_key"] == "new-secret"
    assert public_provider_metadata(draft)["staged"]["kuafu"]["has_credentials"] is True


def test_settings_api_persists_draft_probe_active_without_returning_key(tmp_path):
    config = load_config() | {"data_dir": str(tmp_path), "auth_mode": "local"}
    registry = {"providers": [{
        "id": "kuafu", "baseUrl": "https://api.example/v1", "endpoint": "/responses",
        "requestProtocol": "responses", "model": "deepseek-v4.1-flash", "enabled": False,
        "role": "primary", "pricing": {"currency": "CNY", "cacheHitInputPerMillion": .054,
        "cacheMissInputPerMillion": 1.62, "outputPerMillion": 4.86},
    }]}
    headers = {"x-sourceloom": "1"}
    with TestClient(create_app(config)) as client:
        imported = client.post("/api/admin/settings/import-readweave", json=registry | {"credentials": {"kuafu": "secret"}}, headers=headers)
        assert imported.status_code == 200
        assert imported.json()["state"] == "draft"
        assert "secret" not in json.dumps(imported.json())
        probed = client.post("/api/admin/settings/probe", json={"provider_id": "kuafu"}, headers=headers)
        assert probed.status_code == 200
        assert probed.json()["state"] == "probed"
        activated = client.post("/api/admin/settings/activate", json={"provider_id": "kuafu"}, headers=headers)
        assert activated.status_code == 200
        assert activated.json()["state"] == "active"
        assert "secret" not in json.dumps(activated.json())
        assert client.get("/api/config").json()["provider_metadata"]["active"]["provider_id"] == "kuafu"
    assert (tmp_path / "provider-settings.json").is_file()
    with TestClient(create_app(load_config() | {"data_dir": str(tmp_path), "auth_mode": "local"})) as restarted:
        settings = restarted.get("/api/admin/settings").json()
        assert settings["state"] == "active"
        assert settings["provider_metadata"]["active"]["provider_id"] == "kuafu"
        assert "secret" not in json.dumps(settings)


def test_settings_window_payload_round_trips_and_server_side_import_never_returns_keys(tmp_path):
    registry_path=tmp_path/'readweave-registry.private.json'
    registry_path.write_text(json.dumps({"providers":[{
        "id":"kuafu","kind":"model","baseUrl":"https://api.example/v1","endpoint":"/responses",
        "requestProtocol":"responses","model":"deepseek-v4.1-flash","enabled":True,"priority":10,
        "pricing":{"currency":"CNY","cacheHitInputPerMillion":.054,"cacheMissInputPerMillion":1.62,
                   "outputPerMillion":4.86,"displayMultiplier":.15}},
        {"id":"tinyfish","kind":"search","baseUrl":"https://search.example","endpoint":"/",
         "requestProtocol":"rest-search","authType":"x-api-key","enabled":True,"priority":10,
         "pricing":{"currency":"USD","searchPerRequest":0}}],
        "credentials":{"kuafu":"model-secret","tinyfish":"search-secret"}}),encoding='utf-8')
    config=load_config()|{"data_dir":str(tmp_path/'data'),"auth_mode":"local",
                          "readweave_registry_path":str(registry_path)}
    headers={"x-sourceloom":"1"}
    with TestClient(create_app(config)) as client:
        imported=client.post('/api/admin/settings/import-readweave',json={},headers=headers)
        assert imported.status_code==200
        assert imported.json()['settings']['interface']['primary_provider']=='kuafu'
        assert 'secret' not in json.dumps(imported.json())
        payload={"interface":{"primary_provider":"kuafu","providers":[{
            "id":"kuafu","protocol":"responses","model":"deepseek-v4.1-flash","priority":10}]},
            "retrieval":{"search_order":["tinyfish","octen"],"query_limit":1,"open_limit":3},
            "generation":{"content_patch_limit":1,"format_patch_limit":2,
                          "heading_numbering":"numbered","media_collapsed":False},
            "cost":{"cache_hit_input":.054,"cache_miss_input":1.62,"output":4.86,
                    "display_multiplier":.15,"fx_rate":7.2},
            "readweave":{"profile_id":"rw-low-cost","profile_digest":"digest"}}
        saved=client.put('/api/admin/settings',json=payload,headers=headers)
        assert saved.status_code==200 and saved.json()['state']=='draft'
        checked=client.post('/api/admin/settings/probe',json=payload,headers=headers)
        assert checked.status_code==200 and checked.json()['state']=='probed'
        active=client.post('/api/admin/settings/activate',json=payload,headers=headers)
        assert active.status_code==200 and active.json()['state']=='active'
        view=client.get('/api/admin/settings',headers=headers).json()
        assert view['settings']['retrieval']=={"search_order":["tinyfish","octen"],"query_limit":1,"open_limit":3}
        assert view['settings']['generation']['heading_numbering']=='numbered'
        assert view['settings']['generation']['media_collapsed'] is False
        assert 'secret' not in json.dumps(view)
    restarted_config=load_config()|{"data_dir":str(tmp_path/'data'),"auth_mode":"local",
                                    "readweave_registry_path":str(registry_path)}
    with TestClient(create_app(restarted_config)) as restarted:
        view=restarted.get('/api/admin/settings',headers=headers).json()
        assert view['settings']['retrieval']['query_limit']==1
        assert view['settings']['generation']['heading_numbering']=='numbered'


def test_responses_transport_normalizes_usage_and_keeps_wire_protocol(tmp_path, skill, monkeypatch):
    store, queue, project, bundle = prepared(tmp_path, skill)
    job = queue.enqueue(project["id"], bundle)
    captured = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            captured.append((url, kwargs))
            return httpx.Response(200, json={
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"ok":true}'}]}],
                "usage": {"input_tokens": 100, "output_tokens": 10,
                           "input_tokens_details": {"cached_tokens": 25}},
            })

    monkeypatch.setattr("sourceloom.providers.httpx.AsyncClient", Client)
    config = load_config() | {
        "provider": "openai-compatible",
        "provider_id": "kuafu",
        "protocol": "responses",
        "endpoint": "/responses",
        "base_url": "https://api.example/v1",
        "api_key": "synthetic-key",
        "pricing_cny": {"input": 1.62, "cached_input": .054, "output": 4.86},
        "writing_skill_dir": str(skill),
        "max_input_bytes": 100000,
    }
    assert Provider(store, config).call(project["id"], "writer", {"source": "text"}, {"type": "object"}, job) == {"ok": True}
    assert captured[0][0] == "https://api.example/v1/responses"
    wire = captured[0][1]["json"]
    assert "instructions" in wire and "input" in wire and "messages" not in wire
    assert wire["reasoning"] == {"effort": "none"}
    assert wire["temperature"] == 0 and wire["stream"] is False
    row = store.costs(project["id"])[0]
    assert row["body"]["protocol"] == "responses"
    assert row["body"]["cache_hit_input_tokens"] == 25
    assert row["body"]["cache_miss_input_tokens"] == 75
    assert row["body"]["actual_cny"] == pytest.approx(.00017145)


def test_kuafu_transient_401_replays_once_only_after_catalog_revalidation(tmp_path, skill, monkeypatch):
    store, queue, project, bundle = prepared(tmp_path, skill)
    job = queue.enqueue(project["id"], bundle)

    class Response:
        def __init__(self, status, body):
            self.status_code=status
            self.content=json.dumps(body).encode()
            self._body=body
        def json(self):return self._body

    returned=[Response(401,{"code":"INVALID_API_KEY"}),Response(200,{
        "status":"completed","output_text":'{"ok":true}',
        "usage":{"input_tokens":10,"output_tokens":2}})]
    calls=[]
    monkeypatch.setattr("sourceloom.providers.post_before_deadline",
                        lambda *args:(calls.append(args) or returned.pop(0)))
    monkeypatch.setattr("sourceloom.providers.httpx.get",
                        lambda *args,**kwargs:Response(200,{"data":[]}))
    config=load_config()|{
        "provider":"openai-compatible","provider_id":"kuafu","protocol":"responses",
        "endpoint":"/responses","base_url":"https://api.kuafushe.cc/v1",
        "api_key":"synthetic-key","writing_skill_dir":str(skill),"max_input_bytes":100000,
    }
    assert Provider(store,config).call(project["id"],"writer",{"source":"text"},{"type":"object"},job)=={"ok":True}
    assert len(calls)==2
    assert job["calls"][0]["auth_revalidated"] is True
    assert job["calls"][0]["dispatch_attempts"]==2


def test_large_kuafu_request_uses_configured_official_route_before_dispatch(tmp_path, skill, monkeypatch):
    store, queue, project, bundle = prepared(tmp_path, skill)
    job = queue.enqueue(project["id"], bundle)
    calls=[]

    class Response:
        status_code=200
        content=b'{}'
        def json(self):
            return {"status":"completed","output_text":'{"ok":true}',
                    "usage":{"input_tokens":10,"output_tokens":2}}

    monkeypatch.setattr("sourceloom.providers.post_before_deadline",
                        lambda url,*args:(calls.append(url) or Response()))
    config=load_config()|{
        "provider":"openai-compatible","provider_id":"kuafu","protocol":"responses",
        "endpoint":"/responses","base_url":"https://api.kuafushe.cc/v1",
        "api_key":"kuafu-key","writing_skill_dir":str(skill),"max_input_bytes":100000,
        "kuafu_safe_input_bytes":1,
        "quota_fallback":{"provider":"openai-compatible","provider_id":"deepseek-official",
            "protocol":"responses","endpoint":"/responses","base_url":"https://api.deepseek.com/v1",
            "api_key":"official-key"},
    }
    result=Provider(store,config).call(project["id"],"writer",{"source":"text"},{"type":"object"},job)
    assert result=={"ok":True}
    assert calls==["https://api.deepseek.com/v1/responses"]
    assert len(job["calls"])==1 and job["calls"][0]["provider_id"]=="deepseek-official"
    assert job["route_switches"][0]["reason"]=="kuafu_verified_request_size_boundary"


def test_active_plan_uses_official_compatibility_route_without_kuafu_attempt(tmp_path, skill, monkeypatch):
    store, queue, project, bundle = prepared(tmp_path, skill)
    job = queue.enqueue(project["id"], bundle)
    calls=[]

    class Response:
        status_code=200
        content=b'{}'
        def json(self):
            return {"status":"completed","output_text":'{"ok":true}',
                    "usage":{"input_tokens":10,"output_tokens":2}}

    monkeypatch.setattr("sourceloom.providers.post_before_deadline",
                        lambda url,*args:(calls.append(url) or Response()))
    config=load_config()|{
        "provider":"openai-compatible","provider_id":"kuafu","protocol":"responses",
        "endpoint":"/responses","base_url":"https://api.kuafushe.cc/v1",
        "api_key":"kuafu-key","writing_skill_dir":str(skill),"max_input_bytes":100000,
        "kuafu_safe_input_bytes":999999,"kuafu_fallback_roles":["active_plan"],
        "quota_fallback":{"provider":"openai-compatible","provider_id":"deepseek-official",
            "protocol":"responses","endpoint":"/responses","base_url":"https://api.deepseek.com/v1",
            "api_key":"official-key"},
    }
    result=Provider(store,config).call(project["id"],"active_plan",{"source":"text"},{"type":"object"},job)
    assert result=={"ok":True}
    assert calls==["https://api.deepseek.com/v1/responses"]
    assert job["calls"][0]["provider_id"]=="deepseek-official"
    assert job["route_switches"][0]["reason"]=="kuafu_role_protocol_compatibility"


def test_model_router_responses_keeps_schema_out_of_prompt_and_parses_object(tmp_path, skill, monkeypatch):
    store, queue, project, bundle = prepared(tmp_path, skill)
    job = queue.enqueue(project["id"], bundle)
    captured=[]

    class Response:
        status_code=200
        content=b'{}'
        def json(self):
            return {"status":"succeeded","output":{"ok":True},
                    "usage":{"inputTokens":10,"outputTokens":2,"measurementStatus":"measured"}}

    def post(url,headers,body,deadline):
        captured.append((url,headers,body))
        return Response()
    monkeypatch.setattr("sourceloom.providers.post_before_deadline",post)
    schema={"type":"object","properties":{"ok":{"type":"boolean","description":"SCHEMA_ONLY_MARKER"}},
            "required":["ok"],"additionalProperties":False}
    config=load_config()|{
        "provider":"openai-compatible","provider_id":"model-router-chatgpt",
        "protocol":"responses","endpoint":"/responses","structured_output":"deepseek_strict_tool",
        "base_url":"http://127.0.0.1:13210/v1","model":"chatgpt-web.auto",
        "api_key":"router-key","writing_skill_dir":str(skill),"max_input_bytes":100000,
        "billing_mode":"subscription","max_output_tokens":1000,
    }
    result=Provider(store,config).call(project["id"],"active_plan",{"source":"text"},schema,job)
    assert result=={"ok":True}
    url,headers,wire=captured[0]
    assert url=="http://127.0.0.1:13210/v1/responses"
    assert headers["Idempotency-Key"]==job["calls"][0]["id"]
    assert "SCHEMA_ONLY_MARKER" not in wire["instructions"]
    assert wire["text"]["format"]["schema"]["properties"]["ok"]["description"]=="SCHEMA_ONLY_MARKER"
    assert "reasoning" not in wire and "temperature" not in wire


def test_model_router_codex_responses_uses_supported_effort_and_idempotency(tmp_path, skill, monkeypatch):
    store, queue, project, bundle = prepared(tmp_path, skill)
    job = queue.enqueue(project["id"], bundle)
    captured=[]

    class Response:
        status_code=200
        content=b'{}'
        def json(self):
            return {"status":"succeeded","output":{"ok":True},
                    "usage":{"inputTokens":10,"outputTokens":2}}

    monkeypatch.setattr("sourceloom.providers.post_before_deadline",
                        lambda url,headers,body,deadline:(captured.append((headers,body)) or Response()))
    config=load_config()|{
        "provider":"openai-compatible","provider_id":"model-router-subscription",
        "protocol":"responses","endpoint":"/responses","structured_output":"deepseek_strict_tool",
        "base_url":"http://127.0.0.1:13210/v1","model":"gpt-5.6-luna","effort":"medium",
        "api_key":"router-key","writing_skill_dir":str(skill),"max_input_bytes":100000,
        "billing_mode":"subscription","max_output_tokens":1000,
    }
    assert Provider(store,config).call(project["id"],"active_plan",{"source":"text"},
                                      {"type":"object"},job)=={"ok":True}
    headers,wire=captured[0]
    assert headers["Idempotency-Key"]==job["calls"][0]["id"]
    assert wire["reasoning"]=={"effort":"medium"}
    assert "temperature" not in wire


def test_responses_unknown_usage_remains_unsettled(tmp_path, skill, monkeypatch):
    store, queue, project, bundle = prepared(tmp_path, skill)
    job = queue.enqueue(project["id"], bundle)

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            return httpx.Response(200, json={
                "status": "completed",
                "output_text": '{"ok":true}',
                "usage": {},
            })

    monkeypatch.setattr("sourceloom.providers.httpx.AsyncClient", Client)
    config = load_config() | {
        "provider": "openai-compatible", "provider_id": "kuafu", "protocol": "responses",
        "base_url": "https://api.example/v1", "api_key": "synthetic-key",
        "pricing_cny": {"input": 1.62, "cached_input": .054, "output": 4.86},
        "writing_skill_dir": str(skill), "max_input_bytes": 100000,
    }
    assert Provider(store, config).call(project["id"], "writer", {"source": "text"}, {"type": "object"}, job) == {"ok": True}
    row = store.costs(project["id"])[0]
    assert row["actual"] is None
    assert row["status"] == "unknown"
    assert row["body"]["billing_status"] == "unsettled"
