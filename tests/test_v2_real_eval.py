import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
import subprocess
import sys
import threading
from argparse import Namespace
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import httpx

from scripts.v2_real_eval import (
    Checkpoint,
    Evaluator,
    EvalError,
    campaign_cost_totals,
    extract_costs,
    load_manifest,
    main,
    parse_args,
    scan_history_reports,
    scan_production_db,
    SourceLoomClient,
    TransportUnavailable,
)
from sourceloom.store import Store


def write_manifest(tmp_path, *, name="one.txt", content=b"frozen source", url=None):
    source = tmp_path / name
    source.write_bytes(content)
    item = {"kind": "text", "name": name, "sha256": hashlib.sha256(content).hexdigest()}
    if url:
        item["url"] = url
        item.pop("sha256")
        item["sha256"] = hashlib.sha256(content).hexdigest()
    else:
        item["local_path"] = name
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"materials": [item]}, ensure_ascii=False), encoding="utf-8")
    return manifest, source, item


def test_manifest_verifies_local_digest_and_rejects_internal_duplicate(tmp_path):
    manifest, _, item = write_manifest(tmp_path)
    loaded = load_manifest(manifest)
    assert loaded[0]["source_key"].startswith("sha256:")
    duplicate = json.loads(manifest.read_text(encoding="utf-8"))
    duplicate["materials"].append(item)
    manifest.write_text(json.dumps(duplicate), encoding="utf-8")
    with pytest.raises(EvalError, match="重复材料"):
        load_manifest(manifest)


def test_duplicate_checks_search_report_and_sqlite_without_returning正文(tmp_path):
    manifest, _, _ = write_manifest(tmp_path, url="https://Example.test/article?id=2")
    item = load_manifest(manifest)[0]
    report = tmp_path / "history.json"
    report.write_text(json.dumps({"source_url": item["canonical_url"], "private正文": "should not be returned"}), encoding="utf-8")
    db = tmp_path / "production.sqlite"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE jobs (source_url TEXT, body TEXT)")
    connection.execute("INSERT INTO jobs VALUES (?, ?)", (item["canonical_url"], json.dumps({"body": "private"})))
    connection.commit()
    connection.close()
    report_matches = scan_history_reports([report], item)
    db_matches = scan_production_db([db], item)
    assert report_matches == [{"kind": "report", "path": str(report)}]
    assert db_matches[0]["kind"] == "production_db"
    assert "private正文" not in json.dumps(report_matches + db_matches, ensure_ascii=False)


def test_history_scan_excludes_current_manifest_and_checkpoint(tmp_path):
    manifest, _, _ = write_manifest(tmp_path, url="https://example.test/current")
    item = load_manifest(manifest)[0]
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({"source": item["canonical_url"]}), encoding="utf-8")
    old_report = tmp_path / "old-report.json"
    old_report.write_text(json.dumps({"status": "different material"}), encoding="utf-8")
    assert scan_history_reports([tmp_path], item, exclude=(manifest, checkpoint)) == []


def test_history_scan_ignores_only_uncreated_planned_material_rows(tmp_path):
    manifest, _, _ = write_manifest(tmp_path, url="https://example.test/planned")
    item = load_manifest(manifest)[0]
    report = tmp_path / "dry-run.json"
    report.write_text(json.dumps({"mode": "dry-run", "materials": [{
        "source_key": item["source_key"], "status": "planned",
        "project_id": None, "run_id": "",
    }]}), encoding="utf-8")
    assert scan_history_reports([report], item) == []

    report.write_text(json.dumps({"materials": [
        {"source_key": item["source_key"], "status": "planned", "project_id": "p-1"},
        {"source_key": item["source_key"], "status": "planned", "run_id": "r-1"},
        {"source_key": item["source_key"], "status": "retry_pending"},
    ]}), encoding="utf-8")
    assert scan_history_reports([report], item) == [{"kind": "report", "path": str(report)}]


