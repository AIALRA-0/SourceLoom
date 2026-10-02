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
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import httpx


# The evaluator is also invoked as ``python scripts/v2_real_eval.py`` from a
# deployed release directory. In that mode Python puts ``scripts/`` rather
# than the repository root at sys.path[0], so sibling package imports such as
# ``scripts.audit_real_outputs`` would otherwise fail during a real audit.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


TERMINAL = {"completed", "ready_for_review", "failed", "needs_attention", "cancelled", "uncertain", "paused"}
SUCCESS_STATUSES = {"completed", "ready_for_review"}
NORMAL_DELIVERY_STATES = {"ready_for_review", "accepted", "published"}
RECOVERED_DELIVERY_STATE = "recovered_ready_for_review"
DEFAULT_GOAL = "保持原文主旨、作者意图与人称，完整保留信息，改善逻辑与可读性，不自行设计课程或情境"
MAX_WORKERS = 4


class EvalError(RuntimeError):
    """A safe, user-facing evaluation error."""


class TransportUnavailable(EvalError):
    """The request never established a connection and is safe to retry."""


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


def _has_history_identity(value: Any) -> bool:
    """Treat only missing, null, or blank-string IDs as absent."""

    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _is_uncreated_planned_record(value: Any) -> bool:
    return (isinstance(value, Mapping)
            and str(value.get("status") or "").strip().casefold() == "planned"
            and not _has_history_identity(value.get("project_id"))
            and not _has_history_identity(value.get("run_id")))


def _remove_uncreated_planned_records(value: Any) -> Any:
    """Drop only explicit, uncreated ``planned`` rows from report materials."""

    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for key, child in value.items():
            if str(key).casefold() == "materials" and isinstance(child, list):
                cleaned[key] = [_remove_uncreated_planned_records(row) for row in child
                                if not _is_uncreated_planned_record(row)]
            else:
                cleaned[key] = _remove_uncreated_planned_records(child)
        return cleaned
    if isinstance(value, list):
        return [_remove_uncreated_planned_records(child) for child in value]
    return value


def _history_text_for_matching(path: Path, text: str) -> str:
    """Keep reports searchable while excluding only never-created dry-run rows."""

    if path.suffix.lower() == ".json":
        try:
            report = json.loads(text)
        except ValueError:
            return text
        return json.dumps(_remove_uncreated_planned_records(report), ensure_ascii=False)
    if path.suffix.lower() == ".jsonl":
        retained: list[str] = []
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                retained.append(line)
                continue
            if not _is_uncreated_planned_record(row):
                retained.append(json.dumps(_remove_uncreated_planned_records(row), ensure_ascii=False))
        return "\n".join(retained)
    return text


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
            text = _history_text_for_matching(path, text)
            if _matches_text(text, item):
                matches.append({"kind": "report", "path": str(path)})
    return matches


