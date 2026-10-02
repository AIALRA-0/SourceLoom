import json
import re
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from sourceloom.readweave_format_compat import (
    repair_readweave_adjacent_inline_code_spacing,
    repair_readweave_bilingual_name_line_breaks,
    readweave_format_candidates,
    repair_readweave_duplicate_names,
    repair_readweave_mixed_bilingual_explanation,
    repair_readweave_mixed_unit_gloss,
    repair_readweave_prose_spacing,
)
from sourceloom import writing
from sourceloom.writing import normalize_authored_periods


def _lexical_tokens(value):
    return re.findall(r"[A-Za-z]+|[\u3400-\u9fff]|\d+|[μΩ]+", value)


def test_readweave_spacing_filter_repairs_only_chinese_latin_and_number_edges():
    assert repair_readweave_prose_spacing("中文API10与v2组件") == "中文 API10 与 v2 组件"
    assert repair_readweave_prose_spacing("中文 API 文本 10 毫秒") == "中文 API 文本 10 毫秒"


def test_readweave_spacing_filter_keeps_inline_literals_exact_and_spaces_inline_code():
    value = '中文`API`解释 $x=3$结束，中文"API 10ms"不动 [API](https://example.test/10ms)结束'
    assert repair_readweave_prose_spacing(value) == (
        '中文 `API` 解释 $x=3$ 结束，中文"API 10ms"不动 [API](https://example.test/10ms)结束'
    )


def test_readweave_spacing_filter_leaves_blockquotes_and_fenced_code_unchanged():
    value = "> 中文API10\r\n```txt\r\n中文API10\r\n```\r\n正文API10"
    assert repair_readweave_prose_spacing(value) == (
        "> 中文API10\r\n```txt\r\n中文API10\r\n```\r\n正文 API10"
    )


def test_v20_adjacent_inline_code_repair_adds_only_the_missing_separator():
    source = "修复者`public name`；`person ID``R4` 指出\r\n`a``b`"
    expected = "修复者`public name`；`person ID` `R4` 指出\r\n`a` `b`"
    assert repair_readweave_adjacent_inline_code_spacing(source) == expected
    assert repair_readweave_adjacent_inline_code_spacing(expected) == expected


def test_v20_adjacent_inline_code_repair_preserves_protected_markdown():
    source = "\n".join([
        "> `a``b`",
        "| `a``b` |",
        "    `a``b`",
        "# Heading `a``b`",
        "```md",
        "`a``b`",
        "```",
        "正文`a``b`",
    ])
    expected = source.rsplit("正文`a``b`", 1)[0] + "正文`a` `b`"
    assert repair_readweave_adjacent_inline_code_spacing(source) == expected


def test_v20_bilingual_name_soft_wrap_repair_preserves_lexical_tokens():
    source = "操作系统内核\r\n（operating system\r\nkernel）"
    expected = "操作系统内核（operating system kernel）"
    repaired = repair_readweave_bilingual_name_line_breaks(source)
    assert repaired == expected
    assert _lexical_tokens(repaired) == _lexical_tokens(source)
    assert repair_readweave_bilingual_name_line_breaks(repaired) == repaired


def test_v20_bilingual_name_soft_wrap_allows_wrapped_english_inline_code():
    source = "操作系统内核\n（`operating system\nkernel`）"
    expected = "操作系统内核（`operating system kernel`）"
    repaired = repair_readweave_bilingual_name_line_breaks(source)
    assert repaired == expected
    assert _lexical_tokens(repaired) == _lexical_tokens(source)


def test_v20_bilingual_name_soft_wrap_repair_preserves_protected_regions():
    protected = [
        "“操作系统内核\n（operating system\nkernel）”",
        "> 操作系统内核\n> （operating system\nkernel）",
        "```text\n操作系统内核\n（operating system\nkernel）\n```",
        "`操作系统内核\n（operating system\nkernel）`",
        "$操作系统内核\n（operating system\nkernel）$",
        "[操作系统内核\n（operating system\nkernel）](https://example.test)",
    ]
    for source in protected:
        assert repair_readweave_bilingual_name_line_breaks(source) == source


