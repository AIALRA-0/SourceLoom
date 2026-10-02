import pytest

from sourceloom.active_composition import (
    normalize_local_proposal,
    patchable_block_ids,
    retarget_protected_findings,
)


IMAGE_LITERAL = '<img src="nasa-photo.png" alt="Earth observation">'
CAPTION = '图 5. 卫星观测到的云带边缘。'


def _draft():
    return {'blocks': [
        {'id': 'p2-n1-b5', 'kind': 'object', 'source_ids': ['image-5'],
         'markdown': IMAGE_LITERAL + '\n' + CAPTION},
        {'id': 'p2-n1-b6', 'kind': 'explanation', 'source_ids': ['image-5'],
         'markdown': '这张图显示了云带的边界变化。'},
        {'id': 'pure-object', 'kind': 'object', 'source_ids': ['image-6'],
         'markdown': IMAGE_LITERAL},
        {'id': 'wrapped-object', 'kind': 'object', 'source_ids': ['image-7'],
         'markdown': '<figure>' + IMAGE_LITERAL + '</figure>'},
        {'id': 'source-block', 'kind': 'source', 'source_ids': ['text-1'],
         'markdown': '不可修改的原文'},
        {'id': 'document-info', 'kind': 'document_info', 'source_ids': ['page-1'],
         'markdown': '原始文件信息'},
    ]}


def test_hybrid_object_caption_review_stays_patchable_after_retarget():
    draft = _draft()
    review = {'findings': [dict(
        block_id='p2-n1-b5', output_quote=CAPTION, source_id='image-5',
        source_quote='cloud band', problem='FORMAT_PARALLEL_ITEMS_REVIEW',
        required_change='Use a parallel caption form') ]}

    retarget_protected_findings(review, draft, [IMAGE_LITERAL])
    # This finding may enter revision after format review, so a repeated
    # retarget must preserve the exact caption target too.
    retarget_protected_findings(review, draft, [IMAGE_LITERAL])

    assert review['findings'][0]['block_id'] == 'p2-n1-b5'
    assert review['findings'][0]['output_quote'] == CAPTION
    assert patchable_block_ids(draft, [IMAGE_LITERAL]) == {
        'p2-n1-b5', 'p2-n1-b6'}


def test_hybrid_caption_edits_are_allowed_but_literal_overlaps_are_rejected():
    draft = _draft()
    caption_edit = {'block_id': 'p2-n1-b5', 'old_text': CAPTION,
                    'new_text': '图 5：云带边缘的卫星观测结果。', 'reason': 'parallel caption'}
    normalized, rejected = normalize_local_proposal(
        {'edits': [caption_edit]}, draft, [IMAGE_LITERAL])
    assert normalized['edits'] == [caption_edit]
    assert rejected == []

    overlapping_edit = {
        'block_id': 'p2-n1-b5',
        'old_text': IMAGE_LITERAL + '\n' + CAPTION,
        'new_text': IMAGE_LITERAL + '\n图 5：云带边缘的卫星观测结果。',
        'reason': 'caption edit must not include the literal',
    }
    normalized, rejected = normalize_local_proposal(
        {'edits': [overlapping_edit]}, draft, [IMAGE_LITERAL])
    assert normalized['edits'] == []
    assert rejected == [{'block_id': 'p2-n1-b5',
                         'reason': 'protected_literal_overlap'}]

    literal_removal = {
        'block_id': 'p2-n1-b5',
        'old_text': IMAGE_LITERAL + '\n' + CAPTION,
        'new_text': '图 5：云带边缘的卫星观测结果。',
        'reason': 'remove original image',
    }
    normalized, rejected = normalize_local_proposal(
        {'edits': [literal_removal]}, draft, [IMAGE_LITERAL])
    assert normalized['edits'] == []
    assert rejected == [{'block_id': 'p2-n1-b5',
                         'reason': 'protected_original_changed'}]

    explanation_edit = {'block_id': 'p2-n1-b6',
                        'old_text': '这张图显示了云带的边界变化。',
                        'new_text': '这张图呈现了云带边界的变化。',
                        'reason': 'clarify explanation'}
    normalized, rejected = normalize_local_proposal(
        {'edits': [explanation_edit]}, draft, [IMAGE_LITERAL])
    assert normalized['edits'] == [explanation_edit]
    assert rejected == []


def test_hybrid_object_wrapper_html_cannot_be_changed():
    draft = _draft()
    block = next(row for row in draft['blocks'] if row['id'] == 'p2-n1-b5')
    block['markdown'] = '<div align="center">\n' + block['markdown'] + '\n</div>'
    edit = {'block_id': 'p2-n1-b5', 'old_text': '<div align="center">',
            'new_text': '<div align="right">', 'reason': 'alter wrapper'}

    normalized, rejected = normalize_local_proposal({'edits': [edit]}, draft, [IMAGE_LITERAL])

    assert normalized['edits'] == []
    assert rejected == [{'block_id': 'p2-n1-b5', 'reason': 'protected_object_markup_edit'}]


@pytest.mark.parametrize('injected_markup', [
    '<img src="new-image.png">',
    '![new image](new-image.png)',
    '{{source:new-image}}',
])
def test_hybrid_object_caption_cannot_inject_image_or_source_markup(injected_markup):
    draft = _draft()
    edit = {'block_id': 'p2-n1-b5', 'old_text': CAPTION,
            'new_text': CAPTION + ' ' + injected_markup,
            'reason': 'inject protected markup'}

    normalized, rejected = normalize_local_proposal({'edits': [edit]}, draft, [IMAGE_LITERAL])

    assert normalized['edits'] == []
    assert rejected == [{'block_id': 'p2-n1-b5', 'reason': 'protected_object_markup_edit'}]
