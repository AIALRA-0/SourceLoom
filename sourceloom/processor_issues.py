"""Explicit, version-bound local issue choices. Never a generation or review loop."""
import copy
import html
import base64
import re
import time
from collections import OrderedDict
from io import BytesIO

from bs4 import BeautifulSoup

from . import processor
from .store import Conflict, digest, identity

_OLD_PDF_GEOMETRY = OrderedDict()


def _restore_bytes(p,row,store,encoded=None):
    """Recover exactly the registered bytes, never substitute a similar image."""
    if encoded is not None:
        if not isinstance(encoded,str) or len(encoded)>24*1024*1024:raise ValueError('原图文件超过本次恢复上限')
        try:raw=base64.b64decode(encoded,validate=True)
        except (ValueError,TypeError):raise ValueError('原图文件编码无效')
        if digest(raw)!=row.get('sha256'):raise Conflict('文件与此原图摘要不同，未替换任何原件；请选择原始同一文件')
        return raw
    original=next((o for o in p['inventory']['originals'] if o['name']==_document(row) and o['name'].lower().endswith('.pdf')),None)
    if original and _page(row) and store:
        try:
            from pypdf import PdfReader
            page=PdfReader(BytesIO(store.read_blob(original['sha256']))).pages[_page(row)-1]
            for image in page.images:
                if digest(image.data)==row.get('sha256'):return image.data
        except (OSError,ValueError,KeyError,IndexError,RuntimeError):pass
    return None


def _resources(p, store=None):
    """Compile missing display geometry from immutable PDF bytes, read-only.

    Older saved projects predate placement metadata. Reusing the PDF's native
    image references repairs the viewer index, not the source inventory or a
    table provenance conclusion. Exact saved image bytes must match first.
    """
    rows={r['id']:copy.deepcopy(r) for r in p['processor']['resources']}
    if store is None:
        return rows
    originals={o['name']:o for o in p['inventory']['originals'] if o['name'].lower().endswith('.pdf')}
    for row in rows.values():
        if row['kind']!='image' or row.get('placements') or _document(row) not in originals or not _page(row):
            continue
        original=originals[_document(row)]
        key=(original['sha256'],_page(row))
        if key not in _OLD_PDF_GEOMETRY:
            try:
                from pypdf import PdfReader
                raw=store.read_blob(original['sha256'])
                page=PdfReader(BytesIO(raw)).pages[_page(row)-1]
                found={}
                for image in page.images:
                    places=processor.pdf_image_placements(raw,_page(row)-1,image)
                    if places:
                        found[digest(image.data)]=places
                _OLD_PDF_GEOMETRY[key]=found
                while len(_OLD_PDF_GEOMETRY)>64:
                    _OLD_PDF_GEOMETRY.popitem(last=False)
            except (ImportError,ValueError,RuntimeError,IndexError,KeyError,OSError):
                _OLD_PDF_GEOMETRY[key]={}
        places=_OLD_PDF_GEOMETRY.get(key,{}).get(row.get('sha256'))
        if places:
            row.update(placements=places,placement_count=len(places))
    return rows


def _compiled(p, v):
    return processor.compile_result(p, v['markdown'], resource_usages=v.get('resource_usages'),
                                    representations=v.get('representations'),
                                    derived_resources=v.get('derived_resources'),scope_exclusions=v.get('scope_exclusions') or [])


def _page(row):
    places = row.get('placements') or []
    match = re.search(r'page\[(\d+)\]', row.get('locator', ''))
    return places[0]['page'] if places else int(match[1]) if match else None


def _document(row):
    return re.split(r'/(?:page|node)\[',row.get('locator',''),maxsplit=1)[0]


def _baseline(p, ids, document=None, page=None,block_id=None,table_index=None):
    # Prior explicit local comparisons are reusable source mappings, never a
    # current-version PASS. Every cell is compared afresh below.
    candidates=[]
    for v in reversed(p['processor']['versions']):
        if v.get('source_digest') != p['processor']['source_digest']:
            continue
        for record in v.get('representations') or []:
            if set(record.get('source_ids', [])) == set(ids) and record.get('source_grid'):
                if block_id is not None and (record.get('block_id')!=block_id or record.get('table_index',0)!=table_index):continue
                occurrence=record.get('source_occurrence') or {}
                if occurrence and (occurrence.get('document')!=document or occurrence.get('page')!=page):
                    continue
                mapping=next((b for b in v.get('source_map',[]) if b['block_id']==record.get('block_id')),None)
                if page and mapping and _page(mapping) and (_page(mapping)!=page or _document(mapping)!=document):
                    continue
                candidates.append((record,v))
    if len({digest([r['source_grid'],r.get('source_columns')]) for r,v in candidates})>1:
        return None,dict(source_grid_conflict=True)
    return candidates[0] if candidates else (None,None)


def _table_node(doc, compiled, page, baseline, document=None):
    if baseline:
        node = doc.find(attrs={'data-block-id': baseline['block_id']})
        if node and len(node.select('table'))==1:
            return node
    mapped=[]
    for entry in compiled['source_map']:
        if page and f'page[{page}]' in entry.get('locator', '') and (not document or _document(entry)==document):
            node = doc.find(attrs={'data-block-id': entry['block_id']})
            if node and len(node.select('table'))==1:
                mapped.append(node)
    if mapped:return mapped[0] if len(mapped)==1 else None
    # A globally unique table is not the table of every page. Once this
    # candidate has page-level provenance, unmatched page scopes must remain
    # unmatched instead of taking another page's authored table responsibility.
    if page and document and any(_page(entry) for entry in compiled['source_map']):
        return None
    tables = [n for n in doc.select('section[data-block-id]:has(table)') if len(n.select('table'))==1]
    if len(tables) == 1:
        return tables[0]
    return None


def _differences(table, record):
    if not record:
        return []
    actual = [[c.get_text(' ', strip=True) for c in r.find_all(['th', 'td'])]
              for r in table.find_all('tr')] if table else []
    columns = record.get('source_columns') or []
    differences = []
    if record.get('native_full_grid'):
        for row_index,wanted in enumerate(record['source_grid']):
            found=actual[row_index] if row_index<len(actual) else []
            for col,value in enumerate(wanted):
                current=found[col] if col<len(found) else '缺少'
                if current!=value:
                    differences.append(dict(row=wanted[0],row_index=row_index,current_row_index=row_index if found else None,
                                            current_row_label=found[0] if found else None,column=record['source_grid'][0][col],
                                            column_index=col,current=current,expected=value,missing_row=not found))
        if len(actual)>len(record['source_grid']):
            differences.append(dict(row='额外行',column='整行',current='存在多余行',expected='需对照原件',missing_row=True))
        return differences
    for index, name in enumerate(columns, 1):
        found = actual[0][index] if actual and index < len(actual[0]) else '缺少'
        if name.casefold() not in found.casefold():
            differences.append(dict(row='表头',column=name,current=found,expected=name,missing_row=True))
    for wanted in record['source_grid']:
        rows = [r for r in actual[1:] if r and r[0] == wanted[0]]
        if len(rows) != 1:
            differences.append(dict(row=wanted[0], column='整行', current='缺少或重复',
                                    expected=' | '.join(wanted), missing_row=True,**({'ambiguous':True} if len(rows)>1 else {})))
            continue
        for index, value in enumerate(wanted[1:], 1):
            found = rows[0][index] if index < len(rows[0]) else '缺少'
            if found != value:
                differences.append(dict(row=wanted[0], column=columns[index-1] if index <= len(columns) else str(index),
                                        column_index=index, current=found, expected=value,current_row_index=actual.index(rows[0]),current_row_label=rows[0][0]))
    return differences