def test_v20_bilingual_name_soft_wrap_rejects_blank_lines_mismatch_and_chinese_asides():
    sources = [
        "操作系统内核\n\n（operating system\nkernel）",
        "操作系统内核\n（operating system\nkernel)",
        "操作系统内核\n（operating system\n核心）",
        "操作系统内核\n（a\nb\nc\nd）",
    ]
    for source in sources:
        assert repair_readweave_bilingual_name_line_breaks(source) == source


def test_v19_unit_gloss_reordering_needs_all_registered_terms_and_is_idempotent():
    source = "覆盖 10 ns（纳秒，nanosecond）；读数以毫米（millimeter，mm）为单位"
    expected = "覆盖 10 ns 纳秒（nanosecond）；读数以 mm 毫米（millimeter）为单位"
    assert repair_readweave_mixed_unit_gloss(source) == expected
    assert repair_readweave_mixed_unit_gloss(expected) == expected
    assert repair_readweave_mixed_unit_gloss("10 ns（纳秒，unknown）") == "10 ns（纳秒，unknown）"
    assert Counter(_lexical_tokens(source)) == Counter(_lexical_tokens(expected))


def test_v19_unit_repair_respects_four_backtick_fence_with_inner_triple_ticks():
    source = "````md\n```\n```python\n10 ns（纳秒，nanosecond）\n````\n正文 10 ns（纳秒，nanosecond）"
    expected = "````md\n```\n```python\n10 ns（纳秒，nanosecond）\n````\n正文 10 ns 纳秒（nanosecond）"
    assert repair_readweave_mixed_unit_gloss(source) == expected


def test_v19_unit_repair_preserves_markdown_objects_headings_and_html_attributes():
    unit = "10 ns（纳秒，nanosecond）"
    source = "\n".join([
        f"# 标题 {unit}",
        f"> 引用 {unit}",
        f"| 表格 | {unit} |",
        f"正文 [{unit}](https://example.test) <img alt=\"{unit}\">",
        f"`{unit}`",
        "",
    ])
    result = repair_readweave_mixed_unit_gloss(source)
    assert f"# 标题 {unit}" in result
    assert f"> 引用 {unit}" in result
    assert f"| 表格 | {unit} |" in result
    assert f"[{unit}](https://example.test)" in result
    assert f'alt="{unit}"' in result
    assert f"`{unit}`" in result
    assert f"正文 [{unit}](https://example.test)" in result


def test_v20_mixed_bilingual_explanation_moves_only_punctuation_boundary():
    source = "规范化（normalization，把等价字符统一）用于比较"
    expected = "规范化（normalization），把等价字符统一用于比较"
    assert repair_readweave_mixed_bilingual_explanation(source) == expected
    assert Counter(_lexical_tokens(source)) == Counter(_lexical_tokens(expected))
    assert repair_readweave_mixed_bilingual_explanation(expected) == expected


def test_v20_mixed_bilingual_explanation_leaves_names_and_protected_text_alone():
    english_name = "加州大学洛杉矶分校（University of California, Los Angeles）"
    source = "`规范化（normalization，把等价字符统一）` " + english_name
    assert repair_readweave_mixed_bilingual_explanation(source) == source


def test_v19_repairs_leave_quotes_tables_code_fences_and_inline_literals_exact():
    source = (
        "`图形处理器（Graphics Processing Unit，GPU）`\n"
        "> 图形处理器（Graphics Processing Unit，GPU）\n"
        "| 图形处理器（Graphics Processing Unit，GPU） |\n"
        "```md\n"
        "图形处理器（Graphics Processing Unit，GPU）\n"
        "```"
    )
    assert repair_readweave_mixed_unit_gloss(source) == source