def scan_production_db(paths: Iterable[Path], item: Mapping[str, Any]) -> list[dict[str, str]]:
    """Check source identities without scanning prompt, draft, or blob columns.

    SourceLoom stores project snapshots as JSON in ``projects.body`` and
    ``source_versions.body``. Those documents can also contain large prompts
    and generated drafts, so inspect only known inventory identity paths with
    SQLite JSON1. The jobs table is excluded because its call records can be
    large. For older/custom schemas, inspect only explicitly named identity
    columns. An unreadable configured database is an error: silently skipping
    it would turn an incomplete duplicate check into a clean result.
    """

    matches: list[dict[str, str]] = []
    url = str(item.get("canonical_url") or "").strip()
    normalized_url = canonical_url(url) if url else ""
    digests = {str(value).casefold() for value in
               (item.get("verified_sha256"), item.get("sha256")) if value}
    summary_value = item.get("summary") or item.get("source_summary")
    summary = normalize_text(summary_value) if summary_value else ""
    if len(summary) < 12:
        summary = ""

    # A source identity may also be stored in these dedicated columns by an
    # older/custom deployment.  Generic body/content/prompt columns are
    # deliberately excluded.
    url_columns = {"canonical_url", "source_url", "original_url", "url"}
    sha_columns = {"sha256", "source_sha256", "original_sha256", "content_sha256"}
    summary_columns = {"summary", "source_summary", "description"}
    body_json_tables = {
        "projects": ("inventory",),
        "source_versions": ("inventory",),
    }

    def add_match(path: Path, table: str, column: str) -> None:
        record = {"kind": "production_db", "path": str(path), "table": table, "column": column}
        if record not in matches:
            matches.append(record)

    def same_url(value: Any) -> bool:
        if not isinstance(value, str) or not value.strip():
            return False
        try:
            return canonical_url(value) == normalized_url
        except ValueError:
            return False

    def json_paths(prefixes: tuple[str, ...], field: str) -> list[tuple[str, str]]:
        """Return (JSON path, safe report label) pairs for known source fields."""
        result: list[tuple[str, str]] = []
        for prefix in prefixes:
            root = f"$.{prefix}." if prefix else "$."
            if field == "url":
                suffixes = ("source_url", "url", "original_url")
                for suffix in suffixes:
                    result.append((root + suffix, (prefix + "." if prefix else "") + suffix))
            elif field == "sha":
                for collection in ("originals", "resources"):
                    result.append((root + collection + "[*].sha256",
                                   (prefix + "." if prefix else "") + collection + "[*].sha256"))
            elif field == "summary":
                result.append((root + "summary", (prefix + "." if prefix else "") + "summary"))
                result.append((root + "source_summary", (prefix + "." if prefix else "") + "source_summary"))
                for collection in ("objects",):
                    result.append((root + collection + "[*].text",
                                   (prefix + "." if prefix else "") + collection + "[*].text"))
        return result

    for path in paths:
        if not path.is_file():
            raise EvalError(f"生产数据库不存在，重复检查已中止：{path}")
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA mmap_size=0")
            tables = [str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            if not tables:
                raise EvalError(f"生产数据库没有可检查的表，重复检查已中止：{path}")
            found_supported_surface = False
            json1_available = True
            try:
                connection.execute("SELECT json_valid('{}')").fetchone()
            except sqlite3.Error:
                json1_available = False

            for table in tables:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
                    continue
                columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
                names = {str(row[1]).casefold(): str(row[1]) for row in columns}
                if any(name in url_columns | sha_columns | summary_columns for name in names):
                    found_supported_surface = True

                # Scan only dedicated identity columns and stream values in
                # small batches. This avoids applying lower()/LIKE to payloads.
                for normalized_name, column in names.items():
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", column):
                        continue
                    if normalized_name in url_columns and url:
                        cursor = connection.execute(f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL')
                        while batch := cursor.fetchmany(256):
                            if any(same_url(row[0]) for row in batch):
                                add_match(path, table, column)
                                break
                    elif normalized_name in sha_columns and digests:
                        found = connection.execute(
                            f'SELECT 1 FROM "{table}" WHERE lower("{column}") IN ({",".join("?" for _ in digests)}) LIMIT 1',
                            tuple(digests)).fetchone()
                        if found:
                            add_match(path, table, column)
                    elif normalized_name in summary_columns and summary:
                        cursor = connection.execute(f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL')
                        while batch := cursor.fetchmany(64):
                            if any(isinstance(row[0], str) and summary in normalize_text(row[0]) for row in batch):
                                add_match(path, table, column)
                                break

                if table.casefold() not in body_json_tables or "body" not in names:
                    continue
                if not json1_available:
                    raise EvalError(f"SQLite JSON1 不可用，无法安全检查项目清单：{path}")
                body = names["body"]
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", body):
                    continue
                # Invalid project snapshots make the configured duplicate
                # source incomplete. Fail closed without exposing row data.
                malformed = connection.execute(
                    f'SELECT 1 FROM "{table}" WHERE "{body}" IS NOT NULL AND NOT json_valid("{body}") LIMIT 1').fetchone()
                if malformed:
                    raise EvalError(f"生产数据库含有无法读取的项目记录，重复检查已中止：{path}")

                found_supported_surface = True
                prefixes = body_json_tables[table.casefold()]
                if url:
                    url_paths = json_paths(prefixes, "url")
                    # Older project snapshots may have put the source URL at
                    # the JSON root rather than under inventory.
                    url_paths.append(("$.source_url", "source_url"))
                    for json_path, label in url_paths:
                        cursor = connection.execute(
                            f'SELECT json_extract("{body}", ?) FROM "{table}" WHERE json_valid("{body}")',
                            (json_path,))
                        matched = False
                        while batch := cursor.fetchmany(256):
                            if any(same_url(row[0]) for row in batch):
                                matched = True
                                break
                        if matched:
                            add_match(path, table, label)
                            break

                if digests:
                    for json_path, label in json_paths(prefixes, "sha"):
                        # Array paths are expanded by json_each. The query
                        # returns only an existence bit, never source JSON.
                        if "[*]" not in json_path:
                            continue
                        root_path, member = json_path.split("[*]", 1)
                        array_path = root_path
                        member = member.lstrip(".")
                        found = connection.execute(
                            f'''SELECT 1 FROM "{table}" AS snapshot,
                                json_each(snapshot."{body}", ?) AS identity
                                WHERE lower(json_extract(identity.value, ?)) IN ({",".join("?" for _ in digests)})
                                LIMIT 1''',
                            (array_path, "$." + member, *digests)).fetchone()
                        if found:
                            add_match(path, table, label)

                if summary:
                    for json_path, label in json_paths(prefixes, "summary"):
                        if "[*]" not in json_path:
                            # Scalar metadata is typically small; only compare
                            # it when it is an exact normalized source summary.
                            cursor = connection.execute(
                                f'SELECT json_extract("{body}", ?) FROM "{table}" WHERE json_valid("{body}")',
                                (json_path,))
                            matched = False
                            while batch := cursor.fetchmany(256):
                                if any(isinstance(row[0], str) and summary == normalize_text(row[0]) for row in batch):
                                    matched = True
                                    break
                            if matched:
                                add_match(path, table, label)
                            continue
                        root_path, member = json_path.split("[*]", 1)
                        array_path = root_path
                        member = member.lstrip(".")
                        # The only expanded text path is inventory/source
                        # object text; prompts and generated drafts are never
                        # selected or normalized.
                        cursor = connection.execute(
                            f'''SELECT json_extract(identity.value, ?) FROM "{table}" AS snapshot,
                                json_each(snapshot."{body}", ?) AS identity
                                WHERE json_type(identity.value, '$.text') = 'text' ''',
                            ("$." + member, array_path))
                        while batch := cursor.fetchmany(16):
                            if any(isinstance(row[0], str) and summary in normalize_text(row[0]) for row in batch):
                                add_match(path, table, label)
                                break

            connection.close()
        except EvalError:
            if connection is not None:
                connection.close()
            raise
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            raise EvalError(f"无法安全检查生产数据库，重复检查已中止：{path} ({type(exc).__name__})") from exc
        if not found_supported_surface:
            raise EvalError(f"生产数据库没有受支持的来源身份字段，重复检查已中止：{path}")
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
                 proxy_subject: str | None = None, transport_retries: int = 3):
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)
        self.proxy_subject = proxy_subject
        self.transport_retries = max(0, int(transport_retries))

    def close(self) -> None:
        self.client.close()

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"X-SourceLoom": "1"}
        if self.proxy_subject:
            headers.update({"X-Aialra-Authenticated": "1", "X-Aialra-Sub": self.proxy_subject})
        if kwargs.get("json") is not None:
            headers["Content-Type"] = "application/json"
        headers.update(kwargs.pop("headers", {}) or {})
        for attempt in range(self.transport_retries + 1):
            try:
                response = self.client.request(method, self.base_url + path, headers=headers, **kwargs)
                break
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if attempt >= self.transport_retries:
                    raise TransportUnavailable(f"连接 SourceLoom 失败，保留 checkpoint 供下次续跑：{exc}") from exc
                time.sleep(min(2 ** attempt, 4))
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

    def rewrite(self, pid: str) -> dict[str, Any]:
        return self.request("POST", f"/api/projects/{pid}/rewrite", json={})

    def detail(self, pid: str) -> dict[str, Any]:
        return self.request("GET", f"/api/projects/{pid}")

    def output_markdown(self, pid: str) -> bytes:
        return self.request("GET", f"/api/projects/{pid}/output?format=markdown")

    def export_package(self, pid: str) -> bytes:
        return self.request("GET", f"/api/projects/{pid}/export")