def _format_changes(markdown):
    """One deterministic style rule, reporting its actual safe occurrences."""
    protected=set()
    for token in processor.markdown_renderer().parse(markdown):
        if token.map and token.type in {'fence','code_block','blockquote_open','table_open','math_block','html_block'}:
            protected.update(range(*token.map))
    # Preserve multiline display math even when a renderer token has no map.
    math=False
    lines=markdown.splitlines(keepends=True);hits=[]
    for index,line in enumerate(lines):
        if line.strip() in {'$$',r'\[',r'\]'}:
            protected.add(index);math=not math
        if math:protected.add(index)
        if index in protected or line.lstrip().startswith(('>','|','{{','$','#')):
            continue
        parts=re.split(r'(`+[^`]*`+|\$[^$]*\$|\\\([^)]*\\\)|<[^>]+>|!?\[[^\]]*\]\([^)]*\)|https?://\S+|[“「\"][^”」\"]*[”」\"]|‘[^’]*’|\'[^\']*\')',line)
        after=''.join(part if i%2 else part.replace('。','.') for i,part in enumerate(parts))
        if after!=line:
            hits.append(dict(line=index+1,before=line.rstrip('\r\n'),after=after.rstrip('\r\n'),
                             count=sum(part.count('。') for i,part in enumerate(parts) if not i%2)))
            lines[index]=after
    return ''.join(lines),hits


def _projection(group,category,recommended,impact):
    labels={'repair_table':'预览修正','confirm_manual':'对照这张表','confirm_table':'查看核对结果',
            'add_page_reference':f'添加第 {group.get("page")} 页原页参考',
            'replace_page_reference':f'用第 {group.get("page")} 页完整原页替换正文表格',
            'table_image':'用完整原页替换正文表格','insert_resource':'预览补入原图',
            'source_only':'本次仅作回查','exclude_scope':'明确排除本次正文范围',
            'retain_difference':'保留当前差异（不标记为核对一致）','format':'预览格式整理'}
    labels['restore_resource']='恢复原图文件'
    group.update(category=category,recommended_action=recommended,impact=impact,
                 affects_scope='local_content_only',known_facts=group.get('differences') or [],
                 action_labels={a:labels.get(a,a) for a in group['actions']})
    return group


def _previous_resource_position(p, v, sid):
    """Recover only an exact, unique saved slot; a page is not a caption slot."""
    marker = '{{source:'+sid+'}}'
    if marker in v['markdown']:
        return None
    for prior in reversed(p['processor']['versions']):
        text = prior['markdown']
        if prior.get('source_digest') != p['processor']['source_digest'] or text.count(marker) != 1:
            continue
        compiled = _compiled(p, prior)
        if not any(e.get('mapping') == 'explicit_resource' and sid in e.get('source_ids', [])
                   for e in compiled['source_map']):
            continue
        match = re.search(r'(?m)^[ \t]*'+re.escape(marker)+r'[ \t]*(?:\r?\n|$)', text)
        if not match:
            continue
        # Both removals are literal byte-preserving edits made by the editor.
        # Never normalize neighboring prose or find a similar-looking caption.
        candidates = [(match.start(), match.end()), (text.index(marker), text.index(marker)+len(marker))]
        for start, end in candidates:
            if text[:start]+text[end:] == v['markdown']:
                left = text.rfind('\n\n', 0, max(0, start-1))+2
                right = text.find('\n\n', end+1)
                if right < 0: right = len(text)
                return dict(markdown=text, before=(text[:start]+text[end:])[left:right-(end-start)],
                            after=text[left:right])
    return None