def test_readweave_duplicate_name_repair_only_removes_exact_repetition_in_prose():
    value = (
        "South Quay station（South Quay station）与 Cloudflare WARP（WARP）；"
        "South Quay station（South Quay）\n"
        "> South Quay station（South Quay station）\n"
        "`South Quay station（South Quay station）`"
    )

    assert repair_readweave_duplicate_names(value) == (
        "South Quay station与 Cloudflare WARP（WARP）；"
        "South Quay station（South Quay）\n"
        "> South Quay station（South Quay station）\n"
        "`South Quay station（South Quay station）`"
    )


def test_actual_authored_normalizer_applies_filter_but_preserves_source_code_bytes():
    raw_code = "```py\n中文API10。\n```"
    inventory = {"objects": [{"id": "code-1", "kind": "code", "fence_raw": raw_code}]}
    draft = {"blocks": [{"id": "b1", "kind": "explanation", "markdown": f"正文API10。\n\n{raw_code}"}]}

    result = normalize_authored_periods(draft, inventory)

    assert result["blocks"][0]["markdown"] == f"正文 API10\n\n{raw_code}"
    assert draft["blocks"][0]["markdown"].endswith(raw_code)


def test_adjacent_inline_code_repair_runs_in_the_authored_normalizer():
    inventory = {"objects": [], "obligations": []}
    draft = {"blocks": [{"id": "b1", "kind": "explanation",
        "markdown": "修复者`public name`；`person ID``R4` 指出。"}]}
    normalized = normalize_authored_periods(draft, inventory)["blocks"][0]["markdown"]
    assert normalized == "修复者 `public name`；`person ID` `R4` 指出"


def test_bilingual_name_soft_wrap_repair_runs_in_the_authored_normalizer():
    inventory = {"objects": [], "obligations": []}
    draft = {"blocks": [{"id": "b1", "kind": "explanation",
        "markdown": "操作系统内核\n（operating system\nkernel）"}]}
    normalized = normalize_authored_periods(draft, inventory)["blocks"][0]["markdown"]
    assert normalized == "操作系统内核（operating system kernel）"


def test_v19_v20_repairs_are_part_of_the_existing_authored_normalization_step():
    inventory = {"objects": [], "obligations": []}
    draft = {"blocks": [{"id": "b1", "kind": "explanation", "markdown": "\n".join([
        "10 ns（纳秒，nanosecond）",
        "规范化（normalization，把等价字符统一）",
        "我们介绍对称旅行费用（Symmetric Travel Cost，STC）",
    ])}]}
    normalized = normalize_authored_periods(draft, inventory)["blocks"][0]["markdown"]
    assert "10 ns 纳秒（nanosecond）" in normalized
    assert "规范化（normalization），把等价字符统一" in normalized
    assert "我们介绍对称旅行费用（Symmetric Travel Cost，STC）" in normalized


def test_readweave_review_signals_are_candidates_not_automatic_repairs():
    body = "\n".join([
        "World Health Organization（World Health Organization）",
        "内部 context-od-123 标识",
        "普通文本## 标题",
        "公式 $x`q$",
        "公式 $x +$ 中文",
        "行内 a`b` 黏连",
        "复合公式 $3+2=5+1$",
        "时间公式 $x=2024-01-01$ 12:30",
    ])

    candidates = readweave_format_candidates(body)

    assert {item["rule_id"] for item in candidates} == {
        "RW-FMT-122", "RW-FMT-124", "RW-FMT-128", "RW-FMT-129", "RW-FMT-130",
        "RW-FMT-131", "RW-FMT-132", "RW-FMT-133",
    }
    assert all(item["severity"] == "MACHINE_CANDIDATE" for item in candidates)
    assert all(item["old_text"] in body for item in candidates)


def test_v19_acronym_candidate_is_limited_to_explicit_definition_rows():
    body = "\n".join([
        "- 图形处理器（Graphics Processing Unit，GPU）：负责图形计算",
        "我们介绍图形处理器（Graphics Processing Unit，GPU）",
        "普通标题：A、B、C",
        "- 操作系统内核（operating system kernel）：负责资源管理",
    ])
    candidates = readweave_format_candidates(body)
    assert [(item["rule_id"], item["old_text"]) for item in candidates] == [
        ("RW-FMT-136", "- 图形处理器（Graphics Processing Unit，GPU）：负责图形计算"),
    ]


