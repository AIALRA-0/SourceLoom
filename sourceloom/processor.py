"""Material preparation and output compilation, without an article control loop."""
import copy
from collections import Counter
import html
import json
import mimetypes
import re
import time
import zipfile
from io import BytesIO
from functools import lru_cache
from pathlib import PurePosixPath

from bs4 import BeautifulSoup

from .export import (COMPACT_IMAGE_ROLES, apply_image_presentation, editor_storage,
                     image_presentation, safe_html as sanitize_html)
from .ingest import intake, safe_member
from .math_render import markdown_renderer
from .store import Conflict, digest, identity
from .source_links import (known_link_targets, normalize_native_web_target,
                          restore_source_links, safe_absolute_target)

RESOURCE_USAGES = {'body', 'reference', 'exclude'}


def safe_html(raw):
    # Only source IDs and safe native <a> bookmarks survive. Arbitrary element
    # IDs and active HTML remain filtered by the shared sanitizer.
    return sanitize_html(raw, source_anchors=True, fragment_anchors=True)


def resource_usage(state, row):
    return (state.get('resource_usages') or {}).get(row['id']) or (
        'reference' if row['kind'] == 'page' else 'body')


def set_resource_usages(store, pid, changes, base_pack_digest=None):
    if not isinstance(changes, list) or not changes:
        raise ValueError('请选择需要调整的资源')
    p = project(store, pid)
    if not p.get('inventory'):
        raise Conflict('请先准备原件')
    if base_pack_digest and task_pack(store, pid)['digest'] != base_pack_digest:
        raise Conflict('任务包已经更新，请刷新资源选择后重试')
    allowed = {r['id']: r for r in p['processor']['resources']}
    updates = {}
    for row in changes:
        sid, usage = str(row.get('source_id') or ''), str(row.get('usage') or '')
        if sid not in allowed or usage not in RESOURCE_USAGES or sid in updates:
            raise ValueError('资源用途选择无效或重复')
        updates[sid] = usage
    def commit(current):
        state = current['processor']
        if base_pack_digest and state.get('pack_digest') != base_pack_digest:
            raise Conflict('任务包已经更新，请刷新资源选择后重试')
        state.setdefault('resource_usages', {}).update(updates)
    result = store.change(pid, commit)
    pack = task_pack(store, pid)
    return store.change(pid, lambda current: current['processor'].update(
        pack_digest=pack['digest'], policy_digest=pack['template_digest']))


def pdf_image_placements(raw, page_index, image, page=None):
    """Capture source display geometry, retaining each occurrence of one bitmap."""
    ref = getattr(image, 'indirect_reference', None)
    xref = getattr(ref, 'idnum', None)
    if not xref:
        return []
    doc = None
    try:
        if page is None:
            import fitz
            doc = fitz.open(stream=raw, filetype='pdf')
        try:
            if page is None:
                page = doc[page_index]
            return [dict(page=page_index+1, bbox=[round(v, 3) for v in rect],
                         page_width=round(page.rect.width, 3),
                         page_height=round(page.rect.height, 3), unit='pdf_point')
                    for rect in page.get_image_rects(xref)]
        finally:
            if doc is not None:
                doc.close()
    except (ImportError, ValueError, RuntimeError):
        return []


@lru_cache(maxsize=8)
def _pdf_placement_index(raw):
    """Recover old inventory geometry from its exact original, without writes."""
    try:
        import fitz
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(raw))
        placements = {}
        with fitz.open(stream=raw, filetype='pdf') as pdf:
            for page_no, page in enumerate(reader.pages, 1):
                original = pdf[page_no-1]
                for image_no, image in enumerate(page.images, 1):
                    xref = getattr(getattr(image, 'indirect_reference', None), 'idnum', None)
                    if not xref:
                        continue
                    places = [dict(page=page_no, bbox=[round(v, 3) for v in rect],
                                   page_width=round(original.rect.width, 3),
                                   page_height=round(original.rect.height, 3), unit='pdf_point')
                              for rect in original.get_image_rects(xref)]
                    if places:
                        placements[(page_no, image_no)] = places
        return placements
    except (ImportError, ValueError, RuntimeError):
        return {}


def presentation_project(p, read_blob):
    """Read-only rendering projection for candidates predating PDF geometry.

    Locator identity and the frozen original digest must both match. The
    projection does not replace source bytes, saved Markdown or source digest.
    """
    inventory = p.get('inventory') or {}
    missing = [obj for obj in inventory.get('objects', [])
               if obj['kind'] == 'image' and not obj.get('placements')]
    updates = {}
    for original in inventory.get('originals', []):
        if not original['name'].lower().endswith('.pdf'):
            continue
        matches = []
        for obj in missing:
            match = re.fullmatch(re.escape(original['name'])+r'/page\[(\d+)\]/image\[(\d+)\]', obj['locator'])
            if match:
                matches.append((obj, (int(match[1]), int(match[2]))))
        if not matches:
            continue
        try:
            raw = read_blob(original['sha256'])
        except (OSError, KeyError):
            continue
        if digest(raw) != original['sha256']:
            continue
        index = _pdf_placement_index(raw)
        for obj, location in matches:
            if location in index:
                updates[obj['id']] = dict(obj, placements=copy.deepcopy(index[location]),
                                         placement_count=len(index[location]))
    if not updates:
        return p
    return p | {'inventory': p['inventory'] | {'objects': [updates.get(obj['id'], obj)
                 for obj in p['inventory']['objects']]}}

OUTPUT_CONTRACT = """输出完整 Markdown 正文，不输出 JSON、审核回执、工作过程或图片编码。正文使用的原件对象以 {{source:ID}} 独占一行；程序恢复当前任务包中真实存在的资源。原页 marker 只创建回查入口，不把整页当正文插图。
对没有独立 table/formula 资源 ID 的原件，不承诺程序自动恢复该表或公式；可在看清原页后忠实重建成可编辑 Markdown/数学式，或仅保留原页回查并明确尚未完成。不能凭提取文字缺失就猜表格符号或公式。只使用本次实际可见材料，不增补来源外事实。"""


def project(store, pid):
    p = store.get(pid)
    if 'processor' not in p:
        raise Conflict('这是历史生成任务，请在历史工作台打开')
    return p


def create(store, title, preferences='', parent_id=None):
    if not title.strip() or len(title) > 180 or len(preferences) > 12000:
        raise ValueError('材料标题或阅读偏好超过范围')
    if parent_id is not None and not isinstance(parent_id, str):
        raise ValueError('目标目录身份必须是文字')
    parent_id = parent_id or None
    def initialize(p, cx):
        if parent_id:
            parent = cx.execute("SELECT id FROM library_folders WHERE id=? AND scope='processor' AND trashed=0", (parent_id,)).fetchone()
            if not parent:
                raise Conflict('目标目录不存在或在回收站中，请重新选择')
            from .processor_tree import name_key
            siblings = [r[0] for r in cx.execute('SELECT title FROM processor_material_meta WHERE parent=? AND trashed=0 AND archived=0', (parent_id,))]
            siblings += [r[0] for r in cx.execute("SELECT name FROM library_folders WHERE scope='processor' AND parent=? AND trashed=0", (parent_id,))]
            if any(name_key(value) == name_key(title) for value in siblings):
                raise Conflict('目标目录已有同名材料或文件夹，请修改名称')
        p.update(folder=parent_id, processor=dict(
            schema='sourceloom-processor/1', preferences=preferences, versions=[], requests=[],
            active_version=None, source_text='', source_map=[], resources=[], resource_usages={}, checks=[]))
    return store.create(title.strip(), budget=20, initialize=initialize)