def issues(p, vid=None, store=None):
    v = processor.active_version(p, vid)
    compiled = _compiled(p, v)
    doc = BeautifulSoup(compiled['html'], 'html.parser')
    resources = _resources(p,store)
    actionable = [c for c in compiled['checks'] if c['severity'] == 'error']
    groups, resolved, used = [], [], set()
    repeated = {}
    for check in actionable+[c for c in compiled['checks'] if c.get('code')=='ALTERNATIVE_PRESENTATION']:
        row = resources.get(check.get('source_id'))
        if row and row.get('placement_count', 0) > 1:
            repeated.setdefault((_document(row),_page(row)), []).append(row['id'])
    # A valid saved representation is shown as resolved, with an undo/version
    # affordance; it is not silently turned into a fresh warning.
    for (document,page), ids in repeated.items():
        record, prior = _baseline(p, ids,document,page)
        node = _table_node(doc, compiled, page, record,document)
        places=[b for sid in ids for b in resources[sid].get('placements',[]) if b.get('page')==page]
        row_positions={round((b['bbox'][1]+b['bbox'][3])/2/4) for b in places}
        column_positions={round((b['bbox'][0]+b['bbox'][2])/2/4) for b in places}
        native_parent=any(resources[sid].get('parent_id') and next((r for r in resources.values()
                          if r['id']==resources[sid]['parent_id'] and r['kind']=='table'),None) for sid in ids)
        if not record and not native_parent and not (len(row_positions)>=3 and len(column_positions)>=2 and node):
            continue
        differences = _differences(node.select_one('table') if node else None, record)
        manual=next((r for r in v.get('representations') or [] if not r.get('source_grid') and
                     set(r.get('source_ids') or [])==set(ids) and r.get('method')=='table' and node and
                     r.get('block_id')==node.get('data-block-id') and all(processor._alternative_covers(
                     r,sid,v['markdown'],v.get('resource_usages') or {},{r['block_id']:node}) for sid in ids)),None)
        page_text='\n'.join(o.get('text','') for o in p['inventory']['objects'] if o['kind']=='page' and _page(o)==page and _document(o)==document)
        table_label=re.search(r'(?:Table|表)\s*(\d+)\s*[:：]',page_text,re.I)
        title=('表 '+table_label[1] if table_label else '表格')+' 的复用符号已改为正文表格，表示关系待确认'
        if node and {'✓','✗'}<=set(node.get_text()):
            title=title.replace('复用符号','勾／叉')
        group = dict(id='table-'+digest(sorted(ids))[:16], kind='table',
                     title=title, page=page,
                     source_document=document,
                     source_preview_precision='page',
                     source_ids=ids, block_id=node.get('data-block-id') if node else None,
                     draft_preview=str(node.select_one('table')) if node else '<p>对应正文表格缺失</p>',
                     reliable_mapping=bool(record), differences=differences,
                     explanation=('逐格核对本版本后保存表示关系；不会把两个孤立符号插入正文。' if record else
                                  '对照原表与正文后确认本版本的表示关系；明确记录为人工确认，不声称自动逐格证明。'),
                     evidence_scope=(f'仅核对 {sum(len(row)-1 for row in record["source_grid"])} 个已保存来源单元格；不代表全表或全文语义已验证'
                                     if record else '用户对照当前正文与原页，不是程序逐格证明'),
                     actions=['repair_table' if differences and record else 'confirm_manual']+
                             (['add_page_reference','replace_page_reference'] if page else [])+
                             (['retain_difference'] if differences else [])+['exclude_scope'])
        if differences and record:
            group['title']=('表 '+table_label[1] if table_label else '表格')+f'有 {len(differences)} 处与已核对原件不同'
        if prior and prior.get('source_grid_conflict'):
            group['explanation']='保存的来源网格核对结论存在矛盾；不能自动选择一方，请对照真实原件确认，不会声称机器逐格通过'
            group['source_grid_conflict']=True
        ambiguous=any(d.get('ambiguous') for d in differences)
        if ambiguous:
            group['actions']=[a for a in group['actions'] if a!='repair_table']
            group['explanation']='成稿存在同名重复行，不能确定唯一修改目标；请对照原件编辑，不自动广播修正'
        if (record or manual) and not differences and node:
            group.update(title='正文表格的已保存来源单元格已核对一致' if record else '本版本人工对照确认已记录',status='checked',actions=[])
            if manual:group.update(evidence_scope='本版本用户对照确认；正文未改，不是程序逐格或全文语义验证',confirmed_by='user_local_comparison')
            _projection(group,'checked',None,'正文未改；检查不会创建新的成稿版本')
            resolved.append(group)
        else:
            _projection(group,'repair' if differences and record else 'confirm',
                        None if ambiguous else 'repair_table' if differences and record else 'confirm_manual',
                        '只修正所列差异，其他内容不变' if differences and record else '记录用户对照结论，正文不改')
            groups.append(group)
        used.update(ids)
    # Native rectangular grids are actual parsed source content. Translation
    # or merged/ambiguous headers require a user comparison; never invent a
    # numeric truth from prose or match two same-named tables by their title.
    for record in compiled.get('effective_representations') or []:
        if record.get('confirmed_by') != 'native_source_cells_rechecked':continue
        sid=record['source_ids'][0]
        if sid in used:continue
        source=next(o for o in p['inventory']['objects'] if o['id']==sid)
        node=doc.find(attrs={'data-block-id':record['block_id']})
        index=record.get('table_index',0)
        if not node or len(node.select('table'))<=index:continue
        group=dict(id='table-native-'+sid,kind='table',title='原生表格文字与合并结构已核对一致',
                   source_ids=[sid],page=_page(source),source_document=_document(source),
                   block_id=record['block_id'],table_index=index,status='checked',
                   draft_preview=str(node.select('table')[index]),source_preview_html=processor.safe_html(source.get('raw','')),
                   reliable_mapping=True,differences=[],actions=[],
                   evidence_scope='仅核对原生文字单元格与合并结构，不代表图片、脚注关联或全文语义已验证')
        resolved.append(_projection(group,'checked',None,'只读核对，正文与版本未改'))
        used.add(sid)
    native_header_counts={}
    for source in p['inventory']['objects']:
        if source.get('cells'):native_header_counts[digest([c['text'].strip() for c in source['cells'][0]])]=native_header_counts.get(digest([c['text'].strip() for c in source['cells'][0]]),0)+1
    for source in p['inventory']['objects']:
        if source['kind']!='table' or source['id'] in used or not source.get('cells') or '<img' in source.get('raw','').lower():continue
        cells=source['cells']
        if not cells or any(str(c.get('rowspan','1'))!='1' or str(c.get('colspan','1'))!='1' for row in cells for c in row):continue
        source_grid=[[c['text'].strip() for c in row] for row in cells]
        if not source_grid or len({len(r) for r in source_grid})!=1 or not all(source_grid[0]):continue
        candidates=[n for n in doc.select('section[data-block-id]') if len(n.select('table'))==1 and
                    [[c.get_text(' ',strip=True) for c in r.find_all(['th','td'])] for r in n.select_one('table').find_all('tr')][:1]==source_grid[:1]]
        if len(candidates)!=1:continue
        node=candidates[0];sid=source['id']
        if native_header_counts.get(digest(source_grid[0]),0)>1 and sid not in (node.get('data-source-ids') or '').split(','):continue
        record=dict(source_grid=source_grid,source_columns=None,native_full_grid=True,block_id=node['data-block-id'],
                    source_ids=[sid],method='table',source_occurrence=dict(document=_document(source),page=_page(source),source_id=sid))
        differences=_differences(node.select_one('table'),record)
        group=dict(id='table-native-'+sid,kind='table',title=f'原生表格有 {len(differences)} 处差异' if differences else '原生表格单元格已核对一致',
                   source_ids=[sid],page=_page(source),source_document=_document(source),block_id=node['data-block-id'],
                   draft_preview=str(node.select_one('table')),source_preview_html=processor.safe_html(source.get('raw','')),
                   reliable_mapping=True,differences=differences,native_source_record=record,
                   evidence_scope=f'仅核对 {sum(map(len,source_grid))} 个原生文字单元格；合并单元格、图片与语义不在此范围',
                   actions=['repair_table','retain_difference','exclude_scope'] if differences else [])
        if differences:groups.append(_projection(group,'repair','repair_table','只修正所列原生单元格差异'))
        else:
            group['status']='checked';resolved.append(_projection(group,'checked',None,'只读核对，正文未改'))
        used.add(sid)
    # PDF prose tables have no independent table object or parsed cell truth.
    # Their existing page context supports a local human comparison and a
    # complete-page reference, without inventing a source grid or obligation.
    for entry in compiled['source_map']:
        if entry.get('mapping')!='page_context':continue
        node=doc.find(attrs={'data-block-id':entry['block_id']})
        if not node:continue
        pages=[resources[sid] for sid in entry.get('source_ids') or [] if sid in resources and resources[sid]['kind']=='page']
        if len(pages)!=1:continue
        page_row=pages[0];sid=page_row['id'];page=_page(page_row);document=_document(page_row)
        if not any(o['name']==document and o['name'].lower().endswith('.pdf') for o in p['inventory']['originals']):continue
        for index,table in enumerate(node.select('table')):
            if any(g.get('block_id')==entry['block_id'] and (g.get('table_index') in {None,index}) for g in groups+resolved if g['kind']=='table'):continue
            record,prior=_baseline(p,[sid],document,page,entry['block_id'],index)
            differences=_differences(table,record)
            current=next((r for r in v.get('representations') or [] if r.get('source_ids')==[sid] and
                         r.get('block_id')==entry['block_id'] and r.get('table_index',0)==index and all(
                         processor._alternative_covers(r,s,v['markdown'],v.get('resource_usages') or {},{entry['block_id']:node}) for s in [sid])),None)
            checked=bool((record or current) and not differences)
            group=dict(id='table-page-'+digest([document,page,entry['block_id'],index])[:16],kind='table',
                       title=f'第 {page} 页对应的正文表格'+('有来源差异' if differences else '用户对照确认已记录' if current else '尚待对照确认'),
                       source_ids=[sid],page=page,source_document=document,source_kind='page',source_locator=page_row['locator'],
                       block_id=entry['block_id'],table_index=index,draft_preview=str(table),source_preview_precision='page',
                       reliable_mapping=bool(record),differences=differences,
                       evidence_scope=('仅核对保存的来源单元格，不扩大为整张跨页表或全文语义证明' if record else
                                       f'本正文表格实例与第 {page} 页的用户对照关系；无原生网格，不声称自动逐格核验'),
                       explanation='原件没有独立表格资源；利用现有原页定位逐段对照，未自动识别表格或OCR',
                       actions=[] if checked else ['repair_table' if record and differences else 'confirm_manual',
                                                  'add_page_reference','replace_page_reference','exclude_scope'])
            if record and differences:group['source_record']=record
            if checked:
                group.update(status='checked',confirmed_by='saved_source_grid_rechecked' if record else 'user_local_comparison')
                resolved.append(_projection(group,'checked',None,'正文未改；仅此原页/正文表格实例'))
            else:groups.append(_projection(group,'repair' if differences else 'confirm',
                              'repair_table' if differences else 'confirm_manual','只影响此正文表格实例，保留原件和其他表段'))
            if any(d.get('ambiguous') for d in differences):
                group.update(actions=[a for a in group['actions'] if a!='repair_table'],recommended_action=None,
                             explanation='成稿存在同名重复行，无法唯一绑定单元格；请对照原页编辑，不自动广播修正')
    for check in actionable:
        if check.get('code')=='TABLE_SOURCE_DIFFERENCE' and any(g.get('block_id')==check.get('block_id') and g.get('table_index',0)==check.get('table_index',0) for g in groups+resolved):continue
        sid = check.get('source_id')
        if sid in used:
            continue
        row = resources.get(sid)
        if row:
            if check.get('code') in {'PLACEMENT_RELATION_UNVERIFIED','DUPLICATE_RESOURCE','RESOURCE_NOT_AUTHORIZED'}:
                occurrences=[entry for entry in compiled['source_map']
                             if sid in entry.get('source_ids',[]) and entry.get('mapping')=='explicit_resource']
                node=doc.find(attrs={'data-block-id':occurrences[0]['block_id']}) if occurrences else None
                group=dict(id='resource-'+sid,kind='resource',
                           title=('原图已呈现，复用位置仍待对照 · ' if check['code']=='PLACEMENT_RELATION_UNVERIFIED' else '当前资源用法待对照 · ')+row['label'],
                           source_ids=[sid],page=_page(row),source_document=_document(row),
                           block_id=occurrences[0]['block_id'] if occurrences else None,
                           draft_preview=str(node) if node else '',
                           source_preview_url=f'/api/processor/projects/{p["id"]}/files/{row["sha256"]}' if row.get('sha256') and row['kind'] in {'image','page'} else None,
                           explanation='当前资源已经在成稿中；请对照原件中的具体出现位置，必要时编辑。缺少位置证据不等于缺少图片，不会再次插入或声称完整核对。',
                           evidence_scope='复用位置或当前用途尚未完整核验，未作机器或人工通过声明',
                           actions=[])
                groups.append(_projection(group,'confirm',None,'正文与原件不改；位置证据不足保持待确认'))
                used.add(sid)
                continue
            obj=next((o for o in p['inventory']['objects'] if o['id']==sid),{})
            source_preview_html=processor.safe_html(processor.object_html(obj)) if obj.get('kind') in {'table','code','formula'} else None
            unavailable=row['kind'] in {'image','page','media'} and not row.get('sha256')
            if store and row.get('sha256'):
                try:unavailable=digest(store.read_blob(row['sha256']))!=row['sha256']
                except (OSError,KeyError,ValueError):unavailable=True
            optional=processor.resource_usage(p['processor'],row)!='body' or row.get('source_scope') in {'layout_decorative','site_chrome'}
            choices = [] if unavailable else ['insert_resource']+(['source_only'] if optional else ['exclude_scope'])
            group=dict(id='resource-'+sid, kind='resource', title=('原图暂不可用 · ' if unavailable else '正文缺少原件内容 · ')+row['label'],
                               source_ids=[sid], page=_page(row), block_id=None, draft_preview='',
                               source_document=_document(row),
                               source_kind=row['kind'],source_locator=row['locator'],source_preview_html=source_preview_html,
                               previous_position_available=bool(_previous_resource_position(p,v,sid)) if not unavailable else False,
                               source_preview_url=f'/api/processor/projects/{p["id"]}/files/{row["sha256"]}' if row.get('sha256') and row['kind'] in {'image','page'} else None,
                               explanation=('资源文件字节不可用；插入标记不能恢复原图，请查看原页或恢复同一来源文件' if unavailable else
                                            '先预览原件与推荐位置，再插入；范围排除不表示内容已经修复'), actions=choices)
            _projection(group,'service' if unavailable else 'repair',None if unavailable else 'insert_resource',
                        '正文与原件保持；文件恢复前不能声明资源已交付' if unavailable else '只在所选位置插入一次原资源')
            groups.append(group)
        else:
            groups.append(dict(id='check-'+digest(check)[:12], kind='other', title=check['message'],
                               source_ids=[], page=None, block_id=None, draft_preview='',
                               explanation='请查看原件和当前候选；系统不会自动猜测内容。',actions=[],category='repair',recommended_action=None))
    if store:
        for row in resources.values():
            if not row.get('sha256') or row['kind'] not in {'image','page','media'}:continue
            try:available=digest(store.read_blob(row['sha256']))==row['sha256']
            except (OSError,KeyError,ValueError):available=False
            if available or any(g.get('category')=='service' and row['id'] in g['source_ids'] for g in groups):continue
            groups.append(_projection(dict(id='unavailable-'+row['id'],kind='resource',title='原图暂不可用 · '+row['label'],
                        source_ids=[row['id']],page=_page(row),source_document=_document(row),block_id=None,draft_preview='',
                        explanation='保存的资源文件字节不可用；正文标记仍保留，请恢复同一来源文件或参阅原页',actions=[]),
                        'service',None,'保留当前原件与正文；插入标记不能修复文件缺失'))
        for group in groups:
            if group.get('category')!='service' or len(group['source_ids'])!=1:continue
            row=resources[group['source_ids'][0]]
            if not row.get('sha256'):continue
            local=bool(_restore_bytes(p,row,store))
            group.update(actions=['restore_resource'],recommended_action='restore_resource',
                         expected_resource_sha=row['sha256'],local_recovery_available=local,
                         restore_requires_file=not local,action_labels={'restore_resource':'从当前原件恢复原图' if local else '选择原图文件恢复'},
                         impact='只恢复与登记摘要完全相同的文件字节，正文与原件身份不变；不同文件不会被替换')
    formatted,hits=_format_changes(v['markdown'])
    format_count=sum(h['count'] for h in hits)
    if hits:
        group=dict(id='format', kind='format', title=f'普通正文句号 · {format_count} 处可选整理',
                           source_ids=[], page=None, block_id=None, draft_preview='',
                           occurrences=hits,occurrence_count=format_count,examples=hits[:3],
                           explanation='只将列出的普通正文句号整理为本次格式；代码、公式、表格、链接与引用不改。',actions=['format'])
        groups.append(_projection(group,'format','format','仅修改预览所列标点，不修改事实、数字或段落结构'))
    counts={key:sum(g.get('category')==key for g in groups) for key in ('repair','confirm','service')}
    counts['format_occurrences']=format_count
    summary=' · '.join(label for label in (
        f'需修复 {counts["repair"]}' if counts['repair'] else '',
        f'待确认 {counts["confirm"]}' if counts['confirm'] else '',
        f'服务异常 {counts["service"]}' if counts['service'] else '',
        f'格式建议 {format_count} 处' if format_count else '') if label) or '当前文件与资源检查范围未发现待处理问题'
    blocks=[]
    for entry in compiled['source_map']:
        if entry['mapping']=='explicit_resource':
            continue
        node=doc.find(attrs={'data-block-id':entry['block_id']})
        if node:
            blocks.append(dict(block_id=entry['block_id'],label=node.get_text(' ',strip=True)[:100],locator=entry.get('locator','')))
    last=p['processor'].get('last_issue_result') or {}
    if last.get('version_id')!=v['id'] or last.get('draft_digest')!=v['digest']:last=None
    return dict(project_id=p['id'], version_id=v['id'], draft_digest=v['digest'], blocks=blocks,last_issue_result=last,
                source_digest=p['processor']['source_digest'], summary=summary, counts=counts,issues=groups,resolved=resolved,
                semantic_status='not_reviewed')


