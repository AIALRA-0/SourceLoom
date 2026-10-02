import hashlib
from io import BytesIO
import json
import zipfile

from scripts.audit_real_outputs import audit_project, translation_gaps
from sourceloom.media import reading_draft
from sourceloom.reading import presentation
from sourceloom.writing import canonical


SOURCE_BYTES = b"frozen source"
SOURCE_SHA = hashlib.sha256(SOURCE_BYTES).hexdigest()


def project_fixture():
    text = "The service keeps every supplied image and translates the explanation."
    inv = {
        "frozen": True, "digest": "inventory-digest", "unknown": [],
        "objects": [
            {"id": "text", "kind": "text", "text": text},
            {"id": "image", "kind": "image", "text": "Chart", "resource_id": "image-digest",
             "material_candidate": True, "source_scope": "article_media"},
        ],
        "obligations": [
            {"id": "fact", "object_id": "text", "statement": text, "conditions": [], "quantities": [], "negations": [], "status": "reviewed"},
            {"id": "visual", "object_id": "image", "statement": "Chart", "conditions": [], "quantities": [], "negations": [], "status": "reviewed"},
        ],
        "resources": [{"id": "image-digest", "sha256": "image-digest", "name": "chart.png", "mime": "image/png"}],
        "originals": [{"name": "snapshot.md", "sha256": SOURCE_SHA}],
        "source_url": "https://example.test/source",
        "web_snapshot": {"material_manifest": [],
                         "material_summary": {"article_figures": 1, "ready_article_figures": 1, "gaps": []},
                         "rendered_capture": {"attempted": True, "completed": True, "gaps": []}},
    }
    draft = {"blocks": [{"id": "b1", "unit_id": "u1", "kind": "explanation",
                          "markdown": "## 说明\n\n服务会保留全部图片，并把原文说明完整改写成中文\n",
                          "obligation_ids": ["fact", "visual"], "object_ids": ["image"],
                          "embedded_object_ids": [], "evidence": [{"source_id": "text", "quote": text}]}]}
    plan = {"title": "x", "objective": "x", "research_gaps": [], "teaching_functions": [],
            "units": [{"id": "u1", "title": "x", "objective": "x", "obligation_ids": ["fact", "visual"],
                       "prerequisites": [], "stages": ["x"], "object_ids": ["text", "image"],
                       "proof_questions": [], "reader_question": "", "entry_knowledge": [], "example_thread": "",
                       "learning_result": "", "follows_units": [], "bridge_reason": "", "document_info_ids": []}]}
    return {"id": "project", "revision": 2, "accepted_revision": None, "inventory": inv, "draft": draft,
            "plan": plan, "delivery_state": "ready_for_review", "release_issues": [{"code": "acceptance"}],
            "independent_review": {"status": "passed"}, "costs": [],
            "production": {"status": "ready_for_review", "delivery_state": "ready_for_review", "issues": [],
                           "teaching_version": 0, "delivery_checks": {"semantic_status": "passed"}}}


def expected_source():
    return {"source_key": f"url:https://example.test/source|sha256:{SOURCE_SHA}",
            "canonical_url": "https://example.test/source", "sha256": SOURCE_SHA}


def package(project, resource=b"image"):
    digest = hashlib.sha256(resource).hexdigest()
    project["inventory"]["resources"][0].update(id=digest, sha256=digest)
    project["inventory"]["objects"][1]["resource_id"] = digest
    audit = json.dumps({"revision": project["revision"], "inventory": {"digest": project["inventory"]["digest"]}}).encode()
    rows = [
        {"title": "chart.png", "dataFileName": "asset-image"},
        {"title": "snapshot.md", "dataFileName": "asset-original"},
        {"title": "sourceloom-audit.json", "dataFileName": "asset-audit"},
    ]
    meta = {"files": [{"attachments": rows}]}
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("!!!meta.json", json.dumps(meta))
        archive.writestr("material.html", '<section data-readweave-anchor-id="b1">ok</section>')
        archive.writestr("asset-image", resource)
        archive.writestr("asset-original", SOURCE_BYTES)
        archive.writestr("asset-audit", audit)
    return output.getvalue()


