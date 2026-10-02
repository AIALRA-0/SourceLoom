import copy

import pytest

from sourceloom.active_composition import (
    _needs_active_visual_card,
    classify_transparent_svg_placeholders,
)


def _source(raw, **object_fields):
    resource = dict(id='resource-svg', sha256='sha-svg', media_type='image/svg+xml')
    obj = dict(
        id='svg-placeholder', kind='image', source_format='inline-svg',
        resource_id=resource['id'], raw=raw, text='',
        visual_classification={'method': 'all_pixels_alpha_zero'},
    )
    obj.update(object_fields)
    return dict(
        objects=[obj], resources=[resource], originals=[dict(sha256='original-html')],
        unknown=[dict(object_id=obj['id'], reason='visual content unresolved')],
    )


def test_unlabelled_transparent_inline_svg_is_archived_without_a_visual_review():
    raw = '<svg width="12" height="12"><path d="M1 1L4 4"/></svg>'
    source = _source(raw)

    result = classify_transparent_svg_placeholders(source)

    obj = result['objects'][0]
    assert obj['source_scope'] == 'layout_decorative'
    assert obj['raw'] == raw and obj['resource_id'] == 'resource-svg'
    assert result['resources'] == source['resources']
    assert result['originals'] == source['originals']
    assert result['unknown'] == []
    assert 'visual_card' not in obj
    assert not _needs_active_visual_card(obj)
    # The decorative scope remains authoritative even if a later step rebuilds
    # visual classification state from the original object.
    without_classification = copy.deepcopy(obj)
    without_classification.pop('visual_classification')
    assert not _needs_active_visual_card(without_classification)
    assert 'source_scope' not in source['objects'][0]


@pytest.mark.parametrize(('raw', 'fields'), [
    ('<svg aria-label="Chart of yearly sales"><path d="M1 1L4 4"/></svg>', {}),
    ('<svg title="Company logo"><path d="M1 1L4 4"/></svg>', {}),
    ('<svg><title>Company logo</title><path d="M1 1L4 4"/></svg>', {}),
    ('<svg><text>42%</text><path d="M1 1L4 4"/></svg>', {}),
    ('<svg><path d="M1 1L4 4"/></svg>', {'text': 'Accessible diagram label'}),
])
def test_transparent_svg_with_accessible_or_source_text_is_not_decorative(raw, fields):
    source = _source(raw, source_scope='article_media', **fields)

    result = classify_transparent_svg_placeholders(source)

    obj = result['objects'][0]
    assert obj['source_scope'] == 'article_media'
    assert result['unknown'] == source['unknown']
    needs_review = copy.deepcopy(obj)
    needs_review.pop('visual_classification')
    assert _needs_active_visual_card(needs_review)


def test_transparent_non_svg_resource_is_left_eligible_for_review():
    source = _source('', source_format='png', source_scope='article_media')

    result = classify_transparent_svg_placeholders(source)

    obj = result['objects'][0]
    assert obj['source_scope'] == 'article_media'
    assert result['unknown'] == source['unknown']