def _block_piece(v, compiled, block_id):
    entry = next((b for b in compiled['source_map'] if b['block_id'] == block_id), None)
    if not entry:
        raise Conflict('对应正文位置已变化，请重新预览')
    start, end = entry['source_start_line']-1, entry['source_end_line']
    lines = v['markdown'].splitlines(keepends=True)
    return start, end, ''.join(lines[start:end])


def _table_span(piece,table_index=0):
    lines = piece.splitlines(keepends=True)
    ordinal=0
    for index, line in enumerate(lines):
        if line.lstrip().startswith('|') and index+1 < len(lines) and re.match(r'^\s*\|?[\s:|\-]+\|\s*$', lines[index+1]):
            end = index+2
            while end < len(lines) and lines[end].lstrip().startswith('|'):
                end += 1
            if ordinal==table_index:return lines,index,end
            ordinal+=1
    raise Conflict('没有可靠的 Markdown 表格位置，请重新查看对应正文')


def _preview(p, vid, body, store=None):
    v = processor.active_version(p, vid)
    if p['processor']['active_version'] != vid:
        raise Conflict('版本已切换，请重新打开问题')
    listing = issues(p, vid,store)
    group = next((g for g in listing['issues']+listing['resolved'] if g['id'] == body.get('issue_id')), None)
    action=body.get('action')
    if action=='table_image':action='replace_page_reference'
    compatible_check=group and group.get('status')=='checked' and action=='confirm_table'
    if not group or (action not in group['actions'] and not compatible_check):
        raise Conflict('问题或处理选项已变化，请重新检查')
    if action in {'exclude_scope','retain_difference'} and not body.get('acknowledged'):
        raise Conflict('请明确确认保留差异或排除范围；这不表示内容已修复或完整忠实核对通过')
    if action == 'confirm_manual' and not body.get('acknowledged'):
        raise Conflict('请对照原表与正文后明确确认；此操作记录为用户确认，不是自动逐格核对')
    compiled = _compiled(p, v)
    markdown, choices = v['markdown'], copy.deepcopy(v.get('resource_usages') or {})
    representation, derived = None, []
    before = after = ''
    target_id = group.get('block_id') or body.get('block_id')
    if action=='restore_resource':
        row=_resources(p,store)[group['source_ids'][0]]
        recovered=_restore_bytes(p,row,store,body.get('resource_bytes'))
        if recovered is None:raise Conflict('无法从当前冻结原件恢复此文件；请选择与登记摘要相同的原图文件')
        before=after=markdown
    elif action in {'source_only','exclude_scope'}:
        for sid in group['source_ids']:
            choices[sid] = 'reference'
        before = markdown
        protected = set()
        for token in processor.markdown_renderer().parse(markdown):
            if token.map and token.type in {'fence', 'code_block'}:
                protected.update(range(*token.map))
        removed = {'{{source:'+sid+'}}' for sid in group['source_ids']}
        markdown = ''.join(line for index,line in enumerate(markdown.splitlines(keepends=True))
                           if index in protected or line.strip() not in removed)
        after = markdown
    elif action=='retain_difference':
        before=after=markdown
    elif action in {'confirm_table', 'confirm_manual', 'repair_table','add_page_reference','replace_page_reference'}:
        record, prior = _baseline(p, group['source_ids'],group.get('source_document'),group.get('page'),
                                  group.get('block_id') if group.get('table_index') is not None else None,
                                  group.get('table_index'))
        record=group.get('native_source_record') or group.get('source_record') or record
        if action in {'confirm_table','confirm_manual'} and group['differences']:
            raise Conflict('发现表格差异，请先预览按原表修正')
        if not target_id:
            # Recover an actually saved table from a previous comparison.
            candidates = [b for b in compiled['source_map'] if group['page'] and f'page[{group["page"]}]' in b.get('locator', '') and _document(b)==group['source_document'] and b['mapping'] == 'page_context']
            target_id = candidates[0]['block_id'] if candidates else None
        if not target_id:
            raise Conflict('请先选择正文中的插入位置')
        block_start, block_end, before = _block_piece(v, compiled, target_id)
        after = before
        if action == 'repair_table':
            if not record:
                raise Conflict('没有可靠单元格来源，不能自动修复')
            try:
                lines, first, last = _table_span(before,group.get('table_index',0))
                table_lines = lines[first:last]
                repair_one = body.get('difference_index')
                if repair_one is not None and (not isinstance(repair_one,int) or repair_one<0 or repair_one>=len(group['differences'])):
                    raise Conflict('差异位置已变化，请重新预览明确单元格')
                if repair_one is None and len(group['differences'])==1 and not group['differences'][0].get('missing_row'):
                    repair_one=0
                desired = record['source_grid']
                if record.get('native_full_grid'):
                    deltas=group['differences'] if repair_one is None else [group['differences'][int(repair_one)]]
                    for delta in deltas:
                        if delta.get('missing_row'):raise Conflict('缺行或额外行需对照完整原表，当前不提供猜测补行')
                        pos=0 if delta['row_index']==0 else delta['row_index']+1
                        cells=[c.strip() for c in table_lines[pos].strip().strip('|').split('|')]
                        cells[delta['column_index']]=delta['expected']
                        table_lines[pos]='| '+' | '.join(cells)+' |\n'
                    desired=None;repair_one=None
                if repair_one is not None:
                    delta = group['differences'][int(repair_one)]
                    if delta.get('missing_row'):
                        raise Conflict('缺行需使用按原表恢复表格的预览')
                    for i, line in enumerate(table_lines[2:], 2):
                        cells = [c.strip() for c in line.strip().strip('|').split('|')]
                        if cells and cells[0] == delta['row']:
                            cells[delta['column_index']] = delta['expected']
                            table_lines[i] = '| '+' | '.join(cells)+' |\n'
                    desired = None
                if desired is not None:
                    header = [cell.strip() for cell in table_lines[0].strip().strip('|').split('|')]
                    if len(header) != len(record.get('source_columns') or [])+1 or any(
                            name.casefold() not in header[i+1].casefold() for i,name in enumerate(record.get('source_columns') or [])):
                        table_lines[0] = '| '+(header[0] if header else '项目')+' | '+' | '.join(record['source_columns'])+' |\n'
                        table_lines[1] = '| '+' | '.join(['---']*(len(record['source_columns'])+1))+' |\n'
                    for wanted_index, wanted in enumerate(desired):
                        if not any(d['row']==wanted[0] for d in group['differences']):continue
                        matched = False
                        for i, line in enumerate(table_lines[2:], 2):
                            cells = [c.strip() for c in line.strip().strip('|').split('|')]
                            if cells and cells[0] == wanted[0]:
                                table_lines[i] = '| '+' | '.join(wanted)+' |\n'
                                matched = True
                        if not matched:
                            indexed = {line.strip().strip('|').split('|')[0].strip():i for i,line in enumerate(table_lines[2:],2)}
                            next_at = next((indexed[row[0]] for row in desired[wanted_index+1:] if row[0] in indexed), None)
                            prior_at = next((indexed[row[0]] for row in reversed(desired[:wanted_index]) if row[0] in indexed), None)
                            insertion = next_at if next_at is not None else prior_at+1 if prior_at is not None else len(table_lines)
                            table_lines.insert(insertion,'| '+' | '.join(wanted)+' |\n')
                after = ''.join(lines[:first]+table_lines+lines[last:])
            except Conflict:
                if re.search(r'<table\b', before, re.I):
                    if not record.get('native_full_grid'):
                        raise Conflict('当前表格不是可安全逐格修改的 Markdown 表，请查看原件确认，或预览添加完整原页参考')
                    tables=list(re.finditer(r'<table\b[^>]*>.*?</table>',before,re.I|re.S))
                    if len(tables)!=1:raise Conflict('当前块含多个表格，不能确定唯一修改实例')
                    selected=group['differences'] if body.get('difference_index') is None else [group['differences'][body['difference_index']]]
                    patches=[];table=tables[0]
                    rows=list(re.finditer(r'<tr\b[^>]*>.*?</tr>',table[0],re.I|re.S))
                    for d in selected:
                        if d.get('missing_row') or d['row_index']>=len(rows):raise Conflict('缺失结构不能安全猜测补回，请对照原表')
                        row=rows[d['row_index']];cells=list(re.finditer(r'<t[dh]\b[^>]*>(.*?)</t[dh]>',row[0],re.I|re.S))
                        if d['column_index']>=len(cells):raise Conflict('目标单元格已变化，请重新预览')
                        cell=cells[d['column_index']]
                        if '<' in cell[1]:raise Conflict('目标单元格包含复杂结构，当前不能安全替换，请对照编辑')
                        begin=table.start()+row.start()+cell.start(1);end=table.start()+row.start()+cell.end(1)
                        patches.append((begin,end,html.escape(d['expected'])))
                    after=before
                    for begin,end,value in sorted(patches,reverse=True):after=after[:begin]+value+after[end:]
                else:
                    if not prior:raise Conflict('没有可靠保存的原表位置，不能猜测重建缺行')
                    prior_compiled = _compiled(p, prior)
                    _, _, source_piece = _block_piece(prior, prior_compiled, record['block_id'])
                    lines, first, last = _table_span(source_piece)
                    after = before+'\n'+''.join(lines[first:last])
        if action in {'add_page_reference','replace_page_reference'}:
            image = table_image(p, group, store)
            raw, meta = image
            key = digest(raw)
            derived = [dict(id=key, sha256=key, name='original-table-page.png', mime='image/png',
                            source_ids=group['source_ids'], source_digest=p['processor']['source_digest'],
                            page=group['page'], bbox=meta['bbox'],region_precision=meta['precision'])]
            replacement=f'\n![原表所在完整原页参考 · 第 {group["page"]} 页](assets/{key})\n\n'
            if 'assets/'+key in markdown:raise Conflict('本版本已有这份原页参考，未重复插入')
            if action=='add_page_reference':
                try:
                    lines,first,last=_table_span(before,group.get('table_index',0))
                    after=''.join(lines[:last])+replacement+''.join(lines[last:])
                except Conflict:
                    tables=list(re.finditer(r'<table\b[^>]*>.*?</table>',before,re.I|re.S))
                    idx=group.get('table_index',0)
                    if len(tables)>idx:
                        table_end=tables[idx].end();after=before[:table_end]+replacement+before[table_end:]
                    else:after=before+replacement
            else:
                try:
                    lines,first,last=_table_span(before,group.get('table_index',0))
                    after=''.join(lines[:first])+replacement+''.join(lines[last:])
                except Conflict:
                    matches=list(re.finditer(r'<table\b.*?</table>',before,re.I|re.S));idx=group.get('table_index',0)
                    if len(matches)>idx:
                        selected=matches[idx];after=before[:selected.start()]+replacement+before[selected.end():]
                    else:after=before+replacement
                for sid in group['source_ids']:choices[sid]='reference'
        lines = markdown.splitlines(keepends=True)
        markdown = ''.join(lines[:block_start])+after+''.join(lines[block_end:])
        if action in {'confirm_table', 'confirm_manual', 'repair_table'}:
            representation = dict(source_ids=group['source_ids'], block_id=target_id,
                                  method='table', source_grid=record['source_grid'] if record else None,
                                  source_columns=record.get('source_columns') if record else None,
                                  confirmed_by='saved_source_grid_rechecked' if record else 'user_local_comparison',
                                  comparison='本版本逐格比较保存的原件映射' if record else '用户对照原页与成稿后确认',
                                  evidence_scope='local_block_only_not_whole_article')
            representation['source_occurrence']=dict(document=group.get('source_document'),page=group.get('page'),block_id=target_id)
            representation['table_index']=group.get('table_index',0)
            rewritten=BeautifulSoup(processor.markdown_renderer().render(after),'html.parser').select('table')
            index=group.get('table_index',0)
            if len(rewritten)>index:representation['target_table_digest']=digest(str(rewritten[index]).encode())
            if record and record.get('native_full_grid'):representation['native_full_grid']=True
            if action == 'confirm_table':
                check = processor.compile_result(p, markdown, resource_usages=choices)
                check_doc = BeautifulSoup(check['html'],'html.parser')
                node = check_doc.find(attrs={'data-block-id':target_id})
                problem = processor._table_grid_problem(node.select_one('table'),record['source_grid'],record.get('source_columns'))
                if problem:
                    raise Conflict(problem+'；没有保存核对结论')
    elif action == 'insert_resource':
        sid = group['source_ids'][0]
        marker = '{{source:'+sid+'}}'
        if marker in markdown:
            raise Conflict('原资源已存在，未重复插入')
        saved_position = _previous_resource_position(p,v,sid) if not target_id else None
        if saved_position:
            markdown, before, after = saved_position['markdown'], saved_position['before'], saved_position['after']
        else:
            if not target_id:
                raise Conflict('没有可核验的原插入位置，请在正文明确选择位置；不会按页码猜测图注位置')
            start, end, before = _block_piece(v, compiled, target_id)
            after = before+'\n'+marker+'\n'
            lines = markdown.splitlines(keepends=True)
            markdown = ''.join(lines[:start])+after+''.join(lines[end:])
    elif action == 'format':
        before = markdown
        markdown,hits=_format_changes(markdown)
        after=markdown
    changed=markdown!=v['markdown']
    labels={'repair_table':('已修正所列来源差异，其他内容未改','撤销本次修格','修正差异并保存'),
            'confirm_manual':('已记录用户对照确认，本版本正文未改','取消这次确认','记录本次用户对照确认'),
            'confirm_table':('已重新核对保存的来源单元格，正文未改','无需撤销纯检查','返回核对结果'),
            'add_page_reference':('已添加完整原页参考，可编辑表格仍保留','撤销添加原页参考','添加原页参考并保存'),
            'replace_page_reference':('已按明确选择用完整原页替换正文表格，原页还包含其他内容','撤销原页替换','替换正文表格并保存'),
            'insert_resource':('已在所选位置插入原资源，保存为新版本','撤销插图','插入原图并保存'),
            'format':(f'已整理 {group.get("occurrence_count",0)} 处普通正文句号，代码、公式与引用未改','撤销格式整理','整理并保存'),
            'source_only':('已记录本次仅作回查，原件字节仍保留','撤销资源用途决定','记录本次仅作回查'),
            'exclude_scope':('已明确排除本次正文范围，不标记为完整忠实核对通过','撤销范围排除','确认范围排除'),
            'retain_difference':('本次仍保留来源差异，未标记为已核对一致','取消保留差异记录','记录保留来源差异')}
    labels['restore_resource']=('已恢复原图文件，正文未改；仍需按实际内容检查结果处理','原件文件不会撤销为缺失','恢复原图文件')
    if action=='repair_table':
        selected=[group['differences'][body['difference_index']]] if body.get('difference_index') is not None else group['differences']
        count=len(selected)
        if all(not d.get('missing_row') for d in selected):
            labels[action]=(f'已修正表格的 {count} 个单元格，其他内容未改','撤销本次修格',f'修正 {count} 格并保存')
        else:labels[action]=(f'已按原表修复所列 {count} 处单元格／结构差异，其他内容未改','撤销本次表格修复',f'修复 {count} 处差异并保存')
    completion,undo_label,apply_label=labels[action]
    receipt = dict(project_id=p['id'], version_id=vid, source_digest=p['processor']['source_digest'],
                   draft_digest=v['digest'], issue_id=group['id'], action=action,
                   body={k:body[k] for k in ('issue_id','action','block_id','acknowledged','difference_index') if k in body},
                   block_id=target_id, before=before, after=after, markdown=markdown,
                   resource_usages=choices, representation=representation, derived_resources=derived,
                   summary=(f'将添加第 {group.get("page")} 页完整原页参考；可编辑表格保留，原页还包含其他内容' if action=='add_page_reference' else
                            f'将移除当前正文表格，改用第 {group.get("page")} 页完整原页；原页还包含其他内容' if action=='replace_page_reference' else group.get('impact')),
                   body_changed=changed,completion=completion,undo_label=undo_label,apply_label=apply_label,
                   category=group.get('category'),evidence_scope=group.get('evidence_scope','本次局部处理，不是全文语义验证'),
                   undo_available=action!='restore_resource',
                   created=time.time())
    receipt['preview_id'] = digest({k:v for k,v in receipt.items() if k != 'created'})
    receipt['before_html'] = processor.safe_html(processor.markdown_renderer().render(before))
    receipt['after_html'] = processor.safe_html(processor.markdown_renderer().render(after))
    return receipt