def test_final_audit_passes_normal_reviewed_output_and_both_exports():
    project = project_fixture()
    archive = package(project)
    rendered = canonical(reading_draft(presentation(dict(project)))).encode()
    result = audit_project(project, rendered, archive, expected_source())
    assert result["passed"] is True


def test_web_acquisition_fails_when_snapshot_or_concrete_evidence_is_missing():
    project = project_fixture()
    del project["inventory"]["web_snapshot"]
    failed = {row["code"] for row in audit_project(
        project, b"", b"", expected_source())["checks"] if not row["passed"]}
    assert "web_material_acquisition_complete" in failed

    project = project_fixture()
    project["inventory"]["web_snapshot"].pop("material_manifest")
    failed = {row["code"] for row in audit_project(
        project, b"", b"", expected_source())["checks"] if not row["passed"]}
    assert "web_material_acquisition_complete" in failed

    project = project_fixture()
    project["inventory"]["web_snapshot"] = {}
    failed = {row["code"] for row in audit_project(
        project, b"", b"", expected_source())["checks"] if not row["passed"]}
    assert "web_material_acquisition_complete" in failed


def test_web_acquisition_accepts_explicit_zero_figure_capture():
    project = project_fixture()
    project["inventory"]["web_snapshot"]["material_manifest"] = []
    project["inventory"]["web_snapshot"]["material_summary"] = {
        "article_figures": 0, "ready_article_figures": 0, "gaps": []}
    project["inventory"]["web_snapshot"].pop("rendered_capture")
    archive = package(project)
    rendered = canonical(reading_draft(presentation(dict(project)))).encode()

    result = audit_project(project, rendered, archive, expected_source())
    check = next(row for row in result["checks"]
                 if row["code"] == "web_material_acquisition_complete")
    assert check["passed"] is True


def test_web_acquisition_check_does_not_apply_to_local_sources():
    project = project_fixture()
    inventory = project["inventory"]
    inventory["source_url"] = None
    inventory["originals"] = [{"name": "source.md", "sha256": SOURCE_SHA}]
    inventory.pop("web_snapshot")
    expected = {"source_key": f"sha256:{SOURCE_SHA}", "sha256": SOURCE_SHA,
                "local_name": "source.md"}
    archive = package(project)
    rendered = canonical(reading_draft(presentation(dict(project)))).encode()

    result = audit_project(project, rendered, archive, expected)
    check = next(row for row in result["checks"]
                 if row["code"] == "web_material_acquisition_complete")
    assert check["passed"] is True


def test_translation_gap_and_recovered_draft_are_hard_failures():
    project = project_fixture()
    project["draft"]["blocks"][0]["markdown"] = "The output leaves the whole explanation in English."
    project["draft"]["blocks"][0]["unit_id"] = "recovered-source"
    project["delivery_state"] = "recovered_ready_for_review"
    archive = package(project)
    assert translation_gaps(project)
    result = audit_project(project, canonical(project["draft"]).encode(), archive, expected_source())
    failed = {row["code"] for row in result["checks"] if not row["passed"]}
    assert "no_recovered_draft" in failed
    assert "english_prose_has_chinese_landing" in failed


def test_missing_protected_material_and_nonexact_markdown_fail():
    project = project_fixture()
    project["draft"]["blocks"][0]["object_ids"] = []
    archive = package(project)
    result = audit_project(project, b"different", archive, expected_source())
    failed = {row["code"] for row in result["checks"] if not row["passed"]}
    assert "protected_materials_represented" in failed
    assert "markdown_export_exact" in failed


