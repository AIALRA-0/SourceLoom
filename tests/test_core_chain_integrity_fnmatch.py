"""A self-authored synthetic pair retains the known platform-scope regression.

Offline tests construct source inventory from public test text. Private saved
live artifacts are used only by the existing explicit paid opt-in test.

The live test is opt-in because it sends a paid request. It exercises the
existing active_integrity turn once, using the saved original and candidate;
it does not run writing or repair stages.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from sourceloom import active_contracts as A
from sourceloom.active_composition import ActiveComposition
from sourceloom.config import load_config
from sourceloom.durable import Queue
from sourceloom.ingest import intake
from sourceloom.store import Store, digest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / ".local" / "core-chain-live-fnmatch"
LIVE_OPT_IN = "SOURCELOOM_RUN_PAID_INTEGRITY_REGRESSION"
BAD_PLATFORM_CLAIM = "让同一份比较在不同平台上行为一致"
SOURCE_SEMANTIC_QUOTE = (
    "Both parameters are case-normalized using :func:`os.path.normcase`."
)


def _saved_case():
    original = (FIXTURE_ROOT / "original.rst").read_text(encoding="utf-8")
    candidate = (FIXTURE_ROOT / "candidate.md").read_text(encoding="utf-8")
    output_quote = next(
        line for line in candidate.splitlines()
        if "os.path.normcase" in line and BAD_PLATFORM_CLAIM in line
    )
    assert " ".join(SOURCE_SEMANTIC_QUOTE.split()) in " ".join(original.split())
    return original, candidate, output_quote


def _synthetic_case():
    # A deliberately incorrect candidate is test input, never a user draft.
    original = (
        "Synthetic filename comparison\n=============================\n\n"
        "Both parameters are case-normalized using :func:`os.path.normcase`.\n"
        "The normalization can differ between Windows and Unix.\n"
    )
    candidate = (
        "# 合成文件名比较案例\n\n"
        "比较之前，两项参数使用 `os.path.normcase` 处理，"
        "让同一份比较在不同平台上行为一致。\n\n"
        "这里涉及 Windows 和 Unix 的平台差异。\n"
    )
    output_quote = next(
        line for line in candidate.splitlines()
        if "os.path.normcase" in line and BAD_PLATFORM_CLAIM in line
    )
    assert " ".join(SOURCE_SEMANTIC_QUOTE.split()) in " ".join(original.split())
    return original, candidate, output_quote


def _synthetic_job(tmp_path, original):
    source = intake(Store(tmp_path / "data"), [
        ("synthetic-fnmatch.rst", original.encode("utf-8")),
    ])
    responsibilities = {
        obj["id"]: A.ObjectResponsibility(
            source_id=obj["id"], present=True, explain=True,
        ).model_dump()
        for obj in source["objects"]
    }
    obligations = [
        A.SourceObligation(
            id="synthetic-" + obj["id"], source_id=obj["id"],
            quote=obj["text"], meaning=obj["text"], conditions=[],
            quantities=[], negations=[], narrator="synthetic source",
            referents=[],
        ).model_dump()
        for obj in source["objects"]
    ]
    return {
        "source": source, "source_digest": digest(source),
        "object_responsibilities": responsibilities,
        "active_plans": [{"obligations": obligations}],
    }


def test_synthetic_fnmatch_pair_records_the_known_i4_platform_scope_change():
    """Retain the source/candidate contradiction without private saved data."""
    original, candidate, output_quote = _synthetic_case()

    assert "case-normalized" in SOURCE_SEMANTIC_QUOTE
    assert "according to the current operating system" not in SOURCE_SEMANTIC_QUOTE
    assert BAD_PLATFORM_CLAIM in output_quote
    assert "Windows" in candidate and "Unix" in candidate
    assert " ".join(SOURCE_SEMANTIC_QUOTE.split()) in " ".join(original.split())


def test_synthetic_fnmatch_review_fixture_contains_complete_candidate_and_source(tmp_path):
    original, candidate, _ = _synthetic_case()
    job = _synthetic_job(tmp_path, original)
    source, source_ids, block_id, draft, payload = _make_live_payload(job, candidate)

    assert hashlib.sha256(original.encode("utf-8")).hexdigest() in {
        item.get("sha256") for item in source["originals"]
    }
    assert draft["blocks"][0]["markdown"] == candidate
    assert BAD_PLATFORM_CLAIM in payload["actual_draft"]["blocks"][0]["markdown"]
    assert payload["review_scope"] == "whole_candidate"
    assert payload["required_block_ids"] == [block_id]
    assert payload["required_source_ids"] == source_ids
    assert any("normcase" in obj.get("text", "") for obj in source["objects"])
    assert payload["obligations"] == job["active_plans"][0]["obligations"]
    assert payload["object_responsibilities"] == job["object_responsibilities"]


def _make_live_payload(job, candidate):
    source = job["source"]
    source_ids = [obj["id"] for obj in source["objects"]]
    block_id = "fnmatch-regression-candidate"
    draft = {
        "blocks": [
            {
                "id": block_id,
                "unit_id": "fnmatch-regression",
                "kind": "explanation",
                "markdown": candidate,
                "obligation_ids": [],
                "object_ids": source_ids,
                "evidence": [],
            }
        ]
    }
    obligations = [
        obligation
        for part in job.get("active_plans", [])
        for obligation in part.get("obligations", [])
        if job.get("object_responsibilities", {}).get(
            obligation.get("source_id"), {}
        ).get("present")
    ]
    payload = {
        "actual_draft": draft,
        "source_identity": {
            "original_names": [item["name"] for item in source["originals"]],
            "source_digest": job["source_digest"],
        },
        "object_responsibilities": job.get("object_responsibilities", {}),
        "obligations": obligations,
        "external_evidence": [
            binding
            for part in job.get("active_plans", [])
            for binding in part.get("evidence_bindings", [])
        ],
        "visual_cards": [
            {key: value for key, value in card.items() if key != "source_text"}
            for card in job.get("visual_cards", [])
        ],
        "review_scope": "whole_candidate",
        "required_source_ids": source_ids,
        "required_block_ids": [block_id],
    }
    return source, source_ids, block_id, draft, payload


def _single_route_config():
    primary = os.environ.get("AIALRA_KUAFUSHE_DS_PRIMARY_KEY")
    if not primary:
        pytest.fail(
            f"{LIVE_OPT_IN}=1 requires AIALRA_KUAFUSHE_DS_PRIMARY_KEY; "
            "the regression test does not print or persist credentials"
        )
    config = load_config()
    common = {
        "provider": "openai-compatible",
        "model": "deepseek-v4.1-flash",
        "base_url": "https://api.kuafushe.cc/v1",
        "enabled": True,
        "structured_output": "json_object",
        "max_output_tokens": 16000,
        "call_timeout": 240,
    }
    route_id = "kuafu-chat"
    route = common | {
        "provider_id": route_id,
        "protocol": "chat_completions",
        "api_key": primary,
    }
    return config | common | {
        "provider_id": route_id,
        "protocol": "chat_completions",
        "api_key": primary,
        "provider_routes": {route_id: route},
        "provider_credentials": {route_id: primary},
        "role_providers": {},
        "worker_concurrency": 1,
        "generation_pipeline": "active_composition_v2",
        "job_timeout": 0,
    }


def _validate_result(value, source, block_id, candidate):
    review = A.IntegrityReview.model_validate(value).model_dump()
    source_text = {obj["id"]: obj["text"] for obj in source["objects"]}
    for finding in review["findings"]:
        if finding["block_id"] and finding["block_id"] != block_id:
            raise ValueError("整稿意见没有准确指向候选正文")
        if finding["output_quote"] and finding["output_quote"] not in candidate:
            raise ValueError("整稿意见的候选引文不在保存的候选稿中")
        if finding["source_id"]:
            quote = finding["source_quote"]
            if finding["source_id"] not in source_text or (
                quote and quote not in source_text[finding["source_id"]]
            ):
                raise ValueError("整稿意见没有准确指向保存的原文")
    return review


@pytest.mark.skipif(
    os.environ.get(LIVE_OPT_IN) != "1",
    reason=f"paid model call disabled; set {LIVE_OPT_IN}=1 to enable",
)
def test_live_active_integrity_flags_fnmatch_platform_scope_change_once(tmp_path):
    """Send exactly one saved-candidate review through the active_integrity role."""
    _, candidate, output_quote = _saved_case()
    identity = json.loads((FIXTURE_ROOT / "run.json").read_text(encoding="utf-8"))
    source_store = Store(FIXTURE_ROOT / "data")
    saved_job = source_store.job(identity["job"])

    original_path = FIXTURE_ROOT / "original.rst"
    original_digest = hashlib.sha256(original_path.read_bytes()).hexdigest()
    assert original_digest in {
        item.get("sha256") for item in saved_job["source"]["originals"]
    }

    cloned_data = tmp_path / "data"
    shutil.copytree(FIXTURE_ROOT / "data", cloned_data)
    store = Store(cloned_data)
    job = store.job(identity["job"])
    job["writing_skill"]["root"] = str(
        cloned_data / "skills" / job["writing_skill"]["package_digest"]
    )
    job["role_policy"]["active_integrity"] = (
        ROOT / "sourceloom" / "roles" / "active_integrity.md"
    ).read_text(encoding="utf-8")
    job["role_policy_digest"] = digest(job["role_policy"])
    job.pop("pending", None)
    job.pop("worker_owner", None)

    source, source_ids, block_id, draft, payload = _make_live_payload(job, candidate)
    engine = ActiveComposition(
        SimpleNamespace(
            store=store,
            config=_single_route_config(),
            queue=Queue(store, pipeline="active_composition_v2"),
            owner=None,
        )
    )

    calls_before = len(job.get("calls", []))
    key = "active-integrity-fnmatch-regression-" + digest(candidate.encode())[:16]
    review = engine.turn(
        job,
        key,
        "active_integrity",
        A.IntegrityReview,
        source,
        source_ids,
        payload,
        lambda value, _resources: _validate_result(
            value, source, block_id, candidate
        ),
    )
    new_calls = job.get("calls", [])[calls_before:]

    assert len(new_calls) == 1
    assert [call["role"] for call in new_calls] == ["active_integrity"]
    assert any(
        finding["invariant"] == "I4"
        and finding["verdict"] == "FAIL"
        and finding["source_quote"]
        and "normcase" in finding["source_quote"]
        and finding["output_quote"]
        and BAD_PLATFORM_CLAIM in finding["output_quote"]
        for finding in review["findings"]
    ), output_quote