def preview(store, pid, vid, body):
    result = _preview(processor.project(store, pid), vid, body, store)
    # Previews are deterministic and read-only: the client sends the same
    # request back; the server regenerates and compares its binding.
    return result


def apply(store, pid, vid, body):
    token = str(body.get('preview_id') or '')
    if not re.fullmatch(r'[a-f0-9]{64}', token):
        raise ValueError('请先预览再应用')
    initial=processor.project(store,pid)
    if (body.get('request') or {}).get('action')=='confirm_table':
        checked=_preview(initial,vid,body['request'],store)
        if checked['preview_id']!=token:raise Conflict('核对依据已变化，请重新查看')
        return initial
    def commit(p):
        state = p['processor']
        existing = next((v for v in state['versions'] if v.get('issue_action', {}).get('preview_id') == token or
                         any(d.get('preview_id')==token for d in v.get('issue_decisions') or [])), None)
        if existing:
            return
        receipt = _preview(p, vid, body.get('request') or {}, store)
        if receipt['preview_id'] != token:
            raise Conflict('成稿或资源决定已变化，旧预览失效，请重新预览')
        previous = processor.active_version(p, vid)
        if receipt['action']=='restore_resource':
            group=next(g for g in issues(p,vid,store)['issues'] if g['id']==receipt['issue_id'])
            row=_resources(p,store)[group['source_ids'][0]]
            raw=_restore_bytes(p,row,store,(body.get('request') or {}).get('resource_bytes'))
            if raw is None:raise Conflict('原图恢复依据已失效，未写入文件')
            try:store.blob(raw)
            except Conflict:
                # The replacement is the exact registered immutable content.
                # Preserve corrupted bytes under their own hash before an
                # atomic recovery; never overwrite a different source ID.
                path=store.root/'blobs'/row['sha256']
                corrupted=path.read_bytes();store.blob(corrupted)
                temporary=path.with_name('restore-'+identity()+'.tmp')
                temporary.write_bytes(raw);temporary.replace(path)
        new = copy.deepcopy(previous)
        new.update(id=identity() if receipt['body_changed'] else previous['id'], created=time.time(), origin='issue_action' if receipt['body_changed'] else previous['origin'], markdown=receipt['markdown'],
                   digest=digest(receipt['markdown'].encode()), resource_usages=receipt['resource_usages'],
                   semantic_status='not_reviewed', request_id=None)
        new['derived_resources'] = (previous.get('derived_resources') or [])+receipt['derived_resources']
        if receipt['action']=='exclude_scope':
            new['scope_exclusions']=(previous.get('scope_exclusions') or [])+[dict(source_ids=next(g['source_ids'] for g in issues(p,vid,store)['issues'] if g['id']==receipt['issue_id']),draft_digest=new['digest'],reason='explicit_user_scope_exclusion_not_full_fidelity')]
        for resource in receipt['derived_resources']:
            raw, _ = table_image(p, next(g for g in issues(p,vid,store)['issues'] if g['id']==receipt['issue_id']), store)
            if digest(raw) != resource['sha256']:
                raise Conflict('原表预览字节发生变化')
            store.blob(raw)
        records = []
        compiled = _compiled(p, new)
        doc = BeautifulSoup(compiled['html'], 'html.parser')
        for record in previous.get('representations', [])+[receipt['representation']] if receipt['representation'] else previous.get('representations', []):
            if receipt['representation'] and record is not receipt['representation'] and set(record.get('source_ids', [])) & set(receipt['representation']['source_ids']) and record.get('block_id')==receipt['representation']['block_id'] and record.get('table_index',0)==receipt['representation'].get('table_index',0):
                continue
            node = doc.find(attrs={'data-block-id': record.get('block_id')})
            if not node:
                continue
            bound = copy.deepcopy(record)
            _, _, piece = _block_piece(new, compiled, bound['block_id'])
            if record is not receipt['representation'] and digest(piece.encode()) != record.get('target_markdown_digest'):
                tables=node.select('table');index=record.get('table_index',0)
                if not record.get('target_table_digest') or len(tables)<=index or digest(str(tables[index]).encode())!=record['target_table_digest']:continue
            bound.update(id=identity(), draft_digest=new['digest'], resource_snapshot_digest=digest(new['resource_usages']),
                         target_markdown_digest=digest(piece.encode()), created=time.time())
            if all(processor._alternative_covers(bound,sid,new['markdown'],new['resource_usages'],{bound['block_id']:node}) for sid in bound['source_ids']):
                records.append(bound)
        new['representations'] = records
        final = _compiled(p,new)
        new.update(checks=final['checks'], mechanical_pass=final['mechanical_pass'], source_map=final['source_map'],
                   issue_action=dict(preview_id=token, parent_version=vid, action=receipt['action'],
                                     issue_id=receipt['issue_id'], block_id=receipt['block_id'], source_ids=(receipt['representation'] or {}).get('source_ids', []),
                                     body_changed=receipt['body_changed'],completion=receipt['completion'],undo_label=receipt['undo_label'],
                                     undo_available=receipt['undo_available'],
                                     draft_digest=new['digest'],created=time.time()))
        if not receipt['body_changed']:
            new['issue_action']['decision_before']={k:copy.deepcopy(previous.get(k)) for k in ('representations','resource_usages','scope_exclusions','issue_action')}
            new['issue_decisions']=(previous.get('issue_decisions') or [])+[copy.deepcopy(new['issue_action'])]
            state['versions'][state['versions'].index(previous)]=new
        else:
            state['versions'].append(new)
        state['last_issue_result']=dict(new['issue_action'],version_id=new['id'])
        state.update(active_version=new['id'], checks=new['checks'])
        p.update(revision=p['revision']+1,state='candidate')
    return store.change(pid, commit)