def test_audit_rejects_empty_source_project_that_would_otherwise_match_exports():
    project = project_fixture()
    project["inventory"].update(objects=[], obligations=[], originals=[], source_url="")
    project["inventory"]["web_snapshot"] = {}
    project["draft"]["blocks"] = [{"id": "b1", "unit_id": "u1", "kind": "explanation",
        "markdown": "随便编造一段看似完整的内容", "obligation_ids": [], "object_ids": [],
        "evidence": [], "embedded_object_ids": []}]
    expected_markdown = canonical(reading_draft(presentation(dict(project)))).encode()
    audit = {"revision": project["revision"], "inventory": {"digest": project["inventory"]["digest"]}}
    meta = {"files": [{"attachments": [{"title": "sourceloom-audit.json", "dataFileName": "audit.json"}]}]}
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("!!!meta.json", json.dumps(meta))
        archive.writestr("material.html", '<section data-readweave-anchor-id="b1"></section>')
        archive.writestr("audit.json", json.dumps(audit))

    result = audit_project(project, expected_markdown, output.getvalue(), expected_source())
    failed = {row["code"] for row in result["checks"] if not row["passed"]}
    assert result["passed"] is False
    assert {"nonempty_frozen_source_inventory", "source_identity_exact"} <= failed


def test_source_identity_rejects_wrong_url_and_original_hash():
    from scripts.audit_real_outputs import source_identity_match

    project = project_fixture()
    assert source_identity_match(project, expected_source())[0] is True
    project["inventory"]["source_url"] = "https://example.test/other"
    passed, detail = source_identity_match(project, expected_source())
    assert passed is False and "URL" in detail

    project["inventory"]["source_url"] = "https://example.test/source"
    project["inventory"]["originals"][0]["sha256"] = "0" * 64
    passed, detail = source_identity_match(project, expected_source())
    assert passed is False and "SHA-256" in detail


def test_web_identity_accepts_only_manifested_redirect_target_with_matching_raw_bytes():
    from scripts.audit_real_outputs import source_identity_match

    requested = "https://example.test/start"
    final = "https://cdn.example.test/article.md?b=2&a=1"
    project = {"inventory": {"source_url": "https://cdn.example.test/article.md?a=1&b=2#page",
        "originals": [{"name": "web-original.bin", "sha256": SOURCE_SHA}]}}
    expected = {"source_key": f"url:{requested}|sha256:{SOURCE_SHA}", "canonical_url": requested,
                "preflight_final_url": final, "sha256": SOURCE_SHA}
    assert source_identity_match(project, expected)[0] is True
    expected.pop("preflight_final_url")
    assert source_identity_match(project, expected)[0] is False


def test_actual_flask_markdown_original_hash_binds_to_expected_source():
    from scripts.audit_real_outputs import source_identity_match

    # SHA and original filename are from the real Flask README material used
    # in the current campaign manifest; the test stays independent of ignored
    # local corpus files and never refetches or regenerates it.
    sha = "1f2de14735b1ee9d3a342fa7c5d5e87b95727276c0a56c8a9d77221f37880602"
    project = {"inventory": {"source_url": None,
        "originals": [{"name": "flask-readme.md", "sha256": sha}]}}
    expected = {"source_key": f"sha256:{sha}", "sha256": sha, "local_name": "flask-readme.md"}
    assert source_identity_match(project, expected) == (True, "local original SHA-256 matches")


def test_local_upload_intake_records_the_client_filename_as_original(tmp_path):
    from scripts.audit_real_outputs import source_identity_match
    from sourceloom.ingest import intake
    from sourceloom.store import Store

    name = "flask-readme.md"
    inventory = intake(Store(tmp_path), [(name, SOURCE_BYTES)])
    assert inventory["originals"][0] == {
        "name": name, "sha256": SOURCE_SHA, "size": len(SOURCE_BYTES)}
    assert source_identity_match({"inventory": inventory}, {
        "source_key": f"sha256:{SOURCE_SHA}", "sha256": SOURCE_SHA, "local_name": name})[0]


def test_unfinished_project_reports_failures_instead_of_crashing():
    result = audit_project({"id": "unfinished", "inventory": None, "draft": None,
                            "production": {}, "release_issues": None}, b"", b"")
    assert result["passed"] is False
    failed = {row["code"] for row in result["checks"] if not row["passed"]}
    assert {"terminal_delivery", "mechanical_source_binding", "markdown_export_exact",
            "readweave_export_complete"} <= failed
