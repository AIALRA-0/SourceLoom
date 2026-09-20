import hashlib
import json
import sqlite3
from argparse import Namespace
from pathlib import Path

import pytest

from scripts.v2_real_eval import (
    Checkpoint,
    Evaluator,
    EvalError,
    extract_costs,
    load_manifest,
    main,
    parse_args,
    scan_history_reports,
    scan_production_db,
    SourceLoomClient,
)


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
    connection.execute("INSERT INTO jobs VALUES (?, ?)", (item["canonical_url"], "private"))
    connection.commit()
    connection.close()
    report_matches = scan_history_reports([report], item)
    db_matches = scan_production_db([db], item)
    assert report_matches == [{"kind": "report", "path": str(report)}]
    assert db_matches[0]["kind"] == "production_db"
    assert "private正文" not in json.dumps(report_matches + db_matches, ensure_ascii=False)


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

    def detail(self, pid):
        return {"id": pid, "costs": [{"actual": None, "body": {"local_estimate_cny": 0.12, "reservation_cny": 0.15, "billing_status": "unsettled"}}]}

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