def source_prose(obj, link_targets=None):
    """Keep inline source links and code names in the model-readable projection."""
    raw = obj.get('raw', '')
    if not raw.lstrip().startswith('<'):
        return obj.get('text', '')
    doc = BeautifulSoup(restore_source_links(raw, link_targets or {}),'html.parser')
    for anchor in doc.select('a[href]'):
        target = anchor['href']
        label = anchor.get_text(' ',strip=True)
        if safe_absolute_target(target) or target.startswith('#'):
            anchor.replace_with('['+label.replace(']',r'\]')+']('+target.replace(' ', '%20')+')')
    for code in doc.find_all('code'):
        code.replace_with('`'+code.get_text()+'`')
    return doc.get_text(' ',strip=True)


def source_text_projection(inv, resource_usages=None):
    choices = resource_usages or {}
    link_targets = known_link_targets(inv)
    result = []
    excluded = {'site_chrome', 'source_metadata', 'layout_decorative'}
    for obj in inv['objects']:
        if obj.get('source_scope') in excluded or obj['kind'] in {'metadata', 'unknown', 'attachment'}:
            continue
        kind, sid = obj['kind'], obj['id']
        if choices.get(sid) == 'exclude':
            continue
        marker = '{{source:'+sid+'}}'
        if kind == 'page':
            result.append(f'\n## 原件 {obj["locator"]}\n{marker}\n{obj.get("text", "")}')
        elif kind == 'heading':
            result.append('\n## '+source_prose(obj,link_targets))
        elif kind == 'link':
            continue
        elif kind in {'image', 'media'} and not obj.get('resource_id'):
            # An extracted label or URL is not the visual object. Do not give
            # the model an insertion marker that the compiler cannot restore.
            result.append('原文视觉资源尚未取得，需回到原件核对：'+
                          (obj.get('text') or obj.get('target') or obj['locator'])[:180])
        elif kind in {'code', 'formula', 'table', 'image', 'media'}:
            result.append(marker+'\n'+(obj.get('text', '') if kind in {'code','formula','table'}
                                        else obj.get('text', '')[:180]))
        elif obj.get('word_list') and kind in {'text', 'heading'}:
            word_list = obj['word_list']
            prefix = '  ' * min(max(int(word_list.get('level', 0)), 0), 8)
            result.append(prefix + word_list['marker'] + ' ' + source_prose(obj, link_targets))
        else:
            result.append(source_prose(obj,link_targets))
    return '\n\n'.join(result).strip()


def prepare(store, pid, uploads, source_url=None, asset_aliases=None, web_manifest=None):
    baseline = project(store, pid)
    if baseline.get('inventory'):
        raise Conflict('原件已冻结；不同材料请建立新项目，避免覆盖来源')
    def report(value):
        store.change(pid,lambda current:current['processor'].update(
            intake_progress=dict(value,started=(current['processor'].get('intake_progress') or {}).get('started',time.time()),updated=time.time())))
    report(dict(phase='saving_original',completed=0,total=len(uploads)))
    try:
        inv = intake(store, uploads, source_url, asset_aliases, progress=report)
    except Exception as exc:
        report(dict(phase='failed',detail=str(exc)[:240]))
        raise
    # Word numbering and saved field results already have a deterministic
    # resolver. Apply it before freezing this processor's source inventory.
    from .word_structures import resolve_word_structures
    inv = resolve_word_structures(store, inv)
    report(dict(phase='extracting_resources',completed=0,total=len(inv['objects'])))
    if web_manifest:
        # Earlier frozen web bundles kept only the image manifest list. The
        # current browser capture is a mapping with richer position evidence.
        inv['web_snapshot'] = (web_manifest if isinstance(web_manifest,dict)
                               else {'images':web_manifest})
    from .source_context import classify_inert_markup, classify_layout_tables, classify_web_chrome
    inv = classify_layout_tables(classify_web_chrome(store, classify_inert_markup(inv)))
    # Embedded PDF image bytes are independent of the complete page previews.
    # Keep the original PDF and page previews even if an image cannot be decoded.
    for original in inv['originals']:
        if not original['name'].lower().endswith('.pdf'):
            continue
        from pypdf import PdfReader
        pdf_bytes = store.read_blob(original['sha256'])
        reader = PdfReader(BytesIO(pdf_bytes))
        if reader.is_encrypted:
            reader.decrypt('')
        # One Page object per source page retains PyMuPDF's image-info cache.
        # Geometry is optional; source image bytes still use the existing parser.
        geometry_doc = None
        try:
            import fitz
            geometry_doc = fitz.open(stream=pdf_bytes, filetype='pdf')
        except (ImportError, ValueError, RuntimeError):
            pass
        try:
            for page_no, page in enumerate(reader.pages, 1):
                report(dict(phase='extracting_resources',completed=page_no-1,total=len(reader.pages),
                            detail=original['name'], page=page_no))
                try:
                    images = list(page.images)
                except Exception:
                    continue
                geometry_page = None
                if geometry_doc is not None:
                    try:
                        geometry_page = geometry_doc[page_no-1]
                    except (ValueError, RuntimeError):
                        pass
                for index, image in enumerate(images, 1):
                    try:
                        from PIL import Image
                        picture = Image.open(BytesIO(image.data))
                        picture.load()
                        if picture.width * picture.height > 40_000_000:
                            continue
                        mime = Image.MIME.get(picture.format)
                        if mime in {'image/png','image/jpeg','image/gif','image/webp'}:
                            raw = image.data
                            suffix = {'image/png':'png','image/jpeg':'jpg','image/gif':'gif','image/webp':'webp'}[mime]
                        else:
                            buf = BytesIO()
                            picture.convert('RGBA').save(buf, 'PNG')
                            raw, mime, suffix = buf.getvalue(), 'image/png', 'png'
                    except Exception:
                        continue
                    key = store.blob(raw)
                    name = f'figure-{page_no}-{index}.'+suffix
                    inv['resources'].append(dict(id=key, name=name, sha256=key, mime=mime, size=len(raw)))
                    placements = (pdf_image_placements(pdf_bytes, page_no-1, image, page=geometry_page)
                                  if geometry_page is not None else [])
                    tiny = bool(placements) and all(
                        max(place['bbox'][2]-place['bbox'][0], place['bbox'][3]-place['bbox'][1]) < 24
                        for place in placements)
                    inv['objects'].append(dict(id=f'src-{len(inv["objects"])+1:05d}', kind='image',
                        text=f'{original["name"]} 第 {page_no} 页原图 {index}', resource_id=key,
                        locator=f'{original["name"]}/page[{page_no}]/image[{index}]',
                        placements=placements, placement_count=len(placements),
                        purpose_hint=('小尺寸行内组件候选；用途待对照原页确认' if tiny else
                                      '复用资源；逐个原页位置待对照' if len(placements)>1 else
                                      '用途待对照原页确认')))
        finally:
            if geometry_doc is not None:
                geometry_doc.close()
        report(dict(phase='extracting_resources',completed=len(reader.pages),total=len(reader.pages),detail=original['name']))
    resources, locations = [], []
    object_index = {obj['id']:obj for obj in inv['objects']}
    excluded = {'site_chrome', 'source_metadata', 'layout_decorative'}
    for obj in inv['objects']:
        if obj.get('source_scope') in excluded or obj['kind'] in {'metadata', 'unknown', 'attachment'}:
            continue
        row = dict(id=obj['id'], kind=obj['kind'], label=obj.get('text', '')[:180],
                   locator=obj['locator'], marker='{{source:'+obj['id']+'}}')
        if obj.get('parent_id'):
            parent = object_index.get(obj['parent_id'])
            if parent:
                row.update(parent_id=parent['id'],parent_kind=parent['kind'])
                if obj['kind']=='image' and parent['kind']=='table':
                    row['purpose_hint']='来源表格中的图像组件；逐格含义需要对照确认'
        if obj.get('placements'):
            row.update(placements=obj['placements'],placement_count=obj['placement_count'],
                       purpose_hint=obj['purpose_hint'])
        if obj['kind']=='page':
            row['label']='原件页面 · '+obj['locator']
        if obj.get('resource_id'):
            row['sha256'] = obj['resource_id']
        locations.append(dict(source_id=obj['id'], locator=obj['locator'], kind=obj['kind']))
        if obj['kind'] in {'code', 'formula', 'table', 'image', 'page', 'media'}:
            resources.append(row)
    source_text = source_text_projection(inv)
    if not source_text and not resources:
        raise ValueError('没有可交接的正文或视觉材料；原文件格式尚不支持')
    inv['frozen'] = True
    inv['digest'] = digest({k: v for k, v in inv.items() if k != 'digest'})
    def commit(p):
        if p.get('trashed'):
            raise Conflict('材料已移至回收站，解析结果未恢复到活动目录')
        if p.get('inventory'):
            raise Conflict('另一项接入已完成，未覆盖冻结原件')
        p.update(inventory=inv, state='prepared')
        p['processor'].update(source_text=source_text, source_map=locations,
                              resources=resources, source_digest=inv['digest'])
    report(dict(phase='building_index',detail='正在保存完整来源索引与任务说明'))
    p = store.change(pid, commit)
    pack = task_pack(store, pid)
    return store.change(pid, lambda p: p['processor'].update(
        pack_digest=pack['digest'], policy_digest=pack['template_digest'],
        intake_progress=dict(phase='complete',completed=len(inv['objects']),
                             total=len(inv['objects']),updated=time.time())))


