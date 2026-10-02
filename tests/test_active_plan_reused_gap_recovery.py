import copy

from pydantic import BaseModel

from sourceloom.active_composition import (
    ActiveComposition,
    PIPELINE_V2,
    _restore_reused_gap_declarations,
)
from sourceloom.active_resources import Resources
from sourceloom.production import Production
from sourceloom.store import Store


class _TurnResult(BaseModel):
    value: int
    evidence_gaps: list[dict] = []


def test_only_bare_reused_gap_id_restores_saved_text_and_explicit_conflicts_survive():
    saved = [dict(id="gap-p6-salinity", text="gap-p6-salinity: src-00093 salinity",
                  description="src-00093 salinity", source_id="src-00093")]
    declarations = [
        dict(id="gap-p6-salinity", text="gap-p6-salinity", description="gap-p6-salinity"),
        dict(id="gap-p6-salinity", text="gap-p6-salinity: changed meaning",
             description="changed meaning"),
        dict(id="gap-p6-density", text="gap-p6-density", description="gap-p6-density"),
    ]

    restored = _restore_reused_gap_declarations(declarations, saved)

    assert restored[0] == saved[0]
    assert restored[1] == declarations[1]
    assert restored[2] == declarations[2]
    assert restored[0] is not saved[0]
    assert saved[0]["source_id"] == "src-00093"


def test_same_saved_plan_response_reuses_gap_and_records_over_limit_search_without_sending(tmp_path, monkeypatch):
    source_id = "src-00093"
    source = dict(objects=[dict(id=source_id, kind="text", locator="input/p6",
        text="The paper reports salinity, density, and conveyor observations.")],
        obligations=[], resources=[], unknown=[], originals=[], version=1,
        id="source", frozen=False, digest="source-digest")
    gap_ids = ["gap-p6-salinity", "gap-p6-density", "gap-p6-conveyor"]
    saved_gaps = [dict(id=gap_id, text=f"{gap_id}: {source_id} source-grounded evidence gap",
        description=f"{source_id} source-grounded evidence gap", source_id=source_id)
        for gap_id in gap_ids]
    marker = dict(version="active-plan-single-split-v1", status="queued",
        recovery_prefixes=["p6-split1a", "p6-split1b"])
    job = dict(id="split-gap-recovery", project="project", role="active_plan", created=1,
        status="running", pipeline=PIPELINE_V2, link_contract_version=1,
        results={}, calls=[], active_sessions={
            "active-plan-p6-split1a": dict(round=0, corrections=0,
                declared_gap_ids=list(gap_ids), declared_evidence_gaps=copy.deepcopy(saved_gaps),
                external_action_counts={"search": 2, "open": 0}),
        }, active_plan_split_recoveries=[marker], external_resources={},
        generated_resources={}, verified_terminology=[])
    engine = ActiveComposition(Production(Store(tmp_path), {"evidence_query_limit": 2}))
    dispatched = []
    monkeypatch.setattr(Resources, "execute", lambda self, action: dispatched.append(action))
    requests = []
    results = [
        dict(gaps=gap_ids, actions=[dict(kind="search", gap_id="gap-p6-conveyor",
            source_id=source_id, query="official conveyor specification")],
            ready_reason="", result=None),
        dict(gaps=[], actions=[], ready_reason="unresolved gap retained",
            result={"value": 1, "evidence_gaps":[{"id": gap_id} for gap_id in gap_ids]}),
        dict(gaps=[], actions=[], ready_reason="second partition complete",
            result={"value": 2, "evidence_gaps": []}),
    ]

    def call(job, key, role, payload, schema):
        requests.append((key, payload))
        return results.pop(0)

    engine.call = call
    validate = lambda result, resources: result

    first = engine.turn(job, "active-plan-p6-split1a", "active_plan", _TurnResult,
        source, [source_id], {}, validate)
    second = engine.turn(job, "active-plan-p6-split1b", "active_plan", _TurnResult,
        source, [source_id], {}, validate)

    session = job["active_sessions"]["active-plan-p6-split1a"]
    limit_result = session["action_results"][0]
    assert first["evidence_gaps"] == [{"id": gap_id} for gap_id in gap_ids]
    assert second["value"] == 2
    assert requests[0][1]["declared_evidence_gaps"] == saved_gaps
    assert limit_result["status"] == "unavailable"
    assert limit_result["evidence_status"] == "unavailable_limit"
    assert limit_result["gap_id"] == "gap-p6-conveyor"
    assert session["action_history"][-1]["result"] == limit_result
    assert session["external_action_counts"] == {"search": 2, "open": 0}
    assert dispatched == []
    assert marker["status"] == "completed"
    assert set(marker["completed_sessions"]) == {
        "active-plan-p6-split1a", "active-plan-p6-split1b"}