def test_acronym_candidate_ignores_nested_short_ticks_inside_long_fence():
    body = "````md\n```\n```python\n- 图形处理器（Graphics Processing Unit，GPU）：定义\n````\n- 图形处理器（Graphics Processing Unit，GPU）：定义"
    candidates = readweave_format_candidates(body)
    assert [(item["location"], item["rule_id"]) for item in candidates] == [
        ("LINE-0006", "RW-FMT-136"),
    ]


def test_fenced_code_info_line_does_not_close_candidate_scan():
    body = "```md\n```python\n- 图形处理器（Graphics Processing Unit，GPU）：定义\n```\n- 图形处理器（Graphics Processing Unit，GPU）：定义"
    candidates = readweave_format_candidates(body)
    assert [(item["location"], item["rule_id"]) for item in candidates] == [
        ("LINE-0005", "RW-FMT-136"),
    ]


def test_compat_layer_does_not_emit_context_free_name_candidates():
    body = "\n".join([
        "- 规范化（normalization，把等价字符统一）：用于比较",
        "- 操作系统内核（operating system kernel）：负责资源管理",
        "我们介绍对称旅行费用（Symmetric Travel Cost，STC）",
    ])
    assert readweave_format_candidates(body) == []


def test_readweave_candidates_skip_literal_regions_and_protected_blocks():
    body = "context-od-123\n> context-od-123\n| context-od-123 |\n```md\ncontext-od-123\n```\ncontext-od-124"

    candidates = readweave_format_candidates(body, skip_lines=frozenset({7}))

    assert [item["location"] for item in candidates] == ["LINE-0001"]


def test_scan_routes_candidates_into_the_existing_skill_review(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        report = Path(command[command.index("--report") + 1])
        report.write_text(json.dumps({"format": {"findings": [], "candidates": []}}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(writing.subprocess, "run", fake_run)
    draft = {"blocks": [
        {"id": "prose", "kind": "explanation", "markdown": "正文 context-od-123"},
        {"id": "source", "kind": "source", "markdown": "原样 context-od-456"},
    ]}

    result = writing.scan({"root": tmp_path}, draft, tmp_path / "work")

    assert len(result["format"]["candidates"]) == 1
    assert result["format"]["candidates"][0]["rule_id"] == "RW-FMT-124"
    assert result["format"]["candidates"][0]["id"] == "candidates-1"

    from sourceloom.active_composition import located_format_issues, review_format_context, confirmed_format_issues

    located = located_format_issues(result, draft)
    assert located["candidates"][0]["block_id"] == "prose"
    context = review_format_context(located)
    review = {"format_decisions": [{"candidate_id": "candidates-1", "decision": "fix"}]}
    confirmed = confirmed_format_issues(located, review, required=True)
    assert context["candidates"][0]["rule_id"] == "RW-FMT-124"
    assert confirmed[0]["block_id"] == "prose"


def test_acronym_definition_candidate_reaches_active_format(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        report = Path(command[command.index("--report") + 1])
        report.write_text(json.dumps({"format": {"findings": [], "candidates": []}}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(writing.subprocess, "run", fake_run)
    draft = {"blocks": [{"id": "prose", "kind": "explanation",
        "markdown": "- 图形处理器（Graphics Processing Unit，GPU）：负责图形计算"}]}
    report = writing.scan({"root": tmp_path}, draft, tmp_path / "context-review")
    assert report["format"]["candidates"][0]["rule_id"] == "RW-FMT-136"

    from sourceloom.active_composition import located_format_issues, review_format_context

    located = located_format_issues(report, draft)
    assert located["candidates"][0]["block_id"] == "prose"
    assert review_format_context(located)["candidates"][0]["rule_id"] == "RW-FMT-136"
