"""Bounded SourceLoom v2 real-material evaluator.

The default mode is ``dry-run``.  ``offline`` reads and validates the private
manifest/checkpoint without opening a network connection.  ``live`` is an
explicit operation and additionally requires ``--allow-paid`` because the
production endpoint may spend provider credits.

The evaluator never accepts or publishes a project.  It writes timestamped
reports and keeps one checkpoint identity per source so a restart only polls
an already-created intake/production task.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import httpx


TERMINAL = {"completed", "ready_for_review", "failed", "needs_attention", "cancelled", "uncertain", "paused"}
DEFAULT_GOAL = "保持原文主旨、作者意图与人称，完整保留信息，改善逻辑与可读性，不自行设计课程或情境"
MAX_WORKERS = 4


class EvalError(RuntimeError):
    """A safe, user-facing evaluation error."""


def canonical_url(value: str) -> str:
    """Normalize URL identity without fetching it or exposing credentials."""

    parsed = urlsplit(str(value).strip())
    if not parsed.scheme or not parsed.netloc:
        return str(value).strip()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port and not ((parsed.scheme.lower() == "http" and port == 80) or
                     (parsed.scheme.lower() == "https" and port == 443)):
        host += f":{port}"
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", query, ""))


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_identity(item: Mapping[str, Any], base: Path | None = None) -> tuple[str, str | None, str | None]:
    """Return stable identity, canonical URL and local digest."""

    url = item.get("url")
    local = item.get("local_path")
    expected = str(item.get("sha256") or "").lower() or None
    if bool(url) == bool(local):
        raise EvalError(f"材料 {item.get('name') or '<unnamed>'} 必须恰好包含 url 或 local_path")
    if not expected or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise EvalError(f"材料 {item.get('name') or '<unnamed>'} 缺少有效 sha256")
    if url:
        return f"url:{canonical_url(str(url))}|sha256:{expected}", canonical_url(str(url)), None
    path = Path(str(local))
    if base and not path.is_absolute():
        path = base / path
    if not path.is_file():
        raise EvalError(f"本地材料不存在：{path}")
    actual = sha256_file(path)
    if actual != expected:
        raise EvalError(f"本地材料摘要不匹配：{item.get('name') or path.name}")
    return f"sha256:{expected}", None, actual


def load_manifest(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvalError(f"无法读取 manifest：{path}: {exc}") from exc
    items = raw.get("materials") if isinstance(raw, Mapping) else raw
    if not isinstance(items, list) or not items:
        raise EvalError("manifest 必须包含非空 materials 数组")
    result = []
    seen: set[str] = set()
    for index, raw_item in enumerate(items):
        if not isinstance(raw_item, Mapping):
            raise EvalError(f"manifest 第 {index + 1} 项不是对象")
        item = dict(raw_item)
        item.setdefault("name", f"material-{index + 1}")
        item.setdefault("kind", "unknown")
        identity, url, local_digest = source_identity(item, path.parent)
        if identity in seen:
            raise EvalError(f"manifest 内重复材料：{item['name']}")
        seen.add(identity)
        item["source_key"] = identity
        item["canonical_url"] = url
        item["verified_sha256"] = local_digest or str(item["sha256"]).lower()
        result.append(item)
    return result


def _candidate_strings(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _candidate_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _candidate_strings(child)
    elif isinstance(value, (str, int, float)):
        yield str(value)


def _matches_text(text: str, item: Mapping[str, Any]) -> bool:
    haystack = text.casefold()
    url = str(item.get("canonical_url") or "").casefold()
    digest = str(item.get("verified_sha256") or item.get("sha256") or "").casefold()
    if url and url in haystack:
        return True
    if digest and digest in haystack:
        return True
    summary = normalize_text(item.get("summary") or item.get("source_summary") or item.get("description"))
    return bool(summary and len(summary) >= 12 and summary in normalize_text(text))


def scan_history_reports(paths: Iterable[Path], item: Mapping[str, Any], *, exclude: Iterable[Path] = ()) -> list[dict[str, str]]:
    """Search configured reports by identity only; never return matching正文."""

    matches: list[dict[str, str]] = []
    excluded = {path.resolve() for path in exclude}
    for configured in paths:
        files = [configured] if configured.is_file() else list(configured.rglob("*")) if configured.is_dir() else []
        for path in files:
            if not path.is_file() or path.suffix.lower() not in {".json", ".jsonl", ".md", ".txt", ".csv"}:
                continue
            if path.resolve() in excluded:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if _matches_text(text, item):
                matches.append({"kind": "report", "path": str(path)})
    return matches


def scan_production_db(paths: Iterable[Path], item: Mapping[str, Any]) -> list[dict[str, str]]:
    """Search configured SQLite text columns without returning row contents."""

    matches: list[dict[str, str]] = []
    needles = [x for x in (item.get("canonical_url"), item.get("verified_sha256"),
                           item.get("summary"), item.get("source_summary"))
               if x and (len(str(x)) >= 12)]
    for path in paths:
        if not path.is_file():
            continue
        try:
            connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
            tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            for (table,) in tables:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
                    continue
                columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
                text_columns = [str(row[1]) for row in columns if str(row[2]).upper() in {"TEXT", "JSON", "VARCHAR"}]
                for column in text_columns:
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", column):
                        continue
                    clauses = " OR ".join([f'lower("{column}") LIKE ?' for _ in needles])
                    if not clauses:
                        continue
                    params = [f"%{str(needle).casefold()}%" for needle in needles]
                    found = connection.execute(f'SELECT 1 FROM "{table}" WHERE {clauses} LIMIT 1', params).fetchone()
                    if found:
                        matches.append({"kind": "production_db", "path": str(path), "table": table, "column": column})
            connection.close()
        except (OSError, sqlite3.Error):
            continue
    return matches


def duplicate_matches(item: Mapping[str, Any], reports: Iterable[Path], databases: Iterable[Path], *, exclude: Iterable[Path] = ()) -> list[dict[str, str]]:
    return scan_history_reports(reports, item, exclude=exclude) + scan_production_db(databases, item)


def timestamped_path(directory: Path, stem: str, suffix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    candidate = directory / f"{stem}-{stamp}-{uuid4().hex[:8]}{suffix}"
    while candidate.exists():
        candidate = directory / f"{stem}-{stamp}-{uuid4().hex[:8]}{suffix}"
    return candidate


@dataclass
class Checkpoint:
    path: Path
    data: dict[str, Any]
    lock: threading.Lock

    @classmethod
    def load(cls, path: Path) -> "Checkpoint":
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise EvalError(f"checkpoint 无法读取：{path}: {exc}") from exc
        else:
            data = {"version": 1, "materials": {}}
        if not isinstance(data.get("materials"), dict):
            raise EvalError("checkpoint.materials 必须是对象")
        return cls(path, data, threading.Lock())

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)

    def update(self, key: str, **values: Any) -> dict[str, Any]:
        with self.lock:
            record = self.data["materials"].setdefault(key, {"source_key": key})
            record.update(values)
            self.data["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.save()
        return dict(record)


class SourceLoomClient:
    def __init__(self, base_url: str, timeout: float = 45.0, client: httpx.Client | None = None,
                 proxy_subject: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)
        self.proxy_subject = proxy_subject

    def close(self) -> None:
        self.client.close()

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"X-SourceLoom": "1"}
        if self.proxy_subject:
            headers.update({"X-Aialra-Authenticated": "1", "X-Aialra-Sub": self.proxy_subject})
        if kwargs.get("json") is not None:
            headers["Content-Type"] = "application/json"
        headers.update(kwargs.pop("headers", {}) or {})
        response = self.client.request(method, self.base_url + path, headers=headers, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json()
            except ValueError:
                detail = "non-json response"
            raise EvalError(f"HTTP {response.status_code} {path}: {detail}")
        content_type = response.headers.get("content-type", "")
        return response.json() if "json" in content_type else response.content

    def create_project(self, item: Mapping[str, Any], goal: str, budget_cny: float | None) -> dict[str, Any]:
        body: dict[str, Any] = {"title": str(item["name"]), "goal": goal, "mode": "rewrite", "budget_usd": 0}
        if budget_cny is not None:
            body["budget_cny"] = budget_cny
        return self.request("POST", "/api/projects", json=body)

    def enqueue_upload(self, pid: str, path: Path, request_id: str) -> dict[str, Any]:
        with path.open("rb") as handle:
            return self.request("POST", f"/api/projects/{pid}/upload?background=true&generate=false&request_id={request_id}", files={"files": (path.name, handle, "application/octet-stream")})

    def enqueue_url(self, pid: str, url: str, request_id: str) -> dict[str, Any]:
        return self.request("POST", f"/api/projects/{pid}/url", json={"url": url, "background": True, "generate": False, "request_id": request_id})

    def production(self, pid: str) -> dict[str, Any]:
        return self.request("GET", f"/api/projects/{pid}/production")

    def produce(self, pid: str) -> dict[str, Any]:
        return self.request("POST", f"/api/projects/{pid}/produce", json={})

    def detail(self, pid: str) -> dict[str, Any]:
        return self.request("GET", f"/api/projects/{pid}")

    def output_markdown(self, pid: str) -> bytes:
        return self.request("GET", f"/api/projects/{pid}/output?format=markdown")

    def export_package(self, pid: str) -> bytes:
        return self.request("GET", f"/api/projects/{pid}/export")


def extract_costs(detail: Mapping[str, Any]) -> dict[str, Any]:
    rows = detail.get("costs") if isinstance(detail.get("costs"), list) else []
    result = {"estimated": 0.0, "reserved": 0.0, "unsettled": 0.0, "provider_actual": 0.0, "calls": len(rows), "rows": []}
    for row in rows:
        body = row.get("body") if isinstance(row, Mapping) and isinstance(row.get("body"), Mapping) else {}
        estimated = body.get("local_estimate_cny", body.get("estimated_cny", body.get("estimate_cny")))
        reserved = body.get("reservation_cny", body.get("reserved_cny"))
        unsettled = body.get("unsettled_cny")
        provider_actual = body.get("provider_actual_cny", body.get("provider_actual"))
        if unsettled is None and (body.get("billing_status") in {"unsettled", "pending"} or row.get("actual") is None):
            unsettled = estimated if estimated is not None else reserved
        for key, value in (("estimated", estimated), ("reserved", reserved), ("unsettled", unsettled), ("provider_actual", provider_actual)):
            if isinstance(value, (int, float)):
                result[key] += float(value)
        result["rows"].append({"estimated": estimated, "reserved": reserved, "unsettled": unsettled, "provider_actual": provider_actual, "billing_status": body.get("billing_status")})
    return result


def resolve_local(item: Mapping[str, Any], manifest: Path) -> Path | None:
    if not item.get("local_path"):
        return None
    path = Path(str(item["local_path"]))
    return path if path.is_absolute() else manifest.parent / path


class Evaluator:
    def __init__(self, args: argparse.Namespace, manifest: Path, items: list[dict[str, Any]], checkpoint: Checkpoint):
        self.args = args
        self.manifest = manifest
        self.items = items
        self.checkpoint = checkpoint
        subject_env = getattr(args, "proxy_subject_env", "SOURCELOOM_EVAL_SUBJECT")
        proxy_subject = os.environ.get(subject_env) if subject_env else None
        self.client = SourceLoomClient(args.base_url, args.timeout, proxy_subject=proxy_subject) if args.mode == "live" else None
        self.artifacts = timestamped_path(args.output, "artifacts", "")
        self.artifacts.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        if self.client:
            self.client.close()

    def poll(self, pid: str, expected_run_id: str | None = None) -> dict[str, Any]:
        assert self.client
        deadline = time.monotonic() + self.args.max_poll_seconds
        while True:
            state = self.client.production(pid)
            current = state.get("id")
            if expected_run_id and current and current != expected_run_id:
                raise EvalError(f"项目 {pid} 返回了不同任务 {current}，拒绝猜测任务身份")
            status = str(state.get("status") or "unknown")
            if status in TERMINAL or status in {"not_started", "unknown"}:
                return state
            if time.monotonic() >= deadline:
                raise EvalError(f"项目 {pid} 超过轮询时间上限，保留原任务身份")
            time.sleep(self.args.poll_interval)

    def one(self, item: dict[str, Any]) -> dict[str, Any]:
        key = item["source_key"]
        record = self.checkpoint.data["materials"].get(key, {"source_key": key})
        if self.args.mode != "live":
            return {"source_key": key, "name": item["name"], "kind": item["kind"], "status": "planned", "project_id": record.get("project_id"), "run_id": record.get("run_id")}
        assert self.client
        if record.get("manual_review"):
            return dict(record) | {"status": "manual_review"}
        pid = record.get("project_id")
        if record.get("create_attempted") and not pid:
            return self.checkpoint.update(key, status="manual_review", manual_review="create request may have been delivered; inspect server before retrying")
        if not pid:
            self.checkpoint.update(key, name=item["name"], kind=item["kind"], create_attempted=True,
                                   status="creating", request_id=uuid4().hex)
            project = self.client.create_project(item, self.args.goal, self.args.budget_cny)
            pid = str(project["id"])
            self.checkpoint.update(key, project_id=pid, status="created")

        intake_id = record.get("intake_run_id")
        intake_done = bool(record.get("intake_done"))
        if not intake_done:
            if record.get("intake_attempted") and not intake_id:
                return self.checkpoint.update(key, status="manual_review", manual_review="intake request may have been delivered; inspect server before retrying")
            if not record.get("intake_attempted"):
                request_id = str(record.get("request_id") or uuid4().hex)
                self.checkpoint.update(key, intake_attempted=True, request_id=request_id, status="intake_queued")
                local = resolve_local(item, self.manifest)
                response = self.client.enqueue_upload(pid, local, request_id) if local else self.client.enqueue_url(pid, str(item["canonical_url"]), request_id)
                intake_id = str(response.get("id") or response.get("job_id") or "")
                self.checkpoint.update(key, intake_run_id=intake_id or None)
            state = self.poll(pid, intake_id or None)
            if state.get("status") == "not_started" and intake_id:
                detail = self.client.detail(pid)
                completed = next((job for job in detail.get("jobs", [])
                                  if str(job.get("id")) == intake_id and job.get("role") == "intake"), None)
                if completed:
                    state = completed
            if state.get("status") != "completed":
                return self.checkpoint.update(key, status=str(state.get("status") or "intake_failed"), intake_status=state)
            self.checkpoint.update(key, intake_done=True, status="intake_completed")

        run_id = record.get("run_id")
        if record.get("generate_attempted") and not run_id:
            return self.checkpoint.update(key, status="manual_review", manual_review="generation request may have been delivered; inspect server before retrying")
        if not run_id:
            self.checkpoint.update(key, generate_attempted=True, status="generating")
            result = self.client.produce(pid)
            run_id = str(result.get("id") or result.get("job_id") or "")
            self.checkpoint.update(key, run_id=run_id or None)
        state = self.poll(pid, run_id or None)
        detail = self.client.detail(pid)
        costs = extract_costs(detail)
        record = self.checkpoint.update(key, status=state.get("status"), production_status=state,
                                        cost=costs, calls=state.get("call_count", costs["calls"]))
        if state.get("status") in {"completed", "ready_for_review"}:
            folder = self.artifacts / re.sub(r"[^A-Za-z0-9_.-]+", "_", str(item["name"]))[:80]
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "project.json").write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
            (folder / "cost.json").write_text(json.dumps(costs, ensure_ascii=False, indent=2), encoding="utf-8")
            (folder / "output.md").write_bytes(self.client.output_markdown(pid))
            (folder / "readweave-package.zip").write_bytes(self.client.export_package(pid))
            record = self.checkpoint.update(key, artifacts=str(folder), exported=True)
        return record

    def run(self) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        workers = min(self.args.workers, MAX_WORKERS)
        if self.args.mode == "live" and workers == 1:
            results = [self.one(item) for item in self.items]
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sourceloom-eval") as pool:
                futures = {pool.submit(self.one, item): item for item in self.items}
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        results.append({"source_key": item["source_key"], "name": item["name"], "status": "error", "error": str(exc)})
        results.sort(key=lambda row: row.get("source_key", ""))
        return {"version": 1, "mode": self.args.mode, "created_at": datetime.now(timezone.utc).isoformat(),
                "manifest": str(self.manifest), "checkpoint": str(self.checkpoint.path), "workers": workers,
                "accepted_or_published": False, "materials": results}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="私有 JSON manifest")
    parser.add_argument("--mode", choices=("dry-run", "offline", "live"), default="dry-run")
    parser.add_argument("--dry-run", action="store_true", help="兼容别名：等同于 --mode dry-run")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--history-report", action="append", type=Path, default=[])
    parser.add_argument("--production-db", action="append", type=Path, default=[])
    parser.add_argument("--checkpoint", type=Path, default=Path(".local/v2-real-eval/checkpoint.json"))
    parser.add_argument("--output", type=Path, default=Path(".local/v2-real-eval"))
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--poll-interval", type=float, default=3.0)
    parser.add_argument("--max-poll-seconds", type=float, default=1800.0)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--proxy-subject-env", default="SOURCELOOM_EVAL_SUBJECT",
                        help="从指定环境变量读取生产代理身份；值不会写入报告或 checkpoint")
    parser.add_argument("--budget-cny", type=float, default=None)
    parser.add_argument("--goal", default=DEFAULT_GOAL)
    parser.add_argument("--allow-paid", action="store_true", help="live 模式的显式付费调用开关")
    args = parser.parse_args(argv)
    if args.dry_run:
        args.mode = "dry-run"
    if not 1 <= args.workers <= MAX_WORKERS:
        parser.error(f"--workers 必须在 1 到 {MAX_WORKERS} 之间")
    if args.poll_interval < 0 or args.max_poll_seconds <= 0:
        parser.error("轮询间隔必须非负，轮询时间上限必须为正数")
    if args.mode == "live" and not args.allow_paid:
        parser.error("live 模式必须显式提供 --allow-paid；默认使用 dry-run/offline")
    if args.mode == "live" and not (args.history_report or args.production_db):
        parser.error("live 模式必须提供 --history-report 或 --production-db 以执行重复材料检查")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = args.manifest.resolve()
    items = load_manifest(manifest)
    checkpoint = Checkpoint.load(args.checkpoint)
    duplicate_report: dict[str, list[dict[str, str]]] = {}
    for item in items:
        # A checkpoint with a concrete project identity belongs to this run.
        # It may already be present in the production database after a safe
        # restart, so exclude it from the historical duplicate gate.
        if checkpoint.data["materials"].get(item["source_key"], {}).get("project_id"):
            continue
        matches = duplicate_matches(item, args.history_report, args.production_db,
                                    exclude=(manifest, args.checkpoint))
        if matches:
            duplicate_report[item["source_key"]] = matches
    if duplicate_report:
        raise EvalError("发现历史重复材料，已拒绝创建项目：" + ", ".join(duplicate_report))
    evaluator = Evaluator(args, manifest, items, checkpoint)
    try:
        report = evaluator.run()
        report["duplicate_check"] = {"reports": [str(p) for p in args.history_report], "production_dbs": [str(p) for p in args.production_db], "matches": duplicate_report}
        output = timestamped_path(args.output, "v2-real-eval", ".json")
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(output), "checkpoint": str(args.checkpoint), "materials": len(items), "mode": args.mode}, ensure_ascii=False))
        return 0
    finally:
        evaluator.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EvalError as exc:
        raise SystemExit(f"v2-real-eval: {exc}")