def task_pack(store, pid):
    p = project(store, pid)
    if not p.get('inventory'):
        raise Conflict('请先上传材料')
    state = p['processor']
    indexed = [dict(row, usage=resource_usage(state,row),
                    available=bool(row.get('sha256')) if row['kind'] in {'image','media','page'} else True,
                    url=f'/api/processor/projects/{pid}/files/{row["sha256"]}')
               if row.get('sha256') else dict(row,usage=resource_usage(state,row),
                                              available=row['kind'] not in {'image','media','page'})
               for row in state['resources']]
    from .processor_prompt import build_effective_template
    inv = p['inventory']
    template = build_effective_template(
        (r['kind'] for r in indexed if r['usage']=='body'),
        has_formula=any(r['kind']=='formula' and r['usage']=='body' for r in indexed))
    prompt = template['text']+'\n\n'+OUTPUT_CONTRACT
    prompt += '\n\n本次阅读偏好：\n'+(state['preferences'] or '自然、紧凑，必要解释就地完成')
    prompt += '\n\n资源索引（定位信息，不是教学义务；仅“正文使用”可独立插入）：\n'+json.dumps(
        [{k:v for k,v in r.items() if k not in {'sha256', 'url'}}
         for r in indexed if r['usage']!='exclude'], ensure_ascii=False, indent=2)
    references = [dict(source_id=o['id'], label=o.get('text',''),target=o.get('target',''),locator=o['locator'])
        for o in p['inventory']['objects'] if o['kind']=='link' and
        o.get('source_scope') not in {'site_chrome','source_metadata','layout_decorative'}]
    if references:
        prompt += '\n\n原文参考链接（仅供保留原标签与对应位置，不产生目标页解释义务，不访问）：\n'+json.dumps(references,ensure_ascii=False,indent=2)
    attachments = []
    seen = set()
    for original in inv['originals']:
        original_mime = mimetypes.guess_type(original['name'])[0] or 'application/octet-stream'
        origin_kind = 'original'
        origin_context = {}
        if original_mime in {'image/png','image/jpeg','image/gif','image/webp'}:
            matches = [r for r in indexed if r.get('sha256')==original['sha256'] and
                       r['usage']!='exclude']
            # A captured web asset may be site chrome or unused. Being saved as
            # an original does not by itself authorize sending it to a model.
            if not matches:
                continue
            chosen = next((r for r in matches if r['usage']=='body'),matches[0])
            origin_kind = chosen['kind']
            origin_context = dict(resource_id=chosen['id'],locator=chosen['locator'])
        attachments.append(dict(original, mime=original_mime, kind=origin_kind,
                                **origin_context))
        seen.add(original['sha256'])
    by_key = {r['id']:r for r in inv['resources']}
    for row in indexed:
        if row['usage']=='exclude' or not row.get('sha256') or row['sha256'] in seen:
            continue
        asset = by_key[row['sha256']]
        attachments.append(dict(asset, kind=row['kind'], resource_id=row['id'], locator=row['locator']))
        seen.add(row['sha256'])
    for att in attachments:
        att['url'] = f'/api/processor/projects/{pid}/files/{att["sha256"]}'
    result = dict(prompt=prompt, source_text=source_text_projection(inv,state.get('resource_usages')),
                  attachments=attachments, resources=indexed,
                  resource_selection={r['id']:r['usage'] for r in indexed},
                  requires_visual=any(r['usage']!='exclude' and r['kind'] in {'image','page','media'} for r in indexed),
                  policy_digest=template['template_digest'],
                  template_version=template['template_version'],
                  template_digest=template['template_digest'], skill_commit=template['skill_commit'],
                  skill_source_sha256=template['skill_source_sha256'],
                  applicable_rule_ids=template['applicable_rule_ids'],
                  product_exceptions=template['product_exceptions'],
                  source_digest=state['source_digest'])
    result['digest'] = digest({k:v for k,v in result.items() if k != 'digest'})
    return result


def pack_zip(store, pid):
    from .manual_handoff import pack_zip as manual_pack_zip
    return manual_pack_zip(store, pid)


def object_html(obj, target='preview', link_targets=None):
    kind = obj['kind']
    if kind in {'image','media','page'} and not obj.get('resource_id'):
        return '<aside data-resource-unavailable="true">原件视觉资源尚未取得：'+html.escape(
            obj.get('text') or obj.get('target') or obj['locator'])+'</aside>'
    if kind in {'image', 'media'} and obj.get('resource_id'):
        doc = BeautifulSoup('', 'html.parser')
        image = doc.new_tag('img', src='assets/'+obj['resource_id'], alt=obj.get('text', ''))
        info = apply_image_presentation(image, obj, obj.get('presentation_occurrence'))
        caption = ('<figcaption>'+html.escape(obj['figure_caption'])+'</figcaption>'
                   if obj.get('figure_caption') else '')
        wrapper = 'span' if info['role'] in COMPACT_IMAGE_ROLES and not caption else 'figure'
        return '<'+wrapper+' data-source-role="'+info['role']+'">'+str(image)+caption+'</'+wrapper+'>'
    if kind == 'page':
        if target == 'readweave':
            # Native CKEditor does not retain details/summary semantics. Keep
            # exact page attachments reachable without expanding every page
            # image into the article when the editor saves its HTML.
            return ('<p><a data-source-role="page-reference" data-source-id="'+html.escape(obj['id'], quote=True)+
                    '" href="assets/'+obj['resource_id']+'">回查原页 · '+html.escape(obj['locator'])+'</a></p>')
        return '<details><summary>回查原页 · '+html.escape(obj['locator'])+'</summary><img src="assets/'+obj['resource_id']+'" alt="'+html.escape(obj['locator'], quote=True)+'"></details>'
    if kind == 'code':
        return '<pre><code>'+html.escape(obj['text'])+'</code></pre>'
    if kind == 'table' and obj.get('raw', '').lstrip().startswith('<table'):
        return safe_html(restore_source_links(obj['raw'],link_targets or {}))
    if kind == 'formula':
        return markdown_renderer().render('$$\n'+obj['text']+'\n$$')
    if kind == 'link':
        return safe_html('<a href="'+html.escape(obj.get('target', ''), quote=True)+'">'+html.escape(obj['text'])+'</a>')
    return '<p>'+html.escape(obj.get('text', '')).replace('\n', '<br>')+'</p>'


def _table_grid_matches(table, grid, columns=None):
    actual = [[cell.get_text(' ',strip=True) for cell in row.find_all(['th','td'])]
              for row in table.find_all('tr')]
    if columns is None:
        return actual == grid
    if not actual or len(actual[0]) != len(columns)+1 or any(
            name.casefold() not in actual[0][index+1].casefold()
            for index,name in enumerate(columns)):
        return False
    for expected in grid:
        if len(expected)!=len(columns)+1:
            return False
        matches = [row for row in actual[1:] if row and row[0]==expected[0]]
        if len(matches)!=1 or matches[0]!=expected:
            return False
    return True