def extract_costs(detail: Mapping[str, Any]) -> dict[str, Any]:
    rows = detail.get("costs") if isinstance(detail.get("costs"), list) else []
    result = {
        "estimated": 0.0, "reserved": 0.0, "unsettled": 0.0, "provider_actual": 0.0,
        "calls": len(rows), "rows": [],
        "known_settled_call_count": 0, "known_settled_cny": 0.0,
        "local_settled_estimate_call_count": 0, "local_settled_estimate_cny": 0.0,
        "reserved_call_count": 0, "reserved_cny": 0.0,
        "reserved_amount_complete": True,
        "unknown_cost_call_count": 0, "unknown_cost_cny": 0.0,
    }

    def amount(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            number = float(value)
        except (OverflowError, ValueError):
            return None
        return number if math.isfinite(number) else None

    def first_amount(*values: Any) -> float | None:
        for value in values:
            parsed = amount(value)
            if parsed is not None:
                return parsed
        return None

    def cny_currency(*values: Any) -> bool:
        return any(str(value or "").strip().upper() in {"CNY", "RMB", "¥", "￥"}
                   for value in values)

    for row in rows:
        if not isinstance(row, Mapping):
            row = {}
        body = row.get("body") if isinstance(row, Mapping) and isinstance(row.get("body"), Mapping) else {}
        estimated = body.get("local_estimate_cny", body.get("estimated_cny", body.get("estimate_cny")))
        unsettled = body.get("unsettled_cny")
        billing_status = str(body.get("billing_status", row.get("billing_status", "")) or "").strip().casefold()
        currencies = [value for value in (
            body.get("currency"), body.get("currency_code"),
            row.get("currency"), row.get("currency_code"),
        ) if value]
        currency = currencies[0] if currencies else None
        currency_conflict = any(not cny_currency(value) for value in currencies)
        currency_is_cny = bool(currencies) and not currency_conflict and cny_currency(*currencies)
        # Generic provider amounts have no safe currency unless the record says
        # so. A conflicting currency label makes even CNY-suffixed fields
        # ambiguous and therefore unknown.
        local_settled_estimate_cny = None
        if billing_status == "settled_estimate" and not currency_conflict:
            local_settled_estimate_cny = first_amount(body.get("actual_cny"))
            if local_settled_estimate_cny is None and currency_is_cny:
                local_settled_estimate_cny = first_amount(row.get("actual"))
        provider_actual = None if currency_conflict else first_amount(body.get("provider_actual_cny"))
        if provider_actual is None and billing_status == "settled" and not currency_conflict:
            provider_actual = first_amount(body.get("actual_cny"))
            if provider_actual is None and currency_is_cny:
                provider_actual = first_amount(body.get("provider_actual"), row.get("actual"))
        reservation_cny = None if currency_conflict else first_amount(
            body.get("reservation_cny"), body.get("reserved_cny"))
        if reservation_cny is None and currency_is_cny and not currency_conflict:
            reservation_cny = first_amount(body.get("reservation"), body.get("reserved"), row.get("reserved"))
        estimated_cny = first_amount(estimated)
        if estimated_cny is None:
            estimated_cny = local_settled_estimate_cny
        reserved = first_amount(body.get("reservation_cny"), body.get("reserved_cny"), reservation_cny)
        if unsettled is None and (billing_status in {"unsettled", "pending", "reserved"} or row.get("actual") is None):
            unsettled = estimated if estimated is not None else reserved
        unsettled_cny = first_amount(unsettled)
        for key, value in (("estimated", estimated_cny), ("reserved", reservation_cny),
                           ("unsettled", unsettled_cny), ("provider_actual", provider_actual)):
            if value is not None:
                result[key] += value

        is_local_estimate_settled = billing_status == "settled_estimate" and local_settled_estimate_cny is not None
        is_provider_settled = billing_status == "settled" and provider_actual is not None
        is_settled = is_local_estimate_settled or is_provider_settled
        is_reserved = reservation_cny is not None or billing_status == "reserved"
        if is_settled:
            result["known_settled_call_count"] += 1
            if is_local_estimate_settled:
                result["local_settled_estimate_call_count"] += 1
                result["local_settled_estimate_cny"] += local_settled_estimate_cny
                result["known_settled_cny"] += local_settled_estimate_cny
            if is_provider_settled:
                result["known_settled_cny"] += provider_actual
        if not is_provider_settled:
            result["unknown_cost_call_count"] += 1
        if is_reserved:
            result["reserved_call_count"] += 1
            if reservation_cny is None:
                result["reserved_amount_complete"] = False
            else:
                result["reserved_cny"] += reservation_cny
        elif not is_settled:
            # A not-yet-settled call may have a reservation not represented in
            # the detail payload, so the known reservation sum is incomplete.
            result["reserved_amount_complete"] = False

        result["rows"].append({
            "estimated": estimated_cny, "reserved": reservation_cny, "unsettled": unsettled_cny,
            "provider_actual": provider_actual, "billing_status": billing_status or None,
            "currency": currency, "known_settled_cny": (local_settled_estimate_cny
                if is_local_estimate_settled else provider_actual if is_provider_settled else None),
            "local_settled_estimate_cny": local_settled_estimate_cny,
            "reserved_cny": reservation_cny,
        })
    if result["unknown_cost_call_count"]:
        # The final amount for any unsettled/uncategorized call is not known.
        result["unknown_cost_cny"] = None
    result["provider_actual_complete"] = result["unknown_cost_call_count"] == 0
    return result


def resolve_local(item: Mapping[str, Any], manifest: Path) -> Path | None:
    if not item.get("local_path"):
        return None
    path = Path(str(item["local_path"]))
    return path if path.is_absolute() else manifest.parent / path


def expected_source_record(item: Mapping[str, Any], manifest: Path) -> dict[str, Any]:
    """Persist the source identity needed to re-audit the delivered project."""

    local = resolve_local(item, manifest)
    return {
        "source_key": str(item["source_key"]),
        "canonical_url": item.get("canonical_url"),
        "preflight_final_url": item.get("preflight_final_url"),
        "final_url": item.get("final_url"),
        "sha256": str(item.get("verified_sha256") or item.get("sha256") or "").lower(),
        "local_name": local.name if local else None,
    }


def campaign_cost_totals(materials: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize only explicitly known CNY values; unknown amounts stay null."""

    total_calls = 0
    settled_calls = 0
    settled_cny = 0.0
    local_estimate_calls = 0
    local_estimate_cny = 0.0
    provider_actual_cny = 0.0
    reserved_calls = 0
    reserved_cny = 0.0
    unknown_calls = 0
    reserved_complete = True
    for material in materials:
        cost = material.get("cost") if isinstance(material.get("cost"), Mapping) else {}

        def count(value: Any) -> int:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return 0
            try:
                finite = math.isfinite(float(value))
            except (OverflowError, ValueError):
                return 0
            if not finite:
                return 0
            return max(0, int(value))

        def amount(value: Any) -> float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return 0.0
            try:
                parsed = float(value)
            except (OverflowError, ValueError):
                return 0.0
            return parsed if math.isfinite(parsed) else 0.0

        record_calls = count(material.get("calls"))
        cost_calls = count(cost.get("calls"))
        sample_calls = max(record_calls, cost_calls)
        total_calls += sample_calls

        sample_settled_calls = min(sample_calls, count(cost.get("known_settled_call_count")))
        settled_calls += sample_settled_calls
        settled_cny += amount(cost.get("known_settled_cny"))
        local_estimate_calls += min(sample_calls, count(cost.get("local_settled_estimate_call_count")))
        local_estimate_cny += amount(cost.get("local_settled_estimate_cny"))
        provider_actual_cny += amount(cost.get("provider_actual"))

        sample_reserved_calls = min(sample_calls, count(cost.get("reserved_call_count")))
        reserved_calls += sample_reserved_calls
        if cost.get("reserved_cny") is not None:
            reserved_cny += amount(cost.get("reserved_cny"))
        if cost.get("reserved_amount_complete") is False or sample_calls > cost_calls:
            reserved_complete = False

        sample_unknown = max(count(cost.get("unknown_cost_call_count")), sample_calls - sample_settled_calls)
        unknown_calls += sample_unknown

    return {
        "call_count": total_calls,
        "known_settled_call_count": settled_calls,
        "known_settled_cny": settled_cny,
        "local_settled_estimate_call_count": local_estimate_calls,
        "local_settled_estimate_cny": local_estimate_cny,
        "provider_actual_cny": provider_actual_cny,
        "provider_actual_complete": unknown_calls == 0,
        "reserved_call_count": reserved_calls,
        "reserved_cny": reserved_cny,
        "reserved_amount_complete": reserved_complete,
        "unknown_cost_call_count": unknown_calls,
        "unknown_cost_cny": None if unknown_calls else 0.0,
    }


class Evaluator:
    def __init__(self, args: argparse.Namespace, manifest: Path, items: list[dict[str, Any]], checkpoint: Checkpoint):
        self.args = args
        self.manifest = manifest
        self.items = items
        self.checkpoint = checkpoint
        subject_env = getattr(args, "proxy_subject_env", "SOURCELOOM_EVAL_SUBJECT")
        proxy_subject = os.environ.get(subject_env) if subject_env else None
        self.client = SourceLoomClient(args.base_url, args.timeout, proxy_subject=proxy_subject,
                                       transport_retries=getattr(args, "transport_retries", 3)) if args.mode == "live" else None
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
        self.checkpoint.update(key, expected_source=expected_source_record(item, self.manifest))
        record = self.checkpoint.data["materials"].get(key, record)
        if record.get("manual_review"):
            return self.checkpoint.update(key, status="manual_review", verified_success=False)
        pid = record.get("project_id")
        if record.get("create_attempted") and not pid:
            return self.checkpoint.update(key, status="manual_review", verified_success=False,
                manual_review="create request may have been delivered; inspect server before retrying")
        if not pid:
            self.checkpoint.update(key, name=item["name"], kind=item["kind"], create_attempted=True,
                                   status="creating", request_id=uuid4().hex)
            try:
                project = self.client.create_project(item, self.args.goal, self.args.budget_cny)
            except TransportUnavailable:
                # ConnectError/ConnectTimeout means no HTTP connection was
                # established, so no project could have been created.
                self.checkpoint.update(key, create_attempted=False, status="retry_pending")
                raise
            pid = str(project["id"])
            self.checkpoint.update(key, project_id=pid, status="created")

        # Reload after every durable transition.  The in-memory ``record`` may
        # predate project creation when a campaign is resumed.
        record = self.checkpoint.data["materials"].get(key, record)
        intake_id = record.get("intake_run_id")
        intake_done = bool(record.get("intake_done"))
        if not intake_done:
            if record.get("intake_attempted") and not intake_id:
                # Reconcile an interrupted request before sending anything.
                # URL/upload intake uses a stable request_id, so resubmission
                # below is idempotent when no active intake can be discovered.
                observed = self.client.production(pid)
                if observed.get("intake") and observed.get("id"):
                    intake_id = str(observed["id"])
                    self.checkpoint.update(key, intake_run_id=intake_id, status="intake_queued")
                elif observed.get("status") == "not_started":
                    self.checkpoint.update(key, intake_attempted=False, status="created")
                    record = self.checkpoint.data["materials"][key]
                else:
                    detail = self.client.detail(pid)
                    if detail.get("inventory"):
                        intake_done = True
                        self.checkpoint.update(key, intake_done=True, status="intake_completed")
                    else:
                        return self.checkpoint.update(key, status="retry_pending",
                            retry_reason="无法确认先前导入请求，保留项目和请求标识供下次续跑",
                            verified_success=False)
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
                return self.checkpoint.update(key, status=str(state.get("status") or "intake_failed"),
                                              intake_status=state, verified_success=False)
            self.checkpoint.update(key, intake_done=True, status="intake_completed")

        record = self.checkpoint.data["materials"].get(key, record)
        run_id = record.get("run_id")
        if record.get("generate_attempted") and not run_id:
            observed = self.client.production(pid)
            if observed.get("id") and not observed.get("intake"):
                run_id = str(observed["id"])
                self.checkpoint.update(key, run_id=run_id, status=observed.get("status") or "generating")
            elif observed.get("status") == "not_started":
                self.checkpoint.update(key, generate_attempted=False, status="intake_completed")
                record = self.checkpoint.data["materials"][key]
            else:
                return self.checkpoint.update(key, status="retry_pending",
                    retry_reason="无法确认先前生成请求，保留项目供下次续跑",
                    verified_success=False)
        if not run_id:
            self.checkpoint.update(key, generate_attempted=True, status="generating")
            result = self.client.produce(pid)
            run_id = str(result.get("id") or result.get("job_id") or "")
            self.checkpoint.update(key, run_id=run_id or None)
        elif (record.get("transient_error") and record.get("recovered_run_id") == run_id):
            # A rewrite POST may have reached the server before its response
            # connection was interrupted.  The project is evaluator-owned, so
            # the current non-intake task is the only safe successor to adopt.
            observed = self.client.production(pid)
            if observed.get("id") and not observed.get("intake") and str(observed["id"]) != run_id:
                run_id = str(observed["id"])
                self.checkpoint.update(key, run_id=run_id, status=observed.get("status") or "generating")
        state = self.poll(pid, run_id or None)
        detail = self.client.detail(pid)
        delivery_state = str(state.get("delivery_state") or
                             (detail.get("production") or {}).get("delivery_state") or "")
        rewrite_count = int(record.get("rewrite_count") or 0)
        while (state.get("status") in SUCCESS_STATUSES and
               delivery_state == RECOVERED_DELIVERY_STATE and
               rewrite_count < getattr(self.args, "max_rewrites", 2)):
            rewrite_count += 1
            self.checkpoint.update(key, status="rewriting_recovered", rewrite_count=rewrite_count,
                                   recovered_run_id=run_id)
            result = self.client.rewrite(pid)
            run_id = str(result.get("id") or result.get("job_id") or "")
            if not run_id:
                raise EvalError(f"项目 {pid} 的 rewrite 未返回任务标识")
            self.checkpoint.update(key, run_id=run_id, status="generating")
            state = self.poll(pid, run_id)
            detail = self.client.detail(pid)
            delivery_state = str(state.get("delivery_state") or
                                 (detail.get("production") or {}).get("delivery_state") or "")
        costs = extract_costs(detail)
        record = self.checkpoint.update(key, status=state.get("status"), production_status=state,
                                        delivery_state=delivery_state, run_id=run_id,
                                        exported=False, verified_success=False,
                                        cost=costs, calls=state.get("call_count", costs["calls"]))
        successful = (state.get("status") in SUCCESS_STATUSES and
                      delivery_state in NORMAL_DELIVERY_STATES)
        if successful:
            folder = self.artifacts / re.sub(r"[^A-Za-z0-9_.-]+", "_", str(item["name"]))[:80]
            folder.mkdir(parents=True, exist_ok=True)
            markdown = self.client.output_markdown(pid)
            package = self.client.export_package(pid)
            if not markdown.strip():
                raise EvalError(f"项目 {pid} 的 Markdown 导出为空")
            if not package.startswith(b"PK"):
                raise EvalError(f"项目 {pid} 的 ReadWeave 包不是有效 ZIP 响应")
            # Reuse the campaign's read-only quality audit so evaluator
            # success means the current project, semantic review, protected
            # materials, Markdown and exported archive all passed the same
            # checks used for the final campaign report.
            from scripts.audit_real_outputs import audit_project
            quality_audit = audit_project(detail, markdown, package,
                                          record.get("expected_source"))
            (folder / "project.json").write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
            (folder / "cost.json").write_text(json.dumps(costs, ensure_ascii=False, indent=2), encoding="utf-8")
            (folder / "output.md").write_bytes(markdown)
            (folder / "readweave-package.zip").write_bytes(package)
            (folder / "quality-audit.json").write_text(
                json.dumps(quality_audit, ensure_ascii=False, indent=2), encoding="utf-8")
            failed_checks = [str(check.get("code")) for check in quality_audit.get("checks", [])
                             if not check.get("passed")]
            quality_error = "质量验收未通过：" + ", ".join(failed_checks) if failed_checks else None
            record = self.checkpoint.update(key, artifacts=str(folder), exported=True,
                                            verified_success=bool(quality_audit.get("passed")),
                                            quality_audit=quality_audit,
                                            quality_error=quality_error)
        elif state.get("status") in SUCCESS_STATUSES:
            record = self.checkpoint.update(key, exported=False, verified_success=False,
                status="quality_retry_exhausted" if delivery_state == RECOVERED_DELIVERY_STATE else "invalid_delivery_state",
                quality_error=f"终态 {state.get('status')} 的 delivery_state={delivery_state or '<missing>'}")
        return record

    def run(self) -> dict[str, Any]:
        campaign_started_at = datetime.now(timezone.utc)
        campaign_started_clock = time.perf_counter()
        results: list[dict[str, Any]] = []
        workers = min(self.args.workers, MAX_WORKERS)

        def run_sample(item: dict[str, Any]) -> dict[str, Any]:
            started_at = datetime.now(timezone.utc)
            started_clock = time.perf_counter()
            try:
                try:
                    result = self.one(item)
                except Exception as exc:
                    saved = self.checkpoint.update(item["source_key"], status="retry_pending",
                                                   transient_error=str(exc), verified_success=False)
                    result = dict(saved) | {"name": item["name"]}
            finally:
                ended_clock = time.perf_counter()
                ended_at = datetime.now(timezone.utc)
            return dict(result) | {
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "elapsed_seconds": round(max(0.0, ended_clock - started_clock), 6),
            }

        if self.args.mode == "live" and workers == 1:
            for item in self.items:
                result = run_sample(item)
                results.append(result)
                # Repair a failed cause before proceeding to more live
                # materials. Their manifest/checkpoint entries remain intact
                # and can be resumed after the shared issue is fixed.
                if result.get("verified_success") is not True:
                    break
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sourceloom-eval") as pool:
                futures = {pool.submit(run_sample, item): item for item in self.items}
                for future in as_completed(futures):
                    results.append(future.result())
        results.sort(key=lambda row: row.get("source_key", ""))
        verified = sum(row.get("verified_success") is True for row in results)
        campaign_ended_clock = time.perf_counter()
        campaign_ended_at = datetime.now(timezone.utc)
        return {"version": 1, "mode": self.args.mode, "created_at": datetime.now(timezone.utc).isoformat(),
                "manifest": str(self.manifest), "checkpoint": str(self.checkpoint.path), "workers": workers,
                "started_at": campaign_started_at.isoformat(), "ended_at": campaign_ended_at.isoformat(),
                "elapsed_seconds": round(max(0.0, campaign_ended_clock - campaign_started_clock), 6),
                "cost_totals": campaign_cost_totals(results),
                "accepted_or_published": False, "expected_materials": len(self.items),
                "attempted_materials": len(results), "verified_success_count": verified,
                "all_verified": bool(results) and len(results) == len(self.items) and verified == len(self.items),
                "materials": results}


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
    parser.add_argument("--transport-retries", type=int, default=3,
                        help="连接建立失败时的有限重试次数")
    parser.add_argument("--max-rewrites", type=int, default=2,
                        help="recovered_ready_for_review 自动重写次数上限")
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
    if args.transport_retries < 0 or args.max_rewrites < 0:
        parser.error("连接重试次数和自动重写次数必须非负")
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
        return 0 if args.mode != "live" or report["all_verified"] else 1
    finally:
        evaluator.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EvalError as exc:
        raise SystemExit(f"v2-real-eval: {exc}")