def test_dry_run_never_constructs_http_client_and_writes_timestamped_report(tmp_path, monkeypatch):
    manifest, _, _ = write_manifest(tmp_path)
    import scripts.v2_real_eval as module

    monkeypatch.setattr(module, "SourceLoomClient", lambda *args, **kwargs: pytest.fail("dry-run opened HTTP client"))
    args = [str(manifest), "--mode", "dry-run", "--output", str(tmp_path / "reports"), "--checkpoint", str(tmp_path / "checkpoint.json")]
    assert main(args) == 0
    reports = list((tmp_path / "reports").glob("v2-real-eval-*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["accepted_or_published"] is False
    assert report["materials"][0]["status"] == "planned"


def test_cli_subprocess_reaches_real_quality_audit_without_generation(tmp_path):
    manifest, _, _ = write_manifest(tmp_path)
    item = load_manifest(manifest)[0]
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({"version": 1, "materials": {
        item["source_key"]: {
            "source_key": item["source_key"], "name": item["name"], "kind": item["kind"],
            "project_id": "project-existing", "intake_done": True,
            "run_id": "run-existing", "status": "ready_for_review",
        },
    }}), encoding="utf-8")
    history = tmp_path / "history.json"
    history.write_text(json.dumps({"materials": []}), encoding="utf-8")
    project = {
        "id": "project-existing", "revision": 1, "delivery_state": "ready_for_review",
        "production": {"status": "ready_for_review", "delivery_state": "ready_for_review",
                        "delivery_checks": {"semantic_status": "not_reviewed"}, "issues": []},
        "independent_review": {"status": "not_reviewed"},
        "inventory": {"frozen": True, "objects": [], "obligations": [], "resources": [],
                      "originals": [], "unknown": []},
        "draft": {"blocks": []}, "plan": {"nodes": []}, "release_issues": [], "costs": [],
    }
    methods = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            methods.append("GET")
            path = urlsplit(self.path).path
            if path == "/api/projects/project-existing/production":
                body = json.dumps({"id": "run-existing", "status": "ready_for_review",
                                   "delivery_state": "ready_for_review", "call_count": 0}).encode()
                content_type = "application/json"
            elif path == "/api/projects/project-existing":
                body = json.dumps(project).encode()
                content_type = "application/json"
            elif path == "/api/projects/project-existing/output":
                body = b"# Candidate\n"
                content_type = "text/markdown"
            elif path == "/api/projects/project-existing/export":
                body = b"PKinvalid-archive"
                content_type = "application/octet-stream"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        script = Path(__file__).resolve().parents[1] / "scripts" / "v2_real_eval.py"
        result = subprocess.run([
            sys.executable, str(script), str(manifest), "--mode", "live", "--allow-paid",
            "--base-url", f"http://127.0.0.1:{server.server_port}", "--history-report", str(history),
            "--checkpoint", str(checkpoint), "--output", str(tmp_path / "output"),
            "--workers", "1", "--poll-interval", "0", "--max-poll-seconds", "5",
        ], cwd=tmp_path, capture_output=True, text=True, timeout=15)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.returncode == 1
    assert methods and set(methods) == {"GET"}
    updated = json.loads(checkpoint.read_text(encoding="utf-8"))
    record = updated["materials"][item["source_key"]]
    assert record["exported"] is True
    assert record["verified_success"] is False
    assert record["quality_audit"]["passed"] is False
    assert "readweave_export_complete" in record["quality_error"]
    report_file = next((tmp_path / "output").glob("v2-real-eval-*.json"))
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report["materials"][0]["quality_audit"]["passed"] is False


def test_live_report_returns_failure_when_material_did_not_verify(tmp_path, monkeypatch):
    manifest, _, _ = write_manifest(tmp_path)
    monkeypatch.setattr(Evaluator, "run", lambda self: {
        "mode": "live", "all_verified": False,
        "verified_success_count": 0,
        "materials": [{"status": "failed", "verified_success": False}],
    })
    output = tmp_path / "reports"
    code = main([str(manifest), "--mode", "live", "--allow-paid",
                 "--history-report", str(tmp_path / "history.json"),
                 "--output", str(output), "--checkpoint", str(tmp_path / "checkpoint.json")])
    assert code == 1
    reports = list(output.glob("v2-real-eval-*.json"))
    assert len(reports) == 1
    assert json.loads(reports[0].read_text(encoding="utf-8"))["all_verified"] is False


def test_live_requires_explicit_paid_guard_and_history_source(tmp_path):
    manifest, _, _ = write_manifest(tmp_path)
    with pytest.raises(SystemExit):
        parse_args([str(manifest), "--mode", "live", "--allow-paid"])
    with pytest.raises(SystemExit):
        parse_args([str(manifest), "--mode", "live", "--history-report", str(tmp_path / "history.json")])
    assert parse_args([str(manifest), "--mode", "offline", "--workers", "4"]).workers == 4


def test_production_proxy_identity_is_added_in_memory_only():
    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}

        @staticmethod
        def json():
            return {"ok": True}

    class Client:
        def __init__(self):
            self.headers = None

        def request(self, method, url, headers=None, **kwargs):
            self.headers = headers
            return Response()

        def close(self):
            pass

    transport = Client()
    client = SourceLoomClient("http://127.0.0.1:14420", client=transport, proxy_subject="private-owner")
    assert client.request("GET", "/api/projects") == {"ok": True}
    assert transport.headers["X-Aialra-Authenticated"] == "1"
    assert transport.headers["X-Aialra-Sub"] == "private-owner"


