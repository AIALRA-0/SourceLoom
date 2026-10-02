"""Read-only final audit for a completed real-material evaluation campaign.

This command makes no model calls and never mutates a project.  It reads the
project identities from an evaluator checkpoint, downloads the current
project, Markdown output and ReadWeave package, then applies falsifiable
delivery, coverage, translation and archive checks.  It exits non-zero unless
every checkpoint material passes every required check.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sys
import zipfile
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_real_eval import Checkpoint, SourceLoomClient, canonical_url, extract_costs, timestamped_path
from sourceloom.checks import inspect_draft
from sourceloom.media import reading_draft
from sourceloom.reading import presentation
from sourceloom.visual_sources import decorative_resource
from sourceloom.writing import canonical


FINAL_STATES = {"ready_for_review", "accepted", "published"}
PROTECTED_KINDS = {"image", "media", "table", "code", "formula", "link", "page", "attachment", "footnote"}
TEXT_KINDS = {"text", "heading", "page", "link", "footnote"}
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")


def check(rows: list[dict[str, Any]], code: str, passed: bool, detail: str = "") -> None:
    rows.append({"code": code, "passed": bool(passed), "detail": detail})


def strip_non_authored(markdown: str) -> str:
    """Remove code, URLs, raw tags and quoted original lines before language checks."""

    text = re.sub(r"```.*?```|~~~.*?~~~", " ", markdown, flags=re.S)
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"^\s*>.*$", " ", text, flags=re.M)
    text = re.sub(r"!\[[^]]*]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^]]*)]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+|<[^>]+>", " ", text)
    return text


def english_prose(text: str) -> bool:
    clean = strip_non_authored(text)
    return len(WORD.findall(clean)) >= 8 and len(CJK.findall(clean)) <= 2


def translated_block(markdown: str) -> bool:
    clean = strip_non_authored(markdown)
    return len(CJK.findall(clean)) >= 4


def translation_gaps(project: Mapping[str, Any]) -> list[dict[str, str]]:
    """Find English prose obligations that have no Chinese authored landing point.

    This is deliberately a high-precision omission detector, not a semantic
    equivalence claim.  Independent source-bound review is checked separately.
    """

    inventory = project.get("inventory") or {}
    draft = project.get("draft") or {}
    objects = {str(obj.get("id")): obj for obj in inventory.get("objects", [])}
    blocks = list(draft.get("blocks", []))
    gaps: list[dict[str, str]] = []
    for obligation in inventory.get("obligations", []):
        source = objects.get(str(obligation.get("object_id"))) or {}
        if source.get("kind") not in TEXT_KINDS or not english_prose(str(source.get("text") or "")):
            continue
        oid = str(obligation.get("id"))
        landing = [block for block in blocks if oid in block.get("obligation_ids", [])]
        authored = [block for block in landing if block.get("kind") not in {"source", "object"}
                    and block.get("unit_id") != "recovered-source"]
        if not any(translated_block(str(block.get("markdown") or "")) for block in authored):
            gaps.append({"obligation_id": oid, "source_id": str(source.get("id")),
                         "reason": "英文正文义务没有中文改写落点"})
    return gaps


def verify_zip(raw: bytes, project: Mapping[str, Any]) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    inventory = project.get("inventory") or {}
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        names = set(archive.namelist())
        if "!!!meta.json" not in names or "material.html" not in names:
            errors.append("阅读包缺少 !!!meta.json 或 material.html")
            return errors, {"entries": len(names)}
        meta = json.loads(archive.read("!!!meta.json"))
        files = meta.get("files") if isinstance(meta, dict) else None
        if not isinstance(files, list) or len(files) != 1:
            errors.append("阅读包元数据必须包含一个根笔记")
            return errors, {"entries": len(names)}
        attachments = files[0].get("attachments") or []
        for attachment in attachments:
            if attachment.get("dataFileName") not in names:
                errors.append("阅读包附件文件缺失：" + str(attachment.get("title") or attachment.get("attachmentId")))
        expected = [(item, item.get("name")) for item in inventory.get("resources", [])]
        expected += [(item, item.get("name")) for item in inventory.get("originals", [])]
        for item, title in expected:
            digest = str(item.get("sha256") or item.get("id") or "")
            matches = [row for row in attachments if row.get("title") == title and row.get("dataFileName") in names]
            if not any(hashlib.sha256(archive.read(row["dataFileName"])).hexdigest() == digest for row in matches):
                errors.append("阅读包没有逐字节保存材料：" + str(title or digest))
        audit_rows = [row for row in attachments if row.get("title") == "sourceloom-audit.json"]
        if len(audit_rows) != 1:
            errors.append("阅读包缺少唯一审计附件")
        else:
            audit = json.loads(archive.read(audit_rows[0]["dataFileName"]))
            if audit.get("revision") != project.get("revision"):
                errors.append("阅读包审计版本与项目版本不一致")
            if (audit.get("inventory") or {}).get("digest") != inventory.get("digest"):
                errors.append("阅读包审计清单与项目清单不一致")
        html = archive.read("material.html").decode("utf-8", errors="replace")
        anchors = set(re.findall(r'data-readweave-anchor-id=["\']([^"\']+)', html))
        expected_anchors = {str(block.get("id")) for block in (project.get("draft") or {}).get("blocks", [])}
        if anchors != expected_anchors:
            errors.append("阅读包段落锚点与正文段落身份不一致")
        return errors, {"entries": len(names), "attachments": len(attachments),
                        "anchors": len(anchors)}


def expected_source_identity(value: Any) -> dict[str, Any]:
    """Normalize a manifest identity or its checkpoint source key."""

    if isinstance(value, Mapping):
        result = dict(value)
        key = str(result.get("source_key") or "")
        if key.startswith("url:") and "|sha256:" in key:
            key_url, key_sha = key[4:].rsplit("|sha256:", 1)
            if not result.get("canonical_url"):
                result["canonical_url"] = key_url
            result.setdefault("sha256", key_sha)
        elif key.startswith("sha256:"):
            result.setdefault("sha256", key[7:])
        return result
    key = str(value or "")
    if key.startswith("url:") and "|sha256:" in key:
        url, sha = key[4:].rsplit("|sha256:", 1)
        return {"source_key": key, "canonical_url": url, "sha256": sha}
    if key.startswith("sha256:"):
        return {"source_key": key, "sha256": key[7:]}
    return {}


def web_snapshot_source_match(inventory: Mapping[str, Any], expected_sha: str) -> tuple[bool, str]:
    """Ensure a rendered page is bound to the verified original response."""
    originals=[row for row in inventory.get("originals",[]) if isinstance(row,Mapping)]
    browser_original=next((row for row in originals if row.get("name")=="web-original.bin"),None)
    snapshots=[row for row in originals if str(row.get("name") or "").startswith("snapshot.")]
    if not snapshots:
        return False,"web snapshot original is missing"
    snapshot=next((row for row in snapshots if row.get("name")=="snapshot.html"),snapshots[0])
    snapshot_sha=str(snapshot.get("sha256") or "").lower()
    if not browser_original:
        if snapshot_sha==expected_sha:
            return True,"original response is the selected snapshot"
        return False,"snapshot SHA-256 differs from expected response and no browser original is archived"

    original_sha=str(browser_original.get("sha256") or "").lower()
    if original_sha!=expected_sha:
        return False,"archived web-original.bin SHA-256 differs from expected response"
    continuity=(inventory.get("web_snapshot") or {}).get("source_continuity")
    if not isinstance(continuity,Mapping):
        if snapshot_sha==original_sha:
            return True,"snapshot bytes match the archived original response"
        return False,"rendered snapshot differs from original response without continuity metadata"
    if (str(continuity.get("original_response_sha256") or "").lower()!=original_sha
            or str(continuity.get("snapshot_name") or "")!=str(snapshot.get("name") or "")
            or str(continuity.get("snapshot_sha256") or "").lower()!=snapshot_sha):
        return False,"continuity metadata does not match archived original and snapshot hashes"
    basis=continuity.get("basis")
    if basis=="original_response":
        if snapshot_sha!=original_sha:
            return False,"selected original-response snapshot hash differs from the verified response"
        return True,"selected snapshot is the verified original response"
    if basis=="browser_render":
        if (continuity.get("status")!="continuous"
                or str(continuity.get("rendered_candidate_sha256") or "").lower()!=snapshot_sha
                or continuity.get("reason")!="source_content_continuity"):
            return False,"browser snapshot has no accepted source-continuity result"
        return True,"browser snapshot hash matches its accepted source-continuity manifest"
    return False,"snapshot continuity metadata has an unknown source basis"


def source_identity_match(project: Mapping[str, Any], expected: Any) -> tuple[bool, str]:
    """Bind the project to the exact URL/local bytes recorded by the manifest."""

    identity = expected_source_identity(expected)
    sha = str(identity.get("verified_sha256") or identity.get("sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        return False, "expected SHA-256 missing"
    key = str(identity.get("source_key") or "")
    key_identity = expected_source_identity(key) if key else {}
    key_sha = str(key_identity.get("sha256") or "").lower()
    if key_sha and key_sha != sha:
        return False, "checkpoint source key SHA-256 differs from manifest SHA-256"
    inventory = project.get("inventory") or {}
    originals = inventory.get("originals") or []
    url = str(identity.get("canonical_url") or "").strip()
    key_url = str(key_identity.get("canonical_url") or "").strip()
    if url and key_url:
        try:
            if canonical_url(url) != canonical_url(key_url):
                return False, "checkpoint source key URL differs from manifest URL"
        except (ValueError, TypeError):
            return False, "expected source URL is invalid"
    if url:
        allowed_urls = {url}
        for key in ("preflight_final_url", "final_url"):
            candidate = str(identity.get(key) or "").strip()
            if candidate:
                allowed_urls.add(candidate)
        try:
            allowed = {canonical_url(candidate) for candidate in allowed_urls}
            observed = canonical_url(str(inventory.get("source_url") or ""))
        except (ValueError, TypeError):
            return False, "source URL is invalid"
        if not observed or observed not in allowed:
            return False, "project source URL differs from requested/preflight final URL"
        primary = [row for row in originals if isinstance(row, Mapping)
                   and (str(row.get("name") or "") == "web-original.bin"
                        or str(row.get("name") or "").startswith("snapshot."))]
        if not any(str(row.get("sha256") or "").lower() == sha for row in primary):
            return False, "project has no matching original web response SHA-256"
        if any(isinstance(row,Mapping) and str(row.get("name") or "").startswith("snapshot.")
               for row in originals):
            snapshot_ok,snapshot_detail=web_snapshot_source_match(inventory,sha)
            if not snapshot_ok:return False,snapshot_detail
        return True, "URL and original response SHA-256 match"

    local_name = str(identity.get("local_name") or "").strip()
    matching = [row for row in originals if isinstance(row, Mapping)
                and str(row.get("sha256") or "").lower() == sha
                and (not local_name or str(row.get("name") or "") == local_name)]
    if not matching:
        return False, "project has no matching local original SHA-256"
    return True, "local original SHA-256 matches"


def audit_project(project: Mapping[str, Any], markdown: bytes, package: bytes,
                  expected_source: Any = None) -> dict[str, Any]:
    # Keep an unfinished project auditable instead of replacing its concrete
    # failures with a Python exception.
    project = dict(project)
    if not isinstance(project.get("inventory"), Mapping):
        project["inventory"] = {"frozen": False, "objects": [], "obligations": [], "resources": [],
                                "originals": [], "unknown": [{"reason": "尚未形成材料清单"}]}
    if not isinstance(project.get("draft"), Mapping):
        project["draft"] = {"blocks": []}
    checks: list[dict[str, Any]] = []
    production = project.get("production") or {}
    delivery = str(project.get("delivery_state") or production.get("delivery_state") or "")
    status = str(production.get("status") or "")
    check(checks, "terminal_delivery", delivery in FINAL_STATES and status in {"completed", "ready_for_review"},
          f"status={status}, delivery_state={delivery}")
    recovered = delivery.startswith("recovered") or any(
        block.get("unit_id") == "recovered-source" or str(block.get("id", "")).startswith("recovered-")
        for block in (project.get("draft") or {}).get("blocks", []))
    check(checks, "no_recovered_draft", not recovered, delivery)
    independent = project.get("independent_review") or {}
    delivery_checks = production.get("delivery_checks") or {}
    check(checks, "independent_semantic_review",
          independent.get("status") == "passed" and delivery_checks.get("semantic_status") == "passed",
          f"review={independent.get('status')}, semantic={delivery_checks.get('semantic_status')}")
    quality_issues = list(production.get("issues") or [])
    check(checks, "no_recorded_quality_issues", not quality_issues, f"count={len(quality_issues)}")

    inventory = project.get("inventory") or {}
    draft = project.get("draft") or {}
    objects = list(inventory.get("objects") or [])
    obligations = list(inventory.get("obligations") or [])
    originals = list(inventory.get("originals") or [])
    object_ids = {str(row.get("id")) for row in objects if isinstance(row, Mapping) and row.get("id")}
    mapped_obligations = [row for row in obligations if isinstance(row, Mapping)
                          and str(row.get("object_id") or "") in object_ids]
    nonempty_inventory = (inventory.get("frozen") is True and bool(objects) and bool(obligations)
                          and bool(originals) and bool(mapped_obligations)
                          and len(mapped_obligations) == len(obligations))
    check(checks, "nonempty_frozen_source_inventory", nonempty_inventory,
          f"frozen={inventory.get('frozen') is True}, objects={len(objects)}, obligations={len(obligations)}, originals={len(originals)}, mapped={len(mapped_obligations)}")
    identity_ok, identity_detail = source_identity_match(project, expected_source)
    check(checks, "source_identity_exact", identity_ok, identity_detail)
    expected_identity=expected_source_identity(expected_source)
    expected_sha=str(expected_identity.get("verified_sha256") or expected_identity.get("sha256") or "").lower()
    is_web_source=bool(str(inventory.get("source_url") or "").strip()
                       or str(expected_identity.get("canonical_url") or "").strip())
    if is_web_source:
        snapshot_ok,snapshot_detail=web_snapshot_source_match(inventory,expected_sha)
    else:
        snapshot_ok,snapshot_detail=True,"not applicable: source is not a web URL"
    check(checks,"web_snapshot_source_continuity",snapshot_ok,snapshot_detail)
    findings = inspect_draft(inventory, draft, project.get("plan"),
                             require_heading_structure=production.get("teaching_version", 0) >= 2)
    check(checks, "mechanical_source_binding", not findings, f"findings={len(findings)}")
    unknown = list(inventory.get("unknown") or [])
    check(checks, "no_unknown_source_regions", not unknown, f"count={len(unknown)}")

    obligation_ids = {str(item.get("id")) for item in inventory.get("obligations", [])}
    covered = {str(oid) for block in draft.get("blocks", []) for oid in block.get("obligation_ids", [])}
    missing_obligations = sorted(obligation_ids - covered)
    check(checks, "all_obligations_represented", not missing_obligations,
          f"covered={len(covered & obligation_ids)}/{len(obligation_ids)}")

    represented = {str(sid) for block in draft.get("blocks", [])
                   for sid in [*block.get("object_ids", []), *block.get("embedded_object_ids", [])]}
    required_objects = {str(obj.get("id")) for obj in objects
                        if (obj.get("kind") in PROTECTED_KINDS or obj.get("material_candidate")
                            or obj.get("source_scope") == "article_media") and not decorative_resource(obj)}
    missing_objects = sorted(required_objects - represented)
    check(checks, "protected_materials_represented", not missing_objects,
          f"represented={len(required_objects)-len(missing_objects)}/{len(required_objects)}")

    snapshot = inventory.get("web_snapshot")
    web_complete = not is_web_source
    web_detail = "not applicable: source is not a web URL"
    if is_web_source:
        web_complete = isinstance(snapshot, Mapping)
        if web_complete:
            summary = snapshot.get("material_summary")
            manifest = snapshot.get("material_manifest")
            rendered = snapshot.get("rendered_capture")
            figures = summary.get("article_figures") if isinstance(summary, Mapping) else None
            ready_figures = summary.get("ready_article_figures") if isinstance(summary, Mapping) else None
            gaps = summary.get("gaps") if isinstance(summary, Mapping) else None
            concrete_summary = (
                isinstance(figures, int) and not isinstance(figures, bool) and figures >= 0
                and isinstance(ready_figures, int) and not isinstance(ready_figures, bool)
                and ready_figures >= 0
                and isinstance(gaps, list)
            )
            # Explicit counts and the material manifest keep a real page with
            # zero figures distinguishable from an absent or partial capture.
            web_complete = (
                concrete_summary
                and isinstance(manifest, (list, Mapping))
                and not gaps
                and figures == ready_figures
            )
            if rendered is not None:
                if not isinstance(rendered, Mapping):
                    web_complete = False
                elif rendered.get("attempted"):
                    rendered_gaps = rendered.get("gaps")
                    web_complete = (web_complete and rendered.get("completed") is True
                                    and isinstance(rendered_gaps, list) and not rendered_gaps)
            rendered_gaps = rendered.get("gaps") if isinstance(rendered, Mapping) else None
            rendered_gap_count = (len(rendered_gaps) if isinstance(rendered_gaps, (list, Mapping))
                                  else "unknown" if rendered is not None else 0)
            web_detail = (f"manifest={'present' if isinstance(manifest, (list, Mapping)) else 'missing'}, "
                          f"figures={ready_figures if ready_figures is not None else 0}/"
                          f"{figures if figures is not None else 0}, "
                          f"gaps={len(gaps) if isinstance(gaps, list) else 'unknown'}, "
                          f"rendered_gaps={rendered_gap_count}")
        else:
            web_detail = "web_snapshot missing or invalid"
    check(checks, "web_material_acquisition_complete", web_complete,
          web_detail)

    gaps = translation_gaps(project)
    check(checks, "english_prose_has_chinese_landing", not gaps, f"gaps={len(gaps)}")
    expected_markdown = canonical(reading_draft(presentation(dict(project)))).encode("utf-8")
    check(checks, "markdown_export_exact", bool(markdown) and markdown == expected_markdown,
          f"bytes={len(markdown)}")
    try:
        zip_errors, zip_stats = verify_zip(package, project)
    except (zipfile.BadZipFile, KeyError, ValueError, UnicodeError) as exc:
        zip_errors, zip_stats = [f"阅读包不可验证：{type(exc).__name__}: {exc}"], {}
    check(checks, "readweave_export_complete", not zip_errors, "; ".join(zip_errors[:4]))

    release_codes = {str(row.get("code")) for row in (project.get("release_issues") or [])}
    blocking_release = sorted(release_codes - {"acceptance"})
    check(checks, "no_blocking_release_issue", not blocking_release,
          ",".join(blocking_release))
    costs = extract_costs(project)
    return {"project_id": project.get("id"), "passed": all(row["passed"] for row in checks),
            "checks": checks, "translation_gaps": gaps, "missing_obligation_ids": missing_obligations,
            "missing_protected_object_ids": missing_objects, "mechanical_findings": findings,
            "zip": zip_stats, "cost": costs}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=Path(".local/final-quality-audit"))
    parser.add_argument("--proxy-subject-env", default="SOURCELOOM_EVAL_SUBJECT")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--expected", type=int, default=10)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    checkpoint = Checkpoint.load(args.checkpoint)
    records = list(checkpoint.data["materials"].values())
    subject = os.environ.get(args.proxy_subject_env) if args.proxy_subject_env else None
    client = SourceLoomClient(args.base_url, args.timeout, proxy_subject=subject)
    results: list[dict[str, Any]] = []
    try:
        for record in records:
            pid = record.get("project_id")
            if not pid:
                results.append({"name": record.get("name"), "project_id": None, "passed": False,
                                "error": "checkpoint 没有 project_id"})
                continue
            try:
                project = client.detail(str(pid))
                fetch_errors = []
                try:
                    markdown = client.output_markdown(str(pid))
                except Exception as exc:
                    markdown = b""
                    fetch_errors.append(f"markdown: {type(exc).__name__}: {exc}")
                try:
                    package = client.export_package(str(pid))
                except Exception as exc:
                    package = b""
                    fetch_errors.append(f"readweave: {type(exc).__name__}: {exc}")
                result = audit_project(project, markdown, package,
                                       expected_source=record.get("expected_source") or record.get("source_key"))
                result.update(name=record.get("name"), kind=record.get("kind"), source_key=record.get("source_key"))
                result["export_errors"] = fetch_errors
                results.append(result)
            except Exception as exc:
                results.append({"name": record.get("name"), "project_id": pid, "passed": False,
                                "error": f"{type(exc).__name__}: {exc}"})
    finally:
        client.close()
    passed = sum(bool(row.get("passed")) for row in results)
    report = {"schema": "sourceloom/final-quality-audit/1",
              "created_at": datetime.now(timezone.utc).isoformat(), "checkpoint": str(args.checkpoint),
              "expected": args.expected, "total": len(results), "passed": passed,
              "all_passed": len(results) == args.expected and passed == args.expected,
              "scope_note": "字符规则只检测高置信度翻译缺漏，语义完整性另由逐义务独立审核状态证明",
              "materials": sorted(results, key=lambda row: str(row.get("name") or ""))}
    output = timestamped_path(args.output, "final-quality-audit", ".json")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(output), "passed": passed, "total": len(results),
                      "all_passed": report["all_passed"]}, ensure_ascii=False))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