def _native_table_signature(table):
    return [[(c.get_text(' ', strip=True), str(c.get('rowspan', '1')),
              str(c.get('colspan', '1'))) for c in row.find_all(['th', 'td'])]
            for row in table.find_all('tr')]


def _table_grid_problem(table, grid, columns=None):
    """Give an operator the first local mismatch, without guessing source facts."""
    actual = [[cell.get_text(' ', strip=True) for cell in row.find_all(['th', 'td'])]
              for row in table.find_all('tr')]
    if not actual:
        return '目标正文块没有可读取的表格行'
    if columns is None:
        if len(actual) != len(grid):
            return f'表格行数不符：原件核对 {len(grid)} 行，成稿 {len(actual)} 行'
        for row_no, (expected, found) in enumerate(zip(grid, actual), 1):
            if len(expected) != len(found):
                return f'表格第 {row_no} 行列数不符：原件核对 {len(expected)} 列，成稿 {len(found)} 列'
            for column_no, (wanted, value) in enumerate(zip(expected, found), 1):
                if wanted != value:
                    return f'表格第 {row_no} 行第 {column_no} 列不符：原件核对「{wanted}」，成稿「{value}」'
        return ''
    if len(actual[0]) != len(columns) + 1:
        return f'表格列数不符：核对需要 {len(columns)+1} 列，成稿 {len(actual[0])} 列'
    for index, name in enumerate(columns, 1):
        if name.casefold() not in actual[0][index].casefold():
            return f'表头第 {index+1} 列未对应「{name}」'
    for expected in grid:
        found = [row for row in actual[1:] if row and row[0] == expected[0]]
        if len(found) != 1:
            return f'表格行「{expected[0]}」缺失或重复'
        if len(found[0]) != len(expected):
            return f'表格行「{expected[0]}」的列数不符'
        for index, (wanted, value) in enumerate(zip(expected[1:], found[0][1:]), 1):
            if wanted != value:
                return f'表格行「{expected[0]}」第 {index+1} 列（{columns[index-1]}）不符：原件核对「{wanted}」，成稿「{value}」'
    return ''


def _alternative_covers(record, sid, markdown, choices, sections):
    if (sid not in record.get('source_ids', []) or
            record.get('draft_digest') != digest(markdown.encode()) or
            record.get('resource_snapshot_digest') != digest(choices)):
        return False
    block = sections.get(record.get('block_id'))
    if block is None:
        return False
    start, end = block.get('data-source-start-line'), block.get('data-source-end-line')
    if not start or not end:
        return False
    lines = markdown.splitlines(keepends=True)
    if digest(''.join(lines[int(start)-1:int(end)]).encode()) != record.get('target_markdown_digest'):
        return False
    method = record.get('method')
    target = {'table': 'table', 'formula': 'math, .math-tex',
              'quote': 'blockquote', 'text': 'p'}.get(method)
    if not target or not block.select_one(target):
        return False
    table=None
    if method=='table':
        tables=block.select('table');index=record.get('table_index')
        if index is None:
            if len(tables)!=1:return False
            table=tables[0]
        elif isinstance(index,int) and 0<=index<len(tables):table=tables[index]
        else:return False
        if record.get('target_table_digest') and digest(str(table).encode())!=record['target_table_digest']:return False
    grid = record.get('source_grid')
    if grid is not None:
        if method != 'table' or not isinstance(grid, list):
            return False
        if not _table_grid_matches(table,grid,record.get('source_columns')):
            return False
        if record.get('source_cell_spans') and [
                [[c[1], c[2]] for c in row] for row in _native_table_signature(table)
        ] != record['source_cell_spans']:
            return False
    return True


def _source_grid_signatures(p):
    grid_signatures={}
    for v in p['processor'].get('versions',[]):
        if v.get('source_digest')!=p['processor']['source_digest']:continue
        for r in v.get('representations') or []:
            if r.get('source_grid'):
                key=(tuple(r.get('source_ids') or []),r.get('block_id'),r.get('table_index',0))
                grid_signatures.setdefault(key,set()).add(digest([r['source_grid'],r.get('source_columns')]))
    return grid_signatures


def _rechecked_source_grids(p,markdown,choices,sections):
    """Read-only recheck of explicitly recorded cells at their occurrence."""
    records=[];seen=set();lines=markdown.splitlines(keepends=True)
    grid_signatures=_source_grid_signatures(p)
    for version in reversed(p['processor'].get('versions',[])):
        if version.get('source_digest')!=p['processor']['source_digest']:continue
        for source in version.get('representations') or []:
            key=(tuple(source.get('source_ids') or []),source.get('block_id'),source.get('table_index',0))
            if key in seen or not source.get('source_grid') or source.get('method')!='table':continue
            if len(grid_signatures.get(key,set()))>1:continue
            seen.add(key);node=sections.get(source.get('block_id'))
            if not node:continue
            tables=node.select('table');index=source.get('table_index',0)
            if len(tables)<=index or (source.get('table_index') is None and len(tables)!=1):continue
            if not _table_grid_matches(tables[index],source['source_grid'],source.get('source_columns')):continue
            occurrence=source.get('source_occurrence') or {}
            if occurrence:
                ids=set((node.get('data-source-ids') or '').split(','))
                objects={o['id']:o for o in p['inventory']['objects']}
                locators=[objects[sid].get('locator','') for sid in ids if sid in objects]
                if occurrence.get('page') and not any(f'page[{occurrence["page"]}]' in loc and
                       loc.startswith(occurrence.get('document','')+'/') for loc in locators):continue
            start,end=node.get('data-source-start-line'),node.get('data-source-end-line')
            if not start or not end:continue
            bound=copy.deepcopy(source)
            bound.update(draft_digest=digest(markdown.encode()),resource_snapshot_digest=digest(choices),
                         target_markdown_digest=digest(''.join(lines[int(start)-1:int(end)]).encode()),
                         confirmed_by='saved_source_grid_rechecked')
            if source.get('target_table_digest'):bound['target_table_digest']=digest(str(tables[index]).encode())
            records.append(bound)
    native_counts={}
    for obj in p['inventory']['objects']:
        if obj.get('cells') and obj.get('raw'):
            original = BeautifulSoup(obj['raw'], 'html.parser').find('table')
            if not original:continue
            key=digest(_native_table_signature(original))
            native_counts[key]=native_counts.get(key,0)+1
    for obj in p['inventory']['objects']:
        cells=obj.get('cells')
        if obj['kind']!='table' or not cells or '<img' in obj.get('raw','').lower():continue
        original = BeautifulSoup(obj.get('raw', ''), 'html.parser').find('table')
        if not original:continue
        signature=_native_table_signature(original)
        grid=[[c[0] for c in row] for row in signature]
        matches=[(n, index, table) for n in sections.values()
                 for index, table in enumerate(n.select('table'))
                 if _native_table_signature(table)==signature]
        if len(matches)!=1:continue
        n,index,table=matches[0];start,end=n.get('data-source-start-line'),n.get('data-source-end-line')
        if native_counts.get(digest(signature),0)>1 and obj['id'] not in (n.get('data-source-ids') or '').split(','):continue
        if not start or not end:continue
        records.append(dict(method='table',source_ids=[obj['id']],block_id=n['data-block-id'],table_index=index,source_grid=grid,
                            source_cell_spans=[[[c[1],c[2]] for c in row] for row in signature],
                            source_columns=None,native_full_grid=True,confirmed_by='native_source_cells_rechecked',
                            evidence_scope='parsed_native_text_cells_and_spans_only_not_images_or_whole_article',
                            draft_digest=digest(markdown.encode()),resource_snapshot_digest=digest(choices),
                            target_markdown_digest=digest(''.join(lines[int(start)-1:int(end)]).encode())))
    return records


