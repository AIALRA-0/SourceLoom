"""Small, source-preserving ports of proven ReadWeave format repairs.

ReadWeave's ``readweave_format.ts`` (format-2026-09-v20) separates writable
prose from Markdown literals before normalizing Chinese/Latin and
Chinese/digit boundaries.  SourceLoom already uses the installed writing
skill's scanner and versioned local-patch committer; this module ports only
deterministic repairs and one exact, source-present unit-label reordering.
Candidate v20 mapping: prose spacing, duplicate-name repair, and mixed-unit
labels were already present; adjacent-code spacing and explicit bilingual-name
soft wraps are now ported. Name-code markers, acronym/case, heading/list/numbering,
quoted-continuation, and explanatory rewrites remain out because they need
semantic or article context absent from this shared formatter.
"""

from __future__ import annotations

import re


_HAN = r"\u3400-\u9fff"
_INLINE_LITERAL = re.compile(
    r"`+[^`\r\n]*`+"
    r"|SOURCELOOMLITERAL[A-F0-9]{24}X*"
    r"|\$\$[^$\r\n]*\$\$|\$(?!\$)[^$\r\n]+\$"
    r"|\\\([^\r\n]*?\\\)|\\\[[^\r\n]*?\\\]"
    r"|!?\[[^\]\r\n]*\]\([^\r\n]*?\)"
    r"|https?://[^\s<>，；。（]+"
    r"|[A-Za-z]:[\\/][^\s，；。]+"
    r"|[“「『][^”」』\r\n]*[”」』]"
    r"|\"(?:\\.|[^\"\\\r\n])*\""
    r"|'(?:\\.|[^'\\\r\n])*'",
    re.UNICODE,
)
_HTML_TAG = re.compile(r"<(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^'\">\r\n])*?>")


def _normalize_prose(text: str) -> str:
    return re.sub(rf"(?<=[{_HAN}])(?=[A-Za-z0-9])|(?<=[A-Za-z0-9])(?=[{_HAN}])", " ", text)


def _line(line: str) -> str:
    """Normalize only prose spans; keep code, links, quotes and formulas exact."""
    if line.lstrip().startswith(">"):
        return line

    pieces: list[str] = []
    cursor = 0
    for match in _INLINE_LITERAL.finditer(line):
        start, end = match.span()
        literal = match.group()
        pieces.append(_normalize_prose(line[cursor:start]))

        # ReadWeave handles inline code/math as opaque content, then adds the
        # required Chinese boundary space around those two literal types.
        is_inline_code_or_math = literal.startswith("`") or literal.startswith("$") \
            or literal.startswith(r"\(") or literal.startswith(r"\[")
        before = line[start - 1:start]
        after = line[end:end + 1]
        if is_inline_code_or_math and before and re.fullmatch(rf"[{_HAN}]", before):
            pieces.append(" ")
        pieces.append(literal)
        if is_inline_code_or_math and after and re.fullmatch(rf"[{_HAN}]", after):
            pieces.append(" ")
        cursor = end
    pieces.append(_normalize_prose(line[cursor:]))
    return "".join(pieces)


def _map_authored_prose(body: str, transform) -> str:
    """Transform one writable prose line at a time outside Markdown literals."""
    output: list[str] = []
    fence: tuple[str, int] | None = None
    for raw_line in body.splitlines(keepends=True):
        ending_match = re.search(r"(?:\r\n|\n|\r)$", raw_line)
        ending = ending_match.group() if ending_match else ""
        line = raw_line[:-len(ending)] if ending else raw_line
        marker = re.match(r"^[ \t]*(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = (token[0], len(token))
            elif (token[0] == fence[0] and len(token) >= fence[1]
                    and not line[marker.end():].strip()):
                fence = None
            output.append(raw_line)
            continue
        if (fence is not None or line.lstrip().startswith((">", "|"))
                or re.match(r"^[ \t]{0,3}#{1,6}(?:[ \t]+|$)", line)):
            output.append(raw_line)
            continue
        pieces: list[str] = []
        cursor = 0
        literals = sorted(
            [*_INLINE_LITERAL.finditer(line), *_HTML_TAG.finditer(line)],
            key=lambda match: match.start(),
        )
        for literal in literals:
            if literal.start() < cursor:
                continue
            pieces.append(transform(line[cursor:literal.start()]))
            pieces.append(literal.group())
            cursor = literal.end()
        pieces.append(transform(line[cursor:]))
        output.append("".join(pieces) + ending)
    return "".join(output)


_DUPLICATE_NAME = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9'’.,&+/#_-]*(?:[ \t]+[A-Za-z][A-Za-z0-9'’.,&+/#_-]*){0,15})"
    r"[ \t]*[（(](?P=name)[）)]"
)
_NAME_BOUNDARY = re.compile(r"[A-Za-z0-9'’.,&+/#_-]")