class FakeClient:
    def __init__(self, existing=False, final_status="completed"):
        self.created = 0
        self.enqueued = 0
        self.produced = 0
        self.polls = 0
        self.existing = existing
        self.final_status = final_status
        self.rewritten = 0

    def close(self):
        pass

    def create_project(self, item, goal, budget_cny):
        self.created += 1
        return {"id": "project-1"}

    def enqueue_upload(self, pid, path, request_id):
        self.enqueued += 1
        return {"id": "intake-1"}

    def enqueue_url(self, pid, url, request_id):
        self.enqueued += 1
        return {"id": "intake-1"}

    def production(self, pid):
        self.polls += 1
        if self.polls == 1 and not self.existing:
            return {"id": "intake-1", "status": "completed", "call_count": 0}
        return {"id": "run-1", "status": self.final_status, "call_count": 2}

    def produce(self, pid):
        self.produced += 1
        return {"id": "run-1"}

    def rewrite(self, pid):
        self.rewritten += 1
        return {"id": f"rewrite-{self.rewritten}"}

    def detail(self, pid):
        return {"id": pid, "production": {"delivery_state": "ready_for_review"}, "jobs": [], "costs": [{"actual": None, "body": {"local_estimate_cny": 0.12, "reservation_cny": 0.15, "billing_status": "unsettled"}}]}

    def output_markdown(self, pid):
        return b"# candidate\n"

    def export_package(self, pid):
        return b"PK\x03\x04fake"


def test_live_checkpoint_never_recreates_or_regenerates_on_restart(tmp_path):
    manifest, _, _ = write_manifest(tmp_path)
    item = load_manifest(manifest)[0]
    args = Namespace(mode="live", workers=1, output=tmp_path / "out", base_url="http://unused", timeout=1,
                     poll_interval=0, max_poll_seconds=10, budget_cny=0, goal="goal")
    checkpoint = Checkpoint.load(tmp_path / "checkpoint.json")
    evaluator = Evaluator(args, manifest, item and [item], checkpoint)
    fake = FakeClient()
    evaluator.client = fake
    first = evaluator.one(item)
    assert first["status"] == "completed"
    assert (fake.created, fake.enqueued, fake.produced) == (1, 1, 1)
    evaluator.close()

    checkpoint2 = Checkpoint.load(tmp_path / "checkpoint.json")
    evaluator2 = Evaluator(args, manifest, [item], checkpoint2)
    fake2 = FakeClient(existing=True)
    evaluator2.client = fake2
    second = evaluator2.one(item)
    assert second["status"] == "completed"
    assert (fake2.created, fake2.enqueued, fake2.produced) == (0, 0, 0)
    assert second["cost"]["unsettled"] == pytest.approx(0.12)
    evaluator2.close()