def compile_result(p, markdown, target='preview', resource_usages=None, representations=None, derived_resources=None, scope_exclusions=None):
    if not isinstance(markdown, str) or len(markdown.encode()) > 4 * 1024 * 1024:
        raise ValueError('正文最多 4 MB')
    checks, mapping, inserted = [], [], []
    if scope_exclusions is None:
        current=next((v for v in p['processor'].get('versions',[]) if v['id']==p['processor'].get('active_version') and v['markdown']==markdown),{})
        scope_exclusions=current.get('scope_exclusions') or []
    if scope_exclusions:
        checks.append(dict(severity='error',category='scope',code='EXPLICIT_SCOPE_REDUCTION',
                           message='本次已明确排除部分正文内容，不符合未限定的完整忠实交付；候选可阅读，完整导出需恢复或另行限定范围'))
    if not markdown.strip():
        checks.append(dict(severity='error', code='EMPTY_BODY', message='正文为空'))
    # Older candidates may have geometry in the resource index only. Reading
    # it as a projection preserves both frozen inventory and saved Markdown.
    indexed = {row['id']: row for row in p['processor'].get('resources', [])}
    objects = {}
    for source in p['inventory']['objects']:
        obj = dict(source)
        for key in ('placements', 'parent_kind', 'presentation_role', 'presentation_occurrence'):
            if key not in obj and key in indexed.get(obj['id'], {}):
                obj[key] = indexed[obj['id']][key]
        objects[obj['id']] = obj
    resources = {r['id']:r for r in p['inventory']['resources']}
    if derived_resources is None:
        saved = next((v for v in reversed(p['processor'].get('versions', []))
                      if v.get('markdown') == markdown and v.get('source_digest') == p['processor']['source_digest']), {})
        derived_resources = saved.get('derived_resources', [])
    for row in derived_resources or []:
        if row.get('source_digest') == p['processor']['source_digest'] and row.get('id') == row.get('sha256'):
            resources[row['id']] = row
    from .pdf_resource_groups import compiled_groups, physically_covered_source_ids
    groups = compiled_groups(p, markdown, resource_usages or
                             p['processor'].get('resource_usages') or {}, derived_resources)
    composition_coverage = physically_covered_source_ids(p, groups, objects)
    grouped_lines = {line for group in groups.values()
                     for line in range(group['start_line'] + 1, group['end_line'] + 1)}
    grouped_source_ids = set()
    displayed_compositions = set()
    renderer = markdown_renderer(target)
    tokens = renderer.parse(markdown)
    # A marker is an insertion command only outside a code block. Code bytes
    # containing marker-like text are ordinary visible source, not commands.
    protected = set()
    lines = markdown.splitlines(keepends=True)
    for token in tokens:
        if token.type in {'fence', 'code_block'} and token.map:
            protected.update(range(*token.map))
    choices = dict(resource_usages if resource_usages is not None else p['processor'].get('resource_usages') or {})
    parts, prose, prose_lines, counter = [], [], [], 0
    current_page = None
    link_targets = known_link_targets(p['inventory'])
    def flush():
        nonlocal counter
        if not prose:
            return
        chunk = ''.join(prose)
        counter += 1
        block_id = f'block-{counter:04d}'
        try:
            raw = renderer.render(chunk)
        except ValueError as exc:
            checks.append(dict(severity='error', code='RENDER_ERROR', message=str(exc)))
            raw = '<pre>'+html.escape(chunk)+'</pre>'
        parts.append('<section data-readweave-anchor-id="'+block_id+'" data-block-id="'+block_id+'">'+safe_html(restore_source_links(raw,link_targets))+'</section>')
        entry = dict(block_id=block_id, source_ids=[current_page] if current_page else [],
                     mapping='page_context' if current_page else 'unassigned',
                     source_start_line=min(prose_lines)+1, source_end_line=max(prose_lines)+1)
        if current_page:
            entry['locator'] = objects[current_page]['locator']
        mapping.append(entry)
        prose.clear()
        prose_lines.clear()
    for i, line in enumerate(lines):
        if i + 1 in grouped_lines:
            continue
        group = groups.get(i + 1)
        if group:
            flush()
            members = group['source_ids']
            inserted.extend(members)
            grouped_source_ids.update(members)
            occurrence = inserted.count(members[0])
            block_id = 'resource-' + members[0] + '-composition' + (f'-{occurrence}' if occurrence > 1 else '')
            label = f'原页图组 · {group["original_name"]} · 第 {group["page"]} 页'
            image = ('<img src="assets/' +
                   group['asset']['id'] + '" alt="' + html.escape(label, quote=True) +
                   '" loading="lazy" width="' + str(group['asset']['width']) +
                   '" height="' + str(group['asset']['height']) + '">')
            page_key = (group['original_sha256'], group['page'])
            if page_key in displayed_compositions:
                raw = ('<p><a href="assets/' + group['asset']['id'] + '">' + html.escape(label) + '</a></p>'
                       if target == 'readweave' else
                       '<details><summary>' + html.escape(label) + '</summary>' + image + '</details>')
            else:
                raw = '<figure data-source-role="original-page-composition">' + image + '</figure>'
            displayed_compositions.add(page_key)
            parts.append('<section data-readweave-anchor-id="' + block_id +
                         '" data-block-id="' + block_id + '">' + raw + '</section>')
            mapping.append(dict(block_id=block_id, source_ids=members,
                                locator=group['original_name'] + f'/page[{group["page"]}]',
                                mapping='original_page_composition',
                                composition_scope='whole_original_page',
                                source_start_line=group['start_line'], source_end_line=group['end_line']))
            continue
        matches = list(re.finditer(r'\{\{source:([^}\s]+)\}\}', line)) if i not in protected else []
        if not matches:
            prose.append(line)
            prose_lines.append(i)
            continue
        for match in matches:
            sid = match[1]
            if sid not in objects:
                checks.append(dict(severity='error', code='UNKNOWN_MARKER', source_id=sid, message='不存在的资源标记：'+sid))
            elif sid in inserted and objects[sid]['kind'] in {'code','table','formula'}:
                checks.append(dict(severity='error', code='DUPLICATE_RESOURCE', source_id=sid, message='原件资源重复插入：'+sid))
        # Resource placement is intentionally explicit; embedded Markdown URLs
        # or half-sentences are not silently converted into a different layout.
        if len(matches) != 1 or line.strip() != matches[0][0]:
            checks.append(dict(severity='error', code='MARKER_PLACEMENT', message='资源标记需要独占一行'))
            prose.append(line)
            prose_lines.append(i)
            continue
        flush()
        sid = matches[0][1]
        obj = objects.get(sid)
        if not obj:
            parts.append('<p>'+html.escape(line)+'</p>')
            continue
        usage = choices.get(sid) or ('reference' if obj['kind']=='page' else 'body')
        if usage == 'exclude' or (usage == 'reference' and obj['kind'] != 'page'):
            checks.append(dict(severity='error', code='RESOURCE_NOT_AUTHORIZED', source_id=sid,
                               message='本次资源用途不允许独立插入；可调整用途或编辑标记'))
            parts.append('<p>'+html.escape(line)+'</p>')
            continue
        inserted.append(sid)
        block_id = 'resource-'+sid+('-'+str(inserted.count(sid)) if inserted.count(sid)>1 else '')
        try:
            raw = object_html(obj, target, link_targets)
        except ValueError as exc:
            checks.append(dict(severity='error', code='RESOURCE_RENDER', source_id=sid, message=str(exc)))
            raw = '<pre>'+html.escape(obj.get('text', ''))+'</pre>'
        if obj.get('resource_id') and obj['resource_id'] not in resources:
            checks.append(dict(severity='error', code='MISSING_ASSET', source_id=sid, message='原件资源文件缺失'))
        if obj['kind'] in {'image','media','page'} and not obj.get('resource_id'):
            checks.append(dict(severity='error', code='RESOURCE_BINARY_UNAVAILABLE', source_id=sid,
                               message='原文视觉资源未取得，不能用文字标签或空标记冒充已恢复图片'))
        # Coarse mapping lives in source-map.json. An empty native bookmark
        # becomes a visible editor widget and large blank space, so do not add
        # one merely to prove that the source object has an identity.
        parts.append('<section data-readweave-anchor-id="'+block_id+'" data-block-id="'+block_id+'">'+raw+'</section>')
        mapping.append(dict(block_id=block_id, source_ids=[sid], locator=obj['locator'], mapping='explicit_resource',
                            source_start_line=i+1, source_end_line=i+1))
        if obj['kind']=='page':
            current_page = sid
        # Coarse source mapping, never fabricated sentence evidence.
        if mapping[:-1]:
            previous = mapping[-2]
            if not previous['source_ids']:
                previous.update(source_ids=[sid], locator=obj['locator'], mapping='adjacent_resource')
    flush()
    doc = BeautifulSoup('\n'.join(parts), 'html.parser')
    # Prose is sanitized in chunks separated by original resources. Enforce
    # unique authored bookmark destinations across the assembled article too.
    anchor_counts = Counter(str(node['id']) for node in doc.find_all(id=True))
    for anchor in doc.select('a[id]'):
        if not anchor['id'].startswith('loom-source-') and anchor_counts[anchor['id']] != 1:
            del anchor['id']
    by_resource = {}
    for obj in objects.values():
        if obj.get('resource_id') and obj['kind'] in {'image', 'media'}:
            by_resource.setdefault(obj['resource_id'], []).append(obj)
    for image in doc.select('img[src^="assets/"]'):
        if image.get('data-source-id'):
            continue
        candidates = by_resource.get(image['src'][7:], [])
        # A naked asset URL cannot identify which of mixed source occurrences
        # it means. Only identical presentation evidence may be shared.
        if candidates and all(image_presentation(obj) == image_presentation(candidates[0]) for obj in candidates):
            apply_image_presentation(image, candidates[0])
    sections = {node.get('data-block-id'):node for node in doc.select('[data-block-id]')}
    for item in mapping:
        node = sections.get(item['block_id'])
        if node is not None:
            node['data-source-ids'] = ','.join(item['source_ids'])
            node['data-source-start-line'] = str(item['source_start_line'])
            node['data-source-end-line'] = str(item['source_end_line'])
    signatures=_source_grid_signatures(p)
    representations=_rechecked_source_grids(p,markdown,choices,sections)+[
        r for r in representations or [] if not r.get('source_grid') or
        len(signatures.get((tuple(r.get('source_ids') or []),r.get('block_id'),r.get('table_index',0)),set()))<=1]
    # A saved page-table comparison is local evidence even though page images
    # are reference objects. A known literal difference must also remain in
    # delivery checks; never silently turn a scoped failed comparison into PASS.
    seen_page_grids=set()
    for version in reversed(p['processor'].get('versions',[])):
        if version.get('source_digest')!=p['processor']['source_digest']:continue
        for r in version.get('representations') or []:
            key=(tuple(r.get('source_ids') or []),r.get('block_id'),r.get('table_index',0))
            if key in seen_page_grids or not r.get('source_grid') or len(signatures.get(key,set()))!=1:continue
            seen_page_grids.add(key)
            if not all(objects.get(sid,{}).get('kind')=='page' for sid in r.get('source_ids') or []):continue
            node=sections.get(r.get('block_id'))
            if not node or not set(r['source_ids'])<=set((node.get('data-source-ids') or '').split(',')):continue
            tables=node.select('table');index=r.get('table_index',0)
            if len(tables)<=index:continue
            problem=_table_grid_problem(tables[index],r['source_grid'],r.get('source_columns'))
            if problem:checks.append(dict(severity='error',category='representation',code='TABLE_SOURCE_DIFFERENCE',
                source_id=r['source_ids'][0],block_id=r['block_id'],table_index=index,
                message=problem+'；仅此保存的来源单元格范围，非全文语义核对'))
    for img in doc.find_all('img'):
        src = img.get('src', '')
        if not src.startswith('assets/') or src[7:] not in resources:
            checks.append(dict(severity='error', code='UNRESOLVED_IMAGE', message='图片必须引用本材料的已保存资源：'+src[:120]))
    for row in p['processor']['resources']:
        if (choices.get(row['id']) or ('reference' if row['kind']=='page' else 'body')) != 'body':
            continue
        if row['kind'] in {'image','media','page'} and not row.get('sha256'):
            if row['id'] not in inserted:
                checks.append(dict(severity='error',category='source_availability',
                                   code='RESOURCE_BINARY_UNAVAILABLE',source_id=row['id'],
                                   locator=row['locator'],
                                   message='原文视觉资源未取得；不能仅凭标签断言内容已保留，请补齐原件或明确改为参考用途'))
            continue
        if row['kind'] in {'code','image','table','formula'}:
            if row['id'] in composition_coverage:
                # The complete original page physically presents every saved
                # occurrence. This does not assert Chinese or semantic fidelity.
                continue
            parent=objects.get(row.get('parent_id'))
            if parent and parent['kind']=='table' and parent['id'] in inserted:
                # The literal original table retains its own child occurrence;
                # this is not a file-hash-wide alternative representation.
                source_node=sections.get('resource-'+parent['id'])
                if source_node and row.get('sha256') and source_node.select_one('img[src="assets/'+row['sha256']+'"]'):
                    continue
            alternative = next((a for a in representations or [] if _alternative_covers(
                a,row['id'],markdown,choices,sections)),None)
            if row['id'] in inserted:
                if (row['kind']=='image' and row.get('placement_count',0)>1
                        and not alternative and row['id'] not in grouped_source_ids):
                    checks.append(dict(severity='error',category='representation',
                                       code='PLACEMENT_RELATION_UNVERIFIED',source_id=row['id'],
                                       placement_count=row['placement_count'],
                                       message='同一图像文件在原件中多处承载内容；独立引用不能证明每个位置都已保留，请对照对应正文块'))
                continue
            if alternative:
                automatic=alternative.get('confirmed_by') in {'saved_source_grid_rechecked','native_source_cells_rechecked'}
                scope=alternative.get('evidence_scope','local_block_only_not_whole_article')
                grid=alternative.get('source_grid')
                cell_count=sum(len(r)-(1 if alternative.get('source_columns') else 0) for r in grid) if grid else None
                checks.append(dict(severity='info',category='representation',code='ALTERNATIVE_PRESENTATION',
                                   source_id=row['id'],block_id=alternative['block_id'],
                                   confirmed_by=alternative.get('confirmed_by'),evidence_scope=scope,checked_cell_count=cell_count,
                                   message=('指定正文块已按可靠来源单元格重新核对；仅此范围，非全文语义验证' if automatic else
                                            '指定正文块的替代表达关系由用户对照确认；非程序逐格或全文语义验证')))
            else:
                checks.append(dict(severity='error',category='representation',code='OMITTED_RESOURCE',
                                   source_id=row['id'],locator=row['locator'],
                                   message='资源未独立插入，且没有有效的替代表达关联；对应内容是否缺失尚待核对：'+row['label']))
    # Sanitization must not make a broken external image look like a checked
    # article. Inspect raw tokens too, before unsafe src attributes disappear.
    for token in tokens:
        for child in token.children or []:
            if child.type == 'image':
                src = child.attrGet('src') or ''
                if not src.startswith('assets/') or src[7:] not in resources:
                    checks.append(dict(severity='error', code='UNRESOLVED_IMAGE', message='请用资源标记插入图片：'+src[:120]))
    for token in tokens:
        if token.type in {'fence', 'code_block'}:
            for obj in objects.values():
                if obj['kind'] == 'code' and token.content.rstrip('\n') == obj['text'].rstrip('\n') and obj['id'] in inserted:
                    checks.append(dict(severity='error', code='DUPLICATE_CODE', source_id=obj['id'], message='原代码已用标记插入，无需再复制'))
    for row in resources.values():
        # Checks include actual storage validation in export, not only presence
        # of a resource ID in the JSON index.
        if not re.fullmatch(r'[0-9a-f]{64}', row['sha256']):
            checks.append(dict(severity='error', code='ASSET_ID', message='资源摘要无效'))
    # Advisory only: punctuation does not give the compiler permission to
    # rewrite prose or alter quotations, protected code, formulae or source data.
    for line_no, line in enumerate(lines,1):
        if line_no-1 in protected or line.lstrip().startswith(('>', '{{source:', '|')):
            continue
        if '。' in line:
            checks.append(dict(severity='warning',category='format',code='CHINESE_PERIOD',
                               line=line_no,column=line.index('。')+1,
                               message='作者正文仍有中文句号；可按本次有效格式模板检查，不自动改写'))
    content = str(doc)
    if target == 'readweave':
        content = editor_storage(content)
    effective=[];seen_effective=set()
    for r in representations:
        key=(tuple(r.get('source_ids') or []),r.get('block_id'),r.get('table_index',0))
        if key in seen_effective:continue
        if all(_alternative_covers(r,sid,markdown,choices,sections) for sid in r.get('source_ids') or []):
            effective.append(r);seen_effective.add(key)
    return dict(html=content, checks=checks, source_map=mapping, inserted_source_ids=inserted,effective_representations=effective,
                mechanical_pass=not any(c['severity']=='error' for c in checks),
                semantic_status='not_reviewed', markdown_digest=digest(markdown.encode()))


