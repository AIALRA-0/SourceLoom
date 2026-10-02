from sourceloom.active_composition import normalize_authored_spacing


def _draft(*blocks):
    return {'blocks': [dict(id=f'b{index}', kind=kind, markdown=text,
                           embedded_object_ids=[]) for index, (kind, text) in enumerate(blocks)]}


def test_authored_prose_and_list_get_one_blank_line_idempotently():
    draft = _draft(('explanation', '各项依次为\n- Yellowstone Home'))

    normalized = normalize_authored_spacing(draft, {'objects': []})

    assert normalized['blocks'][0]['markdown'] == '各项依次为\n\n- Yellowstone Home'
    assert normalize_authored_spacing(normalized, {'objects': []}) == normalized
    assert draft['blocks'][0]['markdown'] == '各项依次为\n- Yellowstone Home'

    ordered = normalize_authored_spacing(
        _draft(('explanation', '另一个段落\n1. 第一项')), {'objects': []})
    assert ordered['blocks'][0]['markdown'] == '另一个段落\n\n1. 第一项'


def test_authored_spacing_leaves_protected_markdown_structures_untouched():
    draft = _draft(
        ('explanation', '> 引用\n- 被引用段落后的列表'),
        ('explanation', '名称 | 值\n- 表格后的内容'),
        ('explanation', '</table>\n- HTML 表格后的内容'),
        ('explanation', '## 标题\n- 标题后的列表'),
        ('explanation', '- 前一项\n- 后一项'),
        ('explanation', '    延续的缩进段落\n- 列表'),
        ('explanation', '```text\n段落\n- fenced 内容\n```'),
        ('source', '原文段落\n- 原文列表'),
        ('object', '资源段落\n- 资源列表'),
        ('document_info', '元数据段落\n- 元数据列表'),
    )

    normalized = normalize_authored_spacing(draft, {'objects': []})

    assert normalized == draft
