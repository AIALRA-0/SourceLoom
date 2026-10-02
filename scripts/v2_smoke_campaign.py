"""Prepare and run a small, resumable live smoke campaign on fresh public sources.

This intentionally delegates the actual project lifecycle to ``v2_real_eval`` so
the smoke run exercises the same create → intake → produce → poll → export path.
The default command only downloads and fingerprints source material.  Model
calls require the explicit ``run --allow-paid`` command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

# Direct invocation (``python scripts/v2_smoke_campaign.py``) puts only the
# scripts directory at the front of sys.path. Add the repository root so both
# the evaluator module and its normal project imports resolve consistently.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.v2_real_eval import (
    Checkpoint,
    EvalError,
    Evaluator,
    duplicate_matches,
    load_manifest,
    parse_args as parse_eval_args,
    resolve_local,
    timestamped_path,
)


SAMPLES = (
    {
        "kind": "web",
        "name": "W3C WCAG Contrast Minimum guidance",
        "url": "https://www.w3.org/WAI/WCAG21/Understanding/contrast-minimum.html",
        "media_type": "text/html",
    },
    {
        "kind": "markdown",
        "name": "Astral uv README",
        "url": "https://raw.githubusercontent.com/astral-sh/uv/main/README.md",
        "media_type": "text/markdown",
        "filename": "astral-uv-readme.md",
    },
    {
        "kind": "pdf",
        "name": "IRTF RFC 9775 PDF",
        "url": "https://www.rfc-editor.org/rfc/rfc9775.pdf",
        "media_type": "application/pdf",
        "filename": "rfc-9775.pdf",
    },
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prepare(output: Path, timeout: float) -> Path:
    """Fetch three public sources and create a private, hashed evaluator manifest."""
    materials_dir = timestamped_path(output, "materials", "")
    materials_dir.mkdir(parents=True, exist_ok=False)
    records: list[dict[str, Any]] = []
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "SourceLoom-smoke/1.0"}) as client:
        for sample in SAMPLES:
            response = client.get(sample["url"])
            response.raise_for_status()
            payload = response.content
            if not payload:
                raise EvalError(f"公开素材下载为空：{sample['name']}")
            if sample["kind"] == "pdf" and not payload.startswith(b"%PDF-"):
                raise EvalError(f"公开素材未返回 PDF：{sample['name']}")
            if sample["kind"] == "markdown" and b"\x00" in payload[:4096]:
                raise EvalError(f"Markdown 素材包含二进制数据：{sample['name']}")
            record: dict[str, Any] = {
                "kind": sample["kind"],
                "name": sample["name"],
                "summary": f"fresh-source:{sample['url']}",
                "sha256": digest(payload),
                "source_url": sample["url"],
                "source_bytes": len(payload),
            }
            if sample["kind"] == "web":
                # The production flow receives the URL itself. The preflight
                # digest identifies the fetched snapshot for duplicate checks.
                record["url"] = sample["url"]
                record["preflight_content_type"] = response.headers.get("content-type", "")
            else:
                filename = str(sample["filename"])
                path = materials_dir / filename
                path.write_bytes(payload)
                record["local_path"] = filename
                record["preflight_content_type"] = response.headers.get("content-type", "")
            records.append(record)
    manifest = materials_dir / "manifest.json"
    manifest.write_text(json.dumps({"materials": records}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"manifest": str(manifest.resolve()), "kinds": [r["kind"] for r in records],
                      "source_bytes": {r["kind"]: r["source_bytes"] for r in records}}, ensure_ascii=False))
    return manifest


def parse_run_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--history-report", action="append", type=Path, required=True)
    parser.add_argument("--production-db", action="append", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, default=Path(".local/v2-smoke/checkpoint.json"))
    parser.add_argument("--output", type=Path, default=Path(".local/v2-smoke"))
    parser.add_argument("--poll-interval", type=float, default=3.0)
    parser.add_argument("--max-poll-seconds", type=float, default=1800.0)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--budget-cny", type=float, default=None)
    parser.add_argument("--goal", default="请将材料改写为忠实原文、清楚易读的中文内容，保留原文的范围、条件、链接和重要细节；不把普通说明扩写成额外教学情境")
    parser.add_argument("--proxy-subject-env", default="SOURCELOOM_EVAL_SUBJECT")
    parser.add_argument("--allow-paid", action="store_true", help="确认开始可能产生费用的真实生成")
    args = parser.parse_args(argv)
    if not args.allow_paid:
        parser.error("真实生成必须显式提供 --allow-paid")
    return args


def compare_source_bytes(evaluator: Evaluator, item: dict[str, Any], record: dict[str, Any], output: bytes) -> dict[str, Any]:
    """Prove that a delivered candidate is not a byte-for-byte source fallback."""
    local = resolve_local(item, evaluator.manifest)
    source: bytes | None = local.read_bytes() if local else None
    pid = record.get("project_id")
    if source is None and pid and evaluator.client:
        detail = evaluator.client.detail(str(pid))
        originals = (detail.get("inventory") or {}).get("originals") or []
        # A website may create multiple original objects; compare against each
        # stored raw object, using the source digest to pick the preflight page.
        for original in originals:
            key = original.get("sha256")
            if not key:
                continue
            try:
                candidate = evaluator.client.request("GET", f"/api/projects/{pid}/original/{key}")
            except Exception:
                continue
            if isinstance(candidate, bytes) and candidate:
                source = candidate
                break
    same = bool(source is not None and output == source)
    return {
        "source_raw_bytes_available": source is not None,
        "source_raw_sha256": digest(source) if source is not None else None,
        "output_sha256": digest(output),
        "output_byte_identical_to_source": same,
        "candidate_nonempty": bool(output.strip()),
    }


def run(args: argparse.Namespace) -> int:
    manifest = args.manifest.resolve()
    items = load_manifest(manifest)
    kinds = {item["kind"] for item in items}
    if len(items) != 3 or kinds != {"web", "markdown", "pdf"}:
        raise EvalError("冒烟 manifest 必须恰好包含 web、markdown、pdf 各一份素材")
    checkpoint = Checkpoint.load(args.checkpoint)
    for item in items:
        prior = checkpoint.data["materials"].get(item["source_key"], {})
        if not prior.get("project_id"):
            matches = duplicate_matches(item, args.history_report, args.production_db,
                                        exclude=(manifest, args.checkpoint))
            if matches:
                raise EvalError(f"检测到历史已使用素材，拒绝重复测试：{item['kind']}")

    eval_argv = [
        str(manifest), "--mode", "live", "--allow-paid", "--base-url", args.base_url,
        "--checkpoint", str(args.checkpoint), "--output", str(args.output),
        "--poll-interval", str(args.poll_interval), "--max-poll-seconds", str(args.max_poll_seconds),
        "--timeout", str(args.timeout),
        "--goal", args.goal, "--proxy-subject-env", args.proxy_subject_env,
        *[entry for path in args.history_report for entry in ("--history-report", str(path))],
        *[entry for path in args.production_db for entry in ("--production-db", str(path))],
    ]
    if args.budget_cny is not None:
        eval_argv.extend(("--budget-cny", str(args.budget_cny)))
    eval_args = parse_eval_args(eval_argv)
    evaluator = Evaluator(eval_args, manifest, items, checkpoint)
    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    try:
        for item in items:
            item_started = time.perf_counter()
            try:
                result = evaluator.one(item)
                elapsed = time.perf_counter() - item_started
                safeguards: dict[str, Any] = {"candidate_nonempty": False, "output_byte_identical_to_source": None}
                if result.get("verified_success"):
                    artifact = Path(result["artifacts"])
                    output = (artifact / "output.md").read_bytes()
                    safeguards = compare_source_bytes(evaluator, item, result, output)
                    if safeguards["output_byte_identical_to_source"] is True:
                        result = evaluator.checkpoint.update(item["source_key"], verified_success=False,
                                                             status="source_fallback_detected",
                                                             quality_error="导出结果与原件逐字节相同")
                results.append({"kind": item["kind"], "name": item["name"],
                                "project_id": result.get("project_id"), "run_id": result.get("run_id"),
                                "status": result.get("status"), "delivery_state": result.get("delivery_state"),
                                "verified_success": result.get("verified_success") is True,
                                "elapsed_seconds": round(elapsed, 2), "calls": result.get("calls"),
                                "cost_cny": result.get("cost"), "safeguards": safeguards,
                                "artifacts": result.get("artifacts")})
            except Exception as exc:
                saved = checkpoint.update(item["source_key"], status="smoke_error",
                                          transient_error=f"{type(exc).__name__}: {exc}", verified_success=False)
                results.append({"kind": item["kind"], "name": item["name"],
                                "project_id": saved.get("project_id"), "status": "smoke_error",
                                "elapsed_seconds": round(time.perf_counter() - item_started, 2),
                                "error": f"{type(exc).__name__}: {exc}", "verified_success": False})
        report = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "manifest": str(manifest),
            "checkpoint": str(args.checkpoint.resolve()),
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "all_verified": len(results) == 3 and all(r["verified_success"] for r in results),
            "materials": results,
            "accepted_or_published": False,
        }
        report_path = timestamped_path(args.output.resolve(), "smoke-report", ".json")
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(report_path), "all_verified": report["all_verified"],
                          "elapsed_seconds": report["elapsed_seconds"], "materials": results}, ensure_ascii=False))
        return 0 if report["all_verified"] else 1
    finally:
        evaluator.close()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "prepare":
        parser = argparse.ArgumentParser(description="下载新公开素材并生成私有 smoke manifest")
        parser.add_argument("prepare")
        parser.add_argument("--output", type=Path, default=Path(".local/v2-smoke"))
        parser.add_argument("--timeout", type=float, default=45.0)
        args = parser.parse_args(argv)
        prepare(args.output.resolve(), args.timeout)
        return 0
    if argv and argv[0] == "run":
        return run(parse_run_args(argv[1:]))
    raise EvalError("用法：v2_smoke_campaign.py prepare [--output DIR]；或 run MANIFEST --allow-paid ...")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EvalError as exc:
        raise SystemExit(f"v2-smoke-campaign: {exc}")