def confirm_representation(store, pid, vid, body):
    """Bind one explicit local user comparison to one immutable draft and pack."""
    p = project(store,pid)
    version = active_version(p,vid)
    if p['processor']['active_version'] != vid:
        raise Conflict('请先切换到需要核对的成稿版本')
    source_ids = body.get('source_ids')
    method, block_id = str(body.get('method') or ''), str(body.get('block_id') or '')
    if (not isinstance(source_ids,list) or not source_ids or len(source_ids)>100 or
            len(source_ids)!=len(set(source_ids)) or method not in {'table','formula','quote','text'}):
        raise ValueError('替代表达关联需要明确的资源、正文块和表示方式')
    rows = {r['id']:r for r in p['processor']['resources']}
    choices = dict(version.get('resource_usages') or {})
    if any(sid not in rows or (choices.get(sid) or
            ('reference' if rows[sid]['kind']=='page' else 'body'))!='body' for sid in source_ids):
        raise ValueError('只能关联本版本选为正文使用的资源')
    if any(rows[sid]['kind'] in {'image','media','page'} and not rows[sid].get('sha256')
           for sid in source_ids):
        raise Conflict('原文视觉资源尚未取得，不能确认其替代表达')
    repeated = [sid for sid in source_ids if rows[sid]['kind']=='image'
                and rows[sid].get('placement_count', 0)>1]
    if repeated and (method!='table' or not body.get('source_grid') or
                     not body.get('source_columns')):
        raise ValueError('复用图像有多个原文位置；请在目标表格逐行逐列核对，不能只凭资源名称确认覆盖')
    compiled = compile_result(p,version['markdown'],resource_usages=choices,
                              representations=version.get('representations'))
    doc = BeautifulSoup(compiled['html'],'html.parser')
    target = doc.find(attrs={'data-block-id':block_id})
    selectors = {'table':'table','formula':'math, .math-tex','quote':'blockquote','text':'p'}
    if target is None or not target.select_one(selectors[method]):
        raise ValueError('指定正文块没有可核对的目标表示')
    start, end = int(target['data-source-start-line']),int(target['data-source-end-line'])
    source_piece = ''.join(version['markdown'].splitlines(keepends=True)[start-1:end])
    grid = body.get('source_grid')
    columns = body.get('source_columns')
    if grid is not None:
        if method!='table' or not isinstance(grid,list) or not grid or len(grid)>100 or any(
                not isinstance(row,list) or len(row)>100 or
                any(not isinstance(cell,str) or len(cell)>500 for cell in row) for row in grid):
            raise ValueError('来源表格结构无效')
        if columns is not None and (not isinstance(columns,list) or not columns or
                len(columns)>99 or len(set(columns))!=len(columns) or
                any(not isinstance(cell,str) or not cell or len(cell)>100 for cell in columns) or
                len(set(row[0] for row in grid))!=len(grid)):
            raise ValueError('来源表格列与行名无效')
        problem = _table_grid_problem(target.select_one('table'),grid,columns)
        if problem:
            raise Conflict(problem+'；未保存核对结论')
    target_quote = str(body.get('target_quote') or '').strip()
    comparison = str(body.get('comparison') or '').strip()
    if len(target_quote)>500 or len(comparison)>1500:
        raise ValueError('局部核对说明过长')
    if target_quote and target_quote not in target.get_text(' ', strip=True):
        raise Conflict('成稿定位片段不在指定正文块中，请重新定位')
    record = dict(id=identity(), source_ids=source_ids,block_id=block_id,method=method,
                  source_locators=[rows[sid]['locator'] for sid in source_ids],
                  source_grid=grid,source_columns=columns,confirmed_by='user_local_comparison',
                  target_quote=target_quote,comparison=comparison,
                  evidence_scope='local_block_only_not_whole_article',
                  draft_digest=version['digest'],
                  resource_snapshot_digest=digest(choices),
                  target_markdown_digest=digest(source_piece.encode()),created=time.time())
    if not all(_alternative_covers(record,sid,version['markdown'],choices,
                                   {block_id:target}) for sid in source_ids):
        raise Conflict('核对关联没有绑定到当前正文；请重新定位目标块')
    def commit(current):
        live = active_version(current,vid)
        if current['processor']['active_version']!=vid or live['digest']!=version['digest']:
            raise Conflict('成稿版本已变化，先前局部核对没有沿用')
        records = live.setdefault('representations',[])
        records[:] = [r for r in records if not set(r['source_ids']) & set(source_ids)]
        records.append(record)
        result = compile_result(current,live['markdown'],resource_usages=choices,
                                representations=records)
        live['checks']=result['checks'];live['mechanical_pass']=result['mechanical_pass']
        current['processor']['checks']=result['checks']
    return store.change(pid,commit)