def test_cost_fields_keep_unsettled_distinct_from_provider_actual():
    costs = extract_costs({"costs": [{"actual": None, "body": {"local_estimate_cny": 1.2, "reservation_cny": 1.5, "billing_status": "unsettled"}}, {"actual": 2, "body": {"provider_actual_cny": 2.0, "billing_status": "settled"}}]})
    assert costs["estimated"] == pytest.approx(1.2)
    assert costs["reserved"] == pytest.approx(1.5)
    assert costs["unsettled"] == pytest.approx(1.2)
    assert costs["provider_actual"] == pytest.approx(2.0)


def test_cost_reporting_keeps_unknown_currencies_and_unsettled_amounts_out_of_cny_totals():
    costs = extract_costs({"costs": [
        {"body": {"provider_actual_cny": 2.0, "billing_status": "settled"}},
        {"body": {"provider_actual": 99.0, "currency": "USD", "billing_status": "settled"}},
        {"body": {"provider_actual_cny": 7.0, "currency": "USD", "billing_status": "settled"}},
        {"body": {"local_estimate_cny": 0.8, "reservation_cny": 1.5, "billing_status": "pending"}},
        {"actual": 4.0, "currency": "USD", "billing_status": "settled"},
    ]})

    assert costs["provider_actual"] == pytest.approx(2.0)
    assert costs["known_settled_call_count"] == 1
    assert costs["known_settled_cny"] == pytest.approx(2.0)
    assert costs["reserved_call_count"] == 1
    assert costs["reserved_cny"] == pytest.approx(1.5)
    assert costs["unknown_cost_call_count"] == 4
    assert costs["unknown_cost_cny"] is None

    totals = campaign_cost_totals([{"calls": 6, "cost": costs}])
    assert totals["call_count"] == 6
    assert totals["known_settled_call_count"] == 1
    assert totals["known_settled_cny"] == pytest.approx(2.0)
    assert totals["reserved_call_count"] == 1
    assert totals["reserved_cny"] == pytest.approx(1.5)
    assert totals["reserved_amount_complete"] is False
    assert totals["unknown_cost_call_count"] == 5
    assert totals["unknown_cost_cny"] is None


def test_store_settled_estimates_stay_separate_from_provider_actuals():
    costs = extract_costs({"costs": [
        {"status": "settled", "actual": 0.25, "reserved": 0.05, "currency": "CNY",
         "body": {"billing_status": "settled_estimate", "actual_cny": 0.25,
                  "reservation_cny": None, "reserved_cny": 0.05}},
        {"status": "settled", "actual": 0.50, "reserved": 0.07, "currency": "CNY",
         "body": {"billing_status": "settled_estimate", "actual_cny": None}},
        {"status": "settled", "actual": 99.0, "reserved": 33.0, "currency": "USD",
         "body": {"billing_status": "settled_estimate", "actual_cny": 99.0,
                  "reserved_cny": 33.0}},
    ]})

    assert costs["local_settled_estimate_call_count"] == 2
    assert costs["local_settled_estimate_cny"] == pytest.approx(0.75)
    assert costs["known_settled_cny"] == pytest.approx(0.75)
    assert costs["provider_actual"] == 0.0
    assert costs["provider_actual_complete"] is False
    assert costs["reserved_call_count"] == 2
    assert costs["reserved_cny"] == pytest.approx(0.12)
    assert costs["unknown_cost_call_count"] == 3
    assert costs["unknown_cost_cny"] is None

    totals = campaign_cost_totals([{"calls": 3, "cost": costs}])
    assert totals["known_settled_cny"] == pytest.approx(0.75)
    assert totals["local_settled_estimate_cny"] == pytest.approx(0.75)
    assert totals["provider_actual_cny"] == 0.0
    assert totals["provider_actual_complete"] is False
    assert totals["reserved_cny"] == pytest.approx(0.12)
    assert totals["unknown_cost_cny"] is None