def undo(store, pid, action_version, preview_id=None):
    def change(p):
        state=p['processor']
        version=processor.active_version(p, action_version)
        if preview_id and version.get('issue_action',{}).get('preview_id')!=preview_id:
            raise Conflict('已有后续问题决定，不能撤销另一操作；请先取消最近决定或重新查看对应位置')
        if state['active_version'] != action_version:
            raise Conflict('已有其他编辑或版本切换，未覆盖；可从版本菜单查看处理前的版本')
        parent=version.get('issue_action', {}).get('parent_version')
        if not parent:
            raise Conflict('此版本没有可撤销的问题操作')
        action=version['issue_action']
        if action.get('action')=='restore_resource':raise Conflict('恢复的原件文件不会撤销为缺失；此操作没有修改正文')
        if not action.get('body_changed',True):
            if action.get('draft_digest')!=version['digest']:raise Conflict('正文已改变，不能沿用旧确认撤销')
            before=action.get('decision_before') or {}
            for key,value in before.items():
                if value is None:version.pop(key,None)
                else:version[key]=copy.deepcopy(value)
            version['issue_decisions']=[d for d in version.get('issue_decisions',[]) if d['preview_id']!=action['preview_id']]
            result=_compiled(p,version);version.update(checks=result['checks'],mechanical_pass=result['mechanical_pass'])
            state['checks']=result['checks']
        else:
            prior=processor.active_version(p,parent)
            state.update(active_version=parent,checks=prior['checks'])
        state['last_issue_result']=dict(status='undone',preview_id=action['preview_id'],completion=action.get('undo_label','本次处理')+'已完成',
                                       version_id=state['active_version'],draft_digest=processor.active_version(p)['digest'])
        p['revision']+=1
    return store.change(pid, change)