def save_result(store, pid, markdown, origin='manual', base_version=None, request_id=None):
    p = project(store, pid)
    if not p.get('inventory'):
        raise Conflict('请先准备原件')
    dispatch = next((r for r in p['processor']['requests'] if r['id']==request_id),None) if request_id else None
    # Old handoff records predate resource selection. They used the defaults,
    # not whatever choices the project may have acquired after dispatch.
    choices = copy.deepcopy(((dispatch or {}).get('resource_usages') or {})
                            if dispatch else (p['processor'].get('resource_usages') or {}))
    if base_version and not dispatch:
        choices=copy.deepcopy(active_version(p,base_version).get('resource_usages') or {})
    from .pdf_resource_groups import prepare_group_resources
    group_resources = prepare_group_resources(store, p, markdown, choices)
    compiled = compile_result(p, markdown, resource_usages=choices, derived_resources=group_resources)
    vid = identity()
    version = dict(id=vid, created=time.time(), origin=origin, request_id=request_id,
                   markdown=markdown, source_digest=p['processor']['source_digest'],
                   pack_digest=dispatch['pack_digest'] if dispatch else task_pack(store,pid)['digest'], digest=compiled['markdown_digest'],
                   resource_usages=choices, template_version=(dispatch or {}).get('template_version') or task_pack(store,pid)['template_version'],
                   template_digest=(dispatch or {}).get('template_digest') or task_pack(store,pid)['template_digest'],
                   checks=compiled['checks'], mechanical_pass=compiled['mechanical_pass'],
                   semantic_status='not_reviewed', source_map=compiled['source_map'], representations=[],
                   derived_resources=group_resources)
    if base_version:
        parent = active_version(p, base_version)
        if parent.get('source_digest') == p['processor']['source_digest']:
            # Figure-composition bindings expire with their member occurrence;
            # rebuild them for this draft instead of inheriting stale geometry.
            version['derived_resources'] = [r for r in copy.deepcopy(parent.get('derived_resources') or [])
                                           if not r.get('pdf_component_group')] + group_resources
            version['scope_exclusions']=copy.deepcopy(parent.get('scope_exclusions') or [])
            # Rebind only untouched target occurrences. A changed cell or
            # changed resource choice expires that relation, not every other
            # independent source comparison in the document.
            compiled = compile_result(p, markdown, resource_usages=choices,
                                      derived_resources=version['derived_resources'],scope_exclusions=version['scope_exclusions'])
            doc=BeautifulSoup(compiled['html'],'html.parser')
            sections={n['data-block-id']:n for n in doc.select('[data-block-id]')}
            lines=markdown.splitlines(keepends=True)
            for previous in parent.get('representations') or []:
                node=sections.get(previous.get('block_id'))
                if not node:continue
                piece=''.join(lines[int(node['data-source-start-line'])-1:int(node['data-source-end-line'])])
                if digest(piece.encode())!=previous.get('target_markdown_digest'):
                    tables=node.select('table');index=previous.get('table_index',0)
                    if not previous.get('target_table_digest') or len(tables)<=index or digest(str(tables[index]).encode())!=previous['target_table_digest']:continue
                bound=copy.deepcopy(previous)
                bound.update(draft_digest=version['digest'],resource_snapshot_digest=digest(choices),target_markdown_digest=digest(piece.encode()))
                if all(_alternative_covers(bound,sid,markdown,choices,sections) for sid in bound['source_ids']):version['representations'].append(bound)
            compiled=compile_result(p,markdown,resource_usages=choices,representations=version['representations'],
                                    derived_resources=version['derived_resources'],scope_exclusions=version['scope_exclusions'])
            version.update(checks=compiled['checks'], mechanical_pass=compiled['mechanical_pass'],
                           source_map=compiled['source_map'])
    def commit(current):
        state = current['processor']
        if request_id and any(v.get('request_id')==request_id for v in state['versions']):
            return
        if base_version is not None and state['active_version'] != base_version:
            raise Conflict('稿件版本已经变化，未覆盖另一份编辑')
        state['versions'].append(version)
        state.update(active_version=vid, checks=compiled['checks'])
        current.update(revision=current['revision']+1, state='candidate')
    return store.change(pid, commit)


