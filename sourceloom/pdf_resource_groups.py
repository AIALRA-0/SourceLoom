"""Restore adjacent PDF image components in their source page composition.

An image extraction is not a figure extraction. When several adjacent markers
refer to components on the same PDF page, a rendered original page preserves
overlays, vector marks and reused occurrences without guessing figure bounds.
"""
import math
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


def physically_covered_source_ids(p, groups, objects):
    """Prove complete image occurrences in currently rendered original pages.

    Marker members trigger rendering; they are not the exhaustive contents of
    a PDF page. This is physical presentation evidence, never prose fidelity.
    Unknown geometry or an occurrence on an unrendered page remains uncovered.
    """
    originals = {o['name']: o['sha256'] for o in p['inventory']['originals']}
    assets = {r['id'] for r in p['inventory']['resources']}

    def placement(place):
        try:
            page, box = place['page'], place['bbox']
            width, height = place['page_width'], place['page_height']
            values = [width, height, *box]
            if (place.get('unit') != 'pdf_point' or type(page) is not int or page < 1
                    or len(box) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in values)
                    or width <= 0 or height <= 0
                    or not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)):
                return None
            return page, width, height
        except (KeyError, TypeError):
            return None

    pages = {}
    for group in groups.values():
        asset = group['asset']
        if (asset.get('composition_scope') != 'whole_original_page'
                or asset.get('source_digest') != p['processor']['source_digest']
                or asset.get('id') != asset.get('sha256')
                or originals.get(group['original_name']) != group['original_sha256']):
            continue
        scale = asset.get('render_scale')
        if type(scale) not in (int, float) or not math.isfinite(scale) or scale <= 0:
            continue
        geometries = []
        for sid in group['source_ids']:
            member = objects.get(sid, {})
            locations = member.get('placements') or []
            if not locations:
                break
            parsed = [placement(value) for value in locations]
            if any(value is None or value[0] != group['page'] for value in parsed):
                break
            geometries.extend(parsed)
        else:
            geometry = geometries[0] if geometries else None
            if not geometry or any(value != geometry for value in geometries):
                continue
            _, width, height = geometry
            if (type(asset.get('width')) is not int or type(asset.get('height')) is not int
                    or abs(asset['width'] - width * scale) > 1
                    or abs(asset['height'] - height * scale) > 1):
                continue
            pages[(group['original_name'], group['original_sha256'], group['page'])] = (width, height)

    covered = set()
    for sid, obj in objects.items():
        if obj.get('kind') != 'image' or obj.get('resource_id') not in assets:
            continue
        locator = re.fullmatch(r'(.+\.pdf)/page\[(\d+)\]/image\[\d+\]', obj.get('locator', ''), re.I)
        locations = obj.get('placements') or []
        if not locator or not locations or len(locations) != obj.get('placement_count'):
            continue
        original = originals.get(locator[1])
        parsed = [placement(value) for value in locations]
        if all(value and pages.get((locator[1], original, value[0])) == value[1:] for value in parsed):
            covered.add(sid)
    return covered