def _repair_duplicate_name_prose(prose: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        before = prose[:match.start("name")]
        if " " in name or "\t" in name:
            if before and _NAME_BOUNDARY.fullmatch(before[-1]):
                return match.group()
        elif before:
            last = before[-1]
            boundary = (
                not _NAME_BOUNDARY.fullmatch(before[-2])
                if last in " \t" and len(before) >= 2
                else last in " \t"
                if last in " \t"
                else not _NAME_BOUNDARY.fullmatch(last)
            )
            if not boundary:
                return match.group()
        return name

    return _DUPLICATE_NAME.sub(replace, prose)


def repair_readweave_duplicate_names(body: str) -> str:
    """Remove only a byte-for-byte repeated English name in prose parentheses.

    This is ReadWeave's deterministic FMT-122 repair, scoped to writable prose;
    distinct parenthetical names and Markdown/data literals are preserved.
    """
    output: list[str] = []
    fence: str | None = None
    for raw_line in body.splitlines(keepends=True):
        ending_match = re.search(r"(?:\r\n|\n|\r)$", raw_line)
        ending = ending_match.group() if ending_match else ""
        line = raw_line[:-len(ending)] if ending else raw_line
        marker = re.match(r"^[ \t]*(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token[0]
            elif token[0] == fence:
                fence = None
            output.append(raw_line)
            continue
        if fence is not None or line.lstrip().startswith(">"):
            output.append(raw_line)
            continue
        pieces: list[str] = []
        cursor = 0
        for match in _INLINE_LITERAL.finditer(line):
            pieces.append(_repair_duplicate_name_prose(line[cursor:match.start()]))
            pieces.append(match.group())
            cursor = match.end()
        pieces.append(_repair_duplicate_name_prose(line[cursor:]))
        output.append("".join(pieces) + ending)
    return "".join(output)


def repair_readweave_prose_spacing(body: str) -> str:
    """Apply ReadWeave's safe Han/Latin spacing filter without editing literals.

    The input may be a rendered block. Fenced code and quote lines remain
    byte-for-byte unchanged; authored prose lines receive only boundary spaces.
    """
    output: list[str] = []
    fence: str | None = None
    for raw_line in body.splitlines(keepends=True):
        ending_match = re.search(r"(?:\r\n|\n|\r)$", raw_line)
        ending = ending_match.group() if ending_match else ""
        line = raw_line[:-len(ending)] if ending else raw_line
        marker = re.match(r"^[ \t]*(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token[0]
            elif token[0] == fence:
                fence = None
            output.append(raw_line)
            continue
        if fence is not None:
            output.append(raw_line)
            continue
        output.append(_line(line) + ending)
    return "".join(output)


def repair_readweave_adjacent_inline_code_spacing(body: str) -> str:
    """Separate adjacent single-backtick spans in writable prose only.

    This ports ReadWeave v20's exact boundary repair. It inserts one space
    between two unambiguous inline-code spans; their contents and every
    protected Markdown region remain unchanged.
    """
    output: list[str] = []
    fence: tuple[str, int] | None = None
    adjacent = re.compile(r"(?<!`)(`[^`\r\n]+`)(?=`[^`\r\n]+`)")
    for raw_line in body.splitlines(keepends=True):
        ending_match = re.search(r"(?:\r\n|\n|\r)$", raw_line)
        ending = ending_match.group() if ending_match else ""
        line = raw_line[:-len(ending)] if ending else raw_line
        marker = re.match(r"^[ \t]*(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = (token[0], len(token))
            elif (token[0] == fence[0] and len(token) >= fence[1]
                    and not line[marker.end():].strip()):
                fence = None
            output.append(raw_line)
            continue
        if (fence is not None or re.match(r"^(?: {4}|\t)", line)
                or line.lstrip().startswith((">", "|"))
                or re.match(r"^[ \t]{0,3}#{1,6}(?:[ \t]+|$)", line)):
            output.append(raw_line)
            continue
        output.append(adjacent.sub(r"\1 ", line) + ending)
    return "".join(output)


_BILINGUAL_NAME_SOFT_BREAK = re.compile(
    r"(?P<label>[\u3400-\u9fff][^（）()\r\n]{0,60})"
    r"(?P<between>[ \t]*(?:\r?\n[ \t]*)?)"
    r"(?P<open>[（(])(?P<inner>[^（）()]{1,220})(?P<close>[）)])"
)
_SOFT_BREAK_CODE = re.compile(r"(?<!`)(`+)(?!`)([\s\S]*?)(?<!`)\1(?!`)")
_SOFT_BREAK_OPAQUE = re.compile(
    r"\$\$[\s\S]*?\$\$|\$(?!\$)[^$]+?\$|"
    r"\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]|"
    r"!?\[[^\]]*\]\([^)]*\)|https?://[^\s<>，；。]+|"
    r"[“「『][^”」』]*[”」』]|"
    r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'"
)


def _writable_bilingual_name_span(body: str, start: int, end: int) -> bool:
    """Conservatively exclude Markdown data regions before joining name wraps."""
    cursor = 0
    fence: tuple[str, int] | None = None
    for raw_line in body.splitlines(keepends=True):
        line_end = cursor + len(raw_line)
        line = raw_line.rstrip("\r\n")
        marker = re.match(r"^[ \t]*(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = (token[0], len(token))
            elif (token[0] == fence[0] and len(token) >= fence[1]
                    and not line[marker.end():].strip()):
                fence = None
            if start < line_end and end > cursor:
                return False
        elif fence is not None:
            if start < line_end and end > cursor:
                return False
        elif start < line_end and end > cursor:
            if (line.lstrip().startswith((">", "|"))
                    or re.match(r"^(?: {4}|\t)", line)
                    or _HTML_TAG.search(line)):
                return False
        cursor = line_end

    for match in _SOFT_BREAK_OPAQUE.finditer(body):
        if start < match.end() and end > match.start():
            return False
    for match in _SOFT_BREAK_CODE.finditer(body):
        if start >= match.end() or end <= match.start():
            continue
        if start > match.start() or end < match.end():
            return False
        payload = re.sub(r"[\r\n]+", " ", match.group(2))
        payload = re.sub(r"[ \t]{2,}", " ", payload).strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9'’ .&+/#_-]*", payload):
            return False
    return True


def repair_readweave_bilingual_name_line_breaks(body: str) -> str:
    """Join only ReadWeave v20's explicit bilingual-name soft wraps.

    The repair changes line-break layout to spaces, preserves lexical words and
    punctuation, and leaves protected Markdown or ambiguous parentheticals exact.
    """
    def replace(match: re.Match[str]) -> str:
        original = match.group()
        line_breaks = len(re.findall(r"\r?\n", original))
        opening, closing = match.group("open"), match.group("close")
        if (not line_breaks or line_breaks > 2
                or re.search(r"\r?\n[ \t]*\r?\n", original)
                or (opening == "（") != (closing == "）")):
            return original
        start, end = match.span()
        if not _writable_bilingual_name_span(body, start, end):
            return original
        inner = match.group("inner")
        plain = re.sub(r"`+", "", inner)
        plain = re.sub(r"[\r\n]+", " ", plain)
        plain = re.sub(r"[ \t]{2,}", " ", plain).strip()
        if (not re.fullmatch(
                r"[A-Za-z][A-Za-z0-9'’ .,&+/#_:-]*(?:[，,;；][ \t]*"
                r"[A-Za-z][A-Za-z0-9+/#_-]*)?", plain)
                or re.search(r"[\u3400-\u9fff]", plain)):
            return original
        label = match.group("label").rstrip()
        inner = re.sub(r"[ \t]*\r?\n[ \t]*", " ", inner)
        inner = re.sub(r"[ \t]{2,}", " ", inner).strip()
        return f"{label}{opening}{inner}{closing}"

    return _BILINGUAL_NAME_SOFT_BREAK.sub(replace, body)


# This small table mirrors ReadWeave v19's established unit labels. It is used
# only when the authored line already contains the exact symbol, Chinese
# label, and English label; it never supplies a missing translation.
_UNIT_GLOSSES = (
    ("s", "秒", "second"), ("ms", "毫秒", "millisecond"),
    ("us", "微秒", "microsecond"), ("μs", "微秒", "microsecond"),
    ("ns", "纳秒", "nanosecond"), ("min", "分钟", "minute"), ("h", "小时", "hour"),
    ("Hz", "赫兹", "hertz"), ("kHz", "千赫兹", "kilohertz"),
    ("MHz", "兆赫兹", "megahertz"), ("GHz", "吉赫兹", "gigahertz"),
    ("V", "伏特", "volt"), ("mV", "毫伏", "millivolt"),
    ("A", "安培", "ampere"), ("mA", "毫安", "milliampere"),
    ("W", "瓦特", "watt"), ("mW", "毫瓦", "milliwatt"),
    ("Ω", "欧姆", "ohm"), ("kΩ", "千欧姆", "kilohm"),
    ("m", "米", "meter"), ("mm", "毫米", "millimeter"),
    ("um", "微米", "micrometer"), ("μm", "微米", "micrometer"),
    ("nm", "纳米", "nanometer"), ("g", "克", "gram"), ("kg", "千克", "kilogram"),
    ("K", "开尔文", "kelvin"), ("B", "字节", "byte"),
    ("KB", "千字节", "kilobyte"), ("MB", "兆字节", "megabyte"),
    ("GB", "吉字节", "gigabyte"),
)


def _unit_pattern_token(value: str) -> str:
    return re.escape(value)


def repair_readweave_mixed_unit_gloss(body: str) -> str:
    """Reorder an explicitly present unit triplet; preserve its English case."""
    def repair(prose: str) -> str:
        for symbol, chinese, english in _UNIT_GLOSSES:
            s, c = map(_unit_pattern_token, (symbol, chinese))
            number = r"\d+(?:\.\d+)?"
            # Quantity + (Chinese, English) -> quantity Chinese (English).
            prose = re.sub(
                rf"(?<![\w])({number}[ \t]+{s})[ \t]*[（(][ \t]*{c}[ \t]*[，,][ \t]*"
                rf"([A-Za-z]+)[ \t]*[）)]",
                lambda m: (f"{m.group(1)} {chinese}（{m.group(2)}）"
                           if m.group(2).casefold() == english else m.group(0)),
                prose,
            )
            # Chinese (English, symbol) -> symbol Chinese (English).
            pattern = re.compile(
                rf"(?<![A-Za-z0-9_]){c}[ \t]*[（(][ \t]*([A-Za-z]+)[ \t]*[，,][ \t]*{s}[ \t]*[）)]"
            )
            def move_symbol(match: re.Match[str]) -> str:
                if match.group(1).casefold() != english:
                    return match.group(0)
                before = prose[match.start() - 1:match.start()]
                leading_space = " " if before and re.fullmatch(rf"[{_HAN}]", before) else ""
                return f"{leading_space}{symbol} {chinese}（{match.group(1)}）"
            prose = pattern.sub(move_symbol, prose)
        return prose

    return _map_authored_prose(body, repair)


_MIXED_BILINGUAL_EXPLANATION = re.compile(
    r"(?P<label>[\u3400-\u9fff]{1,40})[（]"
    r"(?P<name>[A-Za-z][A-Za-z'’ .&+/#_-]{1,100})[，,][ \t]*"
    r"(?P<explanation>[\u3400-\u9fff][^（）()\r\n]{2,160})[）]"
)


def repair_readweave_mixed_bilingual_explanation(body: str) -> str:
    """Move an explicit Chinese explanation outside an English-name pair.

    This punctuation-only repair mirrors ReadWeave v20. It keeps every word
    and changes only the comma/parenthesis boundary in writable prose.
    """
    def repair(prose: str) -> str:
        return _MIXED_BILINGUAL_EXPLANATION.sub(
            r"\g<label>（\g<name>），\g<explanation>", prose
        )

    return _map_authored_prose(body, repair)

_FORMAT_SIGNALS = (
    ("RW-FMT-122", re.compile(r"(?<![\w`])([A-Z][A-Za-z'’.-]*(?:[ \t]+[A-Z][A-Za-z'’.-]*){0,7})[ \t]*（\1）"),
     "英文专名在括号中重复了紧邻的同一名称"),
    ("RW-FMT-124", re.compile(r"\bcontext-od-\d{3}\b"),
     "正文出现了内部 context 标识"),
    ("RW-FMT-128", re.compile(r"(?<=\S)#{2,6}[ \t]+"),
     "普通正文中疑似残留损坏的 Markdown 标题标记"),
    ("RW-FMT-129", re.compile(r"`[^`\n]*\$|(?<!\$)\$(?!\$)[^$\n]*`[^$\n]*\$(?!\$)"),
     "数学公式与行内代码定界符发生嵌套"),
    ("RW-FMT-130", re.compile(r"\$[A-Za-z][A-Za-z0-9_]*\s*(?:[+−-]|[=<>])\$(?=[ \t]*[\u3400-\u9fff])"),
     "行内公式以孤立运算符结束，公式边界可能损坏"),
    ("RW-FMT-131", re.compile(r"(?<=[A-Za-z0-9])`(?=[A-Za-z0-9])"),
     "行内代码定界符与相邻英文或数字黏连"),
    ("RW-FMT-132", re.compile(r"(?<!\$)\$(?!\$)([^$\n]+)\$(?!\$)"),
     "一个行内公式可能包含多个独立运算步骤"),
    ("RW-FMT-133", re.compile(r"\$[^$\n]*[=≈≤≥<>][^$\n]*\d{4}-\d{2}-\d{2}\$(?=[ \t]+\d{1,2}:\d{2})"),
     "公式定界符可能包入了日期或时间说明"),
)

_ACRONYM_DEFINITION = re.compile(
    r"^[ \t]*-[ \t]+[\u3400-\u9fff][\u3400-\u9fff·]{1,39}[ \t]*[（(]"
    r"[A-Za-z][A-Za-z'’ .-]{1,100}[，,][ \t]*"
    r"(?P<acronym>[A-Z][A-Z0-9+/#_-]{1,15})[）)][：:]"
)


def _candidate(
    rule_id: str, number: int, raw_line: str, reason: str
) -> dict[str, str]:
    return {
        "rule_id": rule_id,
        "severity": "MACHINE_CANDIDATE",
        "location": f"LINE-{number:04d}",
        "old_text": raw_line,
        "reason": reason,
    }


def readweave_format_candidates(
    body: str,
    protected_literals: tuple[str, ...] = (),
    skip_lines: frozenset[int] = frozenset(),
) -> list[dict[str, str]]:
    """Expose ReadWeave's tested mechanical format signals to SourceLoom review.

    These are contextual review candidates, not auto-repair instructions. The
    exact source line and rule code are passed to the existing format-review
    stage; source literals, fenced code, quotations and table rows are skipped.
    """
    candidates: list[dict[str, str]] = []
    fence: tuple[str, int] | None = None
    for number, raw_line in enumerate(body.splitlines(), 1):
        if number in skip_lines:
            continue
        marker = re.match(r"^[ \t]*(`{3,}|~{3,})", raw_line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = (token[0], len(token))
            elif (token[0] == fence[0] and len(token) >= fence[1]
                    and not raw_line[marker.end():].strip()):
                fence = None
            continue
        if fence is not None or raw_line.lstrip().startswith((">", "|")):
            continue
        prose_line = _mask_inline_data(raw_line)
        for rule_id, pattern, explanation in _FORMAT_SIGNALS:
            subject = raw_line if rule_id in {"RW-FMT-129", "RW-FMT-130", "RW-FMT-131", "RW-FMT-132", "RW-FMT-133"} else prose_line
            matches = list(pattern.finditer(subject))
            if rule_id == "RW-FMT-132":
                matches = [match for match in matches if _is_complex_inline_formula(match.group(1))]
            elif rule_id == "RW-FMT-128":
                matches = [match for match in matches if not _inside_backticks(prose_line, match.start(), match.end())]
            matches = [match for match in matches if not any(
                literal and raw_line.find(literal) <= match.start() < raw_line.find(literal) + len(literal)
                for literal in protected_literals if literal in raw_line
            )]
            if matches:
                candidates.append({
                    "rule_id": rule_id,
                    "severity": "MACHINE_CANDIDATE",
                    "location": f"LINE-{number:04d}",
                    "old_text": raw_line,
                    "reason": explanation,
                })
        # An appended acronym inside a glossary name needs source context.
        # Restrict this signal to an explicit definition row so prose such as
        # “我们介绍术语（English Name，ABC）” cannot be misread as a term.
        if _ACRONYM_DEFINITION.match(prose_line):
            candidates.append(_candidate(
                "RW-FMT-136", number, raw_line,
                "术语定义项把缩写放在英文名称括号内；需结合当前来源确认名称与缩写关系，再由现有来源绑定复核处理",
            ))
    return candidates


def _mask_inline_data(line: str) -> str:
    value = _INLINE_LITERAL.sub(lambda match: " " * len(match.group()), line)
    return _HTML_TAG.sub(lambda match: " " * len(match.group()), value)


def _inside_backticks(line: str, start: int, end: int) -> bool:
    return len(re.findall(r"`", line[:start])) % 2 == 1 or len(re.findall(r"`", line[:end])) % 2 == 1


def _is_complex_inline_formula(formula: str) -> bool:
    relations = len(re.findall(r"[=≈≤≥<>]", formula))
    if relations >= 2:
        return True
    if relations == 0:
        return False
    numeric_payload = re.sub(r"\\(?:text|mathrm)\{[^{}]*\}", "", formula)
    numeric_payload = re.sub(r"\\[A-Za-z]+", "", numeric_payload)
    arithmetic = len(re.findall(r"[+−×÷]|(?<=\d)\s*-(?=\s*(?:\d|\())", numeric_payload))
    return arithmetic >= 2 and not re.search(r"[A-Za-z]", numeric_payload)