def active_version(p, vid=None):
    vid = vid or p['processor']['active_version']
    item = next((v for v in p['processor']['versions'] if v['id']==vid), None)
    if not item:
        raise Conflict('尚无可用成稿')
    return item


def version_view(p, vid=None):
    """Project current mechanical checks without changing a saved candidate."""
    version = copy.deepcopy(active_version(p, vid))
    compiled = compile_result(p, version['markdown'],
                              resource_usages=version.get('resource_usages'),
                              representations=version.get('representations'),
                              derived_resources=version.get('derived_resources') or [],
                              scope_exclusions=version.get('scope_exclusions') or [])
    version.update(checks=compiled['checks'], mechanical_pass=compiled['mechanical_pass'])
    return version


def export_package(store, p):
    p = presentation_project(p, store.read_blob)
    version = active_version(p)
    compiled = compile_result(p, version['markdown'], 'readweave',
                              resource_usages=version.get('resource_usages'),
                              representations=version.get('representations'),
                              derived_resources=version.get('derived_resources') or [],
                              scope_exclusions=version.get('scope_exclusions') or [])
    if not compiled['mechanical_pass']:
        raise Conflict('文件与资源检查有未解决问题，请按检查提示修正或定位资源后再导出')
    files, attachments, names = {}, [], {}
    def attach(raw, title, mime, role, key):
        eid = digest([key,title,mime,role])
        name = 'asset-'+eid+(mimetypes.guess_extension(mime) or '.bin')
        if name not in files:
            files[name] = raw
            attachments.append(dict(attachmentId='att'+eid[:24], title=title, role=role,
                                    mime=mime, dataFileName=name, position=len(attachments)))
        return name
    unique = {}
    for resource in p['inventory']['resources']+(version.get('derived_resources') or []):
        unique.setdefault(resource['id'], resource)
    for key, row in unique.items():
        raw = store.read_blob(row['sha256'])
        if digest(raw) != row['sha256']:
            raise Conflict('资源保存字节与摘要不一致')
        names[key] = attach(raw, row['name'], row['mime'], 'image' if row['mime'].startswith('image/') else 'file', key)
    for original in p['inventory']['originals']:
        raw = store.read_blob(original['sha256'])
        if digest(raw) != original['sha256']:
            raise Conflict('原件保存字节与摘要不一致')
        attach(raw, original['name'], mimetypes.guess_type(original['name'])[0] or 'application/octet-stream', 'file', original['sha256'])
    doc = BeautifulSoup(compiled['html'], 'html.parser')
    # Some native PDF links spell an absolute web address as http:www.host.
    # The native importer treats that spelling as an attachment, so make the
    # explicit web protocol hierarchical without changing the saved source.
    for link in doc.select('a[href]'):
        link['href'] = normalize_native_web_target(link['href'])
    # CKEditor preserves block anchor attributes on paragraphs/headings, but
    # strips them from <pre> during a real editor save. Keep a code-only block's
    # identity on the existing native paragraph path; code bytes stay untouched.
    for code in doc.select('pre[data-readweave-anchor-id]'):
        paragraph = doc.new_tag('p', attrs={'data-readweave-anchor-id': code['data-readweave-anchor-id']})
        del code['data-readweave-anchor-id']
        code.insert_before(paragraph)
    for img in doc.select('img[src^="assets/"]'):
        img['src'] = names[img['src'][7:]]
    for link in doc.select('a[href^="assets/"]'):
        key = link['href'][7:]
        if key not in names:
            raise Conflict('原页回查资源文件缺失')
        link['href'] = names[key]
    # Original attachments and exact page/section mapping travel with the note.
    attach(json.dumps(dict(schema='sourceloom-source-map/1', source_digest=p['processor']['source_digest'],
                           source_url=p['inventory'].get('source_url'), originals=p['inventory']['originals'],
                           locations=p['processor']['source_map'], blocks=compiled['source_map'],
                           resource_usages=version.get('resource_usages',{}),
                           representations=compiled['effective_representations'],scope_exclusions=version.get('scope_exclusions',[])),
                      ensure_ascii=False, indent=2).encode(), 'source-map.json', 'application/json', 'file', version['digest'])
    attach(version['markdown'].encode(), 'article.md', 'text/markdown', 'file', version['digest'])
    attach(json.dumps(dict(version_id=version['id'], origin=version['origin'],
                           mechanical_checks=compiled['checks'], semantic_status='not_reviewed',
                           source_digest=p['processor']['source_digest'], draft_digest=version['digest'],
                           pack_digest=version['pack_digest'],template_version=version.get('template_version'),
                           template_digest=version.get('template_digest')),
                      ensure_ascii=False, indent=2).encode(), 'sourceloom-receipt.json', 'application/json', 'file', version['digest'])
    files['material.html'] = editor_storage(str(doc)).encode()
    artifact = digest([digest(files['material.html']), sorted((n,digest(b)) for n,b in files.items())])
    key = p['id']+':'+str(p['revision'])+':'+artifact
    meta = dict(formatVersion=2, appVersion='1.0.0', files=[dict(noteId='loom'+p['id'][:12],
        title=p['title'], type='text', mime='text/html', format='html', dataFileName='material.html',
        attachments=attachments, children=[], attributes=[dict(type='label',name='sourceloomCandidate',
            value=key,isInheritable=False),dict(type='label',name='sourceloomSemanticStatus',value='not_reviewed',isInheritable=False)])])
    buf = BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('!!!meta.json', json.dumps(meta,ensure_ascii=False))
        for name, raw in files.items():
            safe_member(name)
            z.writestr(name, raw)
    return buf.getvalue()