def test_campaign_and_samples_report_start_end_and_elapsed_time(tmp_path):
    manifest, _, _ = write_manifest(tmp_path)
    item = load_manifest(manifest)[0]
    args = Namespace(mode="dry-run", workers=1, output=tmp_path / "out")
    evaluator = Evaluator(args, manifest, [item], Checkpoint.load(tmp_path / "checkpoint.json"))

    report = evaluator.run()
    sample = report["materials"][0]

    assert sample["started_at"] <= sample["ended_at"]
    assert sample["elapsed_seconds"] >= 0
    assert report["started_at"] <= report["ended_at"]
    assert report["elapsed_seconds"] >= 0
    assert report["cost_totals"] == {
        "call_count": 0, "known_settled_call_count": 0, "known_settled_cny": 0.0,
        "local_settled_estimate_call_count": 0, "local_settled_estimate_cny": 0.0,
        "provider_actual_cny": 0.0, "provider_actual_complete": True,
        "reserved_call_count": 0, "reserved_cny": 0.0, "reserved_amount_complete": True,
        "unknown_cost_call_count": 0, "unknown_cost_cny": 0.0,
    }


def test_ready_for_review_is_terminal_and_exports_candidate(tmp_path):
    manifest, _, _ = write_manifest(tmp_path)
    item = load_manifest(manifest)[0]
    args = Namespace(mode="live", workers=1, output=tmp_path / "out", base_url="http://unused", timeout=1,
                     poll_interval=0, max_poll_seconds=10, budget_cny=0, goal="goal")
    evaluator = Evaluator(args, manifest, [item], Checkpoint.load(tmp_path / "checkpoint.json"))
    evaluator.client = FakeClient(final_status="ready_for_review")
    result = evaluator.one(item)
    assert result["status"] == "ready_for_review"
    assert result["exported"] is True
    assert Path(result["artifacts"], "output.md").read_bytes() == b"# candidate\n"
    evaluator.close()


def test_recovered_delivery_is_rewritten_before_it_can_pass(tmp_path, monkeypatch):
    import scripts.audit_real_outputs as audit_module

    # This test isolates recovered-delivery retry behavior. The independent
    # audit itself is exercised by the failure case below.
    monkeypatch.setattr(audit_module, "audit_project", lambda *_: {"passed": True, "checks": []})
    manifest, _, _ = write_manifest(tmp_path)
    item = load_manifest(manifest)[0]
    args = Namespace(mode="live", workers=1, output=tmp_path / "out", base_url="http://unused", timeout=1,
                     poll_interval=0, max_poll_seconds=10, budget_cny=0, goal="goal", max_rewrites=2)
    evaluator = Evaluator(args, manifest, [item], Checkpoint.load(tmp_path / "checkpoint.json"))

    class RecoveredThenClean(FakeClient):
        def production(self, pid):
            self.polls += 1
            if self.polls == 1:
                return {"id": "intake-1", "status": "completed"}
            if self.rewritten:
                return {"id": "rewrite-1", "status": "ready_for_review",
                        "delivery_state": "ready_for_review"}
            return {"id": "run-1", "status": "ready_for_review",
                    "delivery_state": "recovered_ready_for_review"}

        def detail(self, pid):
            state = "ready_for_review" if self.rewritten else "recovered_ready_for_review"
            return {"id": pid, "production": {"delivery_state": state}, "jobs": [], "costs": []}

    fake = RecoveredThenClean()
    evaluator.client = fake
    result = evaluator.one(item)
    assert fake.rewritten == 1
    assert result["verified_success"] is True
    assert result["delivery_state"] == "ready_for_review"
    assert result["run_id"] == "rewrite-1"
    evaluator.close()


