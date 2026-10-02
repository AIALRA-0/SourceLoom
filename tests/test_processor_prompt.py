"""The plain-model task sees the pinned, conflict-resolved writing rules."""

import hashlib
from io import BytesIO
from pathlib import Path

from PIL import Image

from sourceloom import processor
from sourceloom.processor_prompt import (
    SKILL_COMMIT,
    TEMPLATE_VERSION,
    _PINNED_SHA256,
    build_effective_template,
)
from sourceloom.store import Store


def test_skill_snapshot_is_byte_identical_to_pinned_commit():
    root = Path(__file__).parents[1] / "sourceloom" / "prompt_rules"
    for name, expected in _PINNED_SHA256.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected
    assert SKILL_COMMIT == "d4d4b11d6122c0f538186b2f5553f7cce7eb2480"


def test_effective_template_is_complete_pinned_and_deterministic():
    first = build_effective_template(["page", "image", "code", "table"])
    second = build_effective_template(["table", "code", "page", "image"])
    assert first == second
    assert first["template_version"] == TEMPLATE_VERSION
    assert first["skill_commit"] == SKILL_COMMIT
    assert first["template_digest"] == hashlib.sha256(first["text"].encode()).hexdigest()
    assert "FMT-009：" in first["text"]
    assert "FMT-046：" in first["text"]
    assert "FMT-121：" in first["text"]
    assert "EXPL-013：" in first["text"]
    assert "FMT-089：" in first["text"]
    assert "FMT-084：" in first["text"]
    assert len(first["applicable_rule_ids"]) >= 45


def test_product_exceptions_are_single_effective_instruction():
    template = build_effective_template(["code", "image"])
    text = template["text"]
    assert "保留每个原始代码对象一次" in text
    assert "不得依 FMT-072 再生成完整注释副本" in text
    assert "- FMT-072：" not in text
    assert "- FMT-082：" not in text
    assert "- EXPL-017：" not in text
    assert "- FMT-052：" not in text
    assert "普通来源超链接和另见资源仅保留并自然呈现" in text
    assert "普通背景词、代码标识和资源名称不因出现或复用" in text
    assert "不为标点要求把每句话拆成独立段落" in text
    assert "不读取仓库或本地技能文件" in text
    assert "- FMT-096：" in text


def test_formula_rules_are_only_added_when_applicable():
    plain = build_effective_template(["page", "code"])
    formula = build_effective_template(["page", "formula"])
    forced = build_effective_template(["page"], has_formula=True)
    assert not plain["formula_rules_applied"]
    assert "- FMT-067：" not in plain["text"]
    assert formula["formula_rules_applied"] and forced["formula_rules_applied"]
    assert "- FMT-067：" in formula["text"]
    assert "适用的公式解释规则" in formula["text"]
    assert plain["template_digest"] != formula["template_digest"]


def test_page_back_reference_does_not_become_image_explanation_duty():
    page = build_effective_template(["page"])
    image = build_effective_template(["image"])
    assert "- FMT-083：" not in page["text"]
    assert "- FMT-083：" in image["text"]
    assert "可回查的原页不自动成为正文插图" in page["text"]


def test_real_task_pack_uses_same_versioned_effective_prompt(tmp_path):
    store = Store(tmp_path)
    project = processor.create(store, "参考材料", preferences="保持原文层次")
    project = processor.prepare(store, project["id"], [("source.txt", "保留原文事实".encode())])
    pack = processor.task_pack(store, project["id"])
    template = build_effective_template([])
    assert pack["prompt"].startswith(template["text"])
    assert pack["template_version"] == template["template_version"]
    assert pack["template_digest"] == template["template_digest"]
    assert pack["skill_commit"] == template["skill_commit"]
    assert "保持原文层次" in pack["prompt"]
    assert pack["policy_digest"] == project["processor"]["policy_digest"]


def test_reference_only_image_does_not_enable_body_image_rules(tmp_path):
    out = BytesIO()
    Image.new("RGB", (48, 36), "blue").save(out, "PNG")
    store = Store(tmp_path)
    project = processor.create(store, "图片用途")
    project = processor.prepare(store, project["id"], [("figure.png", out.getvalue())])
    image = next(row for row in project["processor"]["resources"] if row["kind"] == "image")
    processor.set_resource_usages(store, project["id"], [{"source_id": image["id"], "usage": "reference"}])
    pack = processor.task_pack(store, project["id"])
    assert "- FMT-083：" not in pack["prompt"]
    assert "- FMT-084：" not in pack["prompt"]
    assert pack["resource_selection"][image["id"]] == "reference"