def operation(store,pid,preview_id):
    """Query an existing identity after an uncertain save; never dispatch."""
    if not re.fullmatch(r'[a-f0-9]{64}',preview_id):raise ValueError('操作身份无效')
    p=processor.project(store,pid)
    last=p['processor'].get('last_issue_result') or {}
    if last.get('status')=='undone' and last.get('preview_id')==preview_id:
        return dict(last,project_id=pid)
    for version in reversed(p['processor']['versions']):
        records=[version.get('issue_action') or {}]+(version.get('issue_decisions') or [])
        found=next((r for r in records if r.get('preview_id')==preview_id),None)
        if found:
            return dict(status='saved',project_id=pid,version_id=version['id'],preview_id=preview_id,
                        active=version['id']==p['processor']['active_version'],body_changed=found.get('body_changed',True),
                        completion=found.get('completion'),undo_label=found.get('undo_label'))
    return dict(status='not_found',project_id=pid,preview_id=preview_id,completion='尚未找到这次操作的保存结果；请保留原操作身份查询，不自动重发')


def table_image(p, group, store):
    """Frozen PDF region; no OCR, network, original write or semantic claim."""
    import fitz
    if store is None:
        raise ValueError('原件存储不可用')
    originals=[o for o in p['inventory']['originals'] if o['name'].lower().endswith('.pdf')]
    original=next((o for o in originals if o['name']==group.get('source_document')),None)
    if original is None and not group.get('source_document') and len(originals)==1:
        original=originals[0]
    if not original or not group.get('page'):
        raise Conflict('此原件没有可用原页区域，请查看原件后确认')
    rows=_resources(p,store)
    boxes=[b['bbox'] for sid in group['source_ids'] for b in rows[sid].get('placements',[]) if b['page']==group['page']]
    if not boxes and not all(rows[sid]['kind']=='page' and _page(rows[sid])==group['page'] for sid in group['source_ids']):
        raise Conflict('没有可靠原表区域，不能用孤立符号代替原表')
    with fitz.open(stream=store.read_blob(original['sha256']),filetype='pdf') as pdf:
        page=pdf[group['page']-1]
        # Image occurrence bounds prove where symbols are, not where a whole
        # table begins or ends. Native table detection can also miss captions
        # or infer just a sub-grid. Without a complete stored table boundary,
        # retain the entire source page; never guess padding around glyphs.
        clip=page.rect
        return page.get_pixmap(matrix=fitz.Matrix(2,2),clip=clip).tobytes('png'),dict(
            page=group['page'],bbox=list(clip),native_source_sha=original['sha256'],
            precision='page',scale=2,scope='complete_original_page_table_bounds_not_proven')