def test_issues_recorded_project_with_missing_image_is_exported_but_not_verified(tmp_path):
    manifest, _, _ = write_manifest(tmp_path)
    item = load_manifest(manifest)[0]
    args = Namespace(mode="live", workers=1, output=tmp_path / "out", base_url="http://unused", timeout=1,
                     poll_interval=0, max_poll_seconds=10, budget_cny=0, goal="goal")
    evaluator = Evaluator(args, manifest, [item], Checkpoint.load(tmp_path / "checkpoint.json"))

    class IssuesRecordedMissingImage(FakeClient):
        def __init__(self):
            super().__init__(final_status="ready_for_review")

        def detail(self, pid):
            return {
                "id": pid,
                "revision": 1,
                "delivery_state": "ready_for_review",
                "production": {
                    "status": "ready_for_review",
                    "delivery_state": "ready_for_review",
                    "pipeline": "active_composition_v2",
                    "issues": ["recorded quality issue"],
                    "delivery_checks": {"semantic_status": "passed"},
                },
                "independent_review": {"status": "issues_recorded", "revision": 1},
                "release_issues": [
                    {"code": "unknown", "message": "image unavailable"},
                    {"code": "inventory_review", "message": "not reviewed"},
                    {"code": "review", "message": "issues recorded"},
                    {"code": "acceptance", "message": "not accepted"},
                ],
                "inventory": {
                    "frozen": True,
                    "objects": [{"id": "logo", "kind": "image", "text": "", "resource_id": "missing",
                                 "source_scope": "article_media", "locator": "figure[1]"}],
                    "obligations": [{"id": "logo-obligation", "object_id": "logo"}],
                    "resources": [],
                    "originals": [],
                    "unknown": [{"object_id": "logo", "reason": "image pixels unavailable"}],
                    "inventory_review": {"status": "complete"},
                    "digest": "inventory-digest",
                },
                "draft": {"blocks": []},
                "plan": None,
                "costs": [],
            }

        def output_markdown(self, pid):
            return b"<div align=center></div>\nlogo pixels unavailable\n"

    evaluator.client = IssuesRecordedMissingImage()
    result = evaluator.one(item)

    assert result["status"] == "ready_for_review"
    assert result["exported"] is True
    assert result["verified_success"] is False
    failed_codes = {check["code"] for check in result["quality_audit"]["checks"] if not check["passed"]}
    assert {"independent_semantic_review", "no_recorded_quality_issues", "no_unknown_source_regions",
            "protected_materials_represented", "no_blocking_release_issue"} <= failed_codes
    evaluator.close()


def test_live_single_worker_stops_after_first_unverified_material(tmp_path):
    manifest, _, _ = write_manifest(tmp_path)
    contents = json.loads(manifest.read_text(encoding="utf-8"))
    second_content = b"second frozen source"
    second_path = tmp_path / "two.txt"
    second_path.write_bytes(second_content)
    contents["materials"].append({
        "kind": "text", "name": "two.txt", "local_path": "two.txt",
        "sha256": hashlib.sha256(second_content).hexdigest(),
    })
    manifest.write_text(json.dumps(contents), encoding="utf-8")
    items = load_manifest(manifest)
    args = Namespace(mode="live", workers=1, output=tmp_path / "out", base_url="http://unused", timeout=1)
    checkpoint = Checkpoint.load(tmp_path / "checkpoint.json")
    evaluator = Evaluator(args, manifest, items, checkpoint)
    attempted = []

    def first_fails(item):
        attempted.append(item["source_key"])
        return checkpoint.update(item["source_key"], status="ready_for_review", verified_success=False)

    evaluator.one = first_fails
    report = evaluator.run()

    assert attempted == [items[0]["source_key"]]
    assert len(report["materials"]) == 1
    assert report["all_verified"] is False
    assert items[1]["source_key"] not in checkpoint.data["materials"]
    evaluator.close()


def test_recovered_delivery_never_counts_as_success_after_rewrite_limit(tmp_path):
    manifest, _, _ = write_manifest(tmp_path)
    item = load_manifest(manifest)[0]
    args = Namespace(mode="live", workers=1, output=tmp_path / "out", base_url="http://unused", timeout=1,
                     poll_interval=0, max_poll_seconds=10, budget_cny=0, goal="goal", max_rewrites=0)
    evaluator = Evaluator(args, manifest, [item], Checkpoint.load(tmp_path / "checkpoint.json"))

    class AlwaysRecovered(FakeClient):
        def production(self, pid):
            self.polls += 1
            if self.polls == 1:
                return {"id": "intake-1", "status": "completed"}
            return {"id": "run-1", "status": "ready_for_review",
                    "delivery_state": "recovered_ready_for_review"}

        def detail(self, pid):
            return {"id": pid, "production": {"delivery_state": "recovered_ready_for_review"},
                    "jobs": [], "costs": []}

    fake = AlwaysRecovered()
    evaluator.client = fake
    result = evaluator.one(item)
    assert result["status"] == "quality_retry_exhausted"
    assert result["verified_success"] is False
    assert result["exported"] is False
    evaluator.close()


