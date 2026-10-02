"""Restore adjacent PDF image components in their source page composition.

An image extraction is not a figure extraction. When several adjacent markers
refer to components on the same PDF page, a rendered original page preserves
overlays, vector marks and reused occurrences without guessing figure bounds.
"""
import re

from .export import COMPACT_IMAGE_ROLES, image_presentation
from .math_render import markdown_renderer
from .store import digest


def marker_groups(p, markdown, choices):
    objects = {o['id']: o for o in p['inventory']['objects']}
    assets = {r['id'] for r in p['inventory']['resources']}
    originals = {o['name']: o for o in p['inventory']['originals']}
    protected = set()
    for token in markdown_renderer().parse(markdown):
        if token.type in {'fence', 'code_block'} and token.map:
            protected.update(range(*token.map))
    groups, run, location = [], [], None

    def flush():
        nonlocal run, location
        if len(run) >= 2 and sum(image_presentation(objects[sid])['role']
                                not in COMPACT_IMAGE_ROLES for sid, _ in run) >= 2:
            groups.append(dict(source_ids=[sid for sid, _ in run],
                               start_line=run[0][1], end_line=run[-1][1],
                               original_sha256=originals[location[0]]['sha256'],
                               original_name=location[0], page=location[1]))
        run, location = [], None

    for i, line in enumerate(markdown.splitlines()):
        if not line.strip():
            continue
        match = re.fullmatch(r'\s*\{\{source:([^}\s]+)\}\}\s*', line) if i not in protected else None
        obj = objects.get(match[1]) if match else None
        locator = re.fullmatch(r'(.+\.pdf)/page\[(\d+)\]/image\[\d+\]', obj.get('locator', ''), re.I) if obj else None
        candidate = (locator[1], int(locator[2])) if locator else None
        valid = (obj and obj['kind'] == 'image' and obj.get('resource_id')
                 and obj['resource_id'] in assets
                 and (choices.get(obj['id']) or 'body') == 'body'
                 and candidate and candidate[0] in originals
                 and obj.get('placements') and all(
                     place.get('page') == candidate[1] and place.get('unit') == 'pdf_point'
                     and len(place.get('bbox') or []) == 4 for place in obj['placements']))
        if not valid or candidate != location or (obj and obj['id'] in [sid for sid, _ in run]):
            flush()
        if valid:
            location = candidate
            run.append((obj['id'], i + 1))
    flush()
    return groups


def prepare_group_resources(store, p, markdown, choices):
    groups = marker_groups(p, markdown, choices)
    if not groups:
        return []
    import fitz
    resources, rendered = [], {}
    for group in groups:
        key = group['original_sha256'], group['page']
        if key not in rendered:
            raw = store.read_blob(key[0])
            if digest(raw) != key[0]:
                raise ValueError('图组对应的冻结 PDF 摘要不一致')
            with fitz.open(stream=raw, filetype='pdf') as doc:
                page = doc[key[1] - 1]
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                rendered_bytes = pixmap.tobytes('png')
                rendered[key] = dict(sha256=store.blob(rendered_bytes), size=len(rendered_bytes),
                                     width=pixmap.width, height=pixmap.height)
        image = rendered[key]
        resources.append(dict(id=image['sha256'], name=f'original-composition-page-{group["page"]}.png',
                              mime='image/png', **image, source_digest=p['processor']['source_digest'],
                              pdf_component_group=group, composition_scope='whole_original_page',
                              render_scale=2))
    return resources


def compiled_groups(p, markdown, choices, derived_resources):
    """Only bind saved compositions to the exact current member occurrence."""
    result = {}
    for group in marker_groups(p, markdown, choices):
        saved = next((r for r in derived_resources or []
                      if r.get('source_digest') == p['processor']['source_digest']
                      and r.get('id') == r.get('sha256')
                      and r.get('composition_scope') == 'whole_original_page'
                      and r.get('pdf_component_group') == group), None)
        if saved:
            result[group['start_line']] = group | {'asset': saved}
    return result