def image_group(p, vid, image_id, store):
    """The same existing image route also serves a confirmed table reference."""
    version=processor.active_version(p,vid)
    if version.get('source_digest')!=p['processor']['source_digest']:
        raise Conflict('原件版本已变化，请重新打开当前材料')
    listing=issues(p,vid,store)
    group=next((g for g in listing['issues']+listing['resolved'] if g['id']==image_id and g['kind']=='table'),None)
    if group:
        return group
    resources=_resources(p,store)
    compiled=_compiled(p,version)
    doc=BeautifulSoup(compiled['html'],'html.parser')
    for record in version.get('representations') or []:
        ids=record.get('source_ids') or []
        stable='table-'+digest(sorted(ids))[:16]
        if image_id not in {record.get('id'),stable} or record.get('method')!='table' or not ids or any(s not in resources for s in ids):
            continue
        node=doc.find(attrs={'data-block-id':record.get('block_id')})
        if not node or not all(processor._alternative_covers(record,sid,version['markdown'],version.get('resource_usages') or {},{record['block_id']:node}) for sid in ids):
            raise Conflict('此版本的表格表示关系已失效，请重新对照确认')
        documents={_document(resources[sid]) for sid in ids}
        pages={_page(resources[sid]) for sid in ids}
        if len(documents)!=1 or len(pages)!=1 or None in pages:
            raise Conflict('表格引用没有明确绑定到同一份原件的同一页')
        return dict(id=stable,kind='table',source_ids=ids,page=next(iter(pages)),source_document=next(iter(documents)),block_id=record['block_id'])
    raise KeyError(image_id)