def test_connection_refusal_is_retried_before_reporting_failure(monkeypatch):
    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}

        @staticmethod
        def json():
            return {"ok": True}

    class FlakyTransport:
        def __init__(self):
            self.calls = 0

        def request(self, *args, **kwargs):
            self.calls += 1
            if self.calls < 3:
                raise httpx.ConnectError("connection refused")
            return Response()

        def close(self):
            pass

    monkeypatch.setattr("scripts.v2_real_eval.time.sleep", lambda _: None)
    transport = FlakyTransport()
    client = SourceLoomClient("http://unused", client=transport, transport_retries=2)
    assert client.request("GET", "/health") == {"ok": True}
    assert transport.calls == 3


def test_connection_refusal_during_create_leaves_checkpoint_resumable(tmp_path):
    manifest, _, _ = write_manifest(tmp_path)
    item = load_manifest(manifest)[0]
    args = Namespace(mode="live", workers=1, output=tmp_path / "out", base_url="http://unused", timeout=1,
                     poll_interval=0, max_poll_seconds=10, budget_cny=0, goal="goal")
    checkpoint = Checkpoint.load(tmp_path / "checkpoint.json")
    evaluator = Evaluator(args, manifest, [item], checkpoint)

    class Offline(FakeClient):
        def create_project(self, item, goal, budget_cny):
            raise TransportUnavailable("connection refused")

    evaluator.client = Offline()
    report = evaluator.run()
    saved = checkpoint.data["materials"][item["source_key"]]
    assert report["all_verified"] is False
    assert saved["status"] == "retry_pending"
    assert saved["create_attempted"] is False
    evaluator.close()


def test_invalid_export_response_cannot_be_counted_as_success(tmp_path):
    manifest, _, _ = write_manifest(tmp_path)
    item = load_manifest(manifest)[0]
    args = Namespace(mode="live", workers=1, output=tmp_path / "out", base_url="http://unused", timeout=1,
                     poll_interval=0, max_poll_seconds=10, budget_cny=0, goal="goal")
    checkpoint = Checkpoint.load(tmp_path / "checkpoint.json")
    evaluator = Evaluator(args, manifest, [item], checkpoint)

    class InvalidPackage(FakeClient):
        def export_package(self, pid):
            return b"not a zip"

    evaluator.client = InvalidPackage()
    report = evaluator.run()
    result = report["materials"][0]
    assert report["verified_success_count"] == 0
    assert report["all_verified"] is False
    assert result["status"] == "retry_pending"
    assert result["verified_success"] is False
    assert "有效 ZIP" in result["transient_error"]
    evaluator.close()


def test_fast_completed_intake_can_be_recovered_from_project_jobs(tmp_path):
    manifest, _, _ = write_manifest(tmp_path)
    item = load_manifest(manifest)[0]
    args = Namespace(mode="live", workers=1, output=tmp_path / "out", base_url="http://unused", timeout=1,
                     poll_interval=0, max_poll_seconds=10, budget_cny=0, goal="goal")
    evaluator = Evaluator(args, manifest, [item], Checkpoint.load(tmp_path / "checkpoint.json"))

    class FastIntake(FakeClient):
        def production(self, pid):
            self.polls += 1
            if self.polls == 1:
                return {"status": "not_started"}
            return {"id": "run-1", "status": "ready_for_review", "call_count": 2}

        def detail(self, pid):
            return {"id": pid, "production": {"delivery_state": "ready_for_review"},
                    "jobs": [{"id": "intake-1", "role": "intake", "status": "completed"}], "costs": []}

    fake = FastIntake()
    evaluator.client = fake
    result = evaluator.one(item)
    assert result["status"] == "ready_for_review"
    assert fake.produced == 1
    evaluator.close()


def test_checkpoint_project_is_not_rejected_as_historical_duplicate(tmp_path):
    manifest, _, item = write_manifest(tmp_path)
    database = tmp_path / "production.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
    connection.execute("INSERT INTO projects VALUES (?, ?)", ("prior", json.dumps({
        "inventory": {"originals": [{"sha256": item["sha256"]}], "resources": [], "objects": []}
    })))
    connection.commit(); connection.close()
    checkpoint = tmp_path / "checkpoint.json"
    key = load_manifest(manifest)[0]["source_key"]
    checkpoint.write_text(json.dumps({"version": 1, "materials": {key: {"project_id": "existing"}}}), encoding="utf-8")
    assert main([str(manifest), "--mode", "offline", "--production-db", str(database),
                 "--checkpoint", str(checkpoint), "--output", str(tmp_path / "out")]) == 0


def test_production_scan_checks_only_source_identity_fields(tmp_path):
    manifest, _, _ = write_manifest(tmp_path, url="https://Example.test/article?id=2")
    item = load_manifest(manifest)[0]
    item["summary"] = "A source summary with enough detail"
    db = tmp_path / "production.sqlite"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
    connection.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, body TEXT NOT NULL, prompt TEXT)")
    inventory = {
        "source_url": "https://example.test/article?id=2#ignored",
        "originals": [{"sha256": item["sha256"]}],
        "resources": [],
        "objects": [{"text": "Earlier source text. A source summary\nwith enough detail appears here."}],
    }
    # The prompt also contains the digest, but is not a valid duplicate signal.
    connection.execute("INSERT INTO projects VALUES (?, ?)", ("prior", json.dumps({
        "inventory": inventory, "prompt": item["sha256"], "draft": {"text": item["summary"]}
    })))
    connection.execute("INSERT INTO jobs VALUES (?, ?, ?)", ("job", json.dumps({"prompt": item["sha256"]}),
                                                            item["sha256"]))
    connection.commit()
    connection.close()

    matches = scan_production_db([db], item)
    assert {match["column"] for match in matches} >= {
        "inventory.source_url", "inventory.originals[*].sha256", "inventory.objects[*].text"
    }
    assert all(match["table"] == "projects" for match in matches)


def test_production_scan_reads_saved_sourceloom_project_inventory(tmp_path):
    store = Store(tmp_path / "production")
    project = store.create("prior source")
    source_digest = "c" * 64
    inventory = {
        "source_url": "https://example.test/article?b=2&a=1#section",
        "originals": [{"name": "source.html", "sha256": source_digest}],
        "resources": [],
        "objects": [{"id": "source", "text": "The prior source has a stable summary for this article."}],
    }
    store.change(project["id"], lambda value: value.update(inventory=inventory))
    with store.connect() as connection:
        connection.execute("INSERT INTO source_versions VALUES (?, ?, ?)",
                           (project["id"], 1, json.dumps({"inventory": inventory})))

    matches = scan_production_db([store.db], {
        "canonical_url": "https://example.test/article?a=1&b=2",
        "verified_sha256": source_digest,
        "summary": "a stable summary for this article",
    })

    assert {match["column"] for match in matches} == {
        "inventory.source_url", "inventory.originals[*].sha256", "inventory.objects[*].text"
    }
    assert {match["table"] for match in matches} == {"projects", "source_versions"}


def test_production_scan_does_not_match_payload_text_or_url_substrings(tmp_path):
    manifest, _, _ = write_manifest(tmp_path, url="https://example.test/article")
    item = load_manifest(manifest)[0]
    db = tmp_path / "production.sqlite"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
    connection.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, prompt TEXT, body TEXT)")
    connection.execute("INSERT INTO projects VALUES (?, ?)", ("prior", json.dumps({
        "inventory": {"source_url": "https://example.test/article-and-more", "originals": [], "resources": [], "objects": []},
        "draft": {"text": item["sha256"]},
    })))
    connection.execute("INSERT INTO jobs VALUES (?, ?, ?)", ("job", item["sha256"], json.dumps({"prompt": item["sha256"]})))
    connection.commit()
    connection.close()

    assert scan_production_db([db], item) == []


def test_production_scan_fails_closed_on_unreadable_source_records(tmp_path):
    db = tmp_path / "production.sqlite"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
    connection.execute("INSERT INTO projects VALUES ('broken', 'not-json')")
    connection.commit()
    connection.close()

    with pytest.raises(EvalError, match="重复检查已中止"):
        scan_production_db([db], {"sha256": "a" * 64})
