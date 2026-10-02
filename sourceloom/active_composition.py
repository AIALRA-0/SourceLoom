"""Checkpointed composition: plan first, write with continuity, repair locally.

Structural delivery checks are deliberately not an independent semantic verdict.
Legacy jobs retain their executor; a job's pipeline identity never changes on resume.
"""
import copy
import json
import re
import time
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from . import active_contracts as A
from .active_resources import Resources, canonical_url
from .active_policy import missing_evidence_gap_policy
from .checks import freeze, inspect_draft
from .contracts import Plan
from .providers import Provider, Uncertain, apply_stream_timeout
from .skills import load_bundle
from .source_context import classify_inert_markup, classify_layout_tables, inventory_groups
from .store import Conflict, digest
from .writing import (canonical, compose, normalize_authored_periods, protected_objects, repair, scan,
                      trim_block_edges, tighten_list_spacing, unwrap_source_marker_images)

PIPELINE = 'active_composition_v1'
PIPELINE_V2 = 'active_composition_v2'


def is_v2(job):
    return job.get('pipeline') == PIPELINE_V2


def is_core_chain(job):
    """Only jobs created after the core-chain cutover use article-level review."""
    return is_v2(job) and job.get('core_chain_version') == 1


class _AccessibleMarkupProbe(HTMLParser):
    """Conservatively detect labels or text that make raw markup meaningful."""

    label_attributes = {'alt', 'title', 'aria-label', 'aria-labelledby', 'aria-describedby'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.has_content = False

    def handle_starttag(self, tag, attrs):
        if any(name.lower() in self.label_attributes and value and value.strip()
               for name, value in attrs):
            self.has_content = True

    def handle_data(self, data):
        if data.strip():
            self.has_content = True


def _unlabelled_transparent_svg(obj):
    classification = obj.get('visual_classification') or {}
    if classification.get('method') != 'all_pixels_alpha_zero':
        return False
    if any(obj.get(key) is not None and str(obj.get(key)).strip() for key in (
            'text', 'original_text', 'original_extracted_text', 'source_text',
            'visible_content', 'alt', 'title', 'aria_label', 'aria-labelledby',
            'caption', 'figure_caption', 'label')):
        return False
    raw = obj.get('raw')
    if not isinstance(raw, str) or not re.search(r'<svg\b', raw, re.IGNORECASE):
        return False
    probe = _AccessibleMarkupProbe()
    try:
        probe.feed(raw)
        probe.close()
    except Exception:
        return False
    return not probe.has_content


def classify_transparent_svg_placeholders(source):
    """Archive blank inline SVGs as decorative without dropping their source bytes."""
    result = copy.deepcopy(source)
    decorative_ids = set()
    for obj in result.get('objects', []):
        if obj.get('kind') in {'image', 'media'} and _unlabelled_transparent_svg(obj):
            obj['source_scope'] = 'layout_decorative'
            decorative_ids.add(obj['id'])
    if decorative_ids:
        # The alpha scan resolved the visual gap: this object has no pixels or
        # accessible source content to review. Keep the object/resource itself.
        result['unknown'] = [gap for gap in result.get('unknown', [])
                             if gap.get('object_id') not in decorative_ids]
    return result


def _needs_active_visual_card(obj):
    return (obj.get('kind') in {'image', 'page', 'media'} and bool(obj.get('resource_id'))
            and not obj.get('visual_classification') and obj.get('source_scope') not in
            {'site_chrome', 'source_metadata', 'layout_decorative'})


def _turn_gap_declarations(gaps):
    """Return gap IDs and their original text without discarding descriptions."""
    declarations=[]
    for value in gaps or []:
        raw=str(value).strip()
        match=re.match(r'^([^\s:：]+)\s*[:：]\s*(.*)$',raw)
        if match:
            declarations.append(dict(id=match[1],text=raw,description=match[2]))
        else:
            declarations.append(dict(id=raw,text=raw,description=raw))
    return declarations


_PROTECTED_PATCH_KINDS = {'object', 'source', 'document_info'}
_HYBRID_OBJECT_MARKUP = re.compile(
    r'<!--|-->|</?[A-Za-z][^>]*>|<![A-Za-z][^>]*>|!\s*\[|\{\{\s*source\s*:',
    re.I)


def _protected_literal_spans(text, literals):
    text=str(text or '')
    spans=[]
    for literal in literals or []:
        if not isinstance(literal,str) or not literal:
            continue
        start=0
        while True:
            start=text.find(literal,start)
            if start<0:break
            spans.append((start,start+len(literal)))
            start+=len(literal)
    merged=[]
    for start,end in sorted(spans):
        if merged and start<=merged[-1][1]:
            merged[-1]=(merged[-1][0],max(merged[-1][1],end))
        else:
            merged.append((start,end))
    return merged


def _outside_protected_literals(text, literals):
    spans=_protected_literal_spans(text,literals)
    pieces=[];cursor=0
    for start,end in spans:
        if start>cursor:pieces.append(text[cursor:start])
        cursor=max(cursor,end)
    if cursor<len(text):pieces.append(text[cursor:])
    return pieces


def _has_authored_object_text(block,literals):
    text=block.get('markdown','')
    spans=_protected_literal_spans(text,literals)
    if not spans:return False
    outside='\n'.join(_outside_protected_literals(text,literals))
    # Ignore structural HTML wrappers around the immutable object. Visible
    # prose, such as an authored Chinese figure caption, remains patchable.
    outside=re.sub(r'<!--.*?-->|<[^>]*>','',outside,flags=re.S)
    return bool(outside.strip())


def _quote_is_outside_protected_literals(block,quote,literals):
    quote=str(quote or '').strip()
    return bool(quote and any(quote in part for part in
                              _outside_protected_literals(block.get('markdown',''),literals)))


def patchable_block_ids(draft,protected_literals=()):
    """Return authored block IDs, including captions outside object literals."""
    protected_literals=tuple(protected_literals or ())
    result=set()
    for block in draft.get('blocks',[]):
        if not block.get('id'):continue
        if block.get('kind') not in _PROTECTED_PATCH_KINDS:
            result.add(block['id'])
        elif (block.get('kind')=='object'
              and _has_authored_object_text(block,protected_literals)):
            result.add(block['id'])
    return result


def retarget_protected_findings(review, draft,protected_literals=()):
    """Move findings on immutable blocks to nearby authored explanation prose.

    A protected object can still be the subject of a valid review. Its bytes
    are never patch targets: when possible, point the finding at the closest
    explanation block bound to the same source object and quote existing prose.
    Findings without such a target remain explicit protected-reference notes.
    """
    protected_literals=tuple(protected_literals or ())
    blocks = draft.get('blocks', [])
    by_id = {block.get('id'): (index, block) for index, block in enumerate(blocks)}
    retained = []
    notes = list(review.get('protected_reference_notes', []))
    receipts = []
    for finding in review.get('findings', []):
        target = by_id.get(finding.get('block_id'))
        if not target or target[1].get('kind') not in _PROTECTED_PATCH_KINDS:
            retained.append(finding)
            continue
        source_index, protected = target
        if (protected.get('kind')=='object'
                and _has_authored_object_text(protected,protected_literals)
                and _quote_is_outside_protected_literals(
                    protected,finding.get('output_quote'),protected_literals)):
            retained.append(finding)
            continue
        source_ids = set(protected.get('source_ids', [])) | set(protected.get('object_ids', []))
        if finding.get('source_id'):
            source_ids = ({finding['source_id']} if not source_ids
                          else source_ids & {finding['source_id']})
        candidates = []
        for index, block in enumerate(blocks):
            if block.get('kind') != 'explanation' or not (source_ids & set(block.get('source_ids', []))):
                continue
            distance = abs(index - source_index)
            # For equal distances, prefer the explanation following the source
            # object: source material is normally rendered before its prose.
            candidates.append((distance, index < source_index, index, block))
        candidates.sort(key=lambda item: item[:3])
        if not candidates:
            notes.append(copy.deepcopy(finding))
            continue
        _, _, _, authored = candidates[0]
        quote = next((line.strip() for line in authored.get('markdown', '').splitlines()
                      if line.strip()), '')
        if not quote:
            notes.append(copy.deepcopy(finding))
            continue
        prior_id = finding.get('block_id')
        finding['block_id'] = authored['id']
        finding['output_quote'] = quote
        finding['required_change'] = (
            '保留关联的原始材料对象，只在这段已有解释文字中修正问题；' +
            finding.get('required_change', '').strip())
        receipts.append(dict(from_block_id=prior_id, to_block_id=authored['id'],
                             source_id=finding.get('source_id', ''),
                             operation='protected_finding_retargeted_to_bound_explanation'))
        retained.append(finding)
    review['findings'] = retained
    if notes:
        review['protected_reference_notes'] = notes
    if receipts:
        review.setdefault('protected_finding_retargets', []).extend(receipts)
    return review


def claim_unknown_patch_continuation(job, original_step_key, continuation_session_key, now=None):
    """Claim at most one new, versioned step after a completed KuaFu SSE deadline.

    The old call remains untouched and its spend remains unknown. This helper
    only qualifies a new logical step; it never edits call or ledger receipts.
    """
    now = time.time() if now is None else now
    candidate = job.get('active_candidate', {})
    markers = candidate.setdefault('unknown_patch_continuations', [])
    existing = next((item for item in markers
                     if item.get('original_step_key') == original_step_key), None)
    if existing:
        return None
    if (job.get('pending') != original_step_key or original_step_key in job.get('results', {})):
        return None
    call = next((item for item in reversed(job.get('calls', []))
                 if item.get('step_key') == original_step_key), None)
    if not call:
        return None
    endpoint = urlsplit(call.get('upstream_base') or '')
    if (call.get('role') != 'active_patch' or call.get('status') != 'uncertain'
            or call.get('protocol') != 'responses' or call.get('streaming') is not True
            or call.get('http_status') != 200 or call.get('response_blob')
            or call.get('upstream_id') or not call.get('dispatch_started')
            or endpoint.hostname not in {'api.kuafushe.cc'}
            or float(call.get('deadline_at') or 0) > now):
        return None
    if not continuation_session_key or continuation_session_key == original_step_key:
        return None
    marker = dict(version='active-patch-unknown-sse-v1',
        original_step_key=original_step_key, original_call_id=call.get('id'),
        original_call_status='uncertain', original_result='unknown_not_replayed',
        continuation_session_key=continuation_session_key,
        status='queued', claimed_at=now)
    markers.append(marker)
    job.pop('pending', None)
    return marker


def _restore_reused_gap_declarations(declarations, saved_declarations):
    """Expand a reused bare ID from its immutable same-session declaration.

    A bare ID carries no new text, so treating it as a changed declaration is
    incorrect. Explicitly supplied text is never normalized or overwritten.
    """
    saved_by_id={}
    duplicates=set()
    for item in saved_declarations or []:
        gap_id=item.get('id') if isinstance(item,dict) else None
        if not gap_id:continue
        if gap_id in saved_by_id:duplicates.add(gap_id)
        saved_by_id[gap_id]=item
    restored=[]
    for declaration in declarations:
        gap_id=declaration.get('id')
        saved=saved_by_id.get(gap_id)
        if (declaration.get('text')==gap_id and saved is not None
                and gap_id not in duplicates):
            restored.append(copy.deepcopy(saved))
        else:
            restored.append(declaration)
    return restored


def _split_recovery_marker(job, session_key):
    if not isinstance(session_key,str) or not session_key.startswith('active-plan-'):
        return None
    prefix=session_key[len('active-plan-'):]
    return next((item for item in job.get('active_plan_split_recoveries',[])
        if prefix in item.get('recovery_prefixes',[])),None)


def _mark_split_recovery_started(job, session_key):
    marker=_split_recovery_marker(job,session_key)
    if marker is None:return
    now=time.time()
    if marker.get('status')!='running':
        marker['started_at']=marker.get('started_at') or now
    marker['status']='running'
    marker['updated_at']=now
    marker.setdefault('completed_sessions',[])


def _mark_split_recovery_completed(job, session_key):
    marker=_split_recovery_marker(job,session_key)
    if marker is None:return
    completed=marker.setdefault('completed_sessions',[])
    if session_key not in completed:completed.append(session_key)
    expected={'active-plan-'+prefix for prefix in marker.get('recovery_prefixes',[])}
    if expected and expected<=set(completed):
        marker.update(status='completed',completed_at=time.time(),updated_at=time.time())
    else:
        marker.update(status='running',updated_at=time.time())


def _mark_split_recovery_failed(job, session_key, error):
    marker=_split_recovery_marker(job,session_key)
    if marker is None:return
    marker.update(status='failed',last_error=str(error),failed_at=time.time(),
                  updated_at=time.time())


def _text_mentions_exact(text, needle):
    if not text or not needle:return False
    if re.fullmatch(r'[\w.-]+',needle,re.UNICODE):
        return re.search(r'(?<!\w)'+re.escape(needle)+r'(?!\w)',text,re.I|re.UNICODE) is not None
    return needle.casefold() in text.casefold()


def _infer_external_action_source(action, gap_descriptions, source_objects, assigned_ids):
    """Align an omitted action source only when current evidence has one identity."""
    assigned=set(assigned_ids)
    all_ids=set(source_objects)
    gap_text='\n'.join(gap_descriptions)
    context='\n'.join([gap_text,action.get('query','')])
    hints=[]

    mentioned={sid for sid in all_ids if _text_mentions_exact(gap_text,sid)}
    if mentioned:
        hints.append(('explicit_source_id_in_gap',mentioned,
                      {'source_ids':sorted(mentioned)}))
        if not mentioned <= assigned:
            return None,dict(reason='gap_source_outside_assigned_group',source_ids=sorted(mentioned),
                methods=[dict(method=method,source_ids=sorted(ids),evidence=evidence)
                         for method,ids,evidence in hints])

    if action.get('kind')=='page' and action.get('url'):
        direct={sid for sid,obj in source_objects.items()
                if sid in assigned and obj.get('kind')=='link' and obj.get('target')
                and canonical_url(action['url'])==canonical_url(obj['target'])}
        if direct:hints.append(('exact_direct_link_target',direct,{'url':action['url']}))

    labels={sid for sid,obj in source_objects.items()
            if sid in assigned and obj.get('kind')=='link'
            and _text_mentions_exact(context,str(obj.get('text','')).strip())}
    if labels:hints.append(('unique_link_label_in_gap_or_query',labels,{
        'labels':sorted({str(source_objects[sid].get('text','')).strip() for sid in labels})}))

    if not hints:return None,dict(reason='no_source_identity_evidence')
    methods=[dict(method=method,source_ids=sorted(ids),evidence=evidence)
             for method,ids,evidence in hints]
    hinted_ids=set().union(*(ids for _,ids,_ in hints))
    if any(len(ids)!=1 for _,ids,_ in hints) or len(hinted_ids)!=1:
        return None,dict(reason='ambiguous_source_identity',source_ids=sorted(hinted_ids),
                          methods=methods)
    source_id=next(iter(hinted_ids))
    return source_id,dict(methods=methods)


def _direct_link_target_saved(resources, source_obj):
    target=source_obj.get('target','')
    if not target:return False
    address=canonical_url(target)
    resource_id=resources.state.get('url_index',{}).get(address)
    entries=resources.state.get('entries',{})
    entry=entries.get(resource_id,{}) if resource_id else {}
    if not entry:
        entry=next((item for item in entries.values()
            if item.get('kind')=='external'
            and canonical_url(item.get('original_url') or item.get('locator',''))==address),{})
    return bool(entry.get('kind')=='external' and entry.get('snapshot_blob'))


def authorize_plan_link_actions(response, source, assigned, saved_decisions):
    """Classify original links before any Planner action can fetch their targets."""
    links={obj['id']:obj for obj in source['objects']
           if obj['id'] in assigned and obj['kind']=='link' and obj.get('target')}
    context={row['source_id']:row for row in plan_link_context(source,assigned)}
    submitted={}
    for item in response.get('link_decisions',[]):
        sid=item['source_id']
        if sid not in links or sid in submitted:
            raise ValueError('链接职责必须唯一对应当前分组的原文链接')
        previous=saved_decisions.get(sid)
        if previous and previous!=item:
            raise ValueError('已分类的原文链接不能在后续取材时改换职责')
        if item['role']=='semantic_dependency' and (
                not item['missing'].strip() or not item['source_quote'].strip() or
                item['source_quote'] not in context[sid]['surrounding_text']):
            raise ValueError('语义依赖必须引用相邻原文，并指出当前论点的具体理解缺口')
        submitted[sid]=item
    decisions={**saved_decisions,**submitted}
    for action in response['actions']:
        if action['kind'] not in {'page','search','image'}:continue
        target_matches={sid for sid,obj in links.items() if action.get('url')
                        and canonical_url(action['url'])==canonical_url(obj['target'])}
        sid=action.get('source_id','')
        if target_matches and sid not in target_matches:
            raise ValueError('读取原文链接目标必须准确绑定该原文链接 source_id')
        matches={sid} if sid in links else set()
        for sid in matches:
            decision=decisions.get(sid)
            if (not decision or decision['role']!='semantic_dependency'
                    or not decision['missing'].strip()):
                raise ValueError('原文链接必须先判定为有具体理解缺口的语义依赖，才能读取目标')
            if action['kind']!='page' or canonical_url(action.get('url',''))!=canonical_url(links[sid]['target']):
                raise ValueError('语义依赖链接只能先读取其精确直接目标')
    return decisions


def normalize_rewrite_reference_turn(raw, source, assigned):
    """Keep ordinary source links as references in the default rewrite path."""
    if not isinstance(raw,dict):return raw,None
    links={obj['id']:obj for obj in source['objects']
           if obj['id'] in assigned and obj['kind']=='link' and obj.get('target')}
    if not links:return raw,None
    cleaned=copy.deepcopy(raw)
    submitted=cleaned.get('link_decisions',[])
    if isinstance(submitted,list):
        cleaned['link_decisions']=[item for item in submitted
            if not isinstance(item,dict) or item.get('source_id') not in links]
    actions=cleaned.get('actions',[])
    if not isinstance(actions,list):return cleaned,None
    targets={canonical_url(obj['target']) for obj in links.values()}
    blocked=[action for action in actions if isinstance(action,dict) and (
        action.get('source_id') in links or
        (action.get('url') and canonical_url(action['url']) in targets))]
    if blocked and len(blocked)!=len(actions):
        raise ValueError('普通改写不能混合原文链接目标研究与其他取材动作')
    if blocked:
        cleaned.update(actions=[],gaps=[])
        if cleaned.get('result') is None:
            cleaned['ready_reason']='普通改写保留原文链接，不读取目标页'
    if blocked or len(cleaned.get('link_decisions',[]))!=len(submitted):
        return cleaned,dict(reason='default_rewrite_links_are_references',
            reference_link_ids=sorted(links),suppressed_action_count=len(blocked))
    return cleaned,None


def normalize_first_plan_link_classification(raw, source, assigned):
    """Suppress only premature reads of links classified as references."""
    if not isinstance(raw,dict) or not isinstance(raw.get('link_decisions'),list):
        return raw,None
    submitted=[A.PlanLinkDecision.model_validate(item).model_dump()
               for item in raw['link_decisions']]
    expected={item['source_id'] for item in plan_link_context(source,assigned)}
    if len(submitted)!=len(expected):
        return raw,None
    decisions=authorize_plan_link_actions(
        {'link_decisions':submitted,'actions':[]},source,assigned,{})
    if set(decisions)!=expected:
        return raw,None
    actions=raw.get('actions',[])
    gaps=raw.get('gaps',[])
    if not isinstance(actions,list) or not isinstance(gaps,list):
        return raw,None
    if not actions or raw.get('result') is not None:
        return raw,None
    links={obj['id']:obj for obj in source['objects'] if obj['id'] in assigned
           and obj['kind']=='link' and obj.get('target')}
    reference_actions=[action for action in actions if action.get('source_id') in links
        and decisions[action['source_id']]['role']=='reference']
    if not reference_actions:
        return raw,None
    if len(reference_actions)!=len(actions):
        raise ValueError('同一 Turn 混合参考链接取材与其他动作，不能安全执行')
    cleaned=copy.deepcopy(raw)
    cleaned.update(actions=[],gaps=[],result=None,
        ready_reason='参考链接职责已冻结，不执行其目标页读取')
    return cleaned,dict(retained_link_decision_ids=list(decisions),
        suppressed_action_count=len(actions),suppressed_gap_count=len(gaps),
        suppressed_result=False,
        reason='classification_complete_before_external_actions')


def plan_link_context(source,assigned):
    """Give the Planner the current source sentence, not the target page."""
    objects=[obj for obj in source['objects'] if obj['id'] in assigned]
    result=[]
    for index,obj in enumerate(objects):
        if obj['kind']!='link' or not obj.get('target'):continue
        locator=obj.get('locator','')
        parent=locator.rsplit('/a[',1)[0] if '/a[' in locator else locator.rsplit('/',1)[0]
        nearby=next((row.get('text','') for row in reversed(objects[:index])
                     if row['kind']!='link' and row.get('locator')==parent),'')
        if not nearby:
            nearby=next((row.get('text','') for row in reversed(objects[:index])
                         if row['kind'] in {'text','heading'}),'')
        heading=next((row.get('text','') for row in reversed(objects[:index])
                      if row['kind']=='heading'),'')
        result.append(dict(source_id=obj['id'],label=obj.get('text',''),
                           target=obj['target'],surrounding_text=nearby[:1200],
                           preceding_heading=heading[:160]))
    return result


def _name_lookup_requested(gap_descriptions, query):
    context='\n'.join([*gap_descriptions,query or ''])
    return re.search(
        r'(?:\b(?:official|full|english|formal)\s+(?:name|term)\b|'
        r'\b(?:name|acronym|abbreviation)\b|英文(?:全称|名称)?|全称|正式名称|官方名称|名称|缩写)',
        context,re.I) is not None


def bounded_prior_context(blocks, limit=8000):
    """Keep a small continuity window only when the plan identifies a risk."""
    selected=[];used=0
    for block in reversed(blocks):
        value=block.get('markdown','')
        if selected and used+len(value)>limit:break
        text=value[-limit:] if not selected and len(value)>limit else value
        selected.append(text);used+=len(text)
    return list(reversed(selected))


def bounded_established_memory(job, max_items=12, max_chars=3200):
    """Expose prior concept anchors without replaying earlier authored units."""
    planned={}
    for part in job.get('active_plans',[]):
        for concept in part.get('concepts',[]):
            if isinstance(concept,dict) and concept.get('id'):
                planned.setdefault(concept['id'],concept)
    result=[];used=0;seen=set()
    for memory in job.get('knowledge_memory',[]):
        for established in memory.get('established',[]):
            cid=established.get('id')
            if not cid or cid in seen:continue
            concept=planned.get(cid,{})
            item=dict(node_id=memory.get('node_id'),concept_id=cid)
            for key in ('chinese_name','english_name','name'):
                if concept.get(key):item[key]=str(concept[key])[:120]
            definition=(established.get('definition') or (
                '' if is_core_chain(job) and job.get('transformation_mode')=='rewrite'
                else concept.get('definition')) or '').strip()
            if definition:item['prior_explanation']=definition[:280]
            source_ids=concept.get('source_ids') or established.get('source_ids') or []
            if source_ids:item['source_ids']=list(source_ids[:8])
            size=len(json.dumps(item,ensure_ascii=False))
            if len(result)>=max_items or used+size>max_chars:break
            result.append(item);used+=size;seen.add(cid)
        if len(result)>=max_items or used>=max_chars:break
    return result


def normalized_concept_name(value):
    """Normalize a concept label for exact bilingual identity checks."""
    normalized=unicodedata.normalize('NFKC',str(value or '')).casefold()
    # Ignore spacing and typography dashes, while retaining meaningful symbols
    # such as the pluses in C++ and the hash in C#.
    return ''.join(char for char in normalized
                   if not char.isspace() and unicodedata.category(char)!='Pd')


def concept_name_pair(concept):
    """Read a Chinese/English label pair without guessing from definitions."""
    chinese=(concept.get('chinese_name') or '').strip()
    english=(concept.get('english_name') or '').strip()
    name=(concept.get('name') or '').strip()
    match=re.fullmatch(r'\s*(.*?)\s*[（(]([^（）()]*)[）)]\s*',name)
    if match:
        left,right=match.groups()
        if re.search(r'[\u3400-\u9fff]',left) and not chinese:chinese=left.strip()
        elif re.search(r'[\u3400-\u9fff]',right) and not chinese:chinese=right.strip()
        if re.search(r'[A-Za-z]',left) and not english:english=left.strip()
        elif re.search(r'[A-Za-z]',right) and not english:english=right.strip()
    elif name:
        if re.search(r'[\u3400-\u9fff]',name) and not chinese:chinese=name
        if re.search(r'[A-Za-z]',name) and not re.search(r'[\u3400-\u9fff]',name) and not english:
            english=name
    return normalized_concept_name(chinese),normalized_concept_name(english)


def coalesce_current_short_rewrite_concepts(job,node,source_objects):
    """Map accepted duplicates and preserve verified name completions in this unit."""
    if job.get('transformation_mode')!='rewrite':return []
    if any(obj.get('kind') not in {'text','heading','link','page','metadata'}
           for obj in source_objects):return []
    prose=[obj for obj in source_objects if obj.get('kind') in {'text','heading'}]
    if not any(obj.get('kind')=='text' for obj in prose):return []
    if sum(len(obj.get('text','')) for obj in prose)>=1200:return []
    if re.search(r'(?mi)^\s*(?:glossary|terminology|术语表|定义)\b',
                 '\n'.join(obj.get('text','') for obj in prose)):
        return []

    planned={concept.get('id'):concept for part in job.get('active_plans',[])
             for concept in part.get('concepts',[]) if concept.get('id')}
    accepted=[];accepted_ids=set()
    for memory in job.get('knowledge_memory',[]):
        for entry in memory.get('established',[]):
            cid=entry.get('id')
            concept=planned.get(cid)
            if not cid or not concept:continue
            chinese,english=concept_name_pair(concept)
            if not chinese:continue
            accepted.append((cid,chinese,english,memory.get('node_id')))
            accepted_ids.add(cid)
    if not accepted:return []

    referenced=set((node.get('requires_concepts') or [])+(node.get('establishes_concepts') or []))
    aliases={}
    name_completions={}
    for cid in sorted(referenced-accepted_ids):
        concept=planned.get(cid)
        if not concept:continue
        chinese,english=concept_name_pair(concept)
        if not chinese or not english:continue
        match=next((row for row in accepted if row[1] and row[2]
                    and row[1:3]==(chinese,english)),None)
        if match:
            aliases[cid]=match[0]
            continue
        # A previously accepted Chinese concept may lack a verified English
        # label. Preserve the current source-backed pair as a name completion;
        # the alias is still routed to the accepted identity and never defines
        # the concept a second time.
        completion_match=next((row for row in accepted if row[1]==chinese and not row[2]),None)
        verified_evidence=(concept.get('naming_status')=='verified'
            and bool(concept.get('english_name','').strip())
            and bool(concept.get('name_evidence'))
            and all(isinstance(item,dict) and item.get('resource_id') and item.get('quote')
                    for item in concept.get('name_evidence',[])))
        if completion_match and verified_evidence:
            abbreviation_shorts=[item.get('short','').strip()
                for item in concept.get('abbreviations',[]) if item.get('short','').strip()]
            first_use_display=(' '.join(abbreviation_shorts)+' ' if abbreviation_shorts else '')
            first_use_display+=concept.get('chinese_name','')+'（'+concept.get('english_name','')+'）'
            aliases[cid]=completion_match[0]
            name_completions[cid]=dict(
                version='short-rewrite-name-completion-v1',unit_id=node.get('id'),
                source_concept_id=cid,accepted_concept_id=completion_match[0],
                chinese_name=concept.get('chinese_name',''),
                english_name=concept.get('english_name',''),
                name_evidence=copy.deepcopy(concept.get('name_evidence',[])),
                source_ids=list(concept.get('source_ids',[])),
                naming_status='verified',abbreviations=copy.deepcopy(concept.get('abbreviations',[])),
                requires_first_use_pair=True,first_use_display=first_use_display,
                instruction=('此前已解释该中文概念；只在当前本单元有来源证据的首次出现处补成“'
                    +first_use_display+'”；缩写置于中文名称之前，全角括号内只放已核实英文名称；'
                    '不要重讲定义或另起术语条目。'))
    if not aliases:return []

    batches=job.get('writing_batches') or []
    unit_ids=set()
    for item in batches[int(job.get('unit_index',0)):]:
        if item.get('id'):unit_ids.add(item['id'])
        unit_ids.update(section.get('id') for section in (item.get('section_outline') or [])
                        if section.get('id'))
    if not unit_ids:
        fallback_nodes=[item for part in job.get('active_plans',[])
                        for item in part.get('nodes',[])]
        for item in fallback_nodes[int(job.get('unit_index',0)):]:
            if item.get('id'):unit_ids.add(item['id'])
            unit_ids.update(section.get('id') for section in (item.get('section_outline') or [])
                            if section.get('id'))
    def remap_node(item,in_scope=False):
        in_scope=in_scope or item.get('id') in unit_ids
        if in_scope:
            if 'requires_concepts' in item:
                item['requires_concepts']=list(dict.fromkeys(
                    aliases.get(cid,cid) for cid in (item.get('requires_concepts') or [])))
            if 'establishes_concepts' in item:
                item['establishes_concepts']=[cid for cid in (item.get('establishes_concepts') or [])
                    if cid not in aliases and cid not in aliases.values()]
        for section in item.get('section_outline') or []:remap_node(section,in_scope)

    receipts=[]
    for cid,target in aliases.items():
        source=planned[cid]
        target_memory=next(row for row in accepted if row[0]==target)
        receipts.append(dict(unit_id=node.get('id'),source_concept_id=cid,
            accepted_concept_id=target,source_names=dict(chinese_name=source.get('chinese_name',''),
                english_name=source.get('english_name',''),name=source.get('name','')),
            accepted_node_id=target_memory[3],
            duplicate_source_ids=list(source.get('source_ids',[])),
            accepted_source_ids=list(planned[target].get('source_ids',[])),
            reason=('accepted_chinese_concept_had_no_english_name; verified_current_source_name_completion'
                    if cid in name_completions else
                    'exact_normalized_chinese_and_english_names_match_prior_established_concept'),
            name_completion=copy.deepcopy(name_completions.get(cid))))

    for part in job.get('active_plans',[]):
        has_uncommitted_node=any(item.get('id') in unit_ids for item in part.get('nodes',[]))
        if has_uncommitted_node:
            part['concepts']=[concept for concept in part.get('concepts',[])
                               if concept.get('id') not in aliases]
            for concept in part.get('concepts',[]):
                concept['requires']=[mapped for cid in (concept.get('requires') or [])
                    if (mapped:=aliases.get(cid,cid))!=concept.get('id')]
                concept['requires']=list(dict.fromkeys(concept['requires']))
        for item in part.get('nodes',[]):remap_node(item)
    for item in job.get('writing_batches',[]):remap_node(item)
    valid_ids={concept.get('id') for part in job.get('active_plans',[])
               for concept in part.get('concepts',[]) if concept.get('id')}|accepted_ids
    missing_target=next((target for target in aliases.values() if target not in valid_ids),None)
    if missing_target:
        raise ValueError('概念合并目标不在已接受知识或当前计划中：'+missing_target)
    # Only inspect references this transaction was authorized to remap. An old
    # accepted plan may retain a legacy prerequisite outside the current
    # concept ledger; that unrelated history must not block this unit.
    residuals=[]
    for part in job.get('active_plans',[]):
        if not any(item.get('id') in unit_ids for item in part.get('nodes',[])):continue
        for concept in part.get('concepts',[]):
            residuals.extend(('concept prerequisite '+concept.get('id',''),cid)
                for cid in (concept.get('requires') or []) if cid in aliases)
        for item in part.get('nodes',[]):
            if item.get('id') not in unit_ids:continue
            for field in ('requires_concepts','establishes_concepts'):
                residuals.extend((field+' '+item.get('id',''),cid)
                    for cid in (item.get(field) or []) if cid in aliases)
            for section in item.get('section_outline') or []:
                if section.get('id') not in unit_ids:continue
                for field in ('requires_concepts','establishes_concepts'):
                    residuals.extend((field+' '+section.get('id',''),cid)
                        for cid in (section.get(field) or []) if cid in aliases)
    for item in job.get('writing_batches',[]):
        if item.get('id') not in unit_ids:continue
        for field in ('requires_concepts','establishes_concepts'):
            residuals.extend((field+' '+item.get('id',''),cid)
                for cid in (item.get(field) or []) if cid in aliases)
        for section in item.get('section_outline') or []:
            if section.get('id') not in unit_ids:continue
            for field in ('requires_concepts','establishes_concepts'):
                residuals.extend((field+' '+section.get('id',''),cid)
                    for cid in (section.get(field) or []) if cid in aliases)
    if residuals:
        where,cid=residuals[0]
        raise ValueError('概念合并后受影响单元仍引用已移除的别名：'+cid+'（'+where+'）')
    if job.get('source') and job.get('writing_batches'):
        job['inventory'],job['plan']=legacy_artifacts(
            job['active_plans'],job['source'],job['writing_batches'],
            job.get('object_responsibilities') if is_core_chain(job) else None)
    job.setdefault('short_rewrite_concept_coalescings',[]).extend(receipts)
    if name_completions:
        stored=job.setdefault('short_rewrite_name_completions',[])
        existing={(row.get('unit_id'),row.get('source_concept_id')) for row in stored}
        stored.extend(copy.deepcopy(row) for row in name_completions.values()
                      if (row.get('unit_id'),row.get('source_concept_id')) not in existing)
    return receipts


def short_rewrite_name_completions(job,node):
    """Return only this unit's verified naming additions for prompts and gates."""
    result=[]
    for row in job.get('short_rewrite_name_completions',[]):
        if row.get('unit_id')!=node.get('id'):continue
        result.append(dict(id=row['source_concept_id'],
            accepted_concept_id=row['accepted_concept_id'],
            chinese_name=row['chinese_name'],english_name=row['english_name'],
            naming_status='verified',name_evidence=copy.deepcopy(row['name_evidence']),
            source_ids=list(row['source_ids']),abbreviations=copy.deepcopy(row.get('abbreviations',[])),
            requires_first_use_pair=bool(row.get('requires_first_use_pair')),
            first_use_display=row.get('first_use_display',''),instruction=row['instruction']))
    return result


def validate_evidence_plan(plan, obligations, objects, assigned, resources=None):
    """Validate only obligation-bound evidence, never free-form research."""
    gaps = {gap['id']: gap for gap in plan.get('evidence_gaps', [])}
    if len(gaps) != len(plan.get('evidence_gaps', [])):
        raise ValueError('证据缺口身份重复')
    obligation_ids=set(obligations)
    for gap in gaps.values():
        if gap['obligation_id'] not in obligation_ids:
            raise ValueError('证据缺口必须绑定当前原文义务：'+gap['id'])
        if gap['source_id'] != obligations[gap['obligation_id']]['source_id']:
            raise ValueError('证据缺口来源与原文义务不一致：'+gap['id'])
        if gap['source_id'] not in assigned or gap['source_id'] not in objects:
            raise ValueError('证据缺口引用了未分配原对象：'+gap['id'])
    binding_ids=[]
    binding_keys=set()
    for binding in plan.get('evidence_bindings', []):
        if not binding.get('id'):
            raise ValueError('证据绑定缺少稳定身份')
        if binding['gap_id'] not in gaps:
            raise ValueError('证据绑定引用了不存在的缺口：'+binding['gap_id'])
        gap=gaps[binding['gap_id']]
        if binding['obligation_id'] != gap['obligation_id']:
            raise ValueError('证据绑定没有回到同一原文义务：'+binding['gap_id'])
        binding_key=binding['id']
        if binding_key in binding_keys:
            raise ValueError('证据绑定身份重复：'+binding['id'])
        binding_keys.add(binding_key)
        binding_ids.append(binding['id'])
        if resources is None:continue
        entry=resources.state['entries'].get(binding['resource_id'])
        if not entry or entry.get('kind')!='external':
            raise ValueError('证据绑定必须来自已读取的外部资源：'+binding['resource_id'])
        if not exact_source_quote(binding['quote'],resources.text(binding['resource_id'])):
            raise ValueError('证据绑定不是已读取资源中的原文片段：'+binding['resource_id'])
    resolutions={row['gap_id']: row for row in plan.get('evidence_resolutions', [])}
    if len(resolutions) != len(plan.get('evidence_resolutions', [])):
        raise ValueError('证据结论身份重复')
    for gap_id, resolution in resolutions.items():
        if gap_id not in gaps:raise ValueError('证据结论引用了不存在的缺口：'+gap_id)
        if resolution['status']=='resolved' and not resolution['binding_ids']:
            raise ValueError('已解决缺口必须保留精确证据绑定：'+gap_id)
        if any(binding_id not in binding_ids for binding_id in resolution['binding_ids']):
            raise ValueError('证据结论包含未保存的绑定：'+gap_id)
    return plan


def prune_reference_link_evidence(plan, reference_link_ids):
    """Remove research receipts owned only by links kept as references."""
    obligations={item['id']:item['source_id'] for item in plan.get('obligations',[])}
    removed={gap['id'] for gap in plan.get('evidence_gaps',[])
             if (gap['source_id'] in reference_link_ids and
                 obligations.get(gap['obligation_id'])==gap['source_id'])}
    if not removed:return plan,dict(gap_ids=[],resolution_gap_ids=[],binding_ids=[])
    cleaned=copy.deepcopy(plan)
    binding_ids=[item['id'] for item in cleaned.get('evidence_bindings',[])
                 if item['gap_id'] in removed]
    resolution_gap_ids=[item['gap_id'] for item in cleaned.get('evidence_resolutions',[])
                        if item['gap_id'] in removed]
    cleaned['evidence_gaps']=[item for item in cleaned['evidence_gaps']
                              if item['id'] not in removed]
    cleaned['evidence_resolutions']=[item for item in cleaned['evidence_resolutions']
                                     if item['gap_id'] not in removed]
    cleaned['evidence_bindings']=[item for item in cleaned['evidence_bindings']
                                  if item['gap_id'] not in removed]
    return cleaned,dict(gap_ids=sorted(removed),
                        resolution_gap_ids=sorted(resolution_gap_ids),
                        binding_ids=sorted(binding_ids))


def downgrade_original_only_evidence_bindings(plan, source, assigned):
    """Do not mistake an exact original quote for independently opened evidence."""
    objects={obj['id']:obj for obj in source['objects'] if obj['id'] in assigned}
    mistaken={binding['id']:binding for binding in plan.get('evidence_bindings',[])
        if binding.get('id') and binding.get('resource_id') in objects
        and exact_source_quote(binding.get('quote',''),
            objects[binding['resource_id']].get('text',''))}
    if not mistaken:
        return plan,[]
    plan=copy.deepcopy(plan)
    plan['evidence_bindings']=[binding for binding in plan.get('evidence_bindings',[])
        if binding.get('id') not in mistaken]
    receipts=[]
    for resolution in plan.get('evidence_resolutions',[]):
        removed=[bid for bid in resolution.get('binding_ids',[]) if bid in mistaken]
        if not removed:
            continue
        resolution['binding_ids']=[bid for bid in resolution['binding_ids'] if bid not in mistaken]
        if resolution.get('status')=='resolved' and not resolution['binding_ids']:
            resolution['status']='unresolved'
            resolution['stop_reason']='原文引文不能代替独立外部证据；本轮外部查证未取得可核验引文'
            resolution['error']='original_quote_misclassified_as_external_evidence'
            for gap in plan.get('evidence_gaps',[]):
                if gap.get('id')==resolution.get('gap_id'):
                    gap['status']='unresolved'
        receipts.append(dict(gap_id=resolution.get('gap_id'),binding_ids=removed,
            source_ids=sorted({mistaken[bid]['resource_id'] for bid in removed}),
            reason='exact_original_quote_is_not_external_evidence'))
    return plan,receipts


def repair_one_missing_json_object_closer(raw,allow_final_root=False):
    """Repair one missing object closer at a structurally proven boundary."""
    stack=[];quoted=False;escaped=False
    for index,char in enumerate(raw):
        if quoted:
            if escaped:escaped=False
            elif char=='\\':escaped=True
            elif char=='"':quoted=False
            continue
        if char=='"':quoted=True
        elif char in '{[':stack.append(char)
        elif char==']' and len(stack)>=2 and stack[-2:] == ['[','{']:
            candidate=raw[:index]+'}'+raw[index:]
            try:json.loads(candidate)
            except json.JSONDecodeError:return None
            return candidate
        elif char in '}]':
            if not stack or (char=='}' and stack[-1]!='{') or (char==']' and stack[-1]!='['):
                return None
            stack.pop()
    # A single absent final brace is safe only when the complete scan ends
    # outside a string with exactly one unclosed root object and no arrays.
    if allow_final_root and not quoted and not escaped and stack==['{']:
        candidate=raw+'}'
        try:json.loads(candidate)
        except json.JSONDecodeError:return None
        return candidate
    return None


def normalize_revision_ids(result, allowed):
    """Accept an unambiguous ID with an attached rationale, retaining both."""
    result=copy.deepcopy(result)
    ids=[]
    for value in result['requires_revision']:
        if value in allowed:
            ids.append(value)
            continue
        match=re.fullmatch(r'((?:candidates|findings)-\d+)\s*[:：]\s*(\S[\s\S]*)',value)
        if not match or match[1] not in allowed:
            raise ValueError('正文修订引用了不存在的检查项目')
        ids.append(match[1])
        result['reasons'].append(value)
    result['requires_revision']=list(dict.fromkeys(ids))
    return result


def exact_source_quote(quote, source, pdf_wrap=False):
    """Resolve unique extraction and omission artifacts to actual source bytes."""
    if not quote or not quote.strip():return None
    if quote in source:return quote
    ellipsis=re.split(r'\s*(?:…|\.\.\.)\s*',quote)
    if len(ellipsis)==2 and all(len(part.strip())>=25 for part in ellipsis):
        first,second=(part.strip() for part in ellipsis)
        if source.count(first)==1 and source.count(second)==1:
            start=source.index(first);end=source.index(second)+len(second)
            if start<source.index(second) and end-start<=1500:
                return source[start:end]
    # The ordinary path permits whitespace only, never spelling or numbers.
    pattern=r'\s+'.join(re.escape(word) for word in quote.split())
    # HTML text extraction can put a line break before punctuation when an
    # inline element ends there ("platform\n,"). Resolve the quotation back
    # to those exact saved characters without changing the source claim.
    for mark in ',.;:!?，。；：':
        pattern=pattern.replace(re.escape(mark),r'\s*'+re.escape(mark))
    matches=list(re.finditer(pattern,source))
    if not matches and pdf_wrap and len(quote)>40:
        # PDF extraction may retain a printed end-of-line word break. Resolve
        # a long citation back to its original bytes, never remove that break
        # from the stored source or silently claim the submitted quote exact.
        words=[]
        for word in quote.split():
            part=''
            for index,char in enumerate(word):
                if index and char.isascii() and char.isalpha() and word[index-1].isascii() and word[index-1].isalpha():
                    part+=r'(?:-\r?\n)?'
                part+=re.escape(char)
            words.append(part)
        matches=list(re.finditer(r'\s+'.join(words),source))
    return matches[0].group() if len(matches)==1 else None


def exact_review_quote_fragment(quote, block):
    """Keep a unique, near-complete exact quote when only framing words differ.

    Dropping negation, numbers, or substantive words could change a review
    finding, so only short deictic/punctuation edges may be removed.
    """
    if not isinstance(quote,str) or not isinstance(block,str) or len(quote)<20:
        return None
    from difflib import SequenceMatcher
    match=SequenceMatcher(None,quote,block,autojunk=False).find_longest_match()
    prefix=quote[:match.a];suffix=quote[match.a+match.size:]
    allowed=set('这它其该此，。：；,. :;\t\r\n')
    fragment=quote[match.a:match.a+match.size]
    if (match.size>=20 and match.size>=len(quote)*.85
            and len(prefix)<=2 and len(suffix)<=2
            and set(prefix+suffix)<=allowed and block.count(fragment)==1):
        return fragment
    return None


def rebind_link_finding_evidence(finding, guides, resources):
    """Bind a quoted destination claim to the saved target-page evidence."""
    guide=next((item for item in guides if item.get('source_id')==finding.get('source_id')),None)
    if not guide or not resources:return None
    matches=[]
    for evidence in guide.get('evidence',[]):
        entry=resources.state.get('entries',{}).get(evidence.get('resource_id'),{})
        if entry.get('kind') not in {'external','image'}:continue
        exact=exact_source_quote(finding.get('source_quote',''),resources.text(evidence['resource_id']))
        if exact is not None:matches.append((evidence['resource_id'],exact))
    if len(matches)!=1:return None
    submitted=finding['source_id'];finding['source_id'],exact=matches[0]
    finding['source_quote']=exact
    return dict(submitted_source_id=submitted,actual_source_id=finding['source_id'],
                operation='content_link_destination_evidence_rebind')


def bind_prefetched_link_evidence(result, source, resources, prefetched):
    """Replace misbound link citations with literal bytes from a fetched target.

    This only repairs evidence provenance. Destination claims still receive the
    normal independent semantic review after planning.
    """
    if not prefetched:return []
    objects={item['id']:item for item in source['objects']}
    receipts=[]
    for brief in result.get('link_briefs',[]):
        if brief.get('role')!='content':continue
        obj=objects.get(brief.get('source_id'),{})
        target=obj.get('target','')
        if not target:continue
        fetched=next((entry for entry in resources.state['entries'].values()
            if entry.get('kind')=='external' and entry.get('original_url')==target
            and entry.get('scope')!='linked_image'),None)
        failed=next((receipt for receipt in resources.state['reads']
            if receipt.get('kind')=='page' and receipt.get('url')==target
            and receipt.get('status')=='unavailable'),None)
        if fetched:
            valid=bool(brief.get('evidence')) and all(
                (entry:=resources.state['entries'].get(item['resource_id']))
                and entry.get('kind')=='external'
                and (entry.get('parent_page_url') or entry.get('original_url'))==target
                and exact_source_quote(item['quote'],resources.text(item['resource_id']))
                for item in brief['evidence'])
            if valid:continue
            excerpt=resources.text(fetched['id'])[:700]
            if not excerpt.strip():continue
            brief['evidence']=[dict(resource_id=fetched['id'],quote=excerpt)]
            brief['unavailable_reason']=''
            receipts.append(dict(source_id=brief['source_id'],target=target,
                resource_id=fetched['id'],reason='literal_excerpt_from_prefetched_target'))
        elif failed and all(item.get('resource_id') in objects for item in brief.get('evidence',[])):
            brief.update(destination='',evidence=[],unavailable_reason=failed['reason'])
            receipts.append(dict(source_id=brief['source_id'],target=target,
                reason='exact_prefetch_failure_record'))
    return receipts


def align_unplaced_concepts(result):
    """Place a planned explanation at its earliest actual source-bearing unit."""
    nodes=result.get('nodes',[])
    concepts={concept['id']:concept for concept in result.get('concepts',[])}
    placed={cid for node in nodes for cid in node.get('establishes_concepts',[])}
    repairs=[]
    for cid,concept in concepts.items():
        if cid in placed:continue
        target=next((node for node in nodes
            if set(node.get('source_ids',[])) & set(concept.get('source_ids',[]))),None)
        if not target:continue
        target['establishes_concepts'].append(cid)
        placed.add(cid)
        repairs.append(dict(concept_id=cid,node_id=target['id'],reason='planned_concept_unplaced'))
    established_before=set()
    for index,node in enumerate(nodes):
        for cid in list(node.get('requires_concepts',[])):
            if cid in established_before or cid in node.get('establishes_concepts',[]):continue
            later=next((future for future in nodes[index+1:]
                if cid in future.get('establishes_concepts',[])),None)
            if (not later or cid not in concepts or
                    not set(concepts[cid].get('requires',[])) <=
                    established_before | set(node.get('establishes_concepts',[]))):continue
            later['establishes_concepts'].remove(cid)
            later['requires_concepts']=list(dict.fromkeys(later.get('requires_concepts',[])+[cid]))
            node['establishes_concepts'].append(cid)
            node['requires_concepts'].remove(cid)
            repairs.append(dict(concept_id=cid,node_id=node['id'],
                previous_node_id=later['id'],reason='first_use_precedes_planned_introduction'))
        established_before.update(node.get('establishes_concepts',[]))
    # A planned concept may depend on another concept that the planner placed
    # later. Introduce that prerequisite at the dependent concept's first
    # explanation, leaving the original source obligations in their own nodes.
    # Cycles and genuinely absent concepts remain validation errors.
    for index,node in enumerate(nodes):
        def hoist(cid, trail):
            if cid in trail or cid not in concepts:return
            for required in concepts[cid].get('requires',[]):
                owner=next((future for future in nodes[index+1:]
                    if required in future.get('establishes_concepts',[])),None)
                if owner:
                    owner['establishes_concepts'].remove(required)
                    if required not in node['establishes_concepts']:
                        node['establishes_concepts'].append(required)
                    repairs.append(dict(concept_id=required,node_id=node['id'],
                        previous_node_id=owner['id'],reason='prerequisite_before_dependent_concept'))
                hoist(required,trail|{cid})
        for cid in list(node.get('establishes_concepts',[])):
            hoist(cid,set())
    established=set()
    for node in nodes:
        pending=list(node.get('establishes_concepts',[]))
        ordered=[]
        while pending:
            ready=next((cid for cid in pending
                if set(concepts.get(cid,{}).get('requires',[])) <= established | set(ordered)),None)
            if ready is None:break
            ordered.append(ready)
            pending.remove(ready)
        if not pending:
            node['establishes_concepts']=ordered
            established.update(ordered)
    return repairs


def merge_adjacent_heading_only_nodes(plan, objects):
    """Attach a standalone source heading to its immediately following content."""
    nodes=plan['nodes']
    merged=[]
    index=0
    while index+1<len(nodes):
        heading,following=nodes[index:index+2]
        if (heading['source_ids'] and
                all(objects.get(sid,{}).get('kind')=='heading' for sid in heading['source_ids']) and
                following['source_ids'] and
                not all(objects.get(sid,{}).get('kind')=='heading' for sid in following['source_ids'])):
            old_id=heading['id']
            following['source_ids']=list(dict.fromkeys(heading['source_ids']+following['source_ids']))
            following['obligation_ids']=list(dict.fromkeys(
                heading['obligation_ids']+following['obligation_ids']))
            following['requires_concepts']=list(dict.fromkeys(
                heading['requires_concepts']+following['requires_concepts']))
            following['establishes_concepts']=list(dict.fromkeys(
                heading['establishes_concepts']+following['establishes_concepts']))
            following['depends_on']=list(dict.fromkeys(
                heading['depends_on']+[d for d in following['depends_on'] if d!=old_id]))
            following['title']=heading['title']
            following['transition_from']=heading['transition_from']
            for later in nodes[index+2:]:
                later['depends_on']=list(dict.fromkeys(
                    following['id'] if d==old_id else d for d in later['depends_on']))
            merged.append(dict(heading_id=old_id,content_id=following['id']))
            nodes.pop(index)
            continue
        index+=1
    return merged


def retire_unanchored_abbreviations(plans, source):
    """Do not teach a formal acronym in a node whose cited source never uses it."""
    objects={obj['id']:obj for obj in source['objects']}
    removed=[]
    for part in plans:
        kept=[]
        for concept in part['concepts']:
            shorts=[a['short'] for a in concept.get('abbreviations',[])
                    if re.fullmatch(r'[A-Z][A-Z0-9]{1,9}',a['short'])]
            texts=[objects[sid]['text'] for sid in concept.get('source_ids',[]) if sid in objects]
            if shorts and not any(re.search(r'(?<![A-Za-z0-9])'+re.escape(short)+r'(?![A-Za-z0-9])',text)
                                  for short in shorts for text in texts):
                removed.append(concept['id'])
            else:kept.append(concept)
        part['concepts']=kept
    if removed:
        retired=set(removed)
        for part in plans:
            for node in part['nodes']:
                node['establishes_concepts']=[cid for cid in node['establishes_concepts'] if cid not in retired]
                node['requires_concepts']=[cid for cid in node['requires_concepts'] if cid not in retired]
    return removed


def _without_markdown_link_destinations(markdown):
    """Keep Markdown link labels while omitting their non-visible destinations."""
    visible=[]
    index=0
    while index<len(markdown):
        if markdown[index]!='[':
            visible.append(markdown[index])
            index+=1
            continue
        slashes=0
        back=index-1
        while back>=0 and markdown[back]=='\\':
            slashes+=1
            back-=1
        if slashes%2:
            visible.append(markdown[index])
            index+=1
            continue
        depth=1
        label_end=index+1
        while label_end<len(markdown) and depth:
            char=markdown[label_end]
            if char=='\\':
                label_end+=2
                continue
            if char=='[':depth+=1
            elif char==']':depth-=1
            label_end+=1
        if depth or label_end>=len(markdown) or markdown[label_end]!='(':
            visible.append(markdown[index])
            index+=1
            continue
        destination_end=label_end+1
        depth=1
        while destination_end<len(markdown) and depth:
            char=markdown[destination_end]
            if char=='\\':
                destination_end+=2
                continue
            if char=='(':depth+=1
            elif char==')':depth-=1
            destination_end+=1
        if depth:
            visible.append(markdown[index])
            index+=1
            continue
        visible.append(markdown[index:label_end])
        index=destination_end
    return ''.join(visible)


def overgrown_short_rewrite_glossary(draft, objects):
    """Catch a glossary detour in a short plain article before paid review."""
    originals=[obj for obj in objects if obj['kind'] in {'text','heading'}]
    prose=[obj for obj in originals if obj['kind']=='text']
    if not prose:return False
    source_chars=sum(len(obj['text']) for obj in originals)
    if source_chars>=1200:return False
    source_text='\n'.join(obj['text'] for obj in originals)
    if re.search(r'(?mi)^\s*(?:glossary|terminology|术语表|定义)\b',source_text):return False
    output=canonical(draft)
    measured_output=_without_markdown_link_destinations(output)
    definitions=len(re.findall(r'(?m)^\s*[-*]\s+[^\n]{1,90}（[^\n]{1,90}）：',output))
    mixed=len(originals)!=len(objects)
    if not mixed:
        return definitions>len(prose) and len(measured_output)>source_chars*2.5
    code_lines=sum(len(obj['text'].splitlines()) for obj in objects if obj['kind']=='code')
    images=sum(obj['kind']=='image' for obj in objects)
    links=sum(obj['kind']=='link' for obj in objects)
    allowance=source_chars*2.5+code_lines*50+images*200+links*100
    return definitions>=max(3,(len(prose)+1)//2) and len(measured_output)>allowance


def carry_unchanged_findings(findings, previous_findings, patch, draft):
    """An unsuccessful or screened edit cannot silently clear an earlier defect."""
    if not patch:return findings
    changed={edit['block_id'] for edit in patch.get('edits',[])}
    blocks={block['id']:block['markdown'] for block in draft['blocks']}
    retained=list(findings)
    for finding in previous_findings:
        bid=finding['block_id']
        if bid in changed or finding['output_quote'] not in blocks.get(bid,''):
            continue
        if not any(current['block_id']==bid and current['problem']==finding['problem']
                   for current in retained):
            retained.append(finding)
    return retained


def clear_resolved_format_issue(job, candidate, node_id):
    """Retire a previous format failure only after this exact unit passes again."""
    if not candidate.pop('unresolved_format',None):return
    prefix=node_id+'：两轮局部修补后仍有 '
    resolved=[item for item in job.get('quality_issues',[]) if item.startswith(prefix)]
    if resolved:
        job['quality_issues']=[item for item in job['quality_issues'] if item not in resolved]
        job.setdefault('resolved_quality_issues',[]).extend(dict(
            issue=item,reason='same_unit_format_replay_passed_after_deterministic_layout_normalization')
            for item in resolved)


def retire_projected_document_info_gap_notes(job):
    """Retire only heading-placement notes fixed by the reader's end-matter projection."""
    from .media import reading_draft
    issues=job.get('quality_issues') or []
    if not issues or not job.get('draft'):return []
    original=job['draft'].get('blocks') or []
    blocks={block.get('id'):block for block in original}
    try:
        projected=reading_draft(dict(draft=job['draft'],inventory=job.get('inventory') or {}))
    except (KeyError,TypeError,ValueError):
        # A presentation projection must never become a new production gate.
        # Keep the original issue when its end-matter move cannot be proven.
        return []
    projected_blocks=projected.get('blocks') or []
    projected_ids=[block.get('id') for block in projected_blocks]
    first_info=next((index for index,block in enumerate(projected_blocks)
                     if block.get('kind')=='document_info'),len(projected_blocks))
    if any(block.get('kind')!='document_info' for block in projected_blocks[first_info:]):
        return []
    kept=[];retired=[]
    for issue in issues:
        if (not all(marker in issue for marker in
                    ('范围外缺口待核查：','editable_block_ids','标题之下'))):
            kept.append(issue);continue
        ids=re.findall(r'(?<![A-Za-z0-9_-])([A-Za-z][A-Za-z0-9_-]*-b\d+)(?![A-Za-z0-9_-])',issue)
        if (not ids or any(bid not in blocks or blocks[bid].get('kind')!='document_info'
                            or bid not in projected_ids[first_info:] for bid in ids)):
            kept.append(issue);continue
        retired.append(dict(issue=issue,block_ids=list(dict.fromkeys(ids)),
            reason='unchanged_document_info_projected_into_collapsed_end_matter',
            draft_digest=digest(canonical(job['draft']).encode())))
    if retired:
        job['quality_issues']=kept
        job.setdefault('resolved_quality_issues',[]).extend(retired)
    return retired


def checkpoint_format_clearable(point, unit, report):
    """Confirm an old failed-format flag is stale on the exact final bytes."""
    if not point.get('unresolved_format'):return False
    records=point.get('format_records',[])
    if not records or point.get('unresolved_content_findings') or point.get('unresolved_revision'):
        return False
    recorded=records[-1]
    current=digest(canonical(unit).encode())
    if not (current==point.get('draft_digest')==point.get('format_digest')==
            recorded.get('digest')==report.get('canonical_digest')):
        return False
    return not report['format']['findings'] and all(
        row['id'] in recorded.get('dismissed',[]) for row in report['format']['candidates'])


def plan_document_preview(source, assigned, prior_groups):
    """Give whole-article shape without leaking future source identities."""
    seen={sid for group in prior_groups for sid in group}|set(assigned)
    return dict(whole_document_source_count=sum(
        o.get('source_scope') not in {'site_chrome','source_metadata'} for o in source['objects']),
        whole_document_files=[o['name'] for o in source.get('originals',[])
                              if not o['name'].startswith('web-assets/')],
        future_headings=[o['text'] for o in source['objects']
                         if o['kind']=='heading' and o['id'] not in seen
                         and o.get('source_scope') not in {'site_chrome','source_metadata'}])


def align_reported_concept_ids(delta, expected):
    """Use exact cited concept identities when a writer reports prose labels."""
    if set(delta['established_concepts'])==set(expected):
        return False
    if not expected:
        return False
    cited=[row['concept_id'] for row in delta['concept_evidence']]
    if len(cited)!=len(set(cited)) or set(cited)!=set(expected):
        return False
    delta['reported_concept_labels']=delta['established_concepts'][:]
    delta['established_concepts']=list(expected)
    return True


def separate_external_citations_from_original_bindings(result, assigned_ids, resources):
    """Keep external name/link citations out of original-object provenance IDs."""
    allowed=set(assigned_ids)
    repairs=[]
    for block in result.get('blocks',[]):
        foreign=[sid for sid in block.get('source_ids',[]) if sid not in allowed]
        if not foreign:continue
        if not set(foreign)<=set(resources.state['entries']):continue
        if any(resources.state['entries'][sid].get('kind')!='external' for sid in foreign):continue
        originals=[sid for sid in block['source_ids'] if sid in allowed]
        if not originals:continue
        block['source_ids']=originals
        repairs.append(dict(block_id=block['id'],external_resource_ids=foreign,
            reason='external_citation_is_not_original_source_object'))
    return repairs


def coordinated_name_in_quote(name,quote):
    """Resolve a single omitted shared modifier in an English coordination."""
    words=name.casefold().split()
    if len(words)<2 or len(words)>3:return False
    head=r'\s+'.join(re.escape(word) for word in words[:-1])
    tail=re.escape(words[-1])
    return bool(re.search(r'\b'+head+r'\s+[a-z][a-z-]*\s+and\s+'+tail+r'\b',quote.casefold()))


def markup_element_name_in_quote(name,quote):
    """An HTML tag's angle brackets do not change its element name."""
    match=re.fullmatch(r'([a-z][a-z0-9-]*) element',name.casefold())
    if not match:return False
    tag=r'<'+re.escape(match[1])+r'>'
    text=quote.casefold()
    return bool(re.search(tag+r'\s+element\b',text) or
                re.search(tag+r'\s+and\s+<[a-z][a-z0-9-]*>\s+elements\b',text) or
                re.search(r'<[a-z][a-z0-9-]*>\s+and\s+'+tag+r'\s+elements\b',text))


def coordinated_color_components_in_quote(name,quote):
    """Confirm each named color in one source list without certifying its order."""
    match=re.fullmatch(r'(foreground|background|link) and '
                       r'(foreground|background|link) colors',name.casefold())
    if not match or match[1]==match[2]:return False
    return any(all(re.search(r'\b'+word+r'\b',part) for word in (*match.groups(), 'colors'))
               for part in re.split(r'[.!?\n]',quote.casefold()))


def markup_property_pair_in_quote(name,quote):
    """Recognize two literal property identifiers listed under properties."""
    match=re.fullmatch(r'([a-z][a-z0-9-]*) and ([a-z][a-z0-9-]*) properties',
                       name.casefold())
    if not match:return False
    return bool(re.search(r'\bproperties\s+<'+re.escape(match[1])+r'>\s+and\s+<' +
                          re.escape(match[2])+r'>',quote.casefold()))


def documented_code_name_in_quote(name,quote):
    """Treat explicit documentation roles as evidence for code-object names."""
    match=re.fullmatch(r'([a-z_][a-z0-9_.]*) (module|function|method|parameter)',
                       name.casefold())
    if not match:return False
    symbol,kind=match.groups(); text=quote.casefold()
    role={'module':'mod','function':'func','method':'meth'}.get(kind)
    if role and re.search(r':'+role+r':`!?~?'+re.escape(symbol)+r'`',text):return True
    return kind=='parameter' and bool(re.search(r'\b'+re.escape(symbol)+r'\s*=',text))


def expand_exact_grouped_findings(result,draft):
    """Split grouped IDs only when every quoted line maps exactly and uniquely."""
    result=copy.deepcopy(result);blocks={b['id']:b['markdown'] for b in draft['blocks']};expanded=[]
    for finding in result['findings']:
        if finding['block_id'] in blocks:
            expanded.append(finding);continue
        ids=[part.strip() for part in re.split(r'[,，]',finding['block_id'])]
        quotes=finding['output_quote'].splitlines()
        if (len(ids)<2 or len(ids)!=len(quotes) or len(ids)!=len(set(ids)) or
                any(bid not in blocks or not quote or blocks[bid].count(quote)!=1 for bid,quote in zip(ids,quotes))):
            expanded.append(finding);continue
        result.setdefault('grouped_finding_expansions',[]).append(dict(
            submitted=finding,block_ids=ids,operation='exact_individual_quote_mapping'))
        expanded.extend(finding|dict(block_id=bid,output_quote=quote) for bid,quote in zip(ids,quotes))
    result['findings']=expanded
    return result


def initialize(job):
    pipeline=job.get('pipeline',PIPELINE)
    if pipeline not in {PIPELINE, PIPELINE_V2}:
        pipeline=PIPELINE
    job['naming_contract_version']=1
    job['joint_review_contract_version']=1
    job['link_contract_version']=1
    role_names=('active_plan','active_write','active_review','active_format','active_revision','active_patch','active_protocol','active_visual','rewrite_scope')
    if is_core_chain(job):
        role_names+=('active_integrity',)
    policy={name:(Path(__file__).parent/'roles'/f'{name}.md').read_text(encoding='utf8') for name in role_names}
    job.update(pipeline=pipeline, stage='active_index', teaching_version=0,
               active_plans=[], active_partition_index=0, unit_index=0,
               knowledge_memory=[], visual_cards=[], quality_issues=[],
               delivery_state='draft', cross_batch_review_required=False,
               content_patch_default=1, content_patch_hard_limit=2,
               format_patch_hard_limit=2,
               role_policy=policy,role_policy_digest=digest(policy))
    if is_core_chain(job):
        job.pop('delivery_state',None)
    return job


def normalize_visual_card_lists(value):
    """Repair a schema-only scalar list without another paid model call"""
    result=copy.deepcopy(value)
    for card in result.get('cards',[]) if isinstance(result,dict) else []:
        # Vision models occasionally use this transparent synonym even though
        # the requested contract names the field ``limitations``.  Moving the
        # supplied strings preserves every statement while avoiding a second
        # paid call for a schema-only label mismatch.
        extra_limitations=card.pop('limitations_note',[])
        if isinstance(extra_limitations,str):
            extra_limitations=[extra_limitations] if extra_limitations.strip() else []
        if isinstance(extra_limitations,list):
            card['limitations']=list(dict.fromkeys([
                *(card.get('limitations') or []),
                *(item for item in extra_limitations if isinstance(item,str) and item.strip()),
            ]))
        for key in ('relationships','uncertainty','limitations','blocking_uncertainty'):
            item=card.get(key)
            if item is None:card[key]=[]
            elif isinstance(item,str):card[key]=[item] if item.strip() else []
        uncertainty=card.get('uncertainty',[])
        for item in card.get('blocking_uncertainty',[]):
            if item not in uncertainty:uncertainty.append(item)
    return result


def link_guides(job, source_ids):
    """Reuse the plan's already-fetched target evidence without another read turn."""
    selected=set(source_ids)
    return [brief for part in job.get('active_plans',[]) for brief in part.get('link_briefs',[])
            if brief['source_id'] in selected]


def writer_reference_scope(source, source_ids, responsibilities):
    """Project existing reference decisions and label-only list items for Writer."""
    selected=set(source_ids)
    objects=source['objects']
    reference_links={obj['id'] for obj in objects if obj['id'] in selected
        and obj['kind']=='link' and not responsibilities.get(obj['id'],{}).get('explain',True)}
    parent_ids={obj.get('parent_id') for obj in objects if obj['id'] in reference_links}
    related_headings={'see also','related resources','further reading','references',
                      '另见','参见','相关资源','延伸阅读','参考资料'}
    in_related_resources=False
    label_ids=set()
    for obj in objects:
        if obj['kind']=='heading':
            in_related_resources=obj.get('text','').strip().rstrip(':：').casefold() in related_headings
            continue
        if not in_related_resources or obj['id'] not in selected or obj['kind']!='text':
            continue
        raw=obj.get('raw','').strip()
        item=re.fullmatch(r'<li\b[^>]*>(.*?)</li>',raw,flags=re.S|re.I)
        if not item or re.search(r'</?(?:p|ul|ol|table|div)\b',item.group(1),re.I):
            continue
        if obj['id'] in parent_ids or re.fullmatch(
                r'\s*\{\{\s*jsxref\([^{}]+\)\s*\}\}\s*',item.group(1),re.I):
            label_ids.add(obj['id'])
    return sorted(reference_links),sorted(label_ids)


def writer_reference_payload(job, source, source_ids, obligations, *, core_rewrite):
    """A Reference is display coverage, never permission to explain its target."""
    if not core_rewrite:
        return dict(obligations=prompt_obligations(obligations),
                    link_guides=link_guides(job,source_ids),
                    object_responsibilities={sid:job['object_responsibilities'][sid]
                        for sid in source_ids} if is_core_chain(job) else {})
    roles=job['object_responsibilities']
    reference_links,label_ids=writer_reference_scope(source,source_ids,roles)
    projected_roles={sid:copy.deepcopy(roles[sid]) for sid in source_ids}
    for sid in label_ids:projected_roles[sid]['explain']=False
    return dict(
        obligations=prompt_obligations(obligations,
            reference_link_ids=reference_links,reference_label_ids=label_ids),
        link_guides=[brief for brief in link_guides(job,source_ids)
                     if brief['source_id'] not in reference_links],
        object_responsibilities=projected_roles,
        reference_link_ids=reference_links,reference_label_ids=label_ids)


def reviewed_link_guides(job, source_ids, obligations, review_ids):
    """After a screened patch, recheck only links touched by that patch."""
    guides=link_guides(job,source_ids)
    if set(review_ids)=={item['id'] for item in obligations}:return guides
    active_sources={item['source_id'] for item in obligations if item['id'] in review_ids}
    return [guide for guide in guides if guide['source_id'] in active_sources]


def discard_redundant_reads_with_result(response, resources):
    """A completed result need not repeat reads already fully in its context."""
    actions=response.get('actions',[])
    if (response.get('result') is None or response.get('gaps') or not actions
            or not all(action.get('kind')=='read' and
                resources.fully_read(action.get('resource_id','')) for action in actions)):
        return response,[]
    return response|{'actions':[]},[action['resource_id'] for action in actions]


def discard_known_plan_protocol_extras(raw):
    """Normalize harmless plan-shape omissions in a copy, preserving raw output."""
    cleaned=copy.deepcopy(raw);removed=[]
    result=cleaned.get('result') if isinstance(cleaned,dict) else None
    if (isinstance(result,dict) and isinstance(result.get('cross_batch_risks'),list)
            and isinstance(result.get('nodes'),list) and result['nodes']
            and isinstance(result['nodes'][-1],dict)):
        risks=result.pop('cross_batch_risks')
        target=result['nodes'][-1]
        target['cross_batch_risks']=list(dict.fromkeys(
            list(target.get('cross_batch_risks') or [])+risks))
        removed.append('result.cross_batch_risks')
    concepts=result.get('concepts',[]) if isinstance(result,dict) else []
    for concept in concepts if isinstance(concepts,list) else []:
        if not isinstance(concept,dict):continue
        if 'requires' not in concept:
            removed.append(concept.get('id',''))
            concept['requires']=[]
        if 'naming_status_effective' in concept and 'naming_status' in concept:
            removed.append(concept.get('id',''))
            concept.pop('naming_status_effective',None)
    nodes=result.get('nodes',[]) if isinstance(result,dict) else []
    if not isinstance(nodes,list) or any(not isinstance(node,dict) for node in nodes):
        # Keep the malformed response intact so the ordinary schema-correction
        # turn can request a real plan. A string node is not a plan to repair.
        return cleaned,list(dict.fromkeys(removed))
    for index,node in enumerate(nodes):
        if 'transition_from' not in node:
            node['transition_from']=(
                '本文起点，无前置段落' if index==0 else
                '承接上一单元“'+str(nodes[index-1].get('title','前文'))+'”')
            removed.append(node.get('id',''))
        if 'prepares_for' not in node:
            node['prepares_for']=(
                '为下一单元“'+str(nodes[index+1].get('title','后文'))+'”准备必要背景'
                if index+1<len(nodes) else '本文在此结束')
            removed.append(node.get('id',''))
        if node.get('explanation_placeholder')=='':
            removed.append(node.get('id',''))
            node.pop('explanation_placeholder',None)
        explanation=node.get('explanation')
        if isinstance(explanation,dict) and explanation.get('section_outline')==[]:
            removed.append(node.get('id',''))
            explanation.pop('section_outline',None)
    return cleaned,list(dict.fromkeys(removed))


def normalize_plan_clarify_expansion(raw, resources, prior_link_decisions=()):
    """Fix only the observed depth/expansion enum mix-up in a validation copy."""
    result=raw.get('result') if isinstance(raw,dict) else None
    nodes=result.get('nodes') if isinstance(result,dict) else None
    if not isinstance(nodes,list) or not any(isinstance(node,dict) and
            node.get('expansion')=='clarify' for node in nodes):
        return raw,[]
    cleaned=copy.deepcopy(raw)
    result=cleaned['result']
    bindings={item.get('id'):item for item in (result.get('evidence_bindings') or [])
              if isinstance(item,dict) and item.get('id')}
    resolved_ids={binding_id for item in (result.get('evidence_resolutions') or [])
                  if isinstance(item,dict) and item.get('status')=='resolved'
                  for binding_id in item.get('binding_ids',[])}
    responsibilities={item.get('source_id'):item for item in
        (result.get('object_responsibilities') or []) if isinstance(item,dict)}
    decisions={item.get('source_id'):item for item in prior_link_decisions
               if isinstance(item,dict)}
    decisions.update({item.get('source_id'):item for item in
        (cleaned.get('link_decisions') or []) if isinstance(item,dict)})
    briefs={item.get('source_id'):item for item in (result.get('link_briefs') or [])
            if isinstance(item,dict)}

    def saved_fact_evidence(item):
        if not isinstance(item,dict):return False
        rid=item.get('resource_id')
        entry=resources.state['entries'].get(rid,{})
        quote=item.get('quote','')
        return bool(entry.get('kind')=='external' and
            entry.get('scope') not in {'name_evidence_excerpt','name_only'} and
            quote and exact_source_quote(quote,resources.text(rid)))

    receipts=[]
    for node in result['nodes']:
        if not isinstance(node,dict) or node.get('expansion')!='clarify':continue
        obligation_ids=set(node.get('obligation_ids',[]))
        fact_ids=[bid for bid in resolved_ids if bid in bindings and
                  bindings[bid].get('obligation_id') in obligation_ids and
                  saved_fact_evidence(bindings[bid])]
        for sid in node.get('source_ids',[]):
            brief=briefs.get(sid,{})
            if (decisions.get(sid,{}).get('role')!='semantic_dependency' or
                    not responsibilities.get(sid,{}).get('explain') or
                    brief.get('role')!='content'):
                continue
            fact_ids.extend(item.get('resource_id','') for item in (brief.get('evidence') or [])
                            if saved_fact_evidence(item))
        fact_ids=list(dict.fromkeys(fact_ids))
        replacement='verified_clarification' if fact_ids else 'source_only'
        node['expansion']=replacement
        receipts.append(dict(node_id=node.get('id',''),field='expansion',
            original='clarify',normalized=replacement,
            reason=('resolved fact-scoped external evidence bound to node'
                    if fact_ids else 'no fact-scoped external evidence bound to node'),
            has_fact_scoped_external_evidence=bool(fact_ids),evidence_ids=fact_ids))
    return cleaned,receipts


def discard_known_review_protocol_extras(raw):
    """Remove an empty duplicate explanation field from a validation copy."""
    cleaned=copy.deepcopy(raw);removed=[]
    result=cleaned.get('result') if isinstance(cleaned,dict) else None
    decisions=result.get('format_decisions',[]) if isinstance(result,dict) else []
    for decision in decisions if isinstance(decisions,list) else []:
        if not isinstance(decision,dict):continue
        if decision.get('reason_note')=='' and decision.get('reason','').strip():
            removed.append(decision.get('candidate_id',''))
            decision.pop('reason_note',None)
    return cleaned,removed


def unique(values, label):
    if len(values) != len(set(values)) or '' in values:
        raise ValueError(label + '身份为空或重复')


def patch_rounds(candidate):
    """Share two committed local repairs across content and format."""
    recorded=(len(candidate.get('patch_history', []))
            + len(candidate.get('revision_history', []))
            + sum(bool(r.get('edits')) for r in candidate.get('format_records', [])))
    return max(recorded,candidate.get('local_patch_attempts',0))


def reserve_patch_attempt(candidate):
    used=patch_rounds(candidate)
    if used>=candidate.get('patch_limit',2):raise ValueError('局部补丁已达到当前设置上限（最多两轮），保留具体问题与原稿')
    candidate['local_patch_attempts']=used+1


def format_signature(text):
    """Only spacing, presentation punctuation and letter case may change here."""
    numeric=tuple(re.findall(r'(?<!\w)[+-]?\d+(?:[.,]\d+)*(?:[eE][+-]?\d+)?%?',text))
    return ''.join(c.casefold() for c in text if unicodedata.category(c)[0] in {'L','N','S'}),numeric


def compiled_term_format_proposal(draft, issues):
    """Reuse an existing exact name only after contextual model confirmation.

    No translation, term inference, source edits, or newly invented English names.
    Ambiguous definitions and locations still need ordinary content revision.
    """
    result=issues.get('format_response',{})
    required=set(result.get('requires_revision',[]))
    if not required or result.get('document_digest')!=digest(canonical(draft).encode()):return None
    definition=re.compile(r'^\s*-\s+([^（\n]+)（([A-Za-z][A-Za-z0-9 -]*)）：')
    registry={};lines={};start=1
    for block in draft['blocks']:
        if block['kind'] not in {'source','object','document_info'}:
            fence=None
            for index,line in enumerate(block['markdown'].split('\n')):
                marker=re.match(r'^\s*(`{3,}|~{3,})',line)
                if marker:
                    if fence is None:fence=marker[1]
                    elif marker[1][0]==fence[0] and len(marker[1])>=len(fence):fence=None
                    continue
                if fence:continue
                lines[start+index]=(block,line)
                match=definition.match(line)
                if match:registry.setdefault(match[1].strip(),set()).add(match[2])
        start+=block['markdown'].count('\n')+2
    edits=[]
    for edit in result.get('edits',[]):
        # Leave word-changing proposals untouched for the next actual scan/review.
        # Compiling the independent confirmed label never declares those fixed.
        if format_signature(edit['old_text'])!=format_signature(edit['new_text']):continue
        edits.append(dict(block_id=edit['block_id'],old_text=edit['old_text'],new_text=edit['new_text'],reason=edit['rule']))
    found=set()
    for issue in issues.get('findings',[])+issues.get('candidates',[]):
        if issue['id'] not in required:
            if not issue.get('old_text') or not any(issue['old_text'] in e['old_text'] for e in result.get('edits',[])):return None
            continue
        if issue['id'] in found or issue['rule_id']!='FORMAT_NESTED_DEFINED_TERM_REVIEW':return None
        found.add(issue['id']);name=issue.get('old_text','')
        names=registry.get(name,set());location=re.fullmatch(r'LINE-(\d+)',issue.get('location',''))
        if len(names)!=1 or not location or int(location[1]) not in lines:return None
        block,line=lines[int(location[1])];match=definition.match(line)
        if not match or line[match.end():].count(name)!=1:return None
        # Replace the unique full definition line, never a similarly named term elsewhere.
        if block['markdown'].count(line)!=1 or name+'（' in line[match.end():]:return None
        replacement=line[:match.end()]+line[match.end():].replace(name,name+'（'+next(iter(names))+'）',1)
        edits.append(dict(block_id=block['id'],old_text=line,new_text=replacement,
            reason=issue['id']+': confirmed same concept; reuse exact existing bilingual name'))
    if found!=required:return None
    # Reject overlapping line/phrase proposals before the transactional committer.
    for i,edit in enumerate(edits):
        for prior in edits[:i]:
            if prior['block_id']==edit['block_id'] and (prior['old_text'] in edit['old_text'] or edit['old_text'] in prior['old_text']):return None
    return dict(document_digest=result['document_digest'],edits=edits)


def _separate_authored_paragraphs_from_lists(markdown):
    """Add Markdown's paragraph/list separator without changing other structures."""
    lines=markdown.splitlines(keepends=True)
    list_item=re.compile(r'^ {0,3}(?:[-+*]|\d{1,9}[.)])\s+')
    prior_list_item=re.compile(r'^\s*(?:[-+*]|\d{1,9}[.)])\s+')
    heading=re.compile(r'^ {0,3}#{1,6}(?:\s|$)')
    quote=re.compile(r'^\s*>')
    table_markup=re.compile(r'^\s*</?(?:table|thead|tbody|tfoot|tr|td|th)\b',re.I)
    thematic_break=re.compile(r'^\s*(?:-{3,}|\*{3,}|_{3,})\s*$')
    result=[]
    for line in lines:
        if result and list_item.match(line):
            previous=result[-1].rstrip('\r\n')
            # Only repair an unambiguous prose-to-list boundary. Indented
            # continuation lines, headings, quotes, tables, rules, and lists
            # already have their own Markdown structure.
            if (previous and not previous[:1].isspace()
                    and not prior_list_item.match(previous)
                    and not heading.match(previous)
                    and not quote.match(previous)
                    and not table_markup.match(previous)
                    and not thematic_break.match(previous)
                    and '|' not in previous):
                separator='\r\n' if result[-1].endswith('\r\n') else '\n'
                result.append(separator)
        result.append(line)
    return ''.join(result)


def normalize_authored_spacing(draft,inventory):
    changed=copy.deepcopy(draft)
    literals=protected_objects(inventory)
    for block in changed['blocks']:
        if block['kind'] in {'object','document_info','source'}:continue
        if re.search(r'(?m)^\s*(?:```|~~~)',block['markdown']):continue
        stripped=block|{'markdown':block['markdown'].strip('\r\n')}
        proposal,_=tighten_list_spacing({'blocks':[stripped]})
        text=_separate_authored_paragraphs_from_lists(proposal['blocks'][0]['markdown'])
        text=re.sub(r'\n{3,}', '\n\n',text)
        if all(block['markdown'].count(v)==text.count(v) for v in literals.values() if v):
            block['markdown']=text
    changed['blocks']=[block for block in changed['blocks'] if not (
        block['kind']=='explanation' and not block['markdown'].strip()
        and not block.get('obligation_ids') and not block.get('object_ids')
        and not block.get('embedded_object_ids'))]
    return changed


def respect_original_format(report,draft,inventory):
    """Source-owned characters are exempt, not rewritten to satisfy prose rules."""
    report=copy.deepcopy(report);text=canonical(draft);ranges=[];cursor=0
    literals=protected_objects(inventory)
    for block in draft['blocks']:
        for sid in block.get('embedded_object_ids',[]):
            literal=literals.get(sid,'')
            if not literal:continue
            for match in re.finditer(re.escape(literal),block['markdown']):
                ranges.append((cursor+match.start(),cursor+match.end(),sid))
        cursor+=len(block['markdown'])+2
    lines=text.splitlines(keepends=True);starts=[];cursor=0
    for line in lines:starts.append(cursor);cursor+=len(line)
    def fully_commented_css_fence(row):
        opening=None
        for index,line in enumerate(lines[:row+1]):
            if re.match(r'^\s*```css\s*$',line,re.I):opening=index
            elif opening is not None and re.match(r'^\s*```\s*$',line):opening=None
        if opening is None:return False
        closing=next((index for index in range(opening+1,len(lines))
                      if re.match(r'^\s*```\s*$',lines[index])),None)
        if closing is None or not opening<row<closing:return False
        effective=[line.strip() for line in lines[opening+1:closing]
                   if line.strip() and line.strip() not in {'}','};'}]
        return bool(effective) and all(re.search(r'/\*[^*]*\*/\s*$',line)
                                       for line in effective)
    exempt=[]
    for category in ('findings','candidates'):
        kept=[]
        for issue in report['format'][category]:
            match=re.fullmatch(r'LINE-(\d+)',issue.get('location',''));raw=issue.get('old_text','').strip()
            row=int(match[1])-1 if match else -1;sid=None
            if raw and 0<=row<len(lines):
                if raw in lines[row]:
                    begin=starts[row]+lines[row].index(raw);end=begin+len(raw)
                    sid=next((s for a,b,s in ranges if a<=begin and end<=b),None)
                # Code-coverage reports point at the fence's opening line,
                # while quoting a code line inside that same protected object.
                if sid is None and issue.get('rule_id','').startswith('FORMAT_CODE_'):
                    sid=next((s for a,b,s in ranges if a<=starts[row]<b and raw in text[a:b]),None)
            if sid:exempt.append(dict(source_id=sid,category=category,issue=issue,reason='Exact protected source characters; original retained'))
            elif (issue.get('rule_id')=='FORMAT_CODE_COMMENT_COVERAGE'
                  and fully_commented_css_fence(row)):
                exempt.append(dict(source_id='',category=category,issue=issue,
                    reason='Every effective CSS line has a legal inline block comment'))
            elif (issue.get('rule_id')=='FORMAT_IMAGE_NOT_CENTERED' and
                  re.search(r'`<img(?:\s|>|/)',raw,re.I) and
                  not re.search(r'<img(?:\s|>|/)|!\[[^]]*\]\(',
                                re.sub(r'`[^`\n]*`','',raw),re.I)):
                exempt.append(dict(source_id='',category=category,issue=issue,
                    reason='Literal HTML tag in inline code is prose, not a rendered image'))
            else:kept.append(issue)
        report['format'][category]=kept
    report['original_exemptions']=exempt
    return report


def candidate_key(issue,draft):
    """A contextual dismissal survives only while its actual block is unchanged."""
    match=re.fullmatch(r'LINE-(\d+)',issue.get('location',''))
    row=int(match[1]) if match else -1;start=1
    for block in draft['blocks']:
        end=start+block['markdown'].count('\n')
        if start<=row<=end:
            return digest([issue['rule_id'],issue.get('old_text'),block['id'],block['markdown']])
        start=end+2
    return digest([issue,canonical(draft)])


def located_format_issues(report,draft):
    """Give the joint first review concrete block addresses for scanner output."""
    result={}
    for category in ('findings','candidates'):
        rows=[]
        for issue in report['format'][category]:
            match=re.fullmatch(r'LINE-(\d+)',issue.get('location',''))
            row=int(match[1]) if match else -1;start=1
            for block in draft['blocks']:
                end=start+block['markdown'].count('\n')
                if start<=row<=end:
                    quote=issue.get('old_text','').strip()
                    if not quote or quote not in block['markdown']:
                        quote=block['markdown'].split('\n')[row-start]
                    if quote.strip():rows.append(issue|dict(block_id=block['id'],output_quote=quote))
                    break
                start=end+2
        result[category]=rows
    return result


def review_format_context(issues):
    """Send review-relevant scanner evidence without duplicated repair data."""
    keys={'id','rule_id','severity','reason','block_id','output_quote'}
    return {category:[{k:v for k,v in row.items() if k in keys} for row in rows]
            for category,rows in issues.items()}


def confirmed_format_issues(issues,review,required=False):
    candidates={c['id']:c for c in issues['candidates']}
    definite_ids={f['id'] for f in issues['findings']}
    decisions=[d for d in review.get('format_decisions',[])
               if d['candidate_id'] not in definite_ids]
    review['format_decisions']=decisions
    ids=[d['candidate_id'] for d in decisions]
    if len(ids)!=len(set(ids)) or not set(ids)<=candidates.keys():
        raise ValueError('格式判断引用重复或不存在的候选')
    if required and set(ids)!=candidates.keys():
        raise ValueError('首次合并核对必须逐项判断格式候选，不能留到修补次数耗尽之后')
    return issues['findings']+[candidates[d['candidate_id']] for d in decisions if d['decision']=='fix']


def planned_heading_depth(draft,level,inventory):
    from markdown_it import MarkdownIt
    result=copy.deepcopy(draft)
    literals=protected_objects(inventory)
    shift=None
    for block in result['blocks']:
        before=block['markdown'];lines=before.splitlines(keepends=True)
        offsets=[];cursor=0
        for line in lines:offsets.append(cursor);cursor+=len(line)
        ranges=[]
        for sid in block.get('embedded_object_ids',[]):
            text=literals.get(sid,'')
            if text:
                start=before.find(text)
                if start>=0:ranges.append((start,start+len(text)))
        for token in MarkdownIt('commonmark').parse(before):
            if token.type!='heading_open' or not token.map:continue
            row=token.map[0]
            if any(start<=offsets[row]<end for start,end in ranges):continue
            if shift is None:shift=level-int(token.tag[1:])
            depth=int(token.tag[1:])+shift
            if not 2<=depth<=6:raise ValueError('正文标题越出规划单元的层级范围，需要调整结构')
            lines[row]=re.sub(r'^( {0,3})#{1,6} ',lambda m:m[1]+'#'*depth+' ',lines[row])
        block['markdown']=''.join(lines)
    return result


def source_spans(source,ids):
    spans=[]
    for obj in source['objects']:
        if obj['id'] not in ids:continue
        text=obj['text'];start=0
        if not text:
            spans.append(dict(id=obj['id']+':0:0',source_id=obj['id'],start=0,end=0,preview=''))
        while start<len(text):
            end=min(len(text),start+900)
            if end<len(text):
                candidates=[m.end()+start for m in re.finditer(r'\n\s*\n|[.!?。！？](?:\s|$)',text[start:end])]
                if candidates and candidates[-1]>start+200:end=candidates[-1]
                else:
                    boundary=text.rfind(' ',start+200,end)
                    if boundary>start:end=boundary+1
            spans.append(dict(id=f"{obj['id']}:{start}:{end}",source_id=obj['id'],start=start,end=end,
                              preview=text[start:min(end,start+140)]))
            start=end
    return spans


def normalize_source_span_ids(selected, source_id, spans):
    """Expand a model-composed contiguous range back to canonical span IDs."""
    normalized=[]
    for span_id in selected:
        if span_id in spans and spans[span_id]['source_id']==source_id:
            normalized.append(span_id)
            continue
        try:
            claimed_source,start,end=span_id.rsplit(':',2)
            start,end=int(start),int(end)
        except (AttributeError,ValueError):
            return selected
        if claimed_source!=source_id or start>=end:
            return selected
        candidates=sorted((span for span in spans.values()
            if span['source_id']==source_id and span['start']>=start and span['end']<=end),
            key=lambda span:span['start'])
        if (not candidates or candidates[0]['start']!=start or candidates[-1]['end']!=end
                or any(left['end']!=right['start'] for left,right in zip(candidates,candidates[1:]))):
            return selected
        normalized.extend(span['id'] for span in candidates)
    return list(dict.fromkeys(normalized))


def prompt_source_spans(source,ids):
    """Expose stable span addresses without repeating already opened text."""
    return [{k:v for k,v in span.items() if k!='preview'} for span in source_spans(source,ids)]


def prompt_resource_catalog(entries):
    """Remove storage-only hashes while retaining every model-useful address."""
    storage_only={'blob','snapshot_blob','resource_id'}
    return [{k:v for k,v in entry.items() if k not in storage_only} for entry in entries]


def prompt_active_plan_catalog(entries, assigned_ids, opened_resources):
    """Keep image references for source and currently opened pages only.

    Completed planning partitions archive external resource metadata for audit
    and later reuse. Their page image lists are useful only when that page is
    part of the current source group or has been opened in the current planning
    session; replaying every historical list bloats each later request.
    """
    current=set(assigned_ids)
    opened={address.get('id') for item in opened_resources
            for address in item.get('addresses',[]) if address.get('id')}
    result=prompt_resource_catalog(entries)
    for entry in result:
        if (entry.get('kind')=='external' and entry.get('id') not in current
                and entry.get('id') not in opened):
            entry.pop('image_refs',None)
    return result


def preceding_plan_context(plans):
    """Carry cross-partition identities without replaying completed plans."""
    # The complete prior plans remain in the server-side validator.  The model
    # only needs compact identities plus the latest transition context; replaying
    # every abbreviation record and dependency chain eventually exceeds a
    # relay's stable request size on otherwise moderate documents.
    concept_keys={'id','name','chinese_name','english_name','requires'}
    node_keys={'id','title','requires_concepts','establishes_concepts'}
    return [dict(
        concepts=[{k:v for k,v in concept.items() if k in concept_keys}
                  for concept in plan.get('concepts',[])],
        nodes=[{k:v for k,v in node.items() if k in node_keys}
               for node in plan.get('nodes',[])[-2:]],
    ) for plan in plans]


def writer_node_context(node, *, core_rewrite=False):
    """Keep writer decisions while removing repeated batch-planning prose."""
    result=copy.deepcopy(node)
    section_keys={'id','title','heading_level','purpose','source_ids','obligation_ids',
                  'requires_concepts','establishes_concepts','explanation'}
    result['section_outline']=[{k:v for k,v in section.items() if k in section_keys}
                               for section in (result.get('section_outline') or [])]
    if core_rewrite:
        for item in [result,*result['section_outline']]:
            if isinstance(item.get('explanation'),dict):
                item['explanation'].pop('reasoning_steps',None)
    for key in ('depends_on','cross_batch_risks'):
        if not result.get(key):result.pop(key,None)
    return result


def writer_concepts_for_payload(concepts, *, core_rewrite):
    """Keep planned identities without authorizing their draft definitions."""
    result=copy.deepcopy(concepts)
    if core_rewrite:
        for concept in result:
            concept.pop('definition',None)
    return result


def writer_reasoning_coverage(node, *, core_rewrite):
    return {} if core_rewrite else {
        'reasoning_step_coverage':list(node.get('explanation',{}).get('reasoning_steps',[]))}


def prompt_obligations(obligations, *, reference_link_ids=(), reference_label_ids=()):
    """Use opened source text once; keep every planned semantic obligation."""
    links=set(reference_link_ids);labels=set(reference_label_ids)
    result=[]
    for obligation in obligations:
        projected={k:v for k,v in obligation.items() if k!='quote'}
        if obligation['source_id'] in links:
            projected['meaning']='Preserve this original reference label and URL in place; the link itself covers this obligation, without describing its destination.'
        elif obligation['source_id'] in labels:
            projected['meaning']='Present only this source-provided related-resource label and its original link where present; no term definition or target summary is authorized.'
        result.append(projected)
    return result


def rebind_single_source_spans(value,source,assigned):
    """Bind a single-span object's unambiguous source identity."""
    result=copy.deepcopy(value);repairs=[]
    by_source={}
    for span in source_spans(source,assigned):by_source.setdefault(span['source_id'],[]).append(span['id'])
    for obligation in result.get('obligations',[]):
        sid=obligation.get('source_id')
        valid=by_source.get(sid,[])
        if len(valid)!=1:continue
        given=obligation.get('source_span_ids',[])
        if not given or (len(given)==1 and given[0]!=valid[0] and given[0].startswith(sid+':')):
            obligation['source_span_ids']=valid
            repairs.append(dict(obligation_id=obligation['id'],source_id=sid,
                                submitted_span_id=given[0] if given else None,actual_span_id=valid[0],
                                operation='single_source_span_offset_alignment'))
    return result,repairs


def writing_batch_contract(contract,node,goal):
    """Use the merged writing batch, not the first planner partition, as scope."""
    result=copy.deepcopy(contract)
    result['purpose']=goal or contract.get('purpose','')
    result['scope_boundary']=(
        '当前写作批次仅处理 node.source_ids 中的原对象；该列表是相邻规划分区合并后的完整范围。'
        '此前任一单独规划分区的范围说明不能排除本批次已经列出的对象。'
        '尚未列入本批次的原对象留待后续批次，不提前展开。')
    return result


def reject_empty_claimed_blocks(draft):
    for block in draft.get('blocks',[]):
        if block.get('obligation_ids') and not block.get('markdown','').strip():
            raise Conflict('承担原文义务的段落不能为空：'+block['id'])


def filter_future_partition_link_briefs(value,link_ids):
    """Keep briefs only for actual links assigned to this partition."""
    result=copy.deepcopy(value)
    selected=set(link_ids)
    deferred=[brief['source_id'] for brief in result.get('link_briefs',[])
              if brief['source_id'] not in selected]
    if deferred:
        result['link_briefs']=[brief for brief in result['link_briefs']
                               if brief['source_id'] in selected]
    return result,deferred


def strip_editorial_link_limits(value):
    """Do not turn planner instructions about article scope into source claims."""
    result=copy.deepcopy(value);removed=[]
    pattern=re.compile(r'本页改写|当前节点|只需说明|不搬入|不把目标页|不展开目标页|不重复展开|本页不再重复')
    for brief in result.get('link_briefs',[]):
        limit=brief.get('limitation','')
        if limit and pattern.search(limit):
            removed.append(dict(source_id=brief['source_id'],reason='editorial_scope_not_source_limit'))
            brief['limitation']=''
    return result,removed


def missing_catalogued_numeronyms(source, assigned, concepts, responsibilities=None):
    """Check names that the reader may see in authored rewrite prose."""
    from .terminology import CATALOG
    known={entry['abbr'].casefold() for entry in CATALOG if re.fullmatch(
        r'[A-Za-z][0-9]{1,2}[A-Za-z]',entry.get('abbr',''))}
    objects={obj['id']:obj for obj in source['objects']}
    def needs_name_plan(sid):
        obj=objects[sid]
        if obj['kind'] not in {'text','heading','link'}:
            return False
        if responsibilities is None:
            return True
        duty=responsibilities.get(sid)
        if duty is None:
            return True
        if not duty['present'] and not duty['explain']:
            return False
        # A preserved reference label is not an authored explanation of its
        # destination, even when the label itself is displayed.
        return obj['kind']!='link' or duty['explain']
    used={match.group().casefold() for sid in assigned if needs_name_plan(sid)
        for match in re.finditer(
            r'(?<![A-Za-z0-9])[A-Za-z][0-9]{1,2}[A-Za-z](?![A-Za-z0-9])',
            objects[sid].get('text',''))}
    declared={a['short'].casefold() for concept in concepts for a in concept['abbreviations']}
    return sorted((used & known)-declared)


def validate_names(plan, resources, allow_unverified_downgrade=False):
    """Evidence addresses are checked mechanically; meaning remains a review duty."""
    for concept in plan['concepts']:
        if not concept['chinese_name'].strip():raise ValueError('概念缺少中文名称：'+concept['id'])
        if concept['naming_status']=='unsearched':
            if not allow_unverified_downgrade:
                raise ValueError('术语名称尚未查证：'+concept['id'])
            concept.update(english_name='',naming_status='ambiguous',name_evidence=[],
                abbreviations=[],naming_note='原文名称尚未查证；正文只保留原文字面内容，不补写未经证实的全称。',
                naming_status_reason='unsearched_model_name_was_removed')
        if concept['naming_status']=='verified':
            if not concept['english_name'].strip() or not concept['name_evidence']:
                if not allow_unverified_downgrade:
                    raise ValueError('已核实术语缺少英文或来源：'+concept['id'])
                concept.update(english_name='',naming_status='ambiguous',name_evidence=[],
                    abbreviations=[],
                    naming_note='当前材料没有提供可核对的英文名称来源；正文只保留原文字面内容。',
                    naming_status_reason='incomplete_verified_name_was_removed')
                continue
            evidence=[];invalid_evidence=False
            for item in concept['name_evidence']:
                key=item['resource_id']
                exact=exact_source_quote(item['quote'],resources.text(key)) if key in resources.state['entries'] else None
                if exact is None:
                    # The model sometimes cites a fetched related page while
                    # copying an exact line from the assigned original. Repair
                    # the address only when that line has one source location.
                    matches=[(sid,found) for sid in concept.get('source_ids',[])
                             if sid in resources.state['entries']
                             for found in [exact_source_quote(item['quote'],resources.text(sid))]
                             if found is not None]
                    if len(matches)==1:
                        key,exact=matches[0]
                        item['resource_id']=key
                if (exact is None and key in resources.state['entries']
                        and resources.state['entries'][key].get('scope')=='name_evidence_excerpt'
                        and concept['english_name'].casefold() in item['quote'].casefold()):
                    original=resources.text(key)
                    phrase=r'\s+'.join(re.escape(part) for part in concept['english_name'].split())
                    hit=re.search(phrase,original,re.I) if phrase else None
                    if hit:
                        exact=original[max(0,hit.start()-80):min(len(original),hit.end()+160)]
                if exact is None:
                    if not allow_unverified_downgrade:
                        raise ValueError('术语名称证据不在已保存来源中：'+concept['id'])
                    concept.update(english_name='',naming_status='ambiguous',name_evidence=[],
                        abbreviations=[],
                        naming_note='术语名称证据无法在已保存来源中核对；正文只保留原文字面内容。',
                        naming_status_reason='invalid_name_evidence_was_removed')
                    invalid_evidence=True
                    break
                item['quote']=exact
                evidence.append(' '.join(exact.casefold().split()))
            if invalid_evidence:continue
            names=[(concept['english_name'],None)]+[(a['english'],a['short']) for a in concept['abbreviations']]
            for name,short in names:
                normalized=' '.join(name.casefold().split())
                if not normalized:raise ValueError('术语英文名称为空：'+concept['id'])
                if any(normalized in q or coordinated_name_in_quote(name,q)
                       or markup_element_name_in_quote(name,q)
                       or coordinated_color_components_in_quote(name,q)
                       or markup_property_pair_in_quote(name,q)
                       or documented_code_name_in_quote(name,q)
                       for q in evidence):continue
                # A planner can cite a plural occurrence while the exact named
                # form is already present in another source it assigned to this
                # same concept. Reuse that real passage; do not lemmatize a name
                # into an English form that the saved material never contains.
                original=next((sid for sid in concept.get('source_ids',[])
                    if sid in resources.state['entries'] and normalized in
                    ' '.join(resources.text(sid).casefold().split())),None)
                if original:
                    quote=resources.text(original)
                    concept['name_evidence'].append(dict(resource_id=original,quote=quote))
                    evidence.append(' '.join(quote.casefold().split()))
                    continue
                # Reuse an existing primary-source glossary pairing only when
                # its exact name and abbreviation agree; never infer a new name.
                entry=next((e for e in resources.state['entries'].values()
                            if e.get('scope')=='name_evidence_excerpt'
                            and e.get('english_name','').casefold()==name.casefold()
                            and (short is None or e.get('abbreviation')==short)),None)
                quote=resources.text(entry['id']) if entry else ''
                if not entry or normalized not in ' '.join(quote.casefold().split()):
                    from .terminology import CATALOG
                    official=next((item for item in CATALOG
                                   if item['en'].casefold()==name.casefold()
                                   and (short is None or item.get('abbr')==short)),None)
                    if official:
                        receipt=resources.execute(dict(kind='page',url=official['url'],resource_id=''))
                        official_entry=next((e for e in resources.state['entries'].values()
                                             if e.get('original_url')==official['url']),None)
                        official_text=resources.text(official_entry['id']) if official_entry else ''
                        hit=re.search(r'\s+'.join(re.escape(part) for part in name.split()),
                                      official_text,re.I)
                        if receipt.get('status')!='unavailable' and hit:
                            entry=official_entry
                            quote=official_text[max(0,hit.start()-80):min(len(official_text),hit.end()+160)]
                    if not entry or normalized not in ' '.join(quote.casefold().split()):
                        if not allow_unverified_downgrade:
                            raise ValueError('英文名称或缩写展开没有对应的原文证据：'+concept['id'])
                        # Do not let an unsupported model-supplied expansion
                        # block the whole document.  Downgrade the name claim
                        # to an explicit unresolved state and keep the source
                        # abbreviation in its ordinary source obligation.
                        concept.update(english_name='',naming_status='ambiguous',
                            name_evidence=[],abbreviations=[],
                            naming_note='原文没有提供可核对的英文名称或缩写展开；正文不得补写未经证实的展开。',
                            naming_status_reason='unsupported_model_name_was_removed')
                        break
                item=dict(resource_id=entry['id'],quote=quote)
                if item not in concept['name_evidence']:concept['name_evidence'].append(item)
                evidence.append(' '.join(quote.casefold().split()))
        elif concept['naming_status']=='not_applicable':
            if concept.get('english_name','').strip() or concept.get('abbreviations'):
                if not allow_unverified_downgrade:
                    raise ValueError('名称不适用不能同时声明英文名称或缩写：'+concept['id'])
                concept.update(english_name='',abbreviations=[],name_evidence=[],
                    naming_status='ambiguous',
                    naming_note='当前材料没有提供足够的名称证据；正文只保留原文字面内容。',
                    naming_status_reason='unsupported_not_applicable_name_was_removed')
            # A Chinese formal concept is not proved to lack an English name
            # merely because a planner says so.  Either verify it, retain an
            # explicit unresolved lookup, or remove an ordinary organizing
            # phrase from the formal-concept ledger.
            if re.search(r'[\u3400-\u9fff]',concept.get('chinese_name','')):
                if not allow_unverified_downgrade:
                    raise ValueError('中文正式概念不能仅凭模型标为没有英文名称：'+concept['id'])
                concept.update(naming_status='ambiguous',
                    naming_note='当前材料不能证明该名称没有英文对应；正文不补写未经证实的英文名称。',
                    naming_status_reason='not_applicable_claim_was_not_proven')
        elif not concept['naming_note'].strip():
            if not allow_unverified_downgrade:
                raise ValueError('未确认名称需要保留具体查证缺口：'+concept['id'])
            concept['naming_note']='当前材料没有提供足够的名称证据；正文只保留原文字面内容。'
            concept['naming_status_reason']=concept.get('naming_status_reason') or 'source_name_evidence_missing'
    return plan


def missing_concept_names(concepts, draft):
    """List missing verified names so one review can repair them together."""
    text=canonical(draft).casefold()
    missing=[]
    for concept in concepts:
        if concept.get('naming_status')!='verified':continue
        # Chinese wording is reviewed semantically; a planner's awkward label
        # must not become an immutable phrase the writer is forced to copy.
        names=[concept['english_name']]
        names += [v for a in concept['abbreviations']
                  for v in (a['short'],a['chinese'],a['english'])]
        absent=[name for name in names if name.casefold() not in text]
        first_use_missing=False
        if concept.get('requires_first_use_pair'):
            chinese=concept.get('chinese_name','').strip()
            authored=[block for block in draft.get('blocks',[])
                      if block['kind'] not in {'source','object','document_info'}
                      and not block.get('embedded_object_ids') and chinese in block['markdown']]
            if not authored or authored[0]['markdown'].count(chinese)!=1:
                first_use_missing=True
            else:
                markdown=authored[0]['markdown'];name_start=markdown.index(chinese)
                name_end=name_start+len(chinese)
                paren=re.match(r'\s*（([^（）()]*)）',markdown[name_end:])
                inside=paren.group(1).strip() if paren else ''
                english=concept.get('english_name','').strip()
                before=markdown[:name_start]
                shorts=[item.get('short','').strip() for item in concept.get('abbreviations',[])
                        if item.get('short','').strip()]
                abbreviation_prefix=(' '.join(shorts)+' ') if shorts else ''
                has_prefix=(not abbreviation_prefix or before.casefold().endswith(abbreviation_prefix.casefold()))
                first_use_missing=(not has_prefix or inside!=english)
        if absent or first_use_missing:
            reported=absent or [concept.get('first_use_display') or concept['english_name']]
            missing.append(dict(concept_id=concept['id'],names=reported,
                chinese_name=concept['chinese_name'],source_ids=concept.get('source_ids',[]),
                first_use_missing=first_use_missing))
    return missing


def insert_verified_name_at_unique_first_use(draft, delta, concepts):
    """Add a verified English label only at an unambiguous authored Chinese use."""
    receipts=[]
    existing=canonical(draft).casefold()
    for concept in concepts:
        chinese=concept.get('chinese_name','').strip()
        english=concept.get('english_name','').strip()
        is_completion=bool(concept.get('requires_first_use_pair'))
        if (concept.get('naming_status')!='verified' or not chinese or not english or
                (not is_completion and english.casefold() in existing) or
                not re.search(r'[\u3400-\u9fff]',chinese)):
            continue
        matches=[block for block in draft['blocks']
                 if block['kind'] not in {'source','object','document_info'}
                 and not block.get('embedded_object_ids')
                 and chinese in block['markdown']]
        if not matches:continue
        block=matches[0]
        if block['markdown'].count(chinese)!=1:continue
        name_start=block['markdown'].index(chinese)
        name_end=name_start+len(chinese)
        if is_completion:
            abbreviations=[item.get('short','').strip() for item in concept.get('abbreviations',[])
                           if item.get('short','').strip()]
            markdown=block['markdown']
            paren=re.match(r'\s*[（(]([^（）()]*)[）)]',markdown[name_end:])
            if paren and paren.group(1).strip()!=english:continue
            if re.match(r'\s*[（(]',markdown[name_end:]) and not paren:continue
            abbreviation_prefix=(' '.join(abbreviations)+' ') if abbreviations else ''
            before=markdown[:name_start]
            has_prefix=bool(abbreviation_prefix and
                before.casefold().endswith(abbreviation_prefix.casefold()))
            existing_prefix=before[-len(abbreviation_prefix):] if has_prefix else ''
            if (has_prefix and paren and paren.group(1).strip()==english
                    and paren.group(0).strip().startswith('（')):
                continue
            previous=markdown[name_start-1] if name_start else ''
            tight_openers='([{（［｛<〈《「『【“‘*_`'
            inserted_prefix=(' ' if previous and not previous.isspace() and previous not in tight_openers else '')+abbreviation_prefix
            if paren:
                old_suffix=paren.group(0)
            else:
                old_suffix=''
            start=name_start-len(existing_prefix)
            end=name_end+len(old_suffix)
            old=markdown[start:end]
            replacement=(existing_prefix or inserted_prefix)+chinese+'（'+english+'）'
            block['markdown']=markdown[:start]+replacement+markdown[end:]
        else:
            if block['markdown'][name_end:name_end+1]=='（':continue
            replacement=chinese+'（'+english+'）'
            old=chinese
            block['markdown']=block['markdown'].replace(old,replacement,1)
        for evidence in delta.get('concept_evidence',[]):
            if (evidence['block_id']==block['id'] and old in evidence['output_quote']):
                evidence['output_quote']=evidence['output_quote'].replace(old,replacement,1)
        receipts.append(dict(concept_id=concept['id'],block_id=block['id'],
            operation=('complete_verified_name_and_abbreviations_at_unique_first_use'
                       if is_completion else 'insert_verified_english_name_at_unique_first_use')))
        existing=canonical(draft).casefold()
    return receipts


def concept_presence(concepts, draft):
    """Final gate: a reviewed draft still cannot omit verified names."""
    missing=missing_concept_names(concepts,draft)
    if missing:raise ValueError('已核实的术语名称或缩写展开在正文中遗漏：'+missing[0]['concept_id'])


def retire_refuted_formal_concepts(job,candidate):
    """Do not resurrect a definition the independent review disproved and removed."""
    concepts={c['id']:c for part in job.get('active_plans',[]) for c in part['concepts']}
    retired=[]
    for cid,concept in concepts.items():
        for review in candidate.get('content_reviews',[]):
            finding=next((f for f in review.get('findings',[])
                if concept['chinese_name'] in f.get('problem','')
                and ('不对应' in f['problem'] or '不准确' in f['problem'])
                and '删除' in f.get('required_change','')),None)
            if not finding:continue
            removed=any(edit['block_id']==finding['block_id']
                and edit['old_text'].lstrip().startswith('- '+concept['chinese_name']+'（')
                and concept['english_name'] in edit['old_text']
                and not edit['new_text'].strip()
                for patch in candidate.get('patch_history',[]) for edit in patch['edits'])
            if removed:retired.append(cid);break
    if not retired:return []
    retired=set(retired)
    for plan in job['active_plans']:
        plan['concepts']=[c for c in plan['concepts'] if c['id'] not in retired]
        for concept in plan['concepts']:
            concept['requires']=[cid for cid in concept['requires'] if cid not in retired]
        for node in plan['nodes']:
            for field in ('requires_concepts','establishes_concepts'):
                node[field]=[cid for cid in node[field] if cid not in retired]
    for node in job.get('writing_batches',[]):
        for field in ('requires_concepts','establishes_concepts'):
            node[field]=[cid for cid in node[field] if cid not in retired]
    delta=candidate['delta']
    delta['established_concepts']=[cid for cid in delta['established_concepts'] if cid not in retired]
    delta['concept_evidence']=[e for e in delta['concept_evidence'] if e['concept_id'] not in retired]
    retired_names={concepts[cid]['english_name'] for cid in retired}
    candidate['unresolved_content_findings']=[finding for finding in
        candidate.get('unresolved_content_findings',[])
        if not ('已查证术语在实际正文中缺少名称' in finding.get('problem','')
                and any(name in finding['problem'] for name in retired_names))]
    job['quality_issues']=[issue for issue in job.get('quality_issues',[])
        if not ('已查证术语在实际正文中缺少名称' in issue
                and any(name in issue for name in retired_names))]
    receipts=[dict(concept_id=cid,reason='independent_review_refuted_name_pair_and_exact_patch_deleted_definition')
              for cid in sorted(retired)]
    job.setdefault('retired_formal_concepts',[]).extend(receipts)
    return receipts


def normalize_local_proposal(proposal,draft,literals):
    """Keep an otherwise useful repair when a model includes an invalid edit."""
    literals=tuple(literals or ())
    blocks={b['id']:b for b in draft['blocks']}
    normalized=[];rejected=[]
    for edit in proposal['edits']:
        old,new=edit['old_text'],edit['new_text']
        if old==new:
            rejected.append(dict(block_id=edit['block_id'],reason='no_change'))
            continue
        if not old:
            rejected.append(dict(block_id=edit['block_id'],reason='empty_patch_target'))
            continue
        block=blocks.get(edit['block_id'],{})
        before=block.get('markdown','')
        if any(literal and literal in old and literal not in new for literal in literals):
            rejected.append(dict(block_id=edit['block_id'],reason='protected_original_changed'))
            continue
        spans=_protected_literal_spans(before,literals)
        start=0;overlaps=False
        while old:
            start=before.find(old,start)
            if start<0:break
            end=start+len(old)
            if any(start<literal_end and end>literal_start
                   for literal_start,literal_end in spans):
                overlaps=True
                break
            start+=1
        if overlaps:
            rejected.append(dict(block_id=edit['block_id'],reason='protected_literal_overlap'))
            continue
        if (block.get('kind')=='object'
                and _has_authored_object_text(block,literals)
                and (_HYBRID_OBJECT_MARKUP.search(old)
                     or _HYBRID_OBJECT_MARKUP.search(new))):
            rejected.append(dict(block_id=edit['block_id'],reason='protected_object_markup_edit'))
            continue
        inline_code=re.findall(r'(?<!`)`[^`\n]+`(?!`)',old)
        if any(new.count(code)<old.count(code) for code in set(inline_code)):
            rejected.append(dict(block_id=edit['block_id'],reason='authored_inline_code_removed'))
            continue
        if '\n' not in old or before.count(old)!=1:
            normalized.append(edit);continue
        lines=[line for line in old.split('\n') if line]
        if not lines or any(before.count(line)!=1 for line in lines):
            normalized.append(edit);continue
        normalized.append(edit|dict(old_text=lines[0],new_text=new))
        normalized.extend(edit|dict(old_text=line,new_text='',
                                    reason=edit['reason']+'（删除已并入上一行的原子行）')
                          for line in lines[1:])
    return proposal|{'edits':normalized},rejected


def heading_level_skips(draft):
    """Count newly introduced heading-level jumps in authored Markdown."""
    from markdown_it import MarkdownIt
    levels=[int(token.tag[1:]) for token in MarkdownIt('commonmark').parse(canonical(draft))
            if token.type=='heading_open']
    return sum(right>left+1 for left,right in zip(levels,levels[1:]))


def bind_planned_headings(result,node):
    """Use approved Chinese section titles when a writer copied an English one."""
    headings={n['id']:n['title'] for n in node.get('section_outline',[]) or [node]
              if re.search(r'[\u3400-\u9fff]',n.get('title',''))}
    changed=[]
    for block in result['blocks']:
        section=next((sid for sid in headings if block['id'].startswith(sid+'-')),None)
        if not section:continue
        lines=block['markdown'].splitlines(keepends=True)
        for index,line in enumerate(lines):
            match=re.match(r'^(#{2,6}) ([^\n]+)(\n?)$',line)
            if not match or not re.search(r'[A-Za-z]',match[2]) or re.search(r'[\u3400-\u9fff]',match[2]):continue
            replacement=match[1]+' '+headings[section]+match[3]
            changed.append(dict(block_id=block['id'],old=line.rstrip('\n'),new=replacement.rstrip('\n'),
                                planned_section_id=section))
            lines[index]=replacement
        block['markdown']=''.join(lines)
    return changed


def heading_repair_context(result,node):
    """Return only invalid headings and enough nearby meaning to rename them."""
    sections={n['id']:n for n in node.get('section_outline',[]) or [node]}
    rows=[]
    blocks=result.get('blocks',[])
    for index,block in enumerate(blocks):
        section=next((sid for sid in sections if block['id'].startswith(sid+'-')),node['id'])
        following=next((other['markdown'] for other in blocks[index+1:]
                        if other.get('kind')=='explanation'), '')
        for heading in re.findall(r'(?m)^#{1,6} [^\n]+',block.get('markdown','')):
            title=heading.split(' ',1)[1]
            if re.search(r'[A-Za-z]',title) and not re.search(r'[\u3400-\u9fff]',title):
                planned=sections.get(section,node)
                rows.append(dict(block_id=block['id'],old_heading=heading,
                    planned_title=planned.get('title',''),purpose=planned.get('purpose',''),
                    nearby_context=following[:1200]))
    return rows


def apply_heading_repairs(result,expected,repairs):
    """Apply exact one-line heading edits without touching any article prose."""
    wanted={(row['block_id'],row['old_heading']) for row in expected}
    edits=repairs.get('edits',[])
    if {(row['block_id'],row['old_heading']) for row in edits}!=wanted:
        raise ValueError('标题小块修复没有逐项对应全部违规标题')
    blocks={block['id']:block for block in result['blocks']}
    changed=[]
    for edit in edits:
        block=blocks.get(edit['block_id'])
        old=edit['old_heading'];new=edit['new_heading'].strip()
        old_match=re.fullmatch(r'(#{1,6}) ([^\n]+)',old)
        new_match=re.fullmatch(r'(#{1,6}) ([^\n]+)',new)
        if (not block or block['markdown'].count(old)!=1 or not old_match or not new_match
                or old_match[1]!=new_match[1] or not re.search(r'[\u3400-\u9fff]',new_match[2])):
            raise ValueError('标题小块修复改变了层级、范围或仍缺少自然中文')
        block['markdown']=block['markdown'].replace(old,new,1)
        for binding in result.get('coverage',[]):
            if binding['block_id']==block['id'] and old in binding['output_quote']:
                binding['output_quote']=binding['output_quote'].replace(old,new,1)
        for binding in result.get('knowledge_delta',{}).get('concept_evidence',[]):
            if binding['block_id']==block['id'] and old in binding['output_quote']:
                binding['output_quote']=binding['output_quote'].replace(old,new,1)
        changed.append(dict(block_id=block['id'],old_heading=old,new_heading=new))
    return changed


def validate_plan(value, source, assigned, prior=(), mode='rewrite', node_limit=6500,
                  require_spans=False,resources=None,require_link_briefs=False,archived_ids=(),
                  concept_limit=7,require_object_responsibilities=False,
                  classified_link_decisions=None,default_reference_links=False):
    from .production import bind_evidence_layout
    value=bind_evidence_layout(value,source)
    # Some structured-output relays append nullable ``*_dummy`` placeholders
    # while satisfying a large nested schema.  A null placeholder carries no
    # user or source content, so remove only that exact transport artifact
    # before strict validation; every non-null or normally named field remains
    # subject to the schema and correction workflow.
    def strip_null_dummy_fields(item):
        if isinstance(item,dict):
            return {key:strip_null_dummy_fields(child) for key,child in item.items()
                    if not (key.endswith('_dummy') and child is None)}
        if isinstance(item,list):return [strip_null_dummy_fields(child) for child in item]
        return item
    value=strip_null_dummy_fields(value)
    plan = A.CompositionPlan.model_validate(value).model_dump()
    objects = {o['id']: o for o in source['objects']}
    assigned = set(assigned)
    if default_reference_links:
        for responsibility in plan['object_responsibilities']:
            if (responsibility['source_id'] in assigned and
                    objects[responsibility['source_id']]['kind']=='link'):
                responsibility['explain']=False
    if require_object_responsibilities:
        decisions=plan['object_responsibilities']
        if len(decisions)!=len(assigned) or {item['source_id'] for item in decisions}!=assigned:
            raise ValueError('每个当前原对象必须有一次独立的正文展示与解释判断')
    reference_link_ids={item['source_id'] for item in plan.get('object_responsibilities',[])
        if item['source_id'] in assigned and not item['explain']
        and objects[item['source_id']]['kind']=='link'} if require_object_responsibilities else set()
    if default_reference_links:
        reference_link_ids.update(sid for sid in assigned if objects[sid]['kind']=='link')
        plan,_=prune_reference_link_evidence(plan,reference_link_ids)
    if classified_link_decisions is not None:
        responsibilities={item['source_id']:item for item in plan['object_responsibilities']}
        gaps=plan['evidence_gaps']
        for sid,decision in classified_link_decisions.items():
            if sid not in assigned:continue
            expected=decision['role']=='semantic_dependency'
            if responsibilities[sid]['explain']!=expected:
                raise ValueError('最终链接职责与取材前的分类不一致：'+sid)
        for sid in assigned:
            obj=objects[sid]
            if obj['kind']!='link' or not responsibilities[sid]['explain']:continue
            if not any(gap['source_id']==sid and gap['missing'].strip() for gap in gaps):
                raise ValueError('语义依赖链接必须声明当前原文缺失的具体意义：'+sid)
    archived=set(archived_ids)-assigned
    if archived:
        from .visual_sources import decorative_resource
        if not archived<=set(objects) or any(not decorative_resource(objects[sid]) for sid in archived):
            raise ValueError('只能排除有来源结构证据的归档对象')
        removed={o['id'] for o in plan['obligations'] if o['source_id'] in archived}
        plan['obligations']=[o for o in plan['obligations'] if o['id'] not in removed]
        plan['link_briefs']=[brief for brief in plan['link_briefs'] if brief['source_id'] not in archived]
        for node in plan['nodes']:
            node['source_ids']=[sid for sid in node['source_ids'] if sid not in archived]
            node['obligation_ids']=[fid for fid in node['obligation_ids'] if fid not in removed]
        dropped={node['id'] for node in plan['nodes'] if not node['source_ids'] and not node['obligation_ids']}
        plan['nodes']=[node for node in plan['nodes'] if node['id'] not in dropped]
        for node in plan['nodes']:
            node['depends_on']=[dependency for dependency in node['depends_on'] if dependency not in dropped]
        removed_concepts={concept['id'] for concept in plan['concepts'] if set(concept['source_ids'])<=archived}
        plan['concepts']=[concept for concept in plan['concepts'] if concept['id'] not in removed_concepts]
        for node in plan['nodes']:
            node['requires_concepts']=[cid for cid in node['requires_concepts'] if cid not in removed_concepts]
            node['establishes_concepts']=[cid for cid in node['establishes_concepts'] if cid not in removed_concepts]
        for concept in plan['concepts']:
            concept['source_ids']=[sid for sid in concept['source_ids'] if sid not in archived]
    merge_adjacent_heading_only_nodes(plan,objects)
    from urllib.parse import unquote,urlsplit
    # Uploaded PDF/DOCX/Markdown sources store source_url=None. urlsplit(None)
    # returns byte fields, which cannot be compared with parsed text links.
    current_page=urlsplit(source.get('source_url') or '')
    # Article bylines often expose the same author profile twice: an empty
    # avatar anchor and a labelled author-name anchor.  A profile URL in this
    # exact duplicate shape is source metadata, not a knowledge link whose
    # destination must be researched and explained.  Resolve it
    # deterministically so a planner cannot oscillate between navigation and
    # content across correction rounds.
    profile_targets={}
    for sid in assigned:
        obj=objects.get(sid,{})
        if obj.get('kind')!='link' or not obj.get('target'):continue
        profile_targets.setdefault(obj['target'],[]).append(obj)
    byline_targets=set()
    for target,items in profile_targets.items():
        parsed=urlsplit(target)
        profile_path=bool(re.match(r'^/@[^/]+/?$',parsed.path)
                          or re.search(r'/(?:author|authors|user|users)/[^/]+/?$',parsed.path,re.I))
        if (profile_path and len(items)>1 and
                any(not item.get('text','').strip() for item in items) and
                any(item.get('text','').strip() for item in items)):
            byline_targets.add(target)
    for brief in plan['link_briefs']:
        obj=objects.get(brief['source_id'],{})
        target=urlsplit(obj.get('target',''))
        decoded_target=unquote(obj.get('target','')).lower()
        figure_image_link=(not obj.get('text','').strip()
            and '/figure[' in obj.get('locator','')
            and bool(re.search(r'\.(?:avif|gif|jpe?g|png|svg|webp)(?:[?#]|$)',decoded_target)))
        path=target.path.rstrip('/').rsplit('/',1)[-1].lower()
        path_stem=path.rsplit('.',1)[0]
        label=obj.get('text','').strip().lower()
        same_page_anchor=(target.hostname==current_page.hostname and
            target.path.rstrip('/')==current_page.path.rstrip('/') and bool(target.fragment))
        if same_page_anchor:
            brief.update(role='navigation',topic='',connection='',destination='',
                         limitation='',evidence=[],unavailable_reason='')
        if obj.get('target') in byline_targets:
            brief.update(role='administrative',topic='',connection='',destination='',
                         limitation='',evidence=[],unavailable_reason='')
        if figure_image_link:
            brief.update(role='administrative',topic='',connection='',destination='',
                         limitation='',evidence=[],unavailable_reason='')
        if (target.hostname and target.hostname==current_page.hostname
            and current_page.path.startswith(target.path.rstrip('/')+'/')
            and any(word in label for word in ('home','index','tips','目录','主页'))):
            brief.update(role='navigation',topic='',connection='',destination='',
                         limitation='',evidence=[],unavailable_reason='')
        if ((path_stem in {'donate','donation','contribute','contributing','contact','privacy','terms','license'}
             or path_stem.startswith('donat') or target.fragment.lower() in {'feedback','contact'}) and
            any(word in label for word in ('donat','contribut','contact','privacy','terms','license',
                                            'feedback','report an error','report error'))):
            brief.update(role='administrative',topic='',connection='',destination='',
                         limitation='',evidence=[],unavailable_reason='')
        if 'site-comments' in target.path and any(word in label for word in ('comment','feedback','意见')):
            brief.update(role='administrative',topic='',connection='',destination='',
                         limitation='',evidence=[],unavailable_reason='')
        if (brief['source_id'] in reference_link_ids and
                brief['role'] in {'content','navigation'} and
                obj.get('source_scope') not in {'site_chrome','source_metadata'} and
                not same_page_anchor):
            brief.update(role='content',topic=obj.get('text','').strip() or obj.get('target',''),
                         connection='原文提供这一参考入口，保留链接文字与地址',
                         destination='',limitation='',evidence=[],unavailable_reason='')
            continue
        if (brief['role']=='navigation' and obj.get('source_scope') not in
                {'site_chrome','source_metadata'} and not same_page_anchor and not (
                target.hostname==current_page.hostname and
                current_page.path.startswith(target.path.rstrip('/')+'/') and
                any(word in label for word in ('home','index','tips','目录','主页')))):
            resource_id=(resources.state.get('url_index',{}).get(
                canonical_url(obj.get('target',''))) if resources else None)
            unavailable=next((item for item in reversed(resources.state.get('reads',[]))
                if item.get('status')=='unavailable' and
                canonical_url(item.get('url',''))==canonical_url(obj.get('target',''))),None) if resources else None
            # Repair a mistaken navigation classification by reading the actual
            # destination once.  This is a bounded, evidence-producing fallback
            # and avoids either aborting the document or inventing an explanation
            # from the anchor label alone.
            if resources and not resource_id and not unavailable and obj.get('target'):
                resources.execute({'kind':'page','url':obj['target']})
                resource_id=resources.state.get('url_index',{}).get(
                    canonical_url(obj['target']))
                unavailable=next((item for item in reversed(resources.state.get('reads',[]))
                    if item.get('status')=='unavailable' and
                    canonical_url(item.get('url',''))==canonical_url(obj.get('target',''))),None)
            entry=resources.state.get('entries',{}).get(resource_id,{}) if resource_id else {}
            if entry.get('kind')=='external':
                evidence=resources.text(resource_id).strip()
                if evidence:
                    quote=evidence[:min(600,len(evidence))]
                    lines=[line.strip() for line in quote.splitlines() if line.strip()]
                    brief.update(role='content',topic=obj.get('text','').strip() or lines[0],
                        connection='原文在当前位置提供该链接，用于补充当前表述所依据的外部内容',
                        destination='；'.join(lines[:3])[:500],limitation=brief.get('limitation',''),
                        evidence=[dict(resource_id=resource_id,quote=quote)],unavailable_reason='')
                    continue
            if unavailable:
                brief.update(role='content',topic=obj.get('text','').strip() or obj.get('target',''),
                    connection='原文在当前位置提供该内容链接，链接文字与地址均需保留',
                    destination='',limitation='',evidence=[],
                    unavailable_reason=unavailable.get('reason','目标页未取得'))
                continue
            raise ValueError('正文知识链接自动读取后仍缺少可核对结果：'+brief['source_id'])
    obligations = {o['id']: o for o in plan['obligations']}
    validate_evidence_plan(plan, obligations, objects, assigned, resources)
    spans={s['id']:s for s in source_spans(source,assigned)}
    used_spans=set()
    unique([o['id'] for o in plan['obligations']], '义务')
    if {o['source_id'] for o in obligations.values()} != assigned:
        raise ValueError('规划必须完整覆盖当前分组的全部原对象，不能覆盖其他分组')
    for obligation in obligations.values():
        text = objects[obligation['source_id']]['text']
        selected=obligation.get('source_span_ids',[])
        if require_spans and not selected:raise ValueError('请用给定的原文片段编号，不要重新抄写引文：'+obligation['id'])
        if selected:
            selected=normalize_source_span_ids(selected,obligation['source_id'],spans)
            obligation['source_span_ids']=selected
            if any(s not in spans or spans[s]['source_id']!=obligation['source_id'] for s in selected):
                raise ValueError('义务片段编号不存在或属于另一原对象')
            ordered=sorted((spans[s] for s in set(selected)),key=lambda s:s['start'])
            obligation['quote']=''.join(text[s['start']:s['end']] for s in ordered)
            # Noncontiguous excerpts remain a list, not a falsely contiguous quote
            if any(a['end']!=b['start'] for a,b in zip(ordered,ordered[1:])):
                obligation['quote']=text[ordered[0]['start']:ordered[-1]['end']]
            used_spans.update(selected)
        if obligation['quote'] not in text or (text and not obligation['quote']):
            raise ValueError('规划引文不在原对象中：' + obligation['id'])
    if require_spans and used_spans!=set(spans):
        raise ValueError('还有未分配的原文片段：'+','.join(sorted(set(spans)-used_spans)))
    if require_link_briefs:
        link_ids={sid for sid in assigned if objects[sid]['kind']=='link'}
        briefs=plan['link_briefs']
        # Do not make a model citation slip fatal to the whole document.  For
        # each content link, fetch its literal destination once when necessary,
        # then bind the brief to bytes from that exact page.  The later semantic
        # reviewer still checks the destination explanation itself.
        if resources:
            for brief in briefs:
                if (brief.get('role')!='content' or
                        brief.get('source_id') in reference_link_ids):continue
                obj=objects.get(brief.get('source_id'),{})
                target=obj.get('target','')
                if not target:continue
                valid=bool(brief.get('evidence')) and all(
                    (entry:=resources.state['entries'].get(item.get('resource_id')))
                    and entry.get('kind')=='external'
                    and (entry.get('parent_page_url') or entry.get('original_url'))==target
                    and exact_source_quote(item.get('quote',''),resources.text(item['resource_id']))
                    for item in brief['evidence'])
                prior_failure=any(row.get('kind')=='page' and row.get('url')==target
                                  and row.get('status')=='unavailable'
                                  for row in resources.state.get('reads',[]))
                if not valid and not prior_failure:
                    resources.execute({'kind':'page','url':target})
            bind_prefetched_link_evidence(plan,source,resources,[{'source_id':sid} for sid in link_ids])
            briefs=plan['link_briefs']
        # Site navigation, footer links and author metadata have a verifiable
        # DOM role. Keep them in the source inventory, but do not spend a model
        # turn asking for invented destination explanations.
        planned_ids={item['source_id'] for item in briefs}
        for sid in sorted(link_ids-planned_ids):
            item=objects[sid];scope=item.get('source_scope');target=item.get('target','')
            parsed=urlsplit(target);path=parsed.path.casefold()
            path_stem=path.rstrip('/').rsplit('/',1)[-1].rsplit('.',1)[0]
            image_wrapper=(not item.get('text','').strip() and
                (parsed.hostname or '').casefold().endswith('substackcdn.com') or (not item.get('text','').strip() and
                 path.endswith(('.png','.jpg','.jpeg','.gif','.webp','.svg'))))
            duplicate_empty=(not item.get('text','').strip() and any(
                objects[other].get('target')==target and objects[other].get('text','').strip()
                for other in link_ids if other!=sid))
            administrative_link=(path_stem in {'donate','donation','contribute','contributing',
                'contact','privacy','terms','license'} or parsed.fragment.casefold() in {'feedback','contact'})
            if scope in {'site_chrome','source_metadata'} or image_wrapper or duplicate_empty or administrative_link:
                briefs.append(dict(source_id=sid,
                    role='administrative' if scope=='source_metadata' or image_wrapper or administrative_link else 'navigation',
                    topic='',connection='',destination='',limitation='',evidence=[],
                    unavailable_reason=''))
                continue
            same_page=(parsed.hostname==current_page.hostname and
                parsed.path.rstrip('/')==current_page.path.rstrip('/') and bool(parsed.fragment))
            if same_page or not target:
                briefs.append(dict(source_id=sid,role='navigation',topic='',connection='',
                    destination='',limitation='',evidence=[],unavailable_reason=''))
                continue
            if sid in reference_link_ids:
                briefs.append(dict(source_id=sid,role='content',
                    topic=item.get('text','').strip() or target,
                    connection='原文提供这一参考入口，保留链接文字与地址',
                    destination='',limitation='',evidence=[],unavailable_reason=''))
                continue
            if resources and parsed.scheme in {'http','https'}:
                resource_id=resources.state.get('url_index',{}).get(canonical_url(target))
                failure=next((row for row in reversed(resources.state.get('reads',[]))
                    if row.get('kind')=='page' and row.get('url')==target
                    and row.get('status')=='unavailable'),None)
                if not resource_id and not failure:
                    resources.execute({'kind':'page','url':target})
                    resource_id=resources.state.get('url_index',{}).get(canonical_url(target))
                    failure=next((row for row in reversed(resources.state.get('reads',[]))
                        if row.get('kind')=='page' and row.get('url')==target
                        and row.get('status')=='unavailable'),None)
                entry=resources.state.get('entries',{}).get(resource_id,{}) if resource_id else {}
                text=resources.text(resource_id).strip() if entry.get('kind')=='external' else ''
                if text:
                    quote=text[:min(700,len(text))]
                    lines=[line.strip() for line in quote.splitlines() if line.strip()]
                    briefs.append(dict(source_id=sid,role='content',
                        topic=item.get('text','').strip() or lines[0],
                        connection='原文在当前位置提供该链接，用于补充当前表述所依据的目标内容',
                        destination='；'.join(lines[:3])[:500],limitation='',
                        evidence=[dict(resource_id=resource_id,quote=quote)],unavailable_reason=''))
                    continue
                if failure:
                    briefs.append(dict(source_id=sid,role='content',
                        topic=item.get('text','').strip() or target,
                        connection='原文在当前位置提供该内容链接，链接文字与地址均需保留',
                        destination='',limitation='',evidence=[],
                        unavailable_reason=failure.get('reason','目标页未取得')))
        unique([item['source_id'] for item in briefs], '链接解释责任')
        if {item['source_id'] for item in briefs}!=link_ids:
            raise ValueError('每个原文链接都需要明确内容或导航职责')
        for brief in briefs:
            if brief['role']!='content':continue
            if brief['source_id'] in reference_link_ids:continue
            obj=objects[brief['source_id']]
            target=obj.get('target','')
            if (not brief.get('evidence') and not brief.get('unavailable_reason')
                    and resources and target):
                resource_id=resources.state.get('url_index',{}).get(canonical_url(target))
                failure=next((row for row in reversed(resources.state.get('reads',[]))
                    if row.get('kind')=='page' and row.get('url')==target
                    and row.get('status')=='unavailable'),None)
                if not resource_id and not failure:
                    resources.execute({'kind':'page','url':target})
                    resource_id=resources.state.get('url_index',{}).get(canonical_url(target))
                    failure=next((row for row in reversed(resources.state.get('reads',[]))
                        if row.get('kind')=='page' and row.get('url')==target
                        and row.get('status')=='unavailable'),None)
                entry=resources.state.get('entries',{}).get(resource_id,{}) if resource_id else {}
                text=resources.text(resource_id).strip() if entry.get('kind')=='external' else ''
                if text:
                    quote=text[:min(700,len(text))]
                    brief['evidence']=[dict(resource_id=resource_id,quote=quote)]
                    if not brief.get('destination','').strip():
                        brief['destination']='；'.join(
                            line.strip() for line in quote.splitlines() if line.strip())[:500]
                elif failure:
                    brief['unavailable_reason']=failure.get('reason','目标页未取得')
            if not brief['topic'].strip():
                brief['topic']=obj.get('text','').strip() or obj.get('target','')
            if not brief['connection'].strip():
                brief['connection']='原文在当前位置提供该内容链接，链接文字与地址均需保留'
            if not brief['destination'].strip() and brief.get('evidence') and resources:
                excerpts=[]
                for item in brief['evidence']:
                    entry=resources.state.get('entries',{}).get(item.get('resource_id'),{})
                    if entry.get('kind')=='external' and exact_source_quote(
                            item.get('quote',''),resources.text(item['resource_id'])):
                        excerpts.extend(line.strip() for line in item['quote'].splitlines()
                                        if line.strip())
                if excerpts:brief['destination']='；'.join(excerpts[:3])[:500]
            if not all(brief[k].strip() for k in ('topic','connection')):
                raise ValueError('内容链接缺少主题、当前位置关联或目标内容：'+brief['source_id'])
            if brief['unavailable_reason']:
                failures=[r for r in resources.state['reads']
                          if r.get('kind')=='page' and r.get('url')==target
                          and r.get('status')=='unavailable'] if resources else []
                if brief['evidence'] or not failures or not any(
                    r.get('reason') and r['reason'] in brief['unavailable_reason'] for r in failures):
                    raise ValueError('内容链接不可用说明缺少本次访问失败记录：'+brief['source_id'])
                continue
            if not brief['destination'].strip():
                raise ValueError('内容链接缺少目标页实际内容：'+brief['source_id'])
            if not brief['evidence']:
                raise ValueError('内容链接缺少已读取的目标证据，不能作为合格计划：'+brief['source_id'])
            for item in brief['evidence']:
                if len(item['quote'])>1200:
                    raise ValueError('链接证据应定位到必要段落，不能重复发送整页：'+brief['source_id'])
                entry=resources.state['entries'].get(item['resource_id']) if resources else None
                if not entry or entry.get('kind')!='external' or not exact_source_quote(item['quote'],resources.text(item['resource_id'])):
                    raise ValueError('内容链接引用了未读取或不准确的目标证据：'+brief['source_id'])
                address=entry.get('parent_page_url') or entry.get('original_url') or entry.get('locator','')
                if target and address!=target:
                    raise ValueError('内容链接引用了其他目标页的证据：'+brief['source_id'])
            page_entries=[entry for entry in resources.state['entries'].values()
                if entry.get('kind')=='external' and entry.get('original_url')==target
                and entry.get('scope')!='linked_image']
            if any(entry.get('image_refs') and entry['chars']<600 for entry in page_entries):
                if not any(resources.state['entries'].get(item['resource_id'],{}).get('scope')=='linked_image'
                    for item in brief['evidence']):
                    raise ValueError('目标页主要内容在图片中，规划前须读取图片：'+brief['source_id'])
    nodes = plan['nodes']
    claimed=set();obligation_owners={};dropped={};kept=[]
    for node in nodes:
        unique_obligations=[fid for fid in node['obligation_ids'] if fid not in claimed]
        if unique_obligations!=node['obligation_ids']:
            if not unique_obligations and node['establishes_concepts']:
                # A planner may add a summary node for obligations that were
                # already assigned while placing a new concept on that empty
                # summary.  Preserve the concept by moving it to the existing
                # owner of the same source obligation, then remove only the
                # redundant structural node.
                owner=next((obligation_owners.get(fid) for fid in node['obligation_ids']
                            if obligation_owners.get(fid)),None)
                if owner:
                    owner['requires_concepts']=list(dict.fromkeys(
                        owner['requires_concepts']+node['requires_concepts']))
                    owner['establishes_concepts']=list(dict.fromkeys(
                        owner['establishes_concepts']+node['establishes_concepts']))
                    node['establishes_concepts']=[]
            node['obligation_ids']=unique_obligations
            node['source_ids']=list(dict.fromkeys(obligations[fid]['source_id'] for fid in unique_obligations))
        if not node['obligation_ids']:
            dropped[node['id']]=list(node['depends_on'])
            continue
        claimed.update(node['obligation_ids']);kept.append(node)
        obligation_owners.update({fid:node for fid in node['obligation_ids']})
    if dropped:
        def live_dependencies(items):
            result=[];pending=list(items);visited=set()
            while pending:
                dependency=pending.pop(0)
                if dependency in visited:continue
                visited.add(dependency)
                if dependency in dropped:pending[0:0]=dropped[dependency]
                else:result.append(dependency)
            return result
        for node in kept:node['depends_on']=live_dependencies(node['depends_on'])
        plan['nodes']=nodes=kept
    prior_nodes = [n for part in prior for n in part['nodes']]
    prior_concepts = [c for part in prior for c in part['concepts']]
    unique([n['id'] for n in prior_nodes + nodes], '编排节点')
    unique([c['id'] for c in prior_concepts + plan['concepts']], '概念')
    # Labels are not identities: a later source section can refine a named concept
    if any(not c['name'].strip() for c in plan['concepts']):raise ValueError('概念名称为空')
    unique([o['id'] for part in prior for o in part['obligations']] + list(obligations), '跨组义务')
    concepts = {c['id']: c for c in prior_concepts + plan['concepts']}
    established = {c for n in prior_nodes for c in n['establishes_concepts']}
    seen = {n['id'] for n in prior_nodes}
    owned = []
    sources = []
    explicitly_established={c for n in nodes for c in n['establishes_concepts']}
    for node in nodes:
        repeated=set(node['establishes_concepts'])&established
        node['requires_concepts']=list(dict.fromkeys(node['requires_concepts']+sorted(repeated)))
        node['establishes_concepts']=[c for c in node['establishes_concepts'] if c not in repeated]
        # Bind a declared but unplaced concept to its first actual use only when
        # its prerequisites are already available; undefined/cyclic references fail
        pending=list(node['requires_concepts'])
        while pending:
            added=[]
            for cid in pending:
                if cid not in established and cid not in explicitly_established and cid in concepts:
                    if set(concepts[cid]['requires'])<=established|set(node['establishes_concepts']):
                        node['establishes_concepts'].append(cid)
                        explicitly_established.add(cid)
                        added.append(cid)
            if not added:break
            pending=[cid for cid in pending if cid not in added]
        if not set(node['depends_on']) <= seen or not set(node['requires_concepts']) <= established|set(node['establishes_concepts']):
            raise ValueError('编排依赖了尚未建立的节点或概念：' + node['id'])
        # A concept defined inside this unit is not reader entry knowledge
        node['requires_concepts']=[c for c in node['requires_concepts'] if c not in node['establishes_concepts']]
        if not set(node['source_ids']) <= assigned or not set(node['obligation_ids']) <= obligations.keys():
            raise ValueError('节点引用了未分配的来源或义务')
        if {obligations[f]['source_id'] for f in node['obligation_ids']} != set(node['source_ids']):
            raise ValueError('节点原对象与义务来源不一致')
        # A source partition may end immediately after its heading. Keep that
        # source obligation here; writing_batches joins it to the next part's
        # content before any writer call instead of asking the planner to
        # invent body text inside the wrong partition.
        # A planning title is an internal routing label.  The writer still
        # receives the full Chinese writing policy, so an English-only label
        # must not prevent an otherwise complete source plan from running.
        node_spans={s for f in node['obligation_ids'] for s in obligations[f].get('source_span_ids',[])}
        size=sum(spans[s]['end']-spans[s]['start'] for s in node_spans) if node_spans else sum(len(obligations[f]['quote']) for f in node['obligation_ids'])
        if size > node_limit:
            raise ValueError('单元负责的信息过长，需要按实际主题拆分义务，不能删减')
        new = set(node['establishes_concepts'])
        if len(new)>concept_limit:
            raise ValueError('一个写作单元首次解释的概念过多，需要按原有主题拆成连续单元：'+node['id'])
        if not new <= concepts.keys() or new & established:
            raise ValueError('概念首次解释位置重复或不存在')
        pending=list(node['establishes_concepts']);ordered=[];available=set(established)
        while pending:
            ready=next((cid for cid in pending if set(concepts[cid]['requires'])<=available),None)
            if ready is None:break
            ordered.append(ready);available.add(ready);pending.remove(ready)
        node['establishes_concepts']=ordered+pending
        for cid in node['establishes_concepts']:
            concept = concepts[cid]
            if not set(concept['requires']) <= established or not set(concept['source_ids']) <= objects.keys():
                raise ValueError('概念前提未建立或来源不存在')
            established.add(cid)
        if mode == 'rewrite' and (node['depth'] == 'progressive' or node['expansion'] == 'authorized_example'):
            raise ValueError('普通改写不能擅自扩展成教学情境')
        if seen and not node['transition_from'].strip():
            raise ValueError('缺少与前文的具体衔接')
        seen.add(node['id'])
        owned.extend(node['obligation_ids'])
        sources.extend(node['source_ids'])
    unique(owned, '义务主要落点')
    if set(owned) != set(obligations) or set(sources) != assigned:
        raise ValueError('存在没有分配到正文的义务或原对象')
    if set(concepts) != established:
        raise ValueError('存在未安排首次解释的概念')
    name_responsibilities=({item['source_id']:item for item in plan['object_responsibilities']}
        if mode=='rewrite' and require_object_responsibilities else None)
    missing_numeronyms=missing_catalogued_numeronyms(
        source,assigned,plan['concepts'],name_responsibilities)
    if missing_numeronyms:
        raise ValueError('原文名称内部的缩写缺少已查证全称规划：'+', '.join(missing_numeronyms))
    if mode == 'rewrite' and plan['contract']['depth'] == 'progressive':
        raise ValueError('改写契约扩大了用户授权范围')
    if prior and plan['contract'] != prior[0]['contract']:
        raise ValueError('后续分组改变了全篇改写契约')
    return plan


def writing_batches(plans, source, limit=6500, concept_limit=10, object_limit=20,
                    present_source_ids=None):
    """Keep the section plan while scheduling adjacent sections in bounded calls."""
    objects={obj['id']:obj for obj in source['objects']}
    sections=copy.deepcopy([n for p in plans for n in p['nodes']])
    if present_source_ids is not None:
        present=set(present_source_ids)
        fact_source={f['id']:f['source_id'] for p in plans for f in p['obligations']}
        for node in sections:
            node['source_ids']=[sid for sid in node['source_ids'] if sid in present]
            node['obligation_ids']=[fid for fid in node['obligation_ids']
                                    if fact_source[fid] in present]
        sections=[node for node in sections if node['source_ids'] and node['obligation_ids']]
        if not sections:
            raise ValueError('改写计划没有保留可呈现的正文信息')
    merge_adjacent_heading_only_nodes({'nodes':sections},objects)
    if len(sections)>1 and sections[-1]['source_ids'] and all(
            objects[sid]['kind']=='heading' for sid in sections[-1]['source_ids']):
        trailing=sections.pop();previous=sections[-1]
        for key in ('source_ids','obligation_ids','establishes_concepts','requires_concepts'):
            previous[key]=list(dict.fromkeys(previous[key]+trailing[key]))
        previous['prepares_for']=trailing['prepares_for']
    spans={s['id']:s for s in source_spans(source,[o['id'] for o in source['objects']])}
    facts={f['id']:f for p in plans for f in p['obligations']}
    batches=[];current=[];used=set();size=0
    def finish():
        if not current:return
        node=copy.deepcopy(current[0])
        node['section_outline']=copy.deepcopy(current)
        for key in ('source_ids','obligation_ids','establishes_concepts','requires_concepts'):
            node[key]=list(dict.fromkeys(v for n in current for v in n[key]))
        node['requires_concepts']=[c for c in node['requires_concepts'] if c not in node['establishes_concepts']]
        node['depends_on']=[batches[-1]['id']] if batches else []
        node['prepares_for']=current[-1]['prepares_for']
        batches.append(node)
    for node in sections:
        selected={s for fid in node['obligation_ids'] for s in facts[fid].get('source_span_ids',[])}
        extra=sum(spans[s]['end']-spans[s]['start'] for s in selected-used)
        if not selected:extra=sum(len(facts[f]['quote']) for f in node['obligation_ids'])
        new_concepts={cid for n in current for cid in n['establishes_concepts']}|set(node['establishes_concepts'])
        new_objects={sid for n in current for sid in n['source_ids']}|set(node['source_ids'])
        if current and (size+extra>limit or len(new_concepts)>concept_limit or
                        len(new_objects)>object_limit):
            finish();current=[];used=set();size=0
            extra=sum(spans[s]['end']-spans[s]['start'] for s in selected) if selected else extra
        current.append(node);used.update(selected);size+=extra
    finish()
    return batches


def legacy_artifacts(plans, source, execution_nodes=None, responsibilities=None):
    facts = [f for part in plans for f in part['obligations']]
    if responsibilities is not None:
        present={sid for sid,role in responsibilities.items() if role['present']}
        facts=[f for f in facts if f['source_id'] in present]
    inv = copy.deepcopy(source)
    if responsibilities is not None:
        inv['object_responsibilities']=copy.deepcopy(responsibilities)
    inv['obligations'] = [dict(id=f['id'], object_id=f['source_id'], statement=f['meaning'],
                              conditions=f['conditions'], quantities=f['quantities'],
                              negations=f['negations'], status='unreviewed') for f in facts]
    # Do not invent the independent inventory-review receipt expected by legacy releases
    inv['inventory_review'] = None
    inv = freeze(inv)
    nodes = execution_nodes or [n for p in plans for n in p['nodes']]
    visible_facts={fact['id'] for fact in facts}
    plan = Plan.model_validate(dict(title=nodes[0]['title'], objective=plans[0]['contract']['purpose'],
        research_gaps=[], units=[dict(id=n['id'], title=n['title'], objective=n['purpose'],
            obligation_ids=[fid for fid in n['obligation_ids'] if fid in visible_facts], prerequisites=n['requires_concepts'],
            stages=n['explanation']['reasoning_steps'] or [n['purpose']], object_ids=n['source_ids'],
            proof_questions=[], follows_units=n['depends_on'], bridge_reason=n['transition_from'],
            entry_knowledge=[n['explanation']['known_start']], reader_question=n['explanation']['obstacle'])
            for n in nodes])).model_dump()
    return inv, plan


def planned_object_responsibilities(plans, source):
    """Freeze one preservation, presentation and explanation decision per source object."""
    roles={obj['id']:dict(preserve=True,present=False,explain=False)
           for obj in source['objects']}
    for part in plans:
        for item in part.get('object_responsibilities',[]):
            roles[item['source_id']].update(present=item['present'],explain=item['explain'])
    for obj in source['objects']:
        if obj.get('source_scope') in {'site_chrome','source_metadata','layout_decorative'}:
            roles[obj['id']].update(present=False,explain=False)
    return roles


def integrity_result(job, findings, *, reviewed_source_ids=(), reviewed_block_ids=(),
                     review_attempted=True):
    """One article-level verdict; a missing review is unknown, never passed."""
    rows=copy.deepcopy(findings)
    expected_sources={sid for group in job.get('active_groups',[]) for sid in group}
    expected_blocks={block['id'] for block in job['draft']['blocks']}
    if (not review_attempted or not expected_sources<=set(reviewed_source_ids)
            or not expected_blocks<=set(reviewed_block_ids)):
        rows.append(dict(invariant='I4',verdict='UNKNOWN',block_id='',source_id='',
                         output_quote='',source_quote='',problem='整稿与原件的语义核对范围尚未完整确认',
                         required_change=''))
        rows.append(dict(invariant='I3',verdict='UNKNOWN',block_id='',source_id='',
                         output_quote='',source_quote='',problem='新增事实的来源核对范围尚未完整确认',
                         required_change=''))
    checks={}
    for invariant in ('I1','I2','I3','I4','I5'):
        related=[row for row in rows if row['invariant']==invariant]
        verdict=('FAIL' if any(row['verdict']=='FAIL' for row in related) else
                 'UNKNOWN' if related else 'PASS')
        checks[invariant]=verdict
    overall=('FAIL' if 'FAIL' in checks.values() else
             'UNKNOWN' if 'UNKNOWN' in checks.values() else 'PASS')
    return dict(verdict=overall,invariants=checks,findings=rows,
                draft_digest=digest(canonical(job['draft']).encode()),
                source_digest=job['source_digest'],review_scope='whole_candidate',
                reviewed_source_ids=list(reviewed_source_ids),
                reviewed_block_ids=list(reviewed_block_ids))


def integrity_allowed_evidence(job,store):
    """Only fact-scoped saved evidence can support an added article claim."""
    saved=job.get('external_resources',{})
    allowed=[]
    def include(item,scope):
        rid=item.get('resource_id','')
        quote=item.get('quote','')
        entry=saved.get(rid,{})
        if not entry or not entry.get('blob') or not quote:
            return
        try:
            saved_text=store.read_blob(entry['blob']).decode()
        except (OSError,UnicodeError):
            return
        if quote not in saved_text:return
        row=dict(id=item.get('id') or scope+'-'+digest([rid,quote])[:16],
                 resource_id=rid,quote=quote,scope=scope)
        if row not in allowed:allowed.append(row)
    for part in job.get('active_plans',[]):
        for binding in part.get('evidence_bindings',[]):
            include(binding,'verified_fact')
        for brief in part.get('link_briefs',[]):
            if not job.get('object_responsibilities',{}).get(brief['source_id'],{}).get('explain'):
                continue
            for evidence in brief.get('evidence',[]):include(evidence,'semantic_dependency')
        for concept in part.get('concepts',[]):
            for evidence in concept.get('name_evidence',[]):include(evidence,'name_only')
    return allowed


def validate_integrity_review(value, draft, source, allowed_evidence, *,
                              whole_candidate=True, protected_literals=()):
    """Validate the one review's explicit I3 provenance receipt and findings."""
    review=A.IntegrityReview.model_validate(value).model_dump()
    blocks={block['id']:block['markdown'] for block in draft['blocks']}
    sources={obj['id']:obj.get('text','') for obj in source['objects']}
    if whole_candidate:
        expected=patchable_block_ids(draft,protected_literals)
        assessments=review['i3_block_assessments']
        assessed=[item['block_id'] for item in assessments]
        if len(assessed)!=len(set(assessed)) or set(assessed)!=expected:
            raise ValueError('I3 正文块来源核对回执必须恰好覆盖全部作者正文块')
        claims_by_block={bid:[] for bid in expected}
        for claim in review['added_fact_claims']:
            if claim['block_id'] not in claims_by_block:
                raise ValueError('I3 新增事实必须属于已核对的作者正文块')
            claims_by_block[claim['block_id']].append(claim)
        for item in assessments:
            has_claim=bool(claims_by_block[item['block_id']])
            if (item['status']=='ADDED_FACTS_PRESENT')!=has_claim:
                raise ValueError('I3 正文块新增事实声明与实际 claim 不一致：'+item['block_id'])
        review['i3_provenance_checked']=True
    evidence={}
    for row in allowed_evidence:
        if row['id'] not in evidence or evidence[row['id']]['scope']=='name_only':
            evidence[row['id']]=row
    for issue in review['findings']:
        if (issue['block_id'] and (issue['block_id'] not in blocks or
                (issue['output_quote'] and issue['output_quote'] not in blocks[issue['block_id']]))):
            raise ValueError('整稿意见没有准确指向候选正文')
        if (issue['source_id'] and (issue['source_id'] not in sources or
                (issue['source_quote'] and issue['source_quote'] not in sources[issue['source_id']]))):
            raise ValueError('整稿意见没有准确指向保存的原文')
    for claim in review['added_fact_claims']:
        if claim['block_id'] not in blocks or claim['output_quote'] not in blocks[claim['block_id']]:
            raise ValueError('新增事实引文没有准确指向候选正文')
        if claim['support']=='SOURCE_SUPPORTED':
            if (not claim['source_quote'] or claim['source_id'] not in sources or
                    claim['source_quote'] not in sources[claim['source_id']]):
                raise ValueError('新增事实的原件支持没有可核对的原文引文')
        elif claim['support']=='EXTERNAL_SUPPORTED':
            row=evidence.get(claim['evidence_id'])
            if not row or row['scope']=='name_only':
                raise ValueError('外部事实没有可用的本任务事实证据；名称查证不授权机制事实')
        else:
            if not any(item['invariant']=='I3' and item['verdict']=='FAIL'
                    and item['block_id']==claim['block_id']
                    and item['output_quote']==claim['output_quote']
                    for item in review['findings']):
                review['findings'].append(dict(invariant='I3',verdict='FAIL',
                    block_id=claim['block_id'],source_id='',
                    output_quote=claim['output_quote'],source_quote='',
                    problem='候选稿新增的事实缺少原件或允许的外部证据',
                    required_change='删除或收窄这项无依据的新增事实'))
    return review


def compile_integrity_review(value, draft, source, allowed_evidence, *,
                             protected_literals=()):
    """Keep grounded findings from one review; incomplete receipts become UNKNOWN."""
    blocks={block['id']:block['markdown'] for block in draft['blocks']}
    sources={obj['id']:obj.get('text','') for obj in source['objects']}
    expected=patchable_block_ids(draft,protected_literals)
    review=value.get('result',value) if isinstance(value,dict) else {}
    if not isinstance(review,dict):review={}
    findings=[]
    def unknown(block_id,problem):
        findings.append(dict(invariant='I3',verdict='UNKNOWN',block_id=block_id,
            source_id='',output_quote='',source_quote='',problem=problem,
            required_change=''))
    def bound(block_id,quote):
        return bool(block_id in blocks and quote and quote in blocks[block_id])
    for raw in review.get('findings',[]) if isinstance(review.get('findings'),list) else []:
        try:
            issue=A.IntegrityFinding.model_validate(raw).model_dump()
        except (ValueError,TypeError):
            unknown('','整稿审核包含无法解析的意见')
            continue
        if (issue['block_id'] and (issue['block_id'] not in blocks or
                issue['output_quote'] and not bound(issue['block_id'],issue['output_quote']))
                or issue['source_id'] and (issue['source_id'] not in sources or
                issue['source_quote'] and issue['source_quote'] not in sources[issue['source_id']])):
            findings.append(dict(invariant=issue['invariant'],verdict='UNKNOWN',
                block_id=issue['block_id'] if issue['block_id'] in blocks else '',
                source_id='',output_quote='',source_quote='',
                problem='审核意见无法绑定到保存的正文或原件',required_change=''))
        else:findings.append(issue)
    evidence={row['id']:row for row in allowed_evidence if row.get('id')}
    claims_by_block={bid:[] for bid in expected}
    raw_claims=review.get('added_fact_claims',[])
    if not isinstance(raw_claims,list):
        raw_claims=[];unknown('','新增事实回执无法解析')
    claims=[]
    for raw in raw_claims:
        try:claim=A.AddedFactClaim.model_validate(raw).model_dump()
        except (ValueError,TypeError):
            unknown(raw.get('block_id','') if isinstance(raw,dict) else '',
                    '新增事实回执无法解析')
            continue
        bid=claim['block_id']
        if not bound(bid,claim['output_quote']) or bid not in expected:
            unknown(bid if bid in blocks else '',
                    '新增事实引文无法绑定到作者正文块')
            continue
        claims.append(claim);claims_by_block[bid].append(claim)
        if claim['support']=='UNSUPPORTED':
            if not any(row['invariant']=='I3' and row['verdict']=='FAIL'
                    and row['block_id']==bid and row['output_quote']==claim['output_quote']
                    for row in findings):
                findings.append(dict(invariant='I3',verdict='FAIL',block_id=bid,
                    source_id='',output_quote=claim['output_quote'],source_quote='',
                    problem='候选稿新增的事实缺少原件或允许的外部证据',
                    required_change='删除或收窄这项无依据的新增事实'))
        elif claim['support']=='SOURCE_SUPPORTED':
            if (not claim['source_quote'] or claim['source_id'] not in sources or
                    claim['source_quote'] not in sources[claim['source_id']]):
                unknown(bid,'原件支持声明无法核验')
        elif (claim['evidence_id'] not in evidence or
              evidence[claim['evidence_id']].get('scope')=='name_only'):
            unknown(bid,'外部支持声明无法绑定到允许的事实证据')
    assessments={};duplicate=set()
    raw_assessments=review.get('i3_block_assessments',[])
    if not isinstance(raw_assessments,list):
        raw_assessments=[];unknown('','正文块来源核对回执无法解析')
    for raw in raw_assessments:
        try:item=A.I3BlockAssessment.model_validate(raw).model_dump()
        except (ValueError,TypeError):
            unknown(raw.get('block_id','') if isinstance(raw,dict) else '',
                    '正文块来源核对回执无法解析')
            continue
        bid=item['block_id']
        if bid not in expected:
            unknown('','来源核对回执指向非作者正文块')
        elif bid in assessments:duplicate.add(bid)
        else:assessments[bid]=item
    for bid in sorted(expected):
        item=assessments.get(bid)
        if bid in duplicate or item is None:
            unknown(bid,'作者正文块的来源核对回执缺失或重复')
        elif (item['status']=='ADDED_FACTS_PRESENT')!=bool(claims_by_block[bid]):
            unknown(bid,'正文块新增事实声明与实际 claim 不一致')
    checked_sources=review.get('checked_source_ids',[])
    checked_blocks=review.get('checked_block_ids',[])
    if not isinstance(checked_sources,list):checked_sources=[]
    if not isinstance(checked_blocks,list):checked_blocks=[]
    return dict(checked_source_ids=[sid for sid in checked_sources if sid in sources],
        checked_block_ids=[bid for bid in checked_blocks if bid in blocks],
        findings=findings,added_fact_claims=claims,
        i3_block_assessments=list(assessments.values()),
        i3_provenance_checked=bool(expected<=set(assessments) and not duplicate
            and not any(row['invariant']=='I3' and row['verdict']=='UNKNOWN'
                        for row in findings)))


def validate_written(value, node, inventory, bundle, prior, source_obligations=(),concepts=(),
                     name_completions=()):
    body = A.WrittenUnit.model_validate(value).model_dump()
    sources={o['id']:o for o in inventory['objects']}
    literals=protected_objects(inventory)
    object_roles=inventory.get('object_responsibilities')
    fact_quotes={f['id']:f for f in source_obligations}
    blocks=[];block_id_repairs={}
    for raw in body['blocks']:
        prefixes=[node['id']]+[n['id'] for n in node.get('section_outline',[])]
        if not any(raw['id'].startswith(prefix+'-') for prefix in prefixes):
            submitted=raw['id'];raw['id']=node['id']+'-'+submitted
            block_id_repairs[submitted]=raw['id']
        if not set(raw['source_ids'])<=set(node['source_ids']):
            raise ValueError('正文引用了当前单元之外的原文')
        embedded=[]
        def insert(match):
            sid=match[1]
            if (sid not in literals and raw['kind']=='document_info' and sid in node['source_ids']
                    and sid in sources and sources[sid]['kind'] in {'text','heading'}):
                # A page-information paragraph is checked against its source
                # obligations later. Its ordinary text is not a protected
                # literal: remove a mistaken marker while keeping the actual
                # Chinese explanation and immutable original in the archive.
                return ''
            if (sid not in literals or sid not in node['source_ids'] or
                    (object_roles is not None and not object_roles[sid]['present'])):
                raise ValueError('原对象插入标记不存在，或把普通文字当作原文搬移')
            embedded.append(sid)
            return literals[sid]
        authored=unwrap_source_marker_images(raw['markdown'])
        text=re.sub(r'\{\{source:([^{}]+)\}\}',insert,authored)
        if '{{source:' in text:raise ValueError('原对象插入标记未完整闭合')
        if raw['obligation_ids'] and not text.strip():
            raise ValueError('承担原文义务的段落不能为空：'+raw['id'])
        # A scoped revision receives compiled text and may preserve the exact
        # literal directly rather than replace it with a template marker again
        embedded.extend(sid for sid in raw['source_ids'] if sid in literals and literals[sid] and literals[sid] in text)
        for heading in re.findall(r'(?m)^#{1,6} ([^\n]+)',raw['markdown']):
            if re.search(r'[A-Za-z]',heading) and not re.search(r'[\u3400-\u9fff]',heading):
                raise ValueError('正文标题照搬了未解释的英文，需要按完整写作技能改写')
        headings=[sid for sid in raw['source_ids'] if sources[sid]['kind']=='heading'] if re.search(r'(?m)^#{1,6} ',text) else []
        evidence=[]
        for sid in raw['source_ids']:
            quotes=list(dict.fromkeys(fact_quotes[f]['quote'] for f in raw['obligation_ids'] if f in fact_quotes and fact_quotes[f]['source_id']==sid))
            evidence.extend(dict(source_id=sid,quote=q) for q in quotes or [sources[sid]['text']])
        blocks.append(dict(id=raw['id'],unit_id=node['id'],kind=raw['kind'],markdown=text,
            obligation_ids=raw['obligation_ids'],object_ids=list(dict.fromkeys(embedded+headings)),
            embedded_object_ids=list(dict.fromkeys(embedded)),
            evidence=evidence))
    # Object preservation is compiler work. References tell us where an object
    # belongs; missing hand-written markup must never discard its original bytes
    for sid in node['source_ids']:
        if sid not in literals:continue
        if object_roles is not None and not object_roles[sid]['present']:continue
        if any(sid in b.get('embedded_object_ids',[]) for b in prior['blocks']+blocks):continue
        obj=sources[sid];literal=literals[sid]
        placed=dict(id=node['id']+'-source-'+sid,unit_id=node['id'],
            kind='document_info' if obj['kind'] in {'page','metadata'} else 'object',
            markdown=literal,obligation_ids=[],object_ids=[sid],embedded_object_ids=[sid],
            evidence=[dict(source_id=sid,quote=obj['text'])])
        position=next((i+1 for i,b in enumerate(blocks) if sid in {e['source_id'] for e in b['evidence']}),len(blocks))
        if obj['kind'] in {'page','metadata'}:position=len(blocks)
        blocks.insert(position,placed)
    # Consecutive list items are one Markdown list, while retaining every source
    # and obligation relation. Rendering blocks must not create loose-list gaps
    merged=[];aliases={}
    for block in blocks:
        if (merged and re.match(r'^[-+*] ',block['markdown'])
                and re.match(r'^[-+*] ',merged[-1]['markdown'])
                and '\n' not in block['markdown'] and not block['embedded_object_ids']
                and not merged[-1]['embedded_object_ids'] and block['kind']==merged[-1]['kind']):
            previous=merged[-1];aliases[block['id']]=previous['id']
            previous['markdown']+='\n'+block['markdown']
            for key in ('obligation_ids','object_ids'):
                previous[key]=list(dict.fromkeys(previous[key]+block[key]))
            previous['evidence']+= [e for e in block['evidence'] if e not in previous['evidence']]
        else:merged.append(block)
    for binding in body['coverage']+body['knowledge_delta']['concept_evidence']:
        binding['block_id']=block_id_repairs.get(binding['block_id'],binding['block_id'])
        binding['block_id']=aliases.get(binding['block_id'],binding['block_id'])
    draft={'blocks':merged}
    draft=planned_heading_depth(draft,node.get('heading_level',2),inventory)
    name_alignments=insert_verified_name_at_unique_first_use(
        draft,body['knowledge_delta'],list(concepts)+list(name_completions))
    if name_alignments:body['knowledge_delta']['name_alignments']=name_alignments
    draft=normalize_authored_periods(draft,inventory)
    normalized_blocks={b['id']:b for b in draft['blocks']}
    for binding in body['knowledge_delta'].get('concept_evidence',[]):
        block=normalized_blocks.get(binding['block_id'])
        if block and binding.get('output_quote') not in block['markdown']:
            binding['output_quote']=block['markdown']
    allowed = set(node['obligation_ids'])
    if any(not set(b['obligation_ids']) <= allowed for b in draft['blocks']):
        raise ValueError('写作把其他单元的事实声明为已经覆盖')
    # The visual model supplies image semantics. This check only makes sure the
    # writer retained the original pixels and placed a source-bound explanation
    if object_roles is None:
        all_blocks=prior['blocks']+draft['blocks']
        for sid in node['source_ids']:
            obj=sources[sid]
            if (obj.get('kind') not in {'image','media'} or obj.get('source_scope') in
                    {'site_chrome','source_metadata','layout_decorative'}):
                continue
            if not any(sid in block.get('embedded_object_ids',[]) for block in all_blocks):
                raise ValueError('正文非文字材料没有保留原始对象：'+sid)
            if not any(block.get('kind')=='explanation'
                       and sid in {item.get('source_id') for item in block.get('evidence',[])}
                       and block.get('markdown','').strip() for block in all_blocks):
                raise ValueError('正文非文字材料缺少与原对象绑定的说明：'+sid)
    subset = inventory | {'obligations': [o for o in inventory['obligations'] if o['id'] in allowed],
                          'objects': [o for o in inventory['objects'] if o['id'] in node['source_ids']]}
    findings = inspect_draft(subset, draft, prior_draft=prior)
    if findings:
        raise ValueError('单元结构校验未通过：' + json.dumps(findings, ensure_ascii=False))
    blocks = {b['id']: b for b in draft['blocks']}
    unique([b['id'] for b in prior['blocks']] + list(blocks), '正文块')
    # The block's declared fact IDs are the model's mapping. Exact body quotations
    # are derived from the actual compiled text, never trusted from self-citation
    # or misrepresented as semantic proof. The independent reader checks meaning
    coverage=[]
    for fid in node['obligation_ids']:
        block=next((b for b in draft['blocks'] if fid in b['obligation_ids']),None)
        if not block:raise ValueError('写作没有提供原信息的正文落点：'+fid)
        coverage.append(dict(obligation_id=fid,block_id=block['id'],output_quote=block['markdown']))
    delta = body['knowledge_delta']
    align_reported_concept_ids(delta,node['establishes_concepts'])
    # The node and compiler-derived coverage are authoritative for these
    # routing fields.  A writer's stale bookkeeping must not discard prose
    # that already covers the required obligations; the independent review
    # still checks whether the prose actually explains them.
    delta['established_concepts']=list(node['establishes_concepts'])
    delta['explained_obligations']=list(node['obligation_ids'])
    delta['unresolved_prerequisites']=[]
    unique([c['concept_id'] for c in delta['concept_evidence']], '概念正文证据')
    if not {c['concept_id'] for c in delta['concept_evidence']} <= set(node['establishes_concepts']):
        raise ValueError('概念记忆引用了未安排的概念')
    for item in delta['concept_evidence']:
        if '{{source:' in item['output_quote']:
            item['output_quote']=re.sub(r'\{\{source:([^{}]+)\}\}',
                lambda match:literals[match[1]] if match[1] in node['source_ids'] and match[1] in literals
                else match.group(),item['output_quote'])
        if item['block_id'] not in blocks or item['output_quote'] not in blocks[item['block_id']]['markdown']:
            raise ValueError('概念记忆的引文不在实际正文中')
    return draft, coverage, delta


class ActiveComposition:
    def __init__(self, host):
        self.host = host
        self.store, self.config, self.queue, self.owner = host.store, host.config, host.queue, host.owner
        limit=self.config.get('active_revision_limit',4)
        if type(limit) is not int or not 1<=limit<=4:
            raise ValueError('主动编排的定向修订上限必须为一至四，不能重置历史或无限循环')
        self.config=self.config|{'active_revision_limit':min(limit,2)}

    def review_integrity_once(self, job, key, payload, draft, source, allowed_evidence,
                              protected_literals=()):
        """One whole-candidate Provider request, with no protocol correction call."""
        if key in job['results']:
            raw=job['results'][key]
        else:
            if self.queue.cancelled(job['id'],self.owner):
                raise Conflict('任务已取消')
            outcome=Provider(self.store,self.config).generate(
                job['project'],'active_integrity',payload,
                A.IntegrityReview.model_json_schema(),job,key,
                lambda:self.queue.cancelled(job['id'],self.owner))
            if outcome.status=='UNKNOWN':
                raise Uncertain(str(outcome.error))
            if outcome.status=='KNOWN_FAILURE':
                if not isinstance(outcome.error,json.JSONDecodeError):
                    raise outcome.error
                raw={}
            else:raw=outcome.value
            job['results'][key]=raw
            job.pop('pending',None)
            self.store.put_job(job)
        return compile_integrity_review(raw,draft,source,allowed_evidence,
                                        protected_literals=protected_literals)

    @staticmethod
    def unavailable_media(job, source_ids):
        selected=set(source_ids)
        return [item for item in job.get('material_acquisition_limits',[])
                if item['object_id'] in selected]

    def commit_candidate(self, job, node, candidate):
        """Commit one useful writer result without another blocking review loop."""
        draft=normalize_authored_spacing(candidate['draft'],job['inventory'])
        checkpoint=dict(node_id=node['id'],draft_digest=digest(canonical(draft).encode()),
            coverage_origin='compiler_from_actual_blocks; mapping_is_not_semantic_proof',
            coverage=candidate['coverage'],knowledge_delta=candidate['delta'],
            format_digest=digest(canonical(draft).encode()),
            format_records=candidate.get('format_records',[]),
            format_escalations=candidate.get('format_escalations',[]),
            dismissed_candidate_keys=candidate.get('dismissed_keys',[]),
            content_patches=candidate.get('patch_history',[]),
            original_format_exemptions=candidate.get('original_format_exemptions',[]),
            layout_normalizations=candidate.get('layout_normalizations',[]),
            content_reviews=candidate.get('content_reviews',[]),
            unresolved_content_findings=candidate.get('unresolved_content_findings',[]),
            unresolved_format=candidate.get('unresolved_format',{}),
            unresolved_revision=candidate.get('unresolved_revision',{}))
        job.setdefault('active_checkpoints',[]).append(checkpoint)
        job['draft']['blocks'].extend(draft['blocks'])
        text=canonical(draft);resource_id='written-'+node['id'];blob=self.store.blob(text.encode())
        job.setdefault('generated_resources',{})[resource_id]=dict(id=resource_id,blob=blob,chars=len(text),
            kind='generated',locator='completed-unit/'+node['id'],draft_digest=checkpoint['draft_digest'])
        evidence={c['concept_id']:c for c in candidate['delta'].get('concept_evidence',[])}
        job['knowledge_memory'].append(dict(node_id=node['id'],delta=candidate['delta'],resource_id=resource_id,
            draft_digest=checkpoint['draft_digest'],block_ids=[b['id'] for b in draft['blocks']],
            established=[dict(id=cid,resource_id=resource_id,
                **({'definition':evidence[cid]['output_quote'],'block_id':evidence[cid]['block_id']}
                   if cid in evidence else {}))
                for cid in candidate['delta'].get('established_concepts',[])]))
        job['unit_index']+=1
        job.pop('active_candidate',None)
        nodes=job.get('writing_batches') or [n for p in job['active_plans'] for n in p['nodes']]
        job['stage']='active_write' if job['unit_index']<len(nodes) else (
            'active_integrity' if is_core_chain(job) else 'active_deliver')
        return 'queued'

    def deterministic_integrity_findings(self, job):
        """Check source identity, saved bytes and a usable export before model review."""
        def finding(invariant, problem, source_id=''):
            return dict(invariant=invariant,verdict='FAIL',block_id='',source_id=source_id,
                        output_quote='',source_quote='',problem=problem,required_change='')
        rows=[]
        project=self.store.get(job['project'])
        if (project['revision']!=job['base_revision'] or
                project['inventory']['digest']!=job['source_digest']):
            rows.append(finding('I1','用户指定的原件版本与当前生成基线不一致'))
        resource_ids={item['id'] for item in job['inventory']['resources']}
        for obj in job['inventory']['objects']:
            if obj.get('resource_id') and obj['resource_id'] not in resource_ids:
                rows.append(finding('I2','原对象引用的资源没有保存在资源清单',obj['id']))
        for item in job['inventory']['originals']+job['inventory']['resources']:
            try:
                self.store.read_blob(item.get('sha256') or item['id'])
            except (FileNotFoundError,KeyError,OSError):
                rows.append(finding('I2','原件或资源字节无法读回',item.get('id','')))
        for card in job.get('visual_cards',[]):
            role=job.get('object_responsibilities',{}).get(card['source_id'],{})
            if role.get('explain') and card.get('blocking_uncertainty'):
                rows.append(dict(invariant='I4',verdict='UNKNOWN',block_id='',
                    source_id=card['source_id'],output_quote='',source_quote='',
                    problem='理解正文所需的原图细节尚无法辨认',required_change=''))
        structural=inspect_draft(job['inventory'],job['draft'],job['plan'])
        for issue in structural:
            invariant='I2' if issue['code'] in {'resource','protected_object','embedded_bytes'} else 'I5'
            rows.append(finding(invariant,issue['message']))
        if not structural:
            try:
                from io import BytesIO
                import zipfile
                from .export import export_zip
                candidate=project|dict(inventory=job['inventory'],plan=job['plan'],
                    draft=job['draft'],revision=job['base_revision']+1,
                    production={'pipeline':job['pipeline'],'core_chain_version':1,
                                'publication_status':'Candidate'},
                    review=None,accepted_revision=None)
                archive=export_zip(self.store,candidate,release=False)
                with zipfile.ZipFile(BytesIO(archive)) as package:
                    if package.testzip() or 'material.html' not in package.namelist():
                        raise ValueError('阅读包字节损坏或缺少正文')
            except (ValueError,KeyError,OSError,Conflict,zipfile.BadZipFile) as error:
                rows.append(finding('I5','候选正文或阅读包无法正常使用：'+str(error)[:180]))
        return rows

    def call(self, job, key, role, payload, schema):
        if key in job['results']:
            return job['results'][key]
        if key in job.get('active_invalid_json', {}):
            fixed=repair_one_missing_json_object_closer(job['active_invalid_json'][key],allow_final_root=True)
            if fixed:
                try:
                    value=json.loads(fixed)
                    schema.model_validate(value)
                except (ValueError,TypeError):pass
                else:
                    job['results'][key]=value
                    job.setdefault('active_json_syntax_repairs',[]).append(dict(
                        step=key,operation='insert_one_missing_object_closer',
                        original_digest=digest(job['active_invalid_json'][key].encode())))
                    job.pop('pending',None)
                    self.store.put_job(job)
                    return value
            result = self.call(job, key+'-protocol', 'active_protocol',
                dict(original_request=payload, returned_text=job['active_invalid_json'][key],
                     instruction='Repair JSON encoding and requested structure only; preserve all words, values and claims'), schema)
            job['results'][key]=result
            self.store.put_job(job)
            return result
        if self.queue.cancelled(job['id'], self.owner):
            raise Conflict('任务已取消')
        if is_core_chain(job):
            outcome=Provider(self.store,self.config).generate(
                job['project'],role,payload,schema.model_json_schema(),job,key,
                lambda: self.queue.cancelled(job['id'],self.owner))
            if outcome.status=='SUCCESS':
                job['results'][key]=outcome.value
                job.pop('pending',None)
                self.store.put_job(job)
                return outcome.value
            if outcome.status=='UNKNOWN':
                raise Uncertain(str(outcome.error))
            if isinstance(outcome.error,json.JSONDecodeError):
                return self.repair_json(job,key,role,payload,schema)
            raise outcome.error
        if job.get('pending'):
            if job['pending'] != key:
                raise Conflict('恢复阶段与已提交请求不一致')
            try:
                value = Provider(self.store, self.config).recover(job)
            except json.JSONDecodeError:
                return self.repair_json(job,key,role,payload,schema)
            if value is None:
                raise Uncertain('原请求结果未知，保留身份，不自动重发')
        else:
            job['pending'] = job['current_step_key'] = key
            self.store.put_job(job)
            cfg = self.config | self.config.get('role_providers', {}).get(role, {})
            call_role=role
            recovery=job.get('transport_recovery_routes',{}).get(key)
            if recovery:
                if recovery.get('status')!='queued' or recovery.get('attempts'):
                    raise Uncertain('该步骤的备用线路恢复请求已尝试，不会重复派发')
                provider_id=recovery.get('provider_id')
                from .providers import apply_route_override
                recovery_version=recovery.get('recovery_protocol_version')
                if recovery_version in {'kuafu-opencode-go-third-v1',
                                        'kuafu-opencode-go-thinking-disabled-v1',
                                        'kuafu-opencode-go-pro-escalation-v1'}:
                    fallback=(self.config.get('fallback_providers') or {}).get(role)
                    fallback_cfg=apply_route_override(cfg,fallback or {})
                    from urllib.parse import urlsplit
                    credential=(fallback_cfg.get('api_key') or
                        (self.config.get('provider_credentials') or {}).get(provider_id))
                    if (not isinstance(fallback,dict) or fallback_cfg.get('provider_id')!=provider_id
                            or fallback_cfg.get('enabled') is False or not credential
                            or fallback_cfg.get('provider')!='openai-compatible'
                            or urlsplit(str(fallback_cfg.get('base_url') or '')).hostname!='opencode.ai'
                            or fallback_cfg.get('protocol','chat_completions')!=recovery.get('protocol')):
                        recovery.update(status='failed',error='configured_opencode_go_route_unavailable')
                        self.store.put_job(job)
                        raise Uncertain('已选 OpenCode Go 备用线路不可用，保留原调用记录，不重复派发')
                    cfg=apply_route_override(cfg,fallback_cfg|{
                        'provider_id':provider_id,'api_key':credential,'backup_provider_id':None,
                        'quota_fallback':None,'kuafu_fallback_roles':[]})
                    if recovery_version=='kuafu-opencode-go-pro-escalation-v1':
                        if recovery.get('model')!='deepseek-v4-pro':
                            recovery.update(status='failed',error='unsupported_go_escalation_model')
                            self.store.put_job(job)
                            raise Uncertain('Go 升级模型不在本次明确支持范围，保留原调用记录，不重复派发')
                        cfg['model']='deepseek-v4-pro'
                        # Remove effort hints at both levels: OpenCode Go may
                        # otherwise inherit one from the provider configuration.
                        provider_options=dict(cfg.get('provider_options') or {})
                        provider_options.pop('reasoning_effort',None)
                        cfg['provider_options']=provider_options
                    call_role=role+'__fallback'
                    role_options=dict(cfg.get('role_options') or {})
                    selected_options=dict(role_options.get(call_role) or {})
                    if recovery_version=='kuafu-opencode-go-pro-escalation-v1':
                        selected_options.pop('reasoning_effort',None)
                    selected_options['thinking']={'type':'disabled'}
                    role_options[call_role]=selected_options
                    cfg['role_options']=role_options
                else:
                    raw=(self.config.get('provider_routes') or {}).get(provider_id)
                    credential=(self.config.get('provider_credentials') or {}).get(provider_id)
                    if (not isinstance(raw,dict) or not credential or raw.get('enabled') is False
                            or raw.get('provider')!='openai-compatible'
                            or raw.get('protocol')!=recovery.get('protocol')):
                        recovery.update(status='failed',error='configured_route_unavailable')
                        self.store.put_job(job)
                        raise Uncertain('已选备用线路不可用，保留已记录的调用，不重复派发')
                    cfg=apply_route_override(cfg,dict(raw)|{
                        'provider_id':provider_id,'api_key':credential,'backup_provider_id':None})
                    call_role=role
                if recovery_version=='kuafu-responses-sse-v1':
                    # This retry is permitted only after the saved 52x gateway page
                    # was confirmed to contain no model artifact.
                    cfg['kuafu_responses_streaming']=True
                recovery.update(status='dispatching',attempts=1,started_at=time.time())
                self.store.put_job(job)
            # A transport fallback belongs to the exact logical step that
            # failed. An earlier uncertain writer call must not contaminate
            # later units of the same role.
            escalation = next((state for session_key,state in
                job.get('active_glossary_quality_escalations',{}).items()
                if not recovery and key.startswith(session_key+'-turn-') and state.get('status')=='queued'
                and state.get('role')==role),None)
            fresh_turn_retry = next((state for session_key,state in
                job.get('active_glossary_quality_retries',{}).items()
                if not recovery and key.startswith(session_key+'-turn-') and state.get('status')=='queued'
                and state.get('role')==role),None)
            exclusive_quality_attempt=escalation or fresh_turn_retry
            if (fresh_turn_retry and cfg.get('provider_id')!=fresh_turn_retry.get('primary_provider_id')):
                fresh_turn_retry.update(status='failed',error='configured_primary_route_changed',
                                        finished_at=time.time())
                self.store.put_job(job)
                raise Uncertain('短篇改写质量重试的主线路已变化，未发送新请求')
            if exclusive_quality_attempt:
                # Claim before dispatch so a restart or uncertain result cannot
                # grant another primary-provider attempt.
                exclusive_quality_attempt.update(status='dispatching',attempts=1,
                    started_at=time.time(),attempt_step=key)
                # Both bounded quality attempts use the configured primary route
                # without provider-side or application-level fallback.
                cfg['backup_provider_id']=None
                cfg['quota_fallback']=None
                cfg['kuafu_fallback_roles']=[]
                self.store.put_job(job)
            if not recovery and not exclusive_quality_attempt and key in job.get('transport_fallback_steps',{}):
                fallback=self.config.get('fallback_providers',{}).get(role,{})
                if fallback:
                    from .providers import apply_route_override
                    cfg=apply_route_override(cfg,fallback)
                    call_role=role+'__fallback'
            if role == 'active_visual' and role not in self.config.get('role_providers', {}):
                cfg |= self.config.get('role_providers', {}).get('visual_extract', {})
            cfg=apply_stream_timeout(cfg)
            cfg['deadline_at']=time.time()+float(cfg.get('call_timeout',90))
            cfg['role_providers'] = {}
            before = len(job['calls'])
            try:
                value = Provider(self.store, cfg).call(job['project'], call_role, payload,
                    schema.model_json_schema(), job, lambda: self.queue.cancelled(job['id'], self.owner))
            except json.JSONDecodeError:
                if exclusive_quality_attempt:
                    exclusive_quality_attempt.update(status='failed',error='invalid_json',finished_at=time.time())
                    self.store.put_job(job)
                if recovery:
                    recovery.update(status='failed',attempt_call_id=(job['calls'][-1].get('id') if len(job['calls'])>before else None),
                                    error='invalid_json')
                    self.store.put_job(job)
                return self.repair_json(job,key,role,payload,schema)
            except Exception as error:
                if exclusive_quality_attempt:
                    exclusive_quality_attempt.update(status='failed',error_type=type(error).__name__,
                                                     finished_at=time.time())
                    self.store.put_job(job)
                if recovery:
                    recovery.update(status='failed',attempt_call_id=(job['calls'][-1].get('id') if len(job['calls'])>before else None),
                                    error_type=type(error).__name__)
                    self.store.put_job(job)
                if len(job['calls']) == before:
                    job.pop('pending', None)
                    self.store.put_job(job)
                raise
            if recovery:
                recovered_call=job['calls'][-1]
                recovery.update(status='completed',attempt_call_id=recovered_call.get('id'),
                                completed_at=time.time())
                job.setdefault('route_switches',[]).append(dict(
                    role=role,from_provider_id=recovery.get('original_provider_id'),
                    to_provider_id=provider_id,reason=('kuafu_responses_versioned_stream_retry'
                        if recovery.get('recovery_protocol_version')=='kuafu-responses-sse-v1'
                        else 'go_reasoning_exhaustion_thinking_disabled_retry'
                        if recovery.get('recovery_protocol_version')=='kuafu-opencode-go-thinking-disabled-v1'
                        else 'go_reasoning_exhaustion_pro_escalation'
                        if recovery.get('recovery_protocol_version')=='kuafu-opencode-go-pro-escalation-v1'
                        else 'kuafu_pair_failure_opencode_go_third_route'
                        if recovery.get('recovery_protocol_version')=='kuafu-opencode-go-third-v1'
                        else 'kuafu_route_after_go_reasoning_exhaustion'),
                    original_call_id=recovery.get('original_call_id'),
                    exhausted_fallback_call_id=recovery.get('exhausted_fallback_call_id'),
                    recovery_call_id=recovered_call.get('id')))
            if exclusive_quality_attempt:
                exclusive_quality_attempt.update(status='completed',
                    attempt_call_id=job['calls'][-1].get('id'),completed_at=time.time())
        job['results'][key] = value
        job.pop('pending', None)
        self.store.put_job(job)
        return value

    def repair_json(self,job,key,role,payload,schema):
        if role=='active_protocol':
            raise ValueError('返回格式修正一次后仍不是有效数据，原响应已保存')
        if is_core_chain(job):
            text=Provider(self.store,self.config).saved_complete_text(job)
            job.setdefault('active_invalid_json',{})[key]=text
            job.pop('pending',None)
            self.store.put_job(job)
            return self.call(job,key,role,payload,schema)
        call=job['calls'][-1]
        if not call.get('response_blob'):
            raise Uncertain('未取得完整响应，不重发未知请求')
        response=json.loads(self.store.read_blob(call['response_blob']))
        if call.get('protocol')=='responses':
            if response.get('status') not in {'completed','complete','succeeded'}:
                raise Uncertain('Responses 原响应未完整结束，不修复部分正文')
            from .providers import _responses_text
            text=_responses_text(response)
        else:
            if call.get('finish_reason') not in {'stop','tool_calls'}:
                raise Uncertain('未取得完整响应，不重发未知请求')
            message=response['choices'][0]['message']
            text=message.get('content') or message['tool_calls'][0]['function']['arguments']
        if not isinstance(text,str) or not text:
            raise ValueError('完整响应中没有可修复的 JSON 正文')
        job.setdefault('active_invalid_json',{})[key]=text
        job.pop('pending',None)
        self.store.put_job(job)
        return self.call(job,key,role,payload,schema)

    def turn(self, job, key, role, schema, source, ids, payload, validate):
        sessions = job.setdefault('active_sessions', {})
        session = sessions.setdefault(key, {'round': 0, 'corrections': 0})
        if role=='active_plan':
            _mark_split_recovery_started(job,key)
            if is_core_chain(job) and job.get('transformation_mode')=='rewrite':
                session['link_decisions']={item['source_id']:dict(
                    source_id=item['source_id'],role='reference',missing='',source_quote='')
                    for item in plan_link_context(source,ids)}
        session.setdefault('declared_gap_ids', [])
        configured_search_routes=(self.config.get('search_routes') or
                                  self.config.get('search_providers') or
                                  self.config.get('retrieval_providers') or [])
        if isinstance(configured_search_routes,dict):
            private_credentials=self.config.get('provider_credentials') or {}
            configured_search_routes={
                route_id:(dict(route,provider_id=route_id,
                    api_key=private_credentials.get(route_id,'')) if isinstance(route,dict) else route)
                for route_id,route in configured_search_routes.items()}
        resources = Resources(self.store, source, session.get('resources'), configured_search_routes,
                              self.config.get('search_order'))
        if 'resources' not in session:
            for sid in ids:
                resources.read(sid)
            # Previous research remains addressable without repeatedly placing all of it in context
            for entry in job.get('external_resources', {}).values():
                resources.state['entries'][entry['id']] = entry
            for entry in job.get('generated_resources', {}).values():
                resources.state['entries'][entry['id']] = entry
        # A failed job may resume with a newer, independently verified glossary.
        # Add only newly catalogued immutable excerpts to its existing session.
        for term in job.get('verified_terminology', []):
            if not term.get('snapshot_blob') or not term.get('quote'):continue
            rid='term-'+digest([term['url'],term['en']])[:20]
            if rid in resources.state['entries']:continue
            resources.add(rid,term['quote'],kind='external',locator=term['url'],
                          snapshot_blob=term['snapshot_blob'],scope='name_evidence_excerpt',
                          abbreviation=term.get('abbr'),english_name=term['en'],note=term['note'])
            resources.read(rid)
        if (role=='active_plan' and job.get('link_contract_version')
                and not is_core_chain(job)
                and not session.get('direct_link_prefetch_complete')):
            from urllib.parse import urlsplit
            # Uploaded documents have source_url=None; urlsplit(None) returns
            # bytes fields that cannot be compared with the parsed link strings.
            current=urlsplit(source.get('source_url') or '')
            seen=set()
            for obj in source['objects']:
                if obj['id'] not in ids or obj['kind']!='link':continue
                if obj.get('source_scope') in {'site_chrome','source_metadata'}:continue
                target=obj.get('target','');parsed=urlsplit(target)
                if not target or target in seen or parsed.scheme not in {'https','http'}:continue
                if (parsed.hostname==current.hostname and parsed.path.rstrip('/')==current.path.rstrip('/')
                        and parsed.fragment):continue
                seen.add(target)
                path=parsed.path.rstrip('/').rsplit('/',1)[-1].lower()
                label=obj.get('text','').lower()
                if path in {'donate','contribute','contributing','contact','privacy','terms','license'}:
                    continue
                if (parsed.hostname==current.hostname and current.path.startswith(parsed.path.rstrip('/')+'/')
                    and any(word in label for word in ('home','index','tips','目录','主页'))):
                    continue
                prefetch_limit=(int(self.config.get('evidence_open_limit',4))
                                if is_v2(job) else 8)
                if len(session.get('direct_link_prefetch',[]))>=max(0,prefetch_limit):break
                action=dict(kind='page',resource_id='',url=target)
                receipt=resources.execute(action)
                session.setdefault('direct_link_prefetch',[]).append(dict(source_id=obj['id'],
                    url=target,status=receipt.get('status','retrieved'),result=receipt))
                session.setdefault('action_history',[]).append(dict(action=action,result=receipt,
                    operation='direct_link_prefetch_before_planning'))
            session['direct_link_prefetch_complete']=True
            session['action_results']=[x['result'] for x in session.get('direct_link_prefetch',[])]
            session['resources']=resources.state
            self.store.put_job(job)
        if role=='active_plan' and session.get('direct_link_prefetch_complete'):
            # Existing checkpoints may have recorded an HTTP-only rejection
            # before same-address HTTPS retrieval was supported. Recheck once
            # without revisiting any already fetched target or model output.
            for item in session.get('direct_link_prefetch',[]):
                if (item.get('status')!='unavailable' or
                        not item.get('url','').startswith('http://') or
                        item.get('https_recheck_complete')):continue
                receipt=resources.execute(dict(kind='page',resource_id='',url=item['url']))
                item.update(status=receipt.get('status','retrieved'),result=receipt,
                            https_recheck_complete=True)
                session.setdefault('action_history',[]).append(dict(
                    action=dict(kind='page',url=item['url']),result=receipt,
                    operation='same_address_https_recheck'))
            session['action_results']=[x['result'] for x in session.get('direct_link_prefetch',[])]
            session['resources']=resources.state
        cap = self.config.get('active_resource_rounds', 8)
        while session['round'] <= cap:
            session['resources'] = resources.state
            self.store.put_job(job)
            catalog=resources.catalog()
            if role=='active_plan':
                catalog=[entry for entry in catalog if entry['id'] in ids
                         or entry.get('kind')=='external' or entry['id'].startswith('term-')]
            opened_resources=resources.context()
            prompt_catalog=(prompt_active_plan_catalog(catalog,ids,opened_resources)
                           if role=='active_plan' else prompt_resource_catalog(catalog))
            request = payload | dict(catalog=prompt_catalog, opened_resources=opened_resources,
                                     previous_action_results=session.get('action_results', []),
                                     action_history=session.get('action_history', []))
            if role=='active_plan':
                request['declared_evidence_gaps']=copy.deepcopy(
                    session.get('declared_evidence_gaps',[]))
                if is_core_chain(job):
                    request['prior_link_decisions']=list(session.get('link_decisions',{}).values())
                    request['link_context']=plan_link_context(source,ids)
                    request['link_classification_required']=bool(
                        request['link_context'] and not session.get('link_decisions'))
            if session.get('correction'):
                request['protocol_correction'] = session['correction']
                if role in {'active_write','active_review'} and session.get('previous_invalid_result'):
                    request['previous_invalid_result']=session['previous_invalid_result']
                    request['correction_scope']=(
                        'Return the complete corrected WrittenUnit. Change only what protocol_correction '
                        'requires, preserve every already-correct paragraph, heading, source binding, image '
                        'marker, factual qualification and prior correction'
                        if role=='active_write' else
                        'Return the complete corrected review. Change only invalid quotations or bindings, '
                        'preserve every valid finding and checked obligation, and do not rewrite the draft')
            try:
                raw = self.call(job, key + '-turn-' + str(session['round']), role, request,
                                A.Turn[schema])
            except Exception as error:
                _mark_split_recovery_failed(job,key,error)
                if _split_recovery_marker(job,key) is not None:
                    self.store.put_job(job)
                raise
            try:
                if (role=='active_plan' and is_core_chain(job)
                        and job.get('transformation_mode')=='rewrite'):
                    raw,reference_receipt=normalize_rewrite_reference_turn(raw,source,ids)
                    if reference_receipt:
                        job.setdefault('plan_protocol_normalizations',[]).append(dict(
                            step=key,turn=session['round'],**reference_receipt))
                if (role=='active_plan' and is_core_chain(job)
                        and request['link_classification_required']):
                    raw,classification_receipt=normalize_first_plan_link_classification(
                        raw,source,ids)
                    if classification_receipt:
                        job.setdefault('plan_protocol_normalizations',[]).append(dict(
                            step=key,turn=session['round'],**classification_receipt))
                if is_v2(job) and role=='active_plan':
                    checked_raw,removed_extras=discard_known_plan_protocol_extras(raw)
                elif is_v2(job) and role=='active_review':
                    checked_raw,removed_extras=discard_known_review_protocol_extras(raw)
                else:
                    checked_raw,removed_extras=raw,[]
                if role=='active_plan' and is_core_chain(job):
                    checked_raw,normalizations=normalize_plan_clarify_expansion(
                        checked_raw,resources,session.get('link_decisions',{}).values())
                    if normalizations:
                        job.setdefault('plan_protocol_normalizations',[]).extend(
                            dict(step=key,**receipt) for receipt in normalizations)
                if removed_extras:
                    job.setdefault('nonblocking_protocol_notes',[]).append(dict(
                        step=key,reason=('redundant naming_status_effective removed from validation copy'
                            if role=='active_plan' else 'empty reason_note removed from validation copy'),
                        object_ids=removed_extras))
                response = A.Turn[schema].model_validate(checked_raw).model_dump()
                response,reused=discard_redundant_reads_with_result(response,resources)
                if role=='active_plan' and is_core_chain(job):
                    pending_classification=bool(request['link_classification_required'])
                    link_decisions=authorize_plan_link_actions(
                        response,source,ids,session.get('link_decisions',{}))
                    if pending_classification:
                        expected={item['source_id'] for item in request['link_context']}
                        if set(link_decisions)!=expected:
                            raise ValueError('首次规划必须先给当前分组每个原文链接判定职责')
                    session['link_decisions']=link_decisions
                    self.store.put_job(job)
                    if response['result'] is None and not response['actions'] and not response['gaps']:
                        session['round']+=1
                        session.pop('correction',None)
                        continue
                if reused:
                    session.setdefault('redundant_result_reads',[]).append(dict(
                        turn=session['round'],resource_ids=reused,
                        operation='already_opened_source_reads_omitted'))
                if response['actions']:
                    if response['result'] is not None or not response['gaps']:
                        raise ValueError('读取动作必须说明缺口，不能同时提交结果')
                    session['action_results'] = []
                    gap_declarations=_turn_gap_declarations(response['gaps'])
                    if is_v2(job) and role=='active_plan':
                        gap_declarations=_restore_reused_gap_declarations(
                            gap_declarations,session.get('declared_evidence_gaps',[]))
                    response_gap_ids={item['id'] for item in gap_declarations}
                    source_objects={obj['id']:obj for obj in source['objects']}
                    source_binding_receipt_ids={}
                    for action_index,action in enumerate(response['actions']):
                        if is_v2(job) and action['kind'] in {'search','page','image'}:
                            gap_id=action.get('gap_id','')
                            if role!='active_plan' or not gap_id or gap_id not in response_gap_ids:
                                raise ValueError('v2 外部检索必须绑定当前 Turn 明确声明的 EvidenceGap')
                            matching_gaps=[item for item in gap_declarations if item['id']==gap_id]
                            if len(matching_gaps)!=1:
                                raise ValueError('当前 Turn 的 EvidenceGap ID 重复，不能安全对齐来源')
                            gap_texts=[item['text'] for item in matching_gaps]
                            source_id=action.get('source_id','')
                            source_obj=source_objects.get(source_id)
                            if source_id and (not source_obj or source_id not in ids):
                                raise ValueError('v2 外部检索必须绑定当前分组的原文义务来源')
                            if source_id:
                                _,alignment=_infer_external_action_source(
                                    action,gap_texts,source_objects,ids)
                                conflicting_hints=[hint for hint in alignment.get('methods',[])
                                                   if source_id not in hint.get('source_ids',[])]
                                if conflicting_hints:
                                    raise ValueError('v2 外部检索 source_id 与当前缺口的独立来源线索冲突')
                            if not source_id:
                                inferred_id,alignment=_infer_external_action_source(
                                    action,gap_texts,source_objects,ids)
                                if not inferred_id:
                                    detail='证据不足或存在歧义'
                                    if alignment.get('reason')=='gap_source_outside_assigned_group':
                                        detail='缺口提及的来源不属于当前分组'
                                    raise ValueError('v2 外部检索必须绑定当前分组的原文义务来源（'+detail+'）')
                                source_id=inferred_id
                                action['source_id']=source_id
                                receipt_id=digest(dict(step=key,turn=session['round'],
                                    action_index=action_index,gap_id=gap_id,source_id=source_id,
                                    alignment=alignment))[:24]
                                receipt=dict(id=receipt_id,step=key,turn=session['round'],
                                    action_index=action_index,gap_id=gap_id,gap_texts=gap_texts,
                                    query=action.get('query',''),source_id=source_id,
                                    operation='deterministic_current_group_source_alignment',
                                    alignment=alignment)
                                receipts=session.setdefault('source_binding_receipts',[])
                                if not any(item.get('id')==receipt_id for item in receipts):
                                    receipts.append(receipt)
                                source_binding_receipt_ids[action_index]=receipt_id
                                source_obj=source_objects.get(source_id)
                            if not source_obj or source_id not in ids:
                                raise ValueError('v2 外部检索必须绑定当前分组的原文义务来源')
                            name_lookup=_name_lookup_requested(gap_texts,action.get('query',''))
                            direct_target_saved=_direct_link_target_saved(resources,source_obj)
                            if (action['kind']=='search' and source_obj.get('kind')=='link'
                                    and source_obj.get('target')):
                                if not direct_target_saved:
                                    raise ValueError('已有直接 URL 时必须先读取并保存目标页，不能先搜索')
                                if not name_lookup:
                                    raise ValueError('直接目标已读取后只允许为当前缺口查找补充正式名称')
                            if action['kind']=='page' and source_obj.get('target'):
                                if canonical_url(action.get('url','')) != canonical_url(source_obj['target']):
                                    if not direct_target_saved:
                                        raise ValueError('直接 URL 证据必须先读取并保存该原文链接目标')
                                    if not name_lookup:
                                        raise ValueError('补充名称来源必须对应当前缺口的正式名称查证')
                            if gap_id not in session['declared_gap_ids']:
                                session['declared_gap_ids'].append(gap_id)
                            declaration=matching_gaps[0]
                            saved_declarations=session.setdefault('declared_evidence_gaps',[])
                            saved=next((item for item in saved_declarations
                                        if item.get('id')==gap_id),None)
                            record=dict(id=gap_id,text=declaration['text'],
                                        description=declaration['description'],source_id=source_id)
                            if saved and any(saved.get(field)!=record[field]
                                             for field in ('text','source_id')):
                                raise ValueError('同一 EvidenceGap ID 的声明或来源发生变化：'+gap_id)
                            if not saved:
                                saved_declarations.append(record)
                        if is_v2(job) and action['kind'] in {'search','page','image'}:
                            counts=session.setdefault('external_action_counts',{'search':0,'open':0})
                            counter='search' if action['kind']=='search' else 'open'
                            limit=int(self.config.get('evidence_query_limit',2) if counter=='search'
                                      else self.config.get('evidence_open_limit',4))
                            if counts[counter]>=max(0,limit):
                                # Keep the action and its gap visible to the next
                                # planning turn, but never issue a request beyond
                                # the configured budget or mislabel it as evidence.
                                item=dict(status='unavailable',complete=False,
                                    evidence_status='unavailable_limit',
                                    reason='v2 evidence action limit reached; no request sent',
                                    gap_id=action.get('gap_id',''),source_id=action.get('source_id',''),
                                    action=copy.deepcopy(action))
                            else:
                                counts[counter]+=1
                                item=resources.execute(action)
                        else:
                            item=resources.execute(action)
                        if item.get('visual_evidence_required'):
                            visual_id=item['id']
                            page=dict(id=visual_id,kind='image',resource_id=item['sha256'],
                                      locator=item['url'],text='')
                            card_response=self.call(job,key+'-linked-visual-'+visual_id,'active_visual',
                                dict(pages=[page],linked_target_page=item['parent_page_url'],
                                     _image_resources=[dict(source_id=visual_id,sha256=item['sha256'])]),A.VisualCards)
                            cards=A.VisualCards.model_validate(normalize_visual_card_lists(card_response)).cards
                            if len(cards)!=1 or cards[0].source_id!=visual_id:
                                raise ValueError('链接图片识别没有对应目标图片')
                            card=cards[0]
                            if card.blocking_uncertainty:
                                raise ValueError('目标页图片仍有阻断理解的不可读内容，原始图片已保存')
                            visible=card.source_text or card.visible_content
                            resources.add(visual_id,visible,kind='external',scope='linked_image',
                                          locator=item['url'],original_url=item['original_url'],
                                          parent_page_url=item['parent_page_url'],
                                          snapshot_blob=item['sha256'],visual_card=card.model_dump())
                            resources.read(visual_id)
                            item={k:v for k,v in item.items() if k!='sha256'}|dict(
                                visual_evidence_required=False,source_text=visible,
                                uncertainty=card.uncertainty,limitations=card.limitations)
                        session['action_results'].append(item)
                    session.setdefault('action_history',[]).extend(
                        dict(action=a,result=r,source_binding_receipt=source_binding_receipt_ids.get(index))
                        for index,(a,r) in enumerate(zip(response['actions'],session['action_results'])))
                    session['round'] += 1
                    session.pop('correction', None)
                    continue
                patch_gap_notes=[]
                if role=='active_patch' and response['result'] is not None and response['gaps']:
                    # A local repair may find issues in blocks it was not allowed
                    # to edit. Preserve those notes, but accept them only when
                    # each note explicitly points to known, non-editable blocks.
                    draft=payload.get('original_draft') or {}
                    known_block_ids={block.get('id','') for block in draft.get('blocks',[])
                                     if block.get('id')}
                    editable_block_ids=set(payload.get('editable_block_ids',[]))
                    for note in response['gaps']:
                        mentioned={block_id for block_id in known_block_ids
                            if re.search(r'(?<![A-Za-z0-9_-])'+re.escape(block_id)+
                                         r'(?![A-Za-z0-9_-])',note)}
                        if not mentioned or mentioned & editable_block_ids:
                            raise ValueError('局部补丁缺口未能证明位于可编辑范围之外：'+note)
                        patch_gap_notes.append(note)
                if (response['result'] is None or (response['gaps'] and not patch_gap_notes) or
                        (not response['ready_reason'].strip() and not is_v2(job))):
                    raise ValueError('资料未齐全，不能提交结果')
                if is_v2(job) and not response['ready_reason'].strip():
                    job.setdefault('nonblocking_protocol_notes',[]).append(dict(
                        step=key,reason='complete structured result returned without a ready_reason'))
                if is_v2(job) and role=='active_plan':
                    returned_gap_ids={gap['id'] for gap in response['result'].get('evidence_gaps',[])}
                    declared_gap_ids=set(session.get('declared_gap_ids',[]))
                    missing_gap_ids=sorted(declared_gap_ids-returned_gap_ids)
                    if missing_gap_ids:
                        raise ValueError('\u89c4\u5212\u6ca1\u6709\u4fdd\u5b58\u672c\u8f6e\u58f0\u660e\u7684 EvidenceGap ID\uff1a'+
                                         '、'.join(missing_gap_ids))
                if not all(resources.fully_read(sid) for sid in ids):
                    raise ValueError('当前来源仍有未读取部分')
                if role=='active_write' and job.get('archived_layout_source_ids'):
                    archived=set(job['archived_layout_source_ids'])
                    original=response['result']
                    removed=[block for block in original['blocks'] if block['source_ids']
                        and set(block['source_ids'])<=archived and not block['obligation_ids']]
                    if removed:
                        removed_ids={block['id'] for block in removed}
                        response['result']['blocks']=[block for block in original['blocks'] if block['id'] not in removed_ids]
                        response['result']['coverage']=[binding for binding in original['coverage']
                            if binding['block_id'] not in removed_ids]
                        delta=response['result']['knowledge_delta']
                        delta['concept_evidence']=[e for e in delta['concept_evidence'] if e['block_id'] not in removed_ids]
                        receipt=dict(step=key,block_ids=sorted(removed_ids),
                            reason='source-scoped archived branding without article obligations')
                        if receipt not in job.setdefault('archived_block_exclusions',[]):
                            job['archived_block_exclusions'].append(receipt)
                if role=='active_write':
                    source_kinds={o['id']:o['kind'] for o in source['objects']}
                    authored=response['result']['blocks']
                    repeated=[block for block in authored if block['kind']=='document_info'
                        and not block['obligation_ids'] and block['source_ids']
                        and all(source_kinds.get(sid)=='link' for sid in block['source_ids'])
                        and all(any(other is not block and sid in other['source_ids']
                                    for other in authored) for sid in block['source_ids'])]
                    if repeated:
                        removed={block['id'] for block in repeated}
                        response['result']['blocks']=[b for b in authored if b['id'] not in removed]
                        response['result']['coverage']=[e for e in response['result']['coverage']
                            if e['block_id'] not in removed]
                        response['result']['knowledge_delta']['concept_evidence']=[e
                            for e in response['result']['knowledge_delta']['concept_evidence']
                            if e['block_id'] not in removed]
                        job.setdefault('redundant_link_indexes',[]).append(sorted(removed))
                    replacements=bind_planned_headings(response['result'],payload['node'])
                    if replacements:job.setdefault('planned_heading_bindings',[]).extend(replacements)
                    actual_blocks={b['id']:b['markdown'] for b in response['result']['blocks']}
                    for entry in response['result']['knowledge_delta']['concept_evidence']:
                        if entry['output_quote'] in actual_blocks.get(entry['block_id'],''):continue
                        matches=[bid for bid,markdown in actual_blocks.items()
                                 if entry['output_quote'] in markdown]
                        if len(matches)==1:
                            job.setdefault('exact_concept_quote_bindings',[]).append(dict(
                                concept_id=entry['concept_id'],old_block_id=entry['block_id'],
                                block_id=matches[0]))
                            entry['block_id']=matches[0]
                    facts={fact['id']:fact['source_id'] for part in job.get('active_plans',[])
                           for fact in part['obligations']}
                    original_objects={obj['id']:obj for obj in source['objects']}
                    for block in response['result']['blocks']:
                        for fid in block['obligation_ids']:
                            sid=facts.get(fid)
                            if not sid or sid in block['source_ids']:continue
                            obj=original_objects[sid]
                            label=obj.get('text','').strip()
                            if (obj['kind']=='link' and label and
                                ' '.join(label.casefold().split()) in ' '.join(block['markdown'].casefold().split())):
                                block['source_ids'].append(sid)
                                receipt=dict(step=key,block_id=block['id'],source_id=sid,
                                    reason='exact original link label appears in authored block')
                                if receipt not in job.setdefault('exact_link_binding_repairs',[]):
                                    job['exact_link_binding_repairs'].append(receipt)
                if role=='active_plan' and session.get('direct_link_prefetch'):
                    repairs=bind_prefetched_link_evidence(response['result'],source,resources,
                                                          session['direct_link_prefetch'])
                    if repairs:
                        job.setdefault('prefetched_link_evidence_repairs',[]).extend(
                            dict(step=key,**repair) for repair in repairs)
                if role=='active_plan':
                    repairs=align_unplaced_concepts(response['result'])
                    if repairs:
                        job.setdefault('planned_concept_alignment',[]).extend(
                            dict(step=key,**repair) for repair in repairs)
                if role=='active_write':
                    repairs=separate_external_citations_from_original_bindings(
                        response['result'],ids,resources)
                    if repairs:
                        job.setdefault('external_citation_scoping',[]).extend(
                            dict(step=key,**repair) for repair in repairs)
                value = validate(response['result'], resources)
                if patch_gap_notes:
                    note=dict(step=key,reason='局部补丁已通过事务校验；范围外缺口保留待核查',
                              gaps=copy.deepcopy(patch_gap_notes))
                    if note not in job.setdefault('nonblocking_protocol_notes',[]):
                        job['nonblocking_protocol_notes'].append(note)
                    for gap in patch_gap_notes:
                        quality_note=f'{key}：范围外缺口待核查：{gap}'
                        if quality_note not in job.setdefault('quality_issues',[]):
                            job['quality_issues'].append(quality_note)
                session['resources'] = resources.state
                job.setdefault('external_resources', {}).update({k: v for k, v in resources.state['entries'].items()
                                                                 if v['kind'] == 'external'})
                session['complete'] = True
                if role=='active_plan':
                    _mark_split_recovery_completed(job,key)
                self.store.put_job(job)
                return value
            except (ValueError, KeyError) as error:
                if (role=='active_write' and '标题照搬了未解释的英文' in str(error)
                        and isinstance(raw,dict) and isinstance(raw.get('result'),dict)):
                    expected=heading_repair_context(raw['result'],payload['node'])
                    if expected:
                        repairs=self.call(job,key+'-heading-repair-'+str(session['corrections']),
                            'active_write',dict(
                                instruction=('Return one exact replacement for every listed heading. Translate or '
                                    'explain it as a concise natural Chinese Markdown heading, retain necessary '
                                    'official names, preserve the heading level, and change no body text'),
                                invalid_headings=expected),A.HeadingRepairs)
                        changed=apply_heading_repairs(raw['result'],expected,repairs)
                        job.setdefault('scoped_heading_repairs',[]).append(dict(step=key,edits=changed))
                        try:
                            value=validate(raw['result'],resources)
                        except (ValueError,KeyError) as repaired_error:
                            error=repaired_error
                        else:
                            session['resources']=resources.state
                            session['complete']=True
                            self.store.put_job(job)
                            return value
                # Planning is an inexpensive bounded artifact and may expose
                # several independent source/link/concept constraints in turn.
                # Contract-shape corrections precede the candidate draft and
                # do not consume either of its two scoped content patch rounds.
                correction_limit=(max(1,min(2,int(self.config.get('max_plan_repairs',2))))
                                  if role=='active_plan' else
                                  max(1,min(2,int(self.config.get('active_structure_correction_limit',2)))))
                glossary_error = (
                    role=='active_write'
                    and '短篇普通改写被扩成术语表' in str(error)
                )
                glossary_extra = (glossary_error and
                    not session.get('short_rewrite_glossary_repair_used') and
                    session['corrections'] >= correction_limit)
                if session['corrections'] >= correction_limit and not glossary_extra:
                    _mark_split_recovery_failed(job,key,error)
                    if _split_recovery_marker(job,key) is not None:
                        self.store.put_job(job)
                    raise ValueError('当前阶段结构修正后仍不成立：' + str(error)) from error
                if glossary_extra:
                    # This error class gets one narrowly scoped extra writer
                    # correction. It consumes the existing round budget and is
                    # recorded before dispatch so a restart cannot grant another.
                    prior_memory=bounded_established_memory(job)
                    session['short_rewrite_glossary_repair_used'] = True
                    session['short_rewrite_glossary_repair'] = dict(
                        source_ids=list(payload.get('node',{}).get('source_ids',[])),
                        knowledge_memory_digest=digest(prior_memory),
                        attempt=session['corrections']+1)
                session['corrections'] += 1
                if role in {'active_write','active_review'} and isinstance(raw,dict) and isinstance(raw.get('result'),dict):
                    session['previous_invalid_result']=raw['result']
                # The exact rejected response already lives in the provider
                # call receipt. Replaying a full invalid plan can push a
                # correction over relay limits; the deterministic validation
                # error is sufficient for the planner to regenerate it.
                job.setdefault('active_correction_receipts',[]).append(dict(
                    step=key,role=role,error=str(error),received_digest=digest(raw)))
                gap_policy=(missing_evidence_gap_policy(error) if role=='active_plan' else None)
                session['correction'] = dict(error=str(error),
                    instruction=('Every finding and link assessment must quote an exact substring of the '
                        'named block after compilation. Recheck block IDs and copy the existing characters; '
                        'do not paraphrase a quotation or drop a valid defect'
                        if role=='active_review' and ('引用实际正文' in str(error) or '准确引用实际正文' in str(error))
                        else 'The {{source:id}} insertion token is valid only for protected image, code, '
                        'table or link objects listed in protected_originals. For ordinary text and page '
                        'metadata, write concise natural Chinese in its place, retaining exact dates, names '
                        'and qualifications. Do not paste large English source passages or remove coverage'
                        if role=='active_write' and '原对象插入标记' in str(error)
                        else 'Rewrite the displayed heading as natural Chinese. Preserve necessary official '
                        'English names in parentheses at first use and explain unfamiliar abbreviations; do '
                        'not leave the heading as an unexplained copy of the English source title. Preserve '
                        'all body content, source bindings, images and factual qualifications'
                        if role=='active_write' and '标题照搬了未解释的英文' in str(error)
                        else 'Use only the saved direct-link prefetch entries for target-page evidence; '
                        'the source page and link label are not target evidence. For each inaccessible '
                        'direct target use its exact recorded failure in unavailable_reason and no invented destination')
                        if role=='active_plan' and ('内容链接引用了' in str(error) or '内容链接缺少' in str(error))
                        else 'Move each concept introduction before its first use, or remove a false prerequisite. '
                        'Within a node, list prerequisite concepts before dependent concepts. Preserve source '
                        'obligations, direct-link evidence and the article order unless a genuine dependency requires change'
                        if role=='active_plan' and ('概念前提' in str(error) or '编排依赖' in str(error))
                        else 'For an unverified English name or abbreviation, request a direct official page action '
                        'and cite an exact quote actually containing that name, or avoid introducing that term; '
                        'preserve all other valid fields and bindings'
                        if role=='active_plan' and '英文名称或缩写展开' in str(error)
                        else 'Return a link_assessments entry for every content link source ID named in the error. '
                        'Copy each adjacent authored explanation exactly from actual_draft into output_quote, '
                        'use the block that contains it, and judge all five booleans independently. '
                        'Keep the existing findings, checked obligations and format decisions; do not rewrite the article'
                        if role=='active_review' and '核对遗漏知识链接' in str(error)
                        else 'Correct only this invalid artifact; preserve all valid content and bindings')
                if gap_policy:
                    session['correction']['instruction']=gap_policy['instruction']
                if glossary_error:
                    scope_node=payload.get('node',{})
                    session['correction']['scope']={
                        'current_node':dict(id=scope_node.get('id'),title=scope_node.get('title'),
                                            source_ids=list(scope_node.get('source_ids',[]))),
                        'already_established_memory':dict(
                            required_prerequisites=payload.get('established_memory',[]),
                            prior_concept_anchors=bounded_established_memory(job)),
                        'current_link_roles':payload.get('link_guides',[]),
                    }
                    session['correction']['instruction']=(
                        'This is a short ordinary rewrite, not a glossary or a new lesson. Return one complete '
                        'corrected WrittenUnit that follows the current node source boundary and link roles. '
                        'Treat source obligations, the concept ledger and reasoning steps as coverage constraints, '
                        'not as requests for one definition or heading per item; keep the reasoning order and write '
                        'ordinary concise prose with necessary link explanations inline. Reserve a formal definition '
                        'list for a genuinely central new technical concept the reader cannot otherwise follow. '
                        'Use established_memory to treat concepts already explained in earlier units as known: '
                        'do not define them again or turn linked names into extra glossary entries. Keep only a '
                        'definition that the current source segment truly needs; otherwise explain terms briefly '
                        'in the article flow. Preserve every exception, contrast, qualification, date, name, '
                        'source obligation, link, and already-correct material from previous_invalid_result. '
                        'Correct any factual misstatement against the current node obligations and opened source. '
                        'Keep the original article purpose and order, and remove only repetitive definition detours.')
                elif role=='active_write' and '标题照搬了未解释的英文' in str(error):
                    session['correction']['instruction']=(
                        'Rewrite every displayed heading as natural Chinese. Preserve necessary official '
                        'English names in parentheses at first use and explain unfamiliar abbreviations; do '
                        'not leave any heading as an unexplained copy of the English source title. Preserve '
                        'all body content, source bindings, images and factual qualifications')
                elif role=='active_write' and any(message in str(error) for message in (
                        '正文图片缺少与原图绑定的说明','正文非文字材料缺少与原对象绑定的说明')):
                    session['correction']['instruction']=(
                        'Add a concise explanatory block immediately after each named source image. Bind '
                        'that block to the same source_id and state only what the saved visual card, caption '
                        'and adjacent source text support. Preserve the existing Chinese headings and every '
                        'other already-correct block exactly')
                elif role=='active_write' and '概念记忆的引文不在实际正文中' in str(error):
                    session['correction']['instruction']=(
                        'For every concept_evidence entry, copy output_quote as an exact non-empty substring '
                        'from the named block in this same WrittenUnit. Rebind block_id if the exact sentence '
                        'is in another block; remove a concept from established_concepts and concept_evidence '
                        'when the draft does not actually establish it. Preserve the authored prose and all '
                        'source bindings exactly')
                elif role=='active_write' and '实际知识增量与规划不符' in str(error):
                    session['correction']['instruction']=(
                        'Correct only knowledge_delta against the supplied node and completed prose. Set '
                        'established_concepts to exactly node.establishes_concepts, explained_obligations '
                        'to exactly node.obligation_ids, and unresolved_prerequisites to an empty list after '
                        'confirming the prose explains them. Bind every concept_evidence quote to an exact '
                        'substring of its named block. Preserve all authored blocks and source bindings exactly')
                elif role=='active_write' and '正文引用了当前单元之外的原文' in str(error):
                    session['correction']['instruction']=(
                        'Remove every source_ids value that is not listed in node.source_ids and rebind the '
                        'affected block only to the exact in-scope source objects that support its text. Prior '
                        'tail context may guide a transition but is not evidence for this batch. Preserve the '
                        'Chinese headings, image explanations and all other already-correct prose')
                elif role=='active_review' and ('核对意见未准确引用实际正文' in str(error)
                                                or '核对意见未准确引用当前原文' in str(error)):
                    session['correction']['instruction']=(
                        'For every finding and link assessment, copy output_quote as an exact non-empty '
                        'substring from the named block in actual_draft. Rebind block_id when that exact '
                        'text is in another block. Preserve all valid findings and checked obligations; '
                        'remove only a claim that cannot be bound to exact existing text')
                session['round'] += 1
        raise ValueError('按需取材仍未收敛，已保存资料与具体缺口，没有生成替代稿')

    def step(self, job):
        stage = job['stage']
        if job.get('pipeline') in {PIPELINE, PIPELINE_V2}:
            version = job.get('core_chain_version')
            if version not in {0, 1}:
                raise Conflict('内部错误：任务缺少明确的新旧主链版本标记')
            if version == 1 and (job.get('pipeline') != PIPELINE_V2 or
                    stage in {'active_review', 'active_revision', 'active_format'}):
                raise Conflict('内部错误：新版任务禁止进入旧逐批审核或修补流程')
            if version == 0 and stage in {'active_integrity', 'active_integrity_repair'}:
                raise Conflict('内部错误：旧任务禁止进入新版整稿审核流程')
        if stage in {'active_write','active_deliver'}:
            for point in job.get('active_checkpoints',[]):
                if not point.get('unresolved_format'):continue
                unit={'blocks':[b for b in job.get('draft',{}).get('blocks',[])
                    if b.get('unit_id')==point['node_id']]}
                if not unit['blocks']:continue
                replay_bundle=load_bundle(job['writing_skill']['root'],job['writing_skill']['package_digest'])
                replay=respect_original_format(scan(replay_bundle,unit,self.store.root/'production'/
                    job['id']/point['node_id']/('checkpoint-format-replay-'+point['draft_digest'][:12])),
                    unit,job['inventory'])
                if checkpoint_format_clearable(point,unit,replay):
                    clear_resolved_format_issue(job,{'unresolved_format':point['unresolved_format']},point['node_id'])
                    point['unresolved_format']={}
                    point['format_reconciliation']=dict(draft_digest=point['draft_digest'],
                        operation='exact_checkpoint_same_bytes_format_replay')
        if stage in {'active_write','active_deliver'} and any(
            point.get('unresolved_content_findings') or point.get('unresolved_revision')
            or point.get('unresolved_format',{}).get('findings')
            or point.get('unresolved_format',{}).get('candidates')
            for point in job.get('active_checkpoints',[])):
            # V2 keeps the exact checkpoint and its findings, but never turns a
            # local repair miss into a document-wide stop.  The next unit still
            # has the saved source inventory and the continuity tail available.
            if is_v2(job):
                note='前序单元仍有已记录的内容或格式问题，已保留原稿并继续后续生成'
                if note not in job.setdefault('quality_issues',[]):
                    job['quality_issues'].append(note)
            else:
                raise ValueError('前一单元仍有未解决的内容或格式问题；已保存原稿与检查结果，停止后续付费生成')
        bundle = load_bundle(job['writing_skill']['root'], job['writing_skill']['package_digest'])
        if stage in {'active_index','active_visual','active_plan'} and job.get('terminology_catalog_version')!=8:
            from .terminology import verified_terms
            job['verified_terminology']=verified_terms(self.store,job['source'])
            job['terminology_catalog_version']=8
            self.store.put_job(job)
        if stage == 'active_index':
            from .word_structures import resolve_word_structures
            from .visual_sources import classify_transparent
            from .source_context import classify_web_chrome
            source = resolve_word_structures(self.store, classify_layout_tables(
                classify_web_chrome(self.store,classify_inert_markup(job['source']))))
            source = classify_transparent(self.store, source)
            source = classify_transparent_svg_placeholders(source)
            from .materials import require_complete_web_materials
            require_complete_web_materials(source)
            for original in source.get('originals', []):
                self.store.read_blob(original['sha256'])
            for resource in source.get('resources', []):
                self.store.read_blob(resource.get('sha256', resource['id']))
            job['source'] = source
            job['visual_count']=sum(_needs_active_visual_card(o) for o in source['objects'])
            job['stage'] = 'active_visual'
            return 'queued'
        source = job['source']
        if stage == 'active_visual':
            from .visual_sources import image_resources
            from .source_context import classify_web_chrome
            source=classify_web_chrome(self.store,source)
            job['source']=source
            done = {card['source_id'] for card in job['visual_cards']}
            pending = [o for o in source['objects'] if _needs_active_visual_card(o)
                       and o['id'] not in done]
            if pending:
                visual_batch_size=(self.config.get('active_v2_visual_batch_size',6) if is_v2(job) else 3)
                batch=pending[:max(1,min(8,int(visual_batch_size)))]
                key = 'active-visual-' + '-'.join(page['id'] for page in batch)
                result = A.VisualCards.model_validate(normalize_visual_card_lists(self.call(job, key, 'active_visual',
                    dict(pages=batch, _image_resources=image_resources(self.store, batch, source)), A.VisualCards))).model_dump()
                if [c['source_id'] for c in result['cards']] != [page['id'] for page in batch]:
                    raise ValueError('视觉卡没有准确对应原对象')
                for page,card in zip(batch,result['cards']):
                    if not set(card['blocking_uncertainty'])<=set(card['uncertainty']):
                        raise ValueError('视觉阻断项必须引用实际无法确定的内容')
                    if page.get('kind')=='media' and card['blocking_uncertainty']:
                        card['limitations']=list(dict.fromkeys(card.get('limitations',[])+
                            card['blocking_uncertainty']))
                        card['blocking_uncertainty']=[]
                    if card['blocking_uncertainty']:
                        # One targeted read of the same image, not a mandatory second reviewer
                        detail = A.VisualCards.model_validate(normalize_visual_card_lists(self.call(job, key+'-detail-'+page['id'], 'active_visual',
                            dict(pages=[page], previous_card=card,
                                 _image_resources=image_resources(self.store, [page], source, [page['id']])), A.VisualCards))).model_dump()
                        if [c['source_id'] for c in detail['cards']] != [page['id']]:
                            raise ValueError('图像局部修复没有准确对应原对象')
                        card = detail['cards'][0]
                        if card['blocking_uncertainty']:
                            # A genuinely unreadable glyph or cropped region is
                            # a local limitation, not a reason to discard the
                            # whole document.  Keep the original image and the
                            # model's explicit uncertainty for the writer and
                            # final reviewer, then continue with every readable
                            # part of the material.
                            card['limitations']=list(dict.fromkeys(
                                card.get('limitations',[])+card['blocking_uncertainty']))
                            card['uncertainty']=list(dict.fromkeys(
                                card.get('uncertainty',[])+card['blocking_uncertainty']))
                            card['blocking_uncertainty']=[]
                    page['original_extracted_text'] = page.get('text', '')
                    page['text'] = card['source_text'] or card['visible_content']
                    page['visual_card'] = card
                    job['visual_cards'].append(card)
                job['visual_index']=len(job['visual_cards'])
                visited={page['id'] for page in batch}
                source['unknown'] = [g for g in source.get('unknown', []) if g['object_id'] not in visited]
                return 'queued'
            active_ids={o['id'] for o in source['objects'] if o.get('source_scope') not in
                {'site_chrome','source_metadata','layout_decorative'}}
            unresolved=[g for g in source.get('unknown',[]) if g['object_id'] in active_ids]
            by_id={o['id']:o for o in source['objects']}
            missing_media=[g for g in unresolved if
                (by_id.get(g['object_id'],{}).get('kind') in {'image','media'}
                 and by_id.get(g['object_id'],{}).get('target'))]
            blocking=[g for g in unresolved if g not in missing_media]
            if missing_media:
                # The original URL, label, locator and bytes of the uploaded
                # document are still present. A blocked remote image must be
                # disclosed as unavailable, not treated as a readable image or
                # allowed to discard the rest of an otherwise complete text.
                job['material_acquisition_limits']=[dict(
                    object_id=g['object_id'],target=by_id[g['object_id']]['target'],
                    reason=g['reason']) for g in missing_media]
            if blocking:
                raise ValueError('原件正文仍有无法读取的对象，尚未开始改写：' + json.dumps(blocking, ensure_ascii=False))
            from .visual_sources import decorative_resource
            job['archived_layout_source_ids']=[o['id'] for o in source['objects'] if decorative_resource(o)]
            plan_chars=(self.config.get('active_v2_plan_source_chars',9000) if is_v2(job)
                        else self.config.get('active_plan_source_chars',12000))
            plan_objects=(self.config.get('active_v2_plan_source_objects',24) if is_v2(job)
                          else self.config.get('active_plan_source_objects',10))
            job['active_groups'] = [[o['id'] for o in group] for group in inventory_groups(
                [o for o in source['objects'] if not decorative_resource(o)],
                plan_chars,plan_objects)]
            job['active_group_prefixes']=['p'+str(index+1)
                                          for index in range(len(job['active_groups']))]
            if not job['active_groups']:raise ValueError('原件仅含已存档的排版资源，没有可改写正文')
            job['active_partition_count']=len(job['active_groups'])
            job['web_chrome_scope_version']=2
            job['stage'] = 'active_plan'
            return 'queued'
        if stage == 'active_plan':
            from .visual_sources import decorative_resource
            node_chars=(self.config.get('active_v2_node_source_chars',6500) if is_v2(job)
                        else self.config.get('active_node_source_chars',6500))
            node_objects=(self.config.get('active_v2_node_source_objects',24) if is_v2(job)
                          else self.config.get('active_node_source_objects',20))
            if job.get('web_chrome_scope_version',0)<2 and not job['active_plans']:
                from .source_context import classify_web_chrome
                source=classify_web_chrome(self.store,source)
                job['source']=source
                job['active_groups']=[[o['id'] for o in group] for group in inventory_groups(
                    [o for o in source['objects'] if not decorative_resource(o)],
                    self.config.get('active_plan_source_chars',12000),
                    self.config.get('active_plan_source_objects',10))]
                job['active_group_prefixes']=['p'+str(index+1)
                                              for index in range(len(job['active_groups']))]
                job['active_partition_count']=len(job['active_groups'])
                job['web_chrome_scope_version']=2
            index = job['active_partition_index']
            if index==len(job['active_groups']):
                retired=retire_unanchored_abbreviations(job['active_plans'],source)
                if retired:job.setdefault('unanchored_formal_concepts',[]).extend(retired)
                job['unanchored_abbreviations_checked']=True
                responsibilities=(planned_object_responsibilities(job['active_plans'],source)
                                  if is_core_chain(job) else None)
                if responsibilities is not None:
                    job['object_responsibilities']=responsibilities
                job['writing_batches']=writing_batches(job['active_plans'],source,
                    node_chars,self.config.get('active_node_concept_limit',10),node_objects,
                    {sid for sid,role in responsibilities.items() if role['present']}
                    if responsibilities is not None else None)
                if is_v2(job):
                    job['cross_batch_review_required']=any(
                        bool(node.get('cross_batch_risks'))
                        for part in job['active_plans'] for node in part.get('nodes',[]))
                job['inventory'],job['plan']=legacy_artifacts(
                    job['active_plans'],source,job['writing_batches'],responsibilities)
                job['draft']={'blocks':[]}
                job['stage']='active_write'
                return 'queued'
            ids = job['active_groups'][index]
            prefixes=job.get('active_group_prefixes') or []
            prefix=(prefixes[index] if index<len(prefixes) and prefixes[index]
                    else 'p' + str(index+1))
            prior = job['active_plans']
            payload = dict(goal=job['goal'], task_mode=job['transformation_mode'], assigned_source_ids=ids,
                object_responsibility_decisions_required=is_core_chain(job),
                **plan_document_preview(source,ids,job['active_groups'][:index]),
                source_spans=prompt_source_spans(source,ids),
                partition_prefix=prefix, node_source_char_limit=node_chars,
                node_concept_limit=self.config.get('active_node_concept_limit',10),
                immutable_contract=prior[0]['contract'] if prior else None,
                preceding_plans=preceding_plan_context(prior),
                visual_cards=[{k:v for k,v in c.items() if k!='source_text'} for c in job['visual_cards'] if c['source_id'] in ids],
                unavailable_original_media=self.unavailable_media(job,ids),
                unavailable_media_rule='An unavailable original image has an exact URL and label but no readable pixels. Preserve that reference and disclose the limitation; never describe unseen image content as observed.')
            def validate_planning(value,resources):
                links={obj['id'] for obj in source['objects']
                       if obj['id'] in ids and obj['kind']=='link'}
                value,deferred=filter_future_partition_link_briefs(value,links)
                if deferred:job.setdefault('deferred_link_briefs',[]).append(dict(
                    partition=prefix,source_ids=deferred,
                    reason='outside assigned source-link objects'))
                value,editorial=strip_editorial_link_limits(value)
                if editorial:job.setdefault('editorial_link_limits_removed',[]).extend(editorial)
                value,repairs=rebind_single_source_spans(value,source,ids)
                if repairs:job.setdefault('source_span_alignments',[]).extend(repairs)
                value,evidence_repairs=downgrade_original_only_evidence_bindings(value,source,ids)
                if evidence_repairs:
                    recorded=job.setdefault('original_evidence_downgrades',[])
                    for receipt in evidence_repairs:
                        item=dict(partition=prefix,**receipt)
                        if item not in recorded:recorded.append(item)
                original_gap_ids={item['id'] for item in value.get('evidence_gaps',[])}
                original_resolution_ids={item['gap_id'] for item in value.get('evidence_resolutions',[])}
                original_binding_ids={item['id'] for item in value.get('evidence_bindings',[])}
                plan=validate_plan(value|({'contract':prior[0]['contract']} if prior else {}),source,ids,prior,
                    job['transformation_mode'],node_chars,
                    require_spans=True,resources=resources,
                    require_link_briefs=bool(job.get('link_contract_version')),
                    archived_ids=job.get('archived_layout_source_ids',[]),
                    concept_limit=self.config.get('active_node_concept_limit',10),
                    require_object_responsibilities=is_core_chain(job),
                    classified_link_decisions=(job.get('active_sessions',{}).get('active-plan-'+prefix,{})
                        .get('link_decisions',{}) if is_core_chain(job) else None),
                    default_reference_links=(is_core_chain(job) and
                        job['transformation_mode']=='rewrite'))
                if is_core_chain(job) and job['transformation_mode']=='rewrite':
                    receipt=dict(partition=prefix,
                        gap_ids=sorted(original_gap_ids-{item['id'] for item in plan['evidence_gaps']}),
                        resolution_gap_ids=sorted(original_resolution_ids-
                            {item['gap_id'] for item in plan['evidence_resolutions']}),
                        binding_ids=sorted(original_binding_ids-
                            {item['id'] for item in plan['evidence_bindings']}))
                    if receipt['gap_ids']:
                        job.setdefault('reference_link_gap_prunes',[]).append(receipt)
                # Drop a proposed formal acronym that the cited original never
                # uses before checking its name. A model may otherwise spend its
                # entire evidence budget proving an unnecessary added glossary
                # term and leave a short rewrite unable to start.
                retired=retire_unanchored_abbreviations([plan],source)
                if retired:
                    job.setdefault('unanchored_formal_concepts',[]).extend(retired)
                return validate_names(plan,resources,allow_unverified_downgrade=is_v2(job)) if job.get('naming_contract_version') else plan
            context_recovery=next((item for item in job.get('active_plan_context_recoveries',[])
                                   if item.get('recovery_prefix')==prefix and item.get('status')=='queued'),None)
            if context_recovery:
                context_recovery.update(status='dispatched',dispatched_at=time.time())
                self.store.put_job(job)
            try:
                result = self.turn(job, 'active-plan-'+prefix, 'active_plan', A.CompositionPart if prior else A.CompositionPlan, source, ids, payload,validate_planning)
            except Exception as error:
                if context_recovery:
                    context_recovery.update(status='failed',error_type=type(error).__name__,failed_at=time.time())
                    self.store.put_job(job)
                raise
            job['active_plans'].append(result)
            job['active_partition_index'] += 1
            for recovery in job.get('active_plan_context_recoveries',[]):
                if recovery.get('recovery_prefix')==prefix and recovery.get('status') in {'queued','dispatched','failed'}:
                    recovery.update(status='completed',completed_at=time.time())
                    recovery.pop('error_type',None)
                    recovery.pop('failed_at',None)
            if job['active_partition_index'] == len(job['active_groups']):
                retired=retire_unanchored_abbreviations(job['active_plans'],source)
                if retired:job.setdefault('unanchored_formal_concepts',[]).extend(retired)
                job['unanchored_abbreviations_checked']=True
                responsibilities=(planned_object_responsibilities(job['active_plans'],source)
                                  if is_core_chain(job) else None)
                if responsibilities is not None:
                    job['object_responsibilities']=responsibilities
                job['writing_batches']=writing_batches(job['active_plans'],source,
                    node_chars,
                    self.config.get('active_node_concept_limit',10),
                    node_objects,
                    {sid for sid,role in responsibilities.items() if role['present']}
                    if responsibilities is not None else None)
                if is_v2(job):
                    job['cross_batch_review_required']=any(
                        bool(node.get('cross_batch_risks'))
                        for part in job['active_plans'] for node in part.get('nodes',[]))
                job['inventory'], job['plan'] = legacy_artifacts(job['active_plans'], source,job['writing_batches'],responsibilities)
                job['draft'] = {'blocks': []}
                job['stage'] = 'active_write'
            return 'queued'
        if stage=='active_write' and job['unit_index']==0 and not job.get('unanchored_abbreviations_checked'):
            retired=retire_unanchored_abbreviations(job['active_plans'],source)
            if retired:
                job.setdefault('unanchored_formal_concepts',[]).extend(retired)
                # A saved writer request already owns the original batch
                # boundaries. Never merge it with a later source range while
                # replaying that exact paid response.
                retired_ids=set(retired)
                for node in job['writing_batches']:
                    node['establishes_concepts']=[cid for cid in node['establishes_concepts']
                        if cid not in retired_ids]
                    node['requires_concepts']=[cid for cid in node['requires_concepts']
                        if cid not in retired_ids]
                job['inventory'],job['plan']=legacy_artifacts(job['active_plans'],source,job['writing_batches'],
                    job.get('object_responsibilities') if is_core_chain(job) else None)
            job['unanchored_abbreviations_checked']=True
        nodes = job.get('writing_batches') or [n for p in job['active_plans'] for n in p['nodes']]
        if stage == 'active_write':
            node = nodes[job['unit_index']]
            core_rewrite=is_core_chain(job) and job['transformation_mode']=='rewrite'
            if is_core_chain(job):
                # The planner accounts for archived objects, but only
                # presentation obligations belong in the reader's draft.
                presented={item['id'] for part in job['active_plans']
                           for item in part['obligations']
                           if job['object_responsibilities'][item['source_id']]['present']}
                node=copy.deepcopy(node)
                node['obligation_ids']=[fid for fid in node['obligation_ids'] if fid in presented]
                for section in node.get('section_outline',[]):
                    section['obligation_ids']=[fid for fid in section['obligation_ids'] if fid in presented]
            job['current_unit_id'] = node['id']
            current_objects=[obj for obj in source['objects'] if obj['id'] in node['source_ids']]
            coalesce_current_short_rewrite_concepts(job,node,current_objects)
            name_completions=short_rewrite_name_completions(job,node)
            obligations = [o for p in job['active_plans'] for o in p['obligations'] if o['id'] in node['obligation_ids']]
            risk_context=(bounded_prior_context(job['draft']['blocks'])
                          if is_v2(job) and node.get('cross_batch_risks') else [])
            current_concepts=[c for p in job['active_plans'] for c in p['concepts']
                              if c['id'] in node['requires_concepts']+node['establishes_concepts']]
            payload = dict(contract=writing_batch_contract(job['active_plans'][0]['contract'],node,job.get('goal','')), node=writer_node_context(node,core_rewrite=core_rewrite),
                **writer_reference_payload(job,source,node['source_ids'],obligations,
                    core_rewrite=core_rewrite),
                obligation_source_rule='Resolve each obligation through source_id and source_span_ids in opened_resources.',
                writing_coverage_rule=(('Source obligations are the factual coverage constraints. The concept ledger tracks identity '
                    'and continuity. Planner reasoning steps are internal scratchpad, not sentences, claims, explanations, '
                    'or checklist items that must separately appear in the article. '
                    'Explain source code only when the explanation adds understanding beyond its visible comments and output. '
                    'For any name_completion, add only the verified Chinese-English pair at its first supported mention '
                    'without restating its definition or creating a term entry.') if core_rewrite else
                    'Treat source obligations, the concept_ledger and reasoning_step_coverage as coverage constraints. '
                    'Reasoning steps guide the source-faithful order and checks; they are not separate definition or heading requests. '
                    'Write a short plain rewrite as ordinary prose, explain a necessary link briefly inline according to its link role, '
                    'and reserve a formal definition list for a genuinely central new technical concept. '
                    'For any name_completion, the Chinese concept has already been explained; add only the verified Chinese-English '
                    'pair at its first supported mention in this unit, without restating its definition or creating a term entry.'),
                concept_ledger=dict(required_concept_ids=list(node['requires_concepts']),
                                    newly_established_concept_ids=list(node['establishes_concepts']),
                                    name_completion_ids=[item['id'] for item in name_completions]),
                name_completions=name_completions,
                **writer_reasoning_coverage(node,core_rewrite=core_rewrite),
                displayed_heading_rule=('Every displayed heading must contain natural Chinese. Keep necessary official '
                    'English names in parentheses after the Chinese wording; never copy an English-only source heading.'),
                source_binding_rule=('Every authored block source_ids entry must come only from node.source_ids. Prior-tail '
                    'context is for transitions and must never be rebound as evidence for this batch.'),
                protected_object_catalog=[dict(source_id=sid,kind=next(o['kind'] for o in source['objects'] if o['id']==sid),
                    insert_marker='{{source:'+sid+'}}') for sid in protected_objects(job['inventory'])
                    if sid in node['source_ids'] and (not is_core_chain(job)
                        or job['object_responsibilities'][sid]['present'])],
                composition_instructions=('section_outline is the original planned order inside this writing batch, not separate API calls. Keep consecutive list items inside one block. Archived source metadata does not belong in the reader article. Insert each required protected object by its given marker; source_ids alone is an evidence mapping, not a displayed object. For unavailable_original_media, preserve the source URL and label and say that the pixels could not be retrieved; never infer the visual content. '
                    'For object_responsibilities, preserve is handled by the archive; present selects what belongs in the article; explain requests explanation only when needed to follow the author. Do not create prose about a preserved but unpresented object. '
                    'For reference_link_ids, the original link label and target fulfill coverage; do not describe or infer the destination. For reference_label_ids, the related-resource label itself fulfills coverage even if the saved plan requested explanation. Do not expand a label into a term definition or account of the target.'
                    if is_core_chain(job) else
                    'section_outline is the original planned order inside this writing batch, not separate API calls. Keep consecutive list items inside one block. Source metadata belongs in unchanged, collapsed end matter, never a prose tour of HTML scripts. Insert each required protected object by its given marker; source_ids alone is an evidence mapping, not a displayed object. For unavailable_original_media, preserve the source URL and label and say that the pixels could not be retrieved; never infer the visual content.'),
                global_spine=[dict(id=n['id'], title=n['title'], purpose=n['purpose']) for n in nodes],
                concepts=writer_concepts_for_payload(current_concepts,
                    core_rewrite=is_core_chain(job) and job['transformation_mode']=='rewrite'),
                established_memory=[dict(node_id=m['node_id'],draft_digest=m['draft_digest'],resource_id=m.get('resource_id'),
                    established=[c for c in m['established'] if c['id'] in node['requires_concepts']])
                    for m in job['knowledge_memory'] if any(c['id'] in node['requires_concepts'] for c in m['established'])],
                previous_final_tail=[b['markdown'] for b in job['draft']['blocks'][-2:]],
                risk_review_context=risk_context,
                already_placed_source_ids=list({sid for b in job['draft']['blocks'] for sid in b.get('embedded_object_ids',[])}),
                next_node=(writer_node_context(nodes[job['unit_index']+1],core_rewrite=True)
                    if core_rewrite and job['unit_index']+1<len(nodes) else
                    nodes[job['unit_index']+1] if job['unit_index']+1<len(nodes) else None),
                visual_cards=[{k:v for k,v in c.items() if k!='source_text'} for c in job['visual_cards'] if c['source_id'] in node['source_ids']],
                unavailable_original_media=self.unavailable_media(job,node['source_ids']))
            def validate(value, resources):
                result=validate_written(value, node, job['inventory'], bundle, job['draft'],
                    obligations,payload['concepts'],payload['name_completions'])
                if (job['transformation_mode']=='rewrite' and overgrown_short_rewrite_glossary(
                        result[0],[obj for obj in source['objects'] if obj['id'] in node['source_ids']])):
                    raise ValueError('短篇普通改写被扩成术语表：只为不可缺少的专业概念保留正式定义，日常词自然解释，保持原文主线')
                return result
            draft, coverage, delta = self.turn(job, 'active-write-'+node['id'], 'active_write', A.WrittenUnit,
                source, node['source_ids'], payload, validate)
            patch_limit=max(1,min(2,int(self.config.get('active_content_patch_limit',2)),
                                  int(self.config.get('active_format_patch_limit',2))))
            job['active_candidate'] = dict(draft=draft, coverage=coverage, delta=delta, rounds=0,
                patch_limit=patch_limit,
                missing_concept_names=missing_concept_names(
                    [c for c in payload['concepts'] if c['id'] in node['establishes_concepts']]
                    +payload['name_completions'],draft))
            if is_core_chain(job):
                return self.commit_candidate(job,node,job['active_candidate'])
            job['stage'] = 'active_review'
            return 'queued'
        if stage=='active_review':
            node=nodes[job['unit_index']];candidate=job['active_candidate']
            post_patch_review=bool(is_v2(job) and candidate.get('post_patch_review_required'))
            name_completions=short_rewrite_name_completions(job,node)
            draft=normalize_authored_spacing(candidate['draft'],job['inventory'])
            if canonical(draft)!=canonical(candidate['draft']):
                candidate.setdefault('layout_normalizations',[]).append(dict(
                    before=digest(canonical(candidate['draft']).encode()),
                    after=digest(canonical(draft).encode()),
                    operation='authored_prose_spacing_before_review'))
                candidate['draft']=draft
                by_id={b['id']:b for b in draft['blocks']}
                for binding in candidate['coverage']:
                    binding['output_quote']=by_id[binding['block_id']]['markdown']
                candidate['delta']['concept_evidence']=[e for e in candidate['delta'].get('concept_evidence',[])
                    if e['block_id'] in by_id and e['output_quote'] in by_id[e['block_id']]['markdown']]
            round_=int(candidate.get('content_review_round',0))
            visual_cards=[{k:v for k,v in c.items() if k!='source_text'} for c in job.get('visual_cards',[]) if c['source_id'] in node['source_ids']]
            preflight=respect_original_format(scan(bundle,draft,self.store.root/'production'/job['id']/node['id']/('review-preflight-'+digest(canonical(draft).encode())[:16])),draft,job['inventory'])
            preflight['format']['candidates']=[c for c in preflight['format']['candidates']
                if candidate_key(c,draft) not in candidate.get('dismissed_keys',[])]
            format_issues=located_format_issues(preflight,draft)
            obligations=[f for p in job['active_plans'] for f in p['obligations'] if f['id'] in node['obligation_ids']]
            review_ids=set(node['obligation_ids'])
            patch=candidate.get('last_patch')
            if patch and any(set(r['checked_obligation_ids'])==review_ids for r in candidate.get('content_reviews',[])):
                changed_ids={e['block_id'] for e in patch['edits']}
                review_ids={fid for b in draft['blocks'] if b['id'] in changed_ids for fid in b['obligation_ids']}
            current_link_guides=reviewed_link_guides(job,node['source_ids'],obligations,review_ids)
            def validate_review(value,resources):
                result=expand_exact_grouped_findings(A.ContentReview.model_validate(value).model_dump(),draft)
                checked=set(result['checked_obligation_ids'])
                if not review_ids<=checked<=set(node['obligation_ids']):
                    raise ValueError('核对遗漏本次影响的原信息，或引用了其他单元')
                blocks={b['id']:b for b in draft['blocks']};objects={o['id']:o for o in source['objects']}
                for row in result['findings']+result['link_assessments']:
                    current=blocks.get(row['block_id'])
                    if current and row['output_quote'] in current['markdown']:continue
                    matches=[(bid,exact_source_quote(row['output_quote'],block['markdown']))
                             for bid,block in blocks.items()]
                    matches=[(bid,quote) for bid,quote in matches if quote is not None]
                    if len(matches)==1:
                        prior=row['block_id'];row['block_id'],row['output_quote']=matches[0]
                        result.setdefault('output_quote_alignments',[]).append(dict(
                            submitted_block_id=prior,actual_block_id=row['block_id'],
                            operation='exact_existing_text_rebind'))
                    elif current:
                        fragment=exact_review_quote_fragment(row['output_quote'],current['markdown'])
                        if fragment:
                            row['output_quote']=fragment
                            result.setdefault('output_quote_alignments',[]).append(dict(
                                actual_block_id=current['id'],operation='unique_exact_review_fragment',
                                reviewer_quote_was_not_verbatim=True))
                for finding in result['findings']:
                    block=blocks.get(finding['block_id'])
                    if not block or finding['output_quote'] in block['markdown']:continue
                    from difflib import SequenceMatcher
                    lines=[line for line in block['markdown'].splitlines() if line.strip()]
                    ranked=sorted(((SequenceMatcher(None,finding['output_quote'],line).ratio(),line)
                                   for line in lines),reverse=True)
                    if (ranked and ranked[0][0]>=.30 and
                            (len(ranked)==1 or ranked[0][0]-ranked[1][0]>=.05)):
                        finding['output_quote']=ranked[0][1]
                        result.setdefault('output_quote_alignments',[]).append(dict(
                            actual_block_id=block['id'],operation='nearest_exact_line_for_reported_defect',
                            reviewer_quote_was_not_verbatim=True))
                literals=protected_objects(job['inventory'])
                guides=[]
                if job.get('link_contract_version'):
                    guides=[g for g in current_link_guides if g['role']=='content']
                    content_ids={g['source_id'] for g in guides}
                    non_content={g['source_id'] for g in link_guides(job,node['source_ids'])
                                 if g['role']!='content' or g['source_id'] not in content_ids}
                    ignored=[a for a in result['link_assessments']
                             if a['source_id'] in non_content]
                    if ignored:result['ignored_noncontent_link_assessments']=ignored
                    assessments=[a for a in result['link_assessments']
                                 if a['source_id'] not in non_content]
                    result['link_assessments']=assessments
                    unique([a['source_id'] for a in assessments], '知识链接核对')
                    submitted={a['source_id'] for a in assessments}
                    if submitted!=content_ids:
                        raise ValueError('核对遗漏知识链接的实际正文解释：缺少 '+
                            ', '.join(sorted(content_ids-submitted))+'；多出 '+
                            ', '.join(sorted(submitted-content_ids)))
                    for assessment in assessments:
                        block=blocks.get(assessment['block_id'])
                        if not block or assessment['output_quote'] not in block['markdown']:
                            raise ValueError('链接核对没有引用实际正文')
                        sid=assessment['source_id']
                        if assessment['output_quote'].strip()==literals.get(sid,'').strip():
                            raise ValueError('链接核对只能引用原链接，缺少作者写出的解释')
                        guide=next(g for g in guides if g['source_id']==sid)
                        checks=('topic_explained','connection_explained')
                        if not guide.get('unavailable_reason'):
                            checks+=('destination_explained',)
                        if guide.get('limitation','').strip():
                            checks+=('limitation_explained',)
                        if any(resources.state['entries'].get(e['resource_id'],{}).get('scope')=='linked_image'
                            for e in guide.get('evidence',[])):
                            checks+=('image_explained',)
                        missing=[name for name in checks if not assessment[name]]
                        if missing and not any(f['block_id']==assessment['block_id'] and
                            f['output_quote'] in block['markdown'] and sid==f['source_id']
                            for f in result['findings']):
                            result['findings'].append(dict(block_id=assessment['block_id'],
                                output_quote=assessment['output_quote'],source_id=sid,
                                source_quote=objects[sid]['text'],problem='知识链接解释缺少：'+', '.join(missing),
                                required_change='只补当前链接的已查证主题、上下文关联、具体内容和必要限制，保留原链接与段落主旨'))
                for finding in result['findings']:
                    if finding['block_id'] not in blocks or finding['output_quote'] not in blocks[finding['block_id']]['markdown']:
                        raise ValueError('核对意见未准确引用实际正文')
                    if finding['source_id']:
                        alignment=rebind_link_finding_evidence(finding,guides,resources)
                        if alignment:result.setdefault('source_quote_alignments',[]).append(alignment)
                        sid=finding['source_id']
                        external=resources.state['entries'].get(sid,{}) if resources else {}
                        if sid not in node['source_ids'] and external.get('kind')!='external':
                            matches=[(other_id,quote) for other_id in node['source_ids']
                                     if other_id in objects
                                     if (quote:=exact_source_quote(finding['source_quote'],
                                         objects[other_id]['text'],objects[other_id]['kind']=='page')) is not None]
                            if len(matches)==1:
                                submitted_sid=sid
                                finding['source_id'],finding['source_quote']=matches[0]
                                sid=finding['source_id']
                                result.setdefault('source_quote_alignments',[]).append(dict(
                                    submitted_source_id=submitted_sid,
                                    actual_source_id=sid,
                                    operation='exact_in_scope_original_object_rebind'))
                            else:
                                finding['_ignore_cross_unit']=True
                                result.setdefault('ignored_cross_unit_findings',[]).append(dict(
                                    block_id=finding['block_id'],submitted_source_id=sid,
                                    reason='review finding cited a source outside the current writing unit'))
                                continue
                        original=sid in node['source_ids']
                        texts=(objects[sid]['text'],literals.get(sid,'')) if original else (resources.text(sid),)
                        pdf_wrap=original and objects[sid]['kind']=='page'
                        exact=next((q for text in texts
                            if (q:=exact_source_quote(finding['source_quote'],text,pdf_wrap)) is not None),None)
                        if exact is None and original:
                            matches=[(other_id,quote) for other_id in node['source_ids']
                                     if other_id in objects
                                     if (quote:=exact_source_quote(finding['source_quote'],
                                         objects[other_id]['text'],objects[other_id]['kind']=='page')) is not None]
                            if len(matches)==1:
                                finding['source_id'],exact=matches[0]
                                result.setdefault('source_quote_alignments',[]).append(dict(
                                    submitted_source_id=sid,actual_source_id=finding['source_id'],
                                    operation='exact_original_object_rebind'))
                        if exact is None and original:
                            # Reviewers occasionally replace one RST role with
                            # inline-code marks while otherwise quoting the
                            # complete source object. Rebind only a uniquely
                            # near-identical whole object, then retain its exact
                            # stored bytes as the evidence.
                            from difflib import SequenceMatcher
                            submitted=' '.join(finding['source_quote'].split())
                            ranked=sorted(((SequenceMatcher(None,submitted,
                                ' '.join(objects[other_id]['text'].split())).ratio(),other_id)
                                for other_id in node['source_ids'] if other_id in objects),reverse=True)
                            if (ranked and ranked[0][0]>=.80 and
                                    (len(ranked)==1 or ranked[0][0]-ranked[1][0]>=.08)):
                                finding['source_id']=ranked[0][1]
                                exact=objects[finding['source_id']]['text']
                                result.setdefault('source_quote_alignments',[]).append(dict(
                                    submitted_source_id=sid,actual_source_id=finding['source_id'],
                                    operation='unique_near_exact_original_object_rebind',
                                    reviewer_quote_was_not_verbatim=True))
                        if exact is None and original and objects[finding['source_id']]['text']:
                            # The finding already names an in-scope immutable
                            # source object.  A paraphrased evidence snippet is
                            # a protocol defect, not a reason to discard the
                            # whole generated article.  Bind the finding to the
                            # complete stored object so the later patch remains
                            # source-grounded without inventing a quotation.
                            exact=objects[finding['source_id']]['text']
                            result.setdefault('source_quote_alignments',[]).append(dict(
                                submitted_source_id=sid,actual_source_id=finding['source_id'],
                                operation='complete_original_object_evidence_fallback',
                                reviewer_quote_was_not_verbatim=True))
                        if exact is None:raise ValueError('核对意见未准确引用当前原文')
                        if exact!=finding['source_quote']:
                            result.setdefault('source_quote_alignments',[]).append(dict(source_id=sid,
                                submitted_quote=finding['source_quote'],actual_quote=exact,
                                operation='source_pdf_wrap_alignment' if pdf_wrap else 'source_whitespace_only'))
                            finding['source_quote']=exact
                result['findings']=[finding for finding in result['findings']
                                    if not finding.pop('_ignore_cross_unit',False)]
                retarget_protected_findings(
                    result,draft,protected_objects(job['inventory']).values())
                # Deterministic omissions enter the same bounded local repair
                # transaction as semantic findings, instead of paying for a
                # second full writer response to discover one absent name.
                expected_concepts=[c for part in job['active_plans'] for c in part['concepts']
                                   if c['id'] in node['establishes_concepts']]
                completion_ids={item['id'] for item in name_completions}
                for omission in missing_concept_names(
                    expected_concepts+name_completions,draft):
                    sid=next((sid for sid in omission['source_ids'] if sid in node['source_ids']),None)
                    if not sid:continue
                    block=next((b for b in draft['blocks'] if any(
                        token and token in b['markdown'] for token in
                        (omission['chinese_name'],*omission['names']))),None)
                    if not block:continue
                    quote=next((line for line in block['markdown'].splitlines()
                                if omission['chinese_name'] in line),block['markdown'].splitlines()[0])
                    if any(f['block_id']==block['id'] and '术语' in f['problem'] for f in result['findings']):continue
                    is_name_completion=omission['concept_id'] in completion_ids
                    result['findings'].append(dict(block_id=block['id'],output_quote=quote,
                        source_id=sid,source_quote=objects[sid]['text'],
                        problem='已查证术语在实际正文中缺少名称：'+', '.join(omission['names']),
                        required_change=('只在本单元首次出现处按要求排成“'+
                            next((item.get('first_use_display','') for item in name_completions
                                  if item['id']==omission['concept_id']), '缩写 中文名（英文名）')+
                            '”：缩写置于中文名称前，全角括号内只放英文名；不重讲此前已接受的定义，'
                            '保持原句主张及其余正文不变' if is_name_completion else
                            '仅在本术语首次定义处补齐已查证的中英文名称和缩写展开，保持原句主张及其余正文不变')))
                # A collapsed facsimile is an original reference, not a new
                # teaching diagram whose immutable bytes a writer should edit
                references={b['id'] for b in draft['blocks'] if b['kind']=='document_info'
                    and b['id']==node['id']+'-source-'+next(iter(b.get('object_ids',[])),'')
                    and all(objects[s]['kind'] in {'page','metadata'} for s in b.get('object_ids',[]))}
                # A finding may quote an intact link/image precisely because
                # its adjacent explanation is missing. Literal preservation
                # does not exempt that explanation from review; the committer
                # separately prevents changing the protected source object.
                def reference_only(f):
                    return f['block_id'] in references
                result.setdefault('protected_reference_notes',[]).extend(
                    f for f in result['findings'] if reference_only(f))
                result['findings']=[f for f in result['findings'] if not reference_only(f)]
                for issue in confirmed_format_issues(format_issues,result,bool(job.get('joint_review_contract_version'))):
                    if any(f['block_id']==issue['block_id'] and issue['output_quote'] in f['output_quote'] for f in result['findings']):continue
                    result['findings'].append(dict(block_id=issue['block_id'],output_quote=issue['output_quote'],
                        source_id='',source_quote='',problem=issue['rule_id']+': '+issue.get('message',issue.get('reason','')),
                        required_change='按完整写作技能修复这处已确认格式问题，与本轮内容修复合并为局部事务，保留原意与原对象'))
                result['findings']=carry_unchanged_findings(result['findings'],
                    candidate.get('content_findings',[]),candidate.get('last_patch'),draft)
                return result
            base='active-review-'+node['id']+'-'+str(round_)
            effective_contract=writing_batch_contract(job['active_plans'][0]['contract'],node,job.get('goal',''))
            review_key=base+'-'+digest([canonical(draft),visual_cards,effective_contract,'named-evidence-v3'])[:16]
            # Compatibility for already-returned reviews: replay only after the
            # archived request proves it reviewed this exact candidate.
            old_calls=[c for c in job['calls'] if c.get('step_key','').startswith(base+'-turn-') and c.get('wire_request_blob')]
            if old_calls:
                same=True
                for c in old_calls:
                    wire=json.loads(self.store.read_blob(c['wire_request_blob']))
                    messages=[m for m in wire.get('messages',[]) if m.get('role')=='user']
                    try:
                        archived=json.loads(messages[-1]['content'])
                        same=same and archived.get('actual_draft')==draft and archived.get('contract')==effective_contract
                    except (ValueError,KeyError,IndexError,TypeError):same=False
                if same:review_key=base
            risk_context=(bounded_prior_context(job['draft']['blocks'])
                          if is_v2(job) and node.get('cross_batch_risks') else [])
            result=self.turn(job,review_key,'active_review',A.ContentReview,
                source,node['source_ids'],dict(contract=writing_batch_contract(job['active_plans'][0]['contract'],node,job.get('goal','')),node=node,
                    actual_draft=draft,obligations=obligations,prior_findings=candidate.get('content_findings',[]),
                    link_guides=current_link_guides,
                    concepts=[c for part in job['active_plans'] for c in part['concepts'] if c['id'] in node['establishes_concepts']+node['requires_concepts']],
                    missing_concept_names=missing_concept_names(
                        [c for part in job['active_plans'] for c in part['concepts']
                         if c['id'] in node['establishes_concepts']]+name_completions,draft),
                    name_completions=name_completions,
                    format_preflight=review_format_context(format_issues),
                    format_decisions_required=bool(job.get('joint_review_contract_version')),
                    visual_cards=visual_cards,
                    unavailable_original_media=self.unavailable_media(job,node['source_ids']),
                    unavailable_media_review_rule='Flag any assertion that describes unavailable image pixels as observed; preserve the original URL and the explicit limitation.',
                    review_obligation_ids=sorted(review_ids),
                    exact_revision=candidate.get('last_patch'),
                    protected_originals=protected_objects(job['inventory']),
                    previous_final_tail=[b['markdown'] for b in job['draft']['blocks'][-2:]],
                    risk_review_context=risk_context),validate_review)
            dismissed={d['candidate_id'] for d in result.get('format_decisions',[]) if d['decision']=='dismiss'}
            candidate.setdefault('dismissed_keys',[]).extend(candidate_key(c,draft)
                for c in preflight['format']['candidates'] if c['id'] in dismissed)
            record=dict(draft_digest=digest(canonical(draft).encode()),**result)
            reviews=candidate.setdefault('content_reviews',[])
            if not reviews or reviews[-1]!=record:reviews.append(record)
            if post_patch_review:candidate.pop('post_patch_review_required',None)
            if result['findings']:
                candidate['content_findings']=result['findings']
                if (post_patch_review or patch_rounds(candidate)>=candidate.get('patch_limit',2)
                        or round_>=self.config.get('active_revision_limit',2)):
                    candidate['unresolved_content_findings']=result['findings']
                    job.setdefault('quality_issues',[]).extend(
                        node['id']+' / '+f['block_id']+'：'+f['problem'] for f in result['findings'])
                    job['stage']='active_format'
                    return 'queued'
                candidate['revision_issues']=result
                candidate['revision_blocks']=list(dict.fromkeys(f['block_id'] for f in result['findings']))
                candidate['content_review_round']=round_+1
                job['stage']='active_revision'
            else:
                candidate.pop('content_findings',None)
                job['stage']='active_format'
            return 'queued'
        if stage == 'active_format':
            from .review_context import editable_lines,line_proposal
            node = nodes[job['unit_index']]
            name_completions=short_rewrite_name_completions(job,node)
            candidate = job['active_candidate']
            reject_empty_claimed_blocks(candidate['draft'])
            retire_refuted_formal_concepts(job,candidate)
            if is_v2(job):
                # Resume v2 candidates saved by earlier workers immediately
                # after active_patch. Those checkpoints used active_format as
                # the next stage and never recorded the required post-patch
                # review, so compare exact draft digests before committing.
                patch=candidate.get('last_patch')
                candidate_digest=digest(canonical(candidate['draft']).encode())
                reviewed_digests={record.get('draft_digest')
                    for record in candidate.get('content_reviews',[])}
                if (patch and patch.get('after')==candidate_digest
                        and candidate_digest not in reviewed_digests):
                    candidate['post_patch_review_required']=True
                    job['stage']='active_review'
                    return 'queued'
                # V2 records unresolved findings on the checkpoint and commits
                # the current candidate.  No format/content gate may prevent
                # the remaining units or the final delivery from being built.
                if candidate.get('unresolved_content_findings') or candidate.get('unresolved_revision'):
                    note=node['id']+'：已有内容问题记录，已保留当前候选并继续生成'
                    if note not in job.setdefault('quality_issues',[]):
                        job['quality_issues'].append(note)
                if candidate.get('unresolved_format'):
                    note=node['id']+'：已有格式问题记录，已保留当前候选并继续生成'
                    if note not in job.setdefault('quality_issues',[]):
                        job['quality_issues'].append(note)
                return self.commit_candidate(job,node,candidate)
            if candidate.get('unresolved_content_findings') or candidate.get('unresolved_revision'):
                raise ValueError('本单元两轮局部修复后仍有内容问题，已保存候选稿与核对意见')
            # Content escalation is not a committed format repair. Preserve its
            # request record but do not count it twice against both budgets.
            candidate['format_rounds']=sum(bool(r.get('edits')) for r in candidate.get('format_records',[]))
            draft=normalize_authored_spacing(candidate['draft'],job['inventory'])
            if canonical(draft)!=canonical(candidate['draft']):
                candidate.setdefault('layout_normalizations',[]).append(dict(
                    before=digest(canonical(candidate['draft']).encode()),after=digest(canonical(draft).encode()),
                    operation='remove_only_redundant_authored_list_spacing'))
                candidate['draft']=draft
                by_id={b['id']:b for b in draft['blocks']}
                for binding in candidate['coverage']:binding['output_quote']=by_id[binding['block_id']]['markdown']
                candidate['delta']['concept_evidence']=[e for e in candidate['delta'].get('concept_evidence',[])
                    if e['block_id'] in by_id and e['output_quote'] in by_id[e['block_id']]['markdown']]
            report = respect_original_format(scan(bundle, draft, self.store.root/'production'/job['id']/node['id']),draft,job['inventory'])
            candidate['original_format_exemptions']=report['original_exemptions']
            findings = report['format']['findings']
            candidates = [c for c in report['format']['candidates'] if candidate_key(c,draft) not in candidate.get('dismissed_keys', [])]
            if findings and patch_rounds(candidate)>=candidate.get('patch_limit',2):
                candidate['unresolved_format']=dict(findings=findings,candidates=candidates)
                job.setdefault('quality_issues',[]).append(node['id']+'：两轮局部修补后仍有 '+
                    str(len(findings))+' 项确定格式问题，正文与检查记录均已保留')
                raise ValueError('本单元两轮局部修补后仍有确定格式问题，已保存原稿与具体检查记录')
            # Candidate flags still need semantic adjudication even when no
            # editing budget remains; dismissing them costs no third patch.
            if findings or candidates:
                if findings and patch_rounds(candidate) >= candidate.get('patch_limit',2):
                    raise ValueError('本单元两轮局部格式修复后仍有问题，保留原稿与具体检查记录')
                result = A.FormatResolution.model_validate(self.call(job,
                    'active-format-'+node['id']+'-'+str(candidate['format_rounds'])+'-'+report['canonical_digest'][:16], 'active_format',
                    dict(document_digest=report['canonical_digest'], blocks=draft['blocks'],
                         findings=findings, candidates=candidates,
                         adjudication_contract='Use requires_revision for confirmed defects needing changed words, definitions or explanations. Lack of permission to fix a defect is NOT a reason to dismiss it. dismissed means actually not a violation, with evidence. Original-format exemptions have already been proved against exact source characters.',
                         editable_lines=editable_lines(draft,job['inventory'],{b['id'] for b in draft['blocks']})), A.FormatResolution)).model_dump()
                if result['line_edits']:
                    if result['edits']:raise ValueError('同一格式事务不能混用行号与片段补丁')
                    proposal=line_proposal(dict(document_digest=result['document_digest'],edits=result['line_edits']),
                        editable_lines(draft,job['inventory'],{b['id'] for b in draft['blocks']}))
                    result['edits']=[dict(block_id=e['block_id'],old_text=e['old_text'],new_text=e['new_text'],rule=e['reason']) for e in proposal['edits']]
                if result['document_digest'] != report['canonical_digest']:
                    raise ValueError('格式补丁基线发生变化')
                if not set(result['dismissed']) <= {c['id'] for c in candidates} or (result['dismissed'] and not result['reasons']):
                    raise ValueError('只能按具体理由排除待判断项，不能排除机械错误')
                result=normalize_revision_ids(result,{c['id'] for c in findings+candidates})
                if set(result['requires_revision'])&set(result['dismissed']):
                    raise ValueError('需要正文修订的项目不能同时声明无违规')
                candidate.setdefault('dismissed_keys',[]).extend(candidate_key(c,draft) for c in candidates if c['id'] in result['dismissed'])
                semantic_patch=any(format_signature(e['old_text']) != format_signature(e['new_text']) for e in result['edits'])
                if semantic_patch or result['requires_revision'] or (not result['edits'] and findings):
                    candidate.setdefault('format_escalations',[]).append(result)
                    actionable=findings+[c for c in candidates if c['id'] not in result['dismissed']]
                    candidate['revision_issues']=dict(findings=actionable,candidates=[],format_response=result,format_check_version=2)
                    candidate['revision_blocks']=[b['id'] for b in draft['blocks'] if any(
                        issue.get('old_text','').strip() and issue['old_text'].strip() in b['markdown']
                        for issue in actionable)]
                    # A boundary whitespace defect points at the adjacent authored blocks
                    if not candidate['revision_blocks']:candidate['revision_blocks']=[b['id'] for b in draft['blocks']]
                    job['stage']='active_revision'
                    return 'queued'
                if result['edits']:
                    if patch_rounds(candidate)>=candidate.get('patch_limit',2):raise ValueError('局部补丁已达到当前设置上限，保留具体问题与原稿')
                    proposal = dict(document_digest=result['document_digest'], edits=[dict(
                        block_id=e['block_id'], old_text=e['old_text'], new_text=e['new_text'], reason=e['rule']) for e in result['edits']])
                    reserve_patch_attempt(candidate);self.store.put_job(job)
                    changed = repair(bundle, draft, proposal, {b['id'] for b in draft['blocks']},
                        self.store.root/'production'/job['id']/node['id']/('format-'+str(candidate['format_rounds'])))
                    # Preserve source literals even if a skill runtime changes its protection behavior
                    for literal in protected_objects(job['inventory']).values():
                        if literal and canonical(draft).count(literal) != canonical(changed).count(literal):
                            raise ValueError('格式补丁改变了原对象')
                    candidate['draft'] = changed
                    # Exact output spans follow only accepted local edits, never a full semantic remap
                    for binding in candidate['coverage']:
                        for edit in result['edits']:
                            if binding['block_id']==edit['block_id']:
                                binding['output_quote']=binding['output_quote'].replace(edit['old_text'],edit['new_text'])
                    for binding in candidate['delta']['concept_evidence']:
                        for edit in result['edits']:
                            if binding['block_id']==edit['block_id']:
                                binding['output_quote']=binding['output_quote'].replace(edit['old_text'],edit['new_text'])
                    candidate['dismissed'] = []
                else:
                    if findings or set(result['dismissed']) != {c['id'] for c in candidates}:
                        raise ValueError('格式问题没有得到局部补丁或有效裁决')
                    candidate['dismissed'] = result['dismissed']
                candidate.setdefault('format_records', []).append(dict(digest=report['canonical_digest'], **result))
                candidate['format_rounds'] += 1
                return 'queued'
            clear_resolved_format_issue(job,candidate,node['id'])
            candidate['draft'] = draft
            concept_presence([c for part in job['active_plans'] for c in part['concepts']
                              if c['id'] in node['establishes_concepts']]+name_completions, draft)
            blocks = {b['id']: b for b in draft['blocks']}
            for binding in candidate['coverage']:
                if binding['output_quote'] not in blocks[binding['block_id']]['markdown']:
                    raise ValueError('局部补丁后正文覆盖引文已失效')
            checkpoint = dict(node_id=node['id'], draft_digest=digest(canonical(draft).encode()),
                coverage_origin='compiler_from_actual_blocks; mapping_is_not_semantic_proof',
                coverage=candidate['coverage'], knowledge_delta=candidate['delta'],
                format_digest=report['canonical_digest'], format_records=candidate.get('format_records', []),
                format_escalations=candidate.get('format_escalations',[]),
                dismissed_candidate_keys=candidate.get('dismissed_keys',[]),
                content_patches=candidate.get('patch_history',[]),
                original_format_exemptions=candidate.get('original_format_exemptions',[]),
                layout_normalizations=candidate.get('layout_normalizations',[]),
                content_reviews=candidate.get('content_reviews',[]))
            checkpoint['unresolved_content_findings']=candidate.get('unresolved_content_findings',[])
            checkpoint['unresolved_format']=candidate.get('unresolved_format',{})
            checkpoint['unresolved_revision']=candidate.get('unresolved_revision',{})
            job.setdefault('active_checkpoints', []).append(checkpoint)
            job['draft']['blocks'].extend(draft['blocks'])
            text=canonical(draft);resource_id='written-'+node['id'];blob=self.store.blob(text.encode())
            job.setdefault('generated_resources',{})[resource_id]=dict(id=resource_id,blob=blob,chars=len(text),
                kind='generated',locator='completed-unit/'+node['id'],draft_digest=checkpoint['draft_digest'])
            # Small, source-recoverable memory, with real final quotations rather than source-as-learned-context
            evidence={c['concept_id']:c for c in candidate['delta']['concept_evidence']}
            job['knowledge_memory'].append(dict(node_id=node['id'], delta=candidate['delta'],resource_id=resource_id,
                draft_digest=checkpoint['draft_digest'], block_ids=list(blocks),
                established=[dict(id=cid,resource_id=resource_id,
                    **({'definition':evidence[cid]['output_quote'],'block_id':evidence[cid]['block_id']} if cid in evidence else {}))
                    for cid in candidate['delta']['established_concepts']]))
            job['unit_index'] += 1
            job.pop('active_candidate')
            job['stage'] = 'active_write' if job['unit_index'] < len(nodes) else 'active_deliver'
            return 'queued'
        if stage=='active_revision':
            candidate=job['active_candidate'];node=nodes[job['unit_index']]
            issues=candidate.get('revision_issues',{})
            compiled=compiled_term_format_proposal(candidate['draft'],issues) if issues.get('format_check_version')==2 else None
            if patch_rounds(candidate)>=candidate.get('patch_limit',2):
                candidate['unresolved_revision']=copy.deepcopy(issues)
                job.setdefault('quality_issues',[]).append(node['id']+'：两轮局部修补已用完，保留未解决意见与当前正文')
                job['stage']='active_format'
                return 'queued'
            if compiled:
                previous=candidate['draft']
                reserve_patch_attempt(candidate);self.store.put_job(job)
                changed=repair(bundle,previous,compiled,set(candidate['revision_blocks']),
                    self.store.root/'production'/job['id']/node['id']/('confirmed-term-format-'+digest(canonical(previous).encode())[:16]))
                reject_empty_claimed_blocks(changed)
                for literal in protected_objects(job['inventory']).values():
                    if literal and canonical(previous).count(literal)!=canonical(changed).count(literal):
                        raise ValueError('术语格式补丁改变了原对象')
                candidate['draft']=changed
                blocks={b['id']:b for b in changed['blocks']}
                for binding in candidate['coverage']:
                    binding['output_quote']=blocks[binding['block_id']]['markdown']
                for binding in candidate['delta'].get('concept_evidence',[]):
                    for edit in compiled['edits']:
                        if binding['block_id']==edit['block_id']:
                            binding['output_quote']=binding['output_quote'].replace(edit['old_text'],edit['new_text'])
                candidate['delta']['concept_evidence']=[e for e in candidate['delta'].get('concept_evidence',[])
                    if e['output_quote'] in blocks[e['block_id']]['markdown']]
                candidate.setdefault('format_records',[]).append(dict(**compiled,
                    operation='confirmed_existing_term_pair',confirmation=candidate['revision_issues']['format_response']))
                job['stage']='active_format'
                return 'queued'
            if 'format_response' in candidate.get('revision_issues',{}) and candidate['revision_issues'].get('format_check_version')!=2:
                candidate.setdefault('superseded_format_reports',[]).append(candidate['revision_issues'])
                job['stage']='active_format'
                return 'queued'
            if 'active_patch' in job.get('role_policy',{}):
                from .production_contracts import LocalRepair
                previous=candidate['draft'];attempt=len(candidate.get('patch_history',[]))
                if attempt>=self.config.get('active_revision_limit',4):
                    raise ValueError('当前单元精确修订仍未解决问题，原文、补丁与意见均已保留')
                literals=protected_objects(job['inventory']).values()
                retarget_protected_findings(candidate['revision_issues'],previous,literals)
                candidate['revision_blocks']=list(dict.fromkeys(
                    finding['block_id'] for finding in candidate['revision_issues'].get('findings',[])))
                allowed=set(candidate['revision_blocks']) & patchable_block_ids(previous,literals)
                if not allowed:
                    candidate['unresolved_revision']=copy.deepcopy(candidate['revision_issues'])
                    candidate.setdefault('protected_patch_notes',[]).append(dict(
                        reason='no_authored_patch_target_after_protected_material_filter',
                        original_block_ids=list(candidate['revision_blocks'])))
                    quality_note=(node['id']+'：修订意见涉及受保护材料，未找到同源解释段落；原材料保持不变，意见已保留')
                    if quality_note not in job.setdefault('quality_issues',[]):
                        job['quality_issues'].append(quality_note)
                    job['stage']='active_format'
                    self.store.put_job(job)
                    return 'queued'
                def validate_patch(value,resources):
                    proposal=LocalRepair.model_validate(value).model_dump()
                    proposal,rejected=normalize_local_proposal(
                        proposal,previous,protected_objects(job['inventory']).values())
                    if rejected:candidate.setdefault('rejected_protected_patch_edits',[]).extend(rejected)
                    if not proposal['edits']:raise ValueError('已确认的问题需要实际补丁')
                    prior_attempts=candidate.get('local_patch_attempts',0)
                    reserve_patch_attempt(candidate);self.store.put_job(job)
                    def commit_checked(transaction,workdir):
                        output=repair(bundle,previous,transaction,allowed,workdir)
                        reject_empty_claimed_blocks(output)
                        for literal in protected_objects(job['inventory']).values():
                            if literal and canonical(previous).count(literal)!=canonical(output).count(literal):
                                raise Conflict('精确修订不能改变原对象')
                        if heading_level_skips(output)>heading_level_skips(previous):
                            raise Conflict('局部补丁引入了标题层级跳跃')
                        return output
                    try:
                        changed=commit_checked(proposal,
                            self.store.root/'production'/job['id']/node['id']/('content-patch-'+str(attempt)))
                    except Conflict as whole_error:
                        if len(proposal['edits'])<2:
                            candidate['local_patch_attempts']=prior_attempts
                            self.store.put_job(job)
                            raise ValueError(str(whole_error)) from whole_error
                        accepted=[];changed=None
                        for index,edit in enumerate(proposal['edits']):
                            trial=proposal|{'edits':accepted+[edit]}
                            try:
                                trial_result=commit_checked(trial,
                                    self.store.root/'production'/job['id']/node['id']/
                                    ('content-patch-screen-'+str(attempt)+'-'+str(index)))
                            except Conflict as local_error:
                                candidate.setdefault('screened_patch_edits',[]).append(dict(
                                    block_id=edit['block_id'],reason=str(local_error)[:180]))
                            else:
                                accepted.append(edit);changed=trial_result
                        if not accepted:
                            candidate['local_patch_attempts']=prior_attempts
                            self.store.put_job(job)
                            raise ValueError(str(whole_error)) from whole_error
                        proposal=proposal|{'edits':accepted}
                    except Exception as error:
                        # A rejected transaction did not modify the draft.
                        # Keep its diagnostic, but only committed patches use
                        # one of the two local repair rounds.
                        candidate['local_patch_attempts']=prior_attempts
                        candidate.setdefault('rejected_patch_transactions',[]).append(str(error)[:240])
                        self.store.put_job(job)
                        raise ValueError(str(error)) from error
                    return changed,proposal
                patch_key='active-patch-'+node['id']+'-'+str(attempt)+'-'+digest([canonical(previous),writing_batch_contract(job['active_plans'][0]['contract'],node,job.get('goal',''))])[:16]
                marker=next((item for item in candidate.get('unknown_patch_continuations',[])
                    if item.get('original_step_key','').startswith(patch_key+'-turn-')),None)
                if marker:
                    patch_key=marker['continuation_session_key']
                    if marker.get('status')=='queued':
                        marker['status']='dispatching'
                        marker['dispatch_started_at']=time.time()
                        self.store.put_job(job)
                patch_payload=dict(contract=writing_batch_contract(job['active_plans'][0]['contract'],node,job.get('goal','')),node=node,
                        document_digest=digest(canonical(previous).encode()),original_draft=previous,
                        editable_block_ids=sorted(allowed),issues=candidate['revision_issues'],
                        protected_literal_spans=[dict(block_id=block['id'],start=start,end=end)
                            for block in previous['blocks'] if block['id'] in allowed
                            for start,end in _protected_literal_spans(block['markdown'],literals)],
                        patch_scope_note=('只修改 issues 所指的作者文字。protected_literal_spans 是相对各 block markdown 的零起始字符区间，'
                            '必须保持原样；混合 object 块只允许改这些区间之外的作者图注文字。source、document_info 和纯 object 块不可编辑。'),
                        link_guides=link_guides(job,node['source_ids']),
                        visual_cards=[{k:v for k,v in c.items() if k!='source_text'} for c in job.get('visual_cards',[]) if c['source_id'] in node['source_ids']],
                        obligations=[f for part in job['active_plans'] for f in part['obligations'] if f['id'] in node['obligation_ids']],
                        previous_final_tail=[b['markdown'] for b in job['draft']['blocks'][-2:]])
                if marker:
                    patch_payload['continuation_note']=(
                        '上一轮 active_patch 的 Responses SSE 在截止时间后没有完整回执。不得假设该请求未执行，也不得重放原步骤。'
                        '本次是唯一的新步骤续接；原稿、证据、材料对象及未知费用记录保持不变。只修改 issues 所指的作者文字，'
                        'protected_literal_spans 内的内容必须保持原样；混合 object 块只允许修改这些区间外的作者图注，'
                        'source、document_info 和纯 object 块不可编辑。')
                    patch_payload['prior_unknown_step_key']=marker['original_step_key']
                try:
                    changed,proposal=self.turn(job,patch_key,'active_patch',LocalRepair,
                        source,node['source_ids'],patch_payload,validate_patch)
                except Exception:
                    if marker:
                        marker['status']='continuation_interrupted_or_rejected'
                        marker['finished_at']=time.time()
                        self.store.put_job(job)
                    raise
                if marker:
                    marker['status']='completed'
                    marker['finished_at']=time.time()
                    marker['result_digest']=digest(canonical(proposal).encode())
                    self.store.put_job(job)
                candidate.setdefault('patch_history',[]).append(dict(before=proposal['document_digest'],
                    after=digest(canonical(changed).encode()),edits=proposal['edits']))
                candidate['last_patch']=candidate['patch_history'][-1]
                candidate['draft']=changed;candidate['dismissed']=[]
                blocks={b['id']:b for b in changed['blocks']}
                for binding in candidate['coverage']:binding['output_quote']=blocks[binding['block_id']]['markdown']
                # Concept memory can always open the actual final unit resource.
                # Keep optional small quotations only if still exact after edits.
                candidate['delta']['concept_evidence']=[e for e in candidate['delta'].get('concept_evidence',[])
                    if e['output_quote'] in blocks[e['block_id']]['markdown']]
                if is_v2(job):candidate['post_patch_review_required']=True
                job['stage']='active_review'
                return 'queued'
            revision_count=len(candidate.get('revision_history',[]))
            if revision_count>=self.config.get('active_revision_limit',4):raise ValueError('当前单元定向修订仍未解决问题，已保存候选与具体意见')
            key='active-revision-'+node['id']+('-'+str(revision_count+1) if revision_count else '')
            previous=candidate['draft']
            payload=dict(contract=writing_batch_contract(job['active_plans'][0]['contract'],node,job.get('goal','')),node=node,
                revision_attempt=revision_count+1,revision_limit=self.config.get('active_revision_limit',4),
                original_draft=previous,editable_block_ids=candidate['revision_blocks'],
                issues=candidate['revision_issues'],
                obligations=[f for part in job['active_plans'] for f in part['obligations'] if f['id'] in node['obligation_ids']],
                previous_final_tail=[b['markdown'] for b in job['draft']['blocks'][-2:]])
            def validate(value,resources):
                value=copy.deepcopy(value)
                for block in previous['blocks']:
                    if block['kind']=='document_info' and block['id'].startswith(node['id']+'-source-'):
                        original=dict(id=block['id'],kind=block['kind'],markdown=block['markdown'],
                            obligation_ids=block['obligation_ids'],source_ids=[e['source_id'] for e in block['evidence']])
                        value['blocks']=[original if b['id']==block['id'] else b for b in value['blocks']]
                revised,coverage,delta=validate_written(value,node,job['inventory'],bundle,job['draft'],payload['obligations'])
                before={b['id']:b for b in previous['blocks']};after={b['id']:b for b in revised['blocks']}
                untouched=lambda mapping:[bid for bid in mapping if bid not in candidate['revision_blocks']]
                if set(before)!=set(after) or untouched(before)!=untouched(after):
                    raise ValueError('定向修订不能新增、删除或移动无关段落')
                for bid,block in before.items():
                    if bid not in candidate['revision_blocks']:
                        if {k:v for k,v in after[bid].items() if k!='evidence'}!={k:v for k,v in block.items() if k!='evidence'}:
                            raise ValueError('定向修订改变了未命中的段落')
                        # Keep the exact existing index for untouched prose,
                        # instead of asking a writer to copy redundant metadata
                        after[bid]['evidence']=copy.deepcopy(block['evidence'])
                for literal in protected_objects(job['inventory']).values():
                    if literal and canonical(previous).count(literal)!=canonical(revised).count(literal):
                        raise ValueError('定向修订改变了原对象或其出现次数')
                return revised,coverage,delta
            revised,coverage,delta=self.turn(job,key,'active_revision',A.WrittenUnit,source,node['source_ids'],payload,validate)
            candidate.setdefault('revision_history',[]).append(dict(before=digest(canonical(previous).encode()),
                after=digest(canonical(revised).encode()),blocks=candidate['revision_blocks']))
            candidate.update(draft=revised,coverage=coverage,delta=delta,
                revision_done=True,dismissed=[])
            job['stage']='active_format' if is_v2(job) else ('active_review' if candidate.get('content_findings') else 'active_format')
            return 'queued'
        if stage == 'active_integrity' and is_core_chain(job):
            reject_empty_claimed_blocks(job['draft'])
            deterministic=self.deterministic_integrity_findings(job)
            if any(row['verdict']=='FAIL' and row['invariant'] in {'I1','I2','I5'}
                   for row in deterministic):
                job['integrity_result']=integrity_result(job,deterministic,review_attempted=False)
                job['stage']='active_deliver'
                return 'queued'
            source_ids=list(dict.fromkeys(sid for group in job['active_groups'] for sid in group))
            block_ids=[block['id'] for block in job['draft']['blocks']]
            obligations=[item for part in job['active_plans'] for item in part['obligations']
                         if job['object_responsibilities'][item['source_id']]['present']]
            payload=dict(actual_draft=job['draft'],source_identity=dict(
                    original_names=[item['name'] for item in job['inventory']['originals']],
                    source_digest=job['source_digest']),
                object_responsibilities=job['object_responsibilities'],
                obligations=obligations,
                external_evidence=integrity_allowed_evidence(job,self.store),
                frozen_source_objects=[obj for obj in source['objects']
                                       if obj['id'] in source_ids],
                visual_cards=[{k:v for k,v in card.items() if k!='source_text'}
                              for card in job.get('visual_cards',[])],
                review_scope='whole_candidate',
                required_source_ids=source_ids,required_block_ids=block_ids)
            payload['required_i3_block_ids']=sorted(patchable_block_ids(
                job['draft'],protected_objects(job['inventory']).values()))
            review=self.review_integrity_once(job,
                'active-integrity-'+digest(canonical(job['draft']).encode())[:16],
                payload,job['draft'],source,payload['external_evidence'],
                protected_literals=protected_objects(job['inventory']).values())
            job['integrity_result']=integrity_result(job,deterministic+review['findings'],
                reviewed_source_ids=review['checked_source_ids'],
                reviewed_block_ids=review['checked_block_ids'])
            failures=[item for item in job['integrity_result']['findings']
                      if item['verdict']=='FAIL']
            editable=patchable_block_ids(job['draft'],protected_objects(job['inventory']).values())
            if (failures and all(item['invariant'] in {'I3','I4'} and
                    item['block_id'] in editable and item['required_change'] for item in failures)):
                job['stage']='active_integrity_repair'
            else:
                job['stage']='active_deliver'
            return 'queued'
        if stage == 'active_integrity_repair' and is_core_chain(job):
            from .production_contracts import LocalRepair
            previous=copy.deepcopy(job['draft'])
            original=job['integrity_result']
            failures=[item for item in original['findings'] if item['verdict']=='FAIL']
            allowed={item['block_id'] for item in failures}
            protected=tuple(protected_objects(job['inventory']).values())
            key='active-integrity-repair-'+original['draft_digest'][:16]
            def validate_repair(value,resources):
                proposal=LocalRepair.model_validate(value).model_dump()
                proposal,rejected=normalize_local_proposal(proposal,previous,protected)
                if rejected:job.setdefault('rejected_integrity_edits',[]).extend(rejected)
                if not proposal['edits']:
                    raise ValueError('整稿修复没有可安全提交的局部改动')
                changed=repair(bundle,previous,proposal,allowed,
                    self.store.root/'production'/job['id']/('integrity-repair-'+original['draft_digest'][:12]))
                for literal in protected:
                    if literal and canonical(previous).count(literal)!=canonical(changed).count(literal):
                        raise ValueError('整稿修复改变了原件对象')
                untouched={block['id']:block for block in previous['blocks']
                           if block['id'] not in allowed}
                if any(next((row for row in changed['blocks'] if row['id']==bid),None)!=block
                       for bid,block in untouched.items()):
                    raise ValueError('整稿修复改变了未命中的段落')
                return changed,proposal
            try:
                changed,proposal=self.turn(job,key,'active_patch',LocalRepair,source,
                    list(dict.fromkeys(item['source_id'] for item in failures if item['source_id'])),
                    dict(original_draft=previous,document_digest=original['draft_digest'],
                         editable_block_ids=sorted(allowed),issues=failures,
                         repair_scope='one focused transaction for the complete candidate'),
                    validate_repair)
            except (ValueError,Conflict) as error:
                job['integrity_result']['repair_note']=str(error)[:240]
                job['stage']='active_deliver'
                return 'queued'
            job['draft']=changed
            job['integrity_repair']=dict(before=original['draft_digest'],
                after=digest(canonical(changed).encode()),edits=proposal['edits'])
            affected_sources=list(dict.fromkeys(sid for block in changed['blocks']
                if block['id'] in allowed for sid in [e['source_id'] for e in block['evidence']]))
            affected_blocks=[block for block in changed['blocks'] if block['id'] in allowed]
            confirmation_payload=dict(review_scope='changed_blocks_only',
                actual_draft={'blocks':affected_blocks},
                prior_findings=failures,exact_repair=job['integrity_repair'],
                required_source_ids=affected_sources,
                required_block_ids=sorted(allowed))
            try:
                confirmation=self.turn(job,'active-integrity-confirm-'+job['integrity_repair']['after'][:16],
                    'active_integrity',A.IntegrityReview,source,affected_sources,
                    confirmation_payload,lambda value,resources:
                        A.IntegrityReview.model_validate(value).model_dump())
                confirmed=(set(allowed)<=set(confirmation['checked_block_ids']) and
                           set(affected_sources)<=set(confirmation['checked_source_ids']))
                remaining=[item for item in original['findings'] if item['verdict']!='FAIL']
                remaining+=confirmation['findings']
                if not confirmed:
                    remaining.append(dict(invariant='I4',verdict='UNKNOWN',block_id='',source_id='',
                        output_quote='',source_quote='',problem='局部修复的影响范围尚未完整复核',
                        required_change=''))
            except (ValueError,Conflict) as error:
                remaining=[item for item in original['findings'] if item['verdict']!='FAIL']
                remaining.append(dict(invariant='I4',verdict='UNKNOWN',block_id='',source_id='',
                    output_quote='',source_quote='',problem='局部修复后的语义复核未完成：'+str(error)[:160],
                    required_change=''))
            remaining+=self.deterministic_integrity_findings(job)
            job['integrity_result']=integrity_result(job,remaining,
                reviewed_source_ids=original['reviewed_source_ids'],
                reviewed_block_ids=original['reviewed_block_ids'])
            job['stage']='active_deliver'
            return 'queued'
        if stage == 'active_deliver':
            if is_core_chain(job):
                result=job.get('integrity_result')
                if not result or result['draft_digest']!=digest(canonical(job['draft']).encode()):
                    raise ValueError('整稿完整性结论缺失或不属于当前候选版本')
                job['publication_status']='Verified' if result['verdict']=='PASS' else 'Candidate'
                job['delivery_checks']=dict(source_digest=job['source_snapshot_digest'],
                    compiled_inventory_digest=job['inventory']['digest'],
                    draft_digest=result['draft_digest'],
                    integrity_verdict=result['verdict'],
                    publication_status=job['publication_status'])
                self.store.put_job(job)
                return 'completed'
            reject_empty_claimed_blocks(job['draft'])
            retire_projected_document_info_gap_notes(job)
            findings = inspect_draft(job['inventory'], job['draft'], job['plan'])
            if findings and not is_v2(job):
                raise ValueError('交付结构检查失败：'+json.dumps(findings, ensure_ascii=False))
            if findings:
                job.setdefault('quality_issues',[]).extend('交付检查：'+str(item) for item in findings)
            if len(job['active_checkpoints']) != len(nodes) and not is_v2(job):
                raise ValueError('部分写作单元缺少检查点')
            for point in job['active_checkpoints']:
                actual = {'blocks': [b for b in job['draft']['blocks'] if b['unit_id']==point['node_id']]}
                if digest(canonical(actual).encode()) != point['draft_digest'] and not is_v2(job):
                    raise ValueError('交付正文与已检查单元版本不同')
            partition_reviews_passed=(not job.get('quality_issues') and all(
                point.get('content_reviews') and not point['content_reviews'][-1].get('findings')
                for point in job['active_checkpoints']))
            job['delivery_checks'] = dict(source_digest=job['source_snapshot_digest'],
                source_digest_kind='initial_inventory_snapshot',compiled_inventory_digest=job['inventory']['digest'],
                draft_digest=digest(canonical(job['draft']).encode()), source_objects=len(source['objects']),
                obligation_count=len(job['inventory']['obligations']), unit_count=len(nodes),
                structural_status='passed',
                semantic_status='passed' if is_v2(job) and partition_reviews_passed else 'not_independently_reviewed',
                unit_review_status='needs_attention' if job.get('quality_issues') else
                    'model_checked' if all(p.get('content_reviews') for p in job['active_checkpoints']) else 'not_requested',
                skill_delivery='unabridged_inline', manual_edits=0)
            if is_v2(job):
                job['delivery_state']='ready_for_review'
                job['independent_review']=dict(status='passed' if partition_reviews_passed else 'issues_recorded',revision=job['base_revision']+1,
                    canonical_digest=job['delivery_checks']['draft_digest'],
                    method='partitioned_source_bound_review',manual_edits=0,
                    reviewed_units=len(job['active_checkpoints']))
                job['delivery_checks']['publication_status']='ready_for_review'
                job['delivery_checks']['cross_batch_review_required']=bool(job.get('cross_batch_review_required'))
                self.store.put_job(job)
                return 'ready_for_review'
            return 'needs_attention' if job.get('quality_issues') else 'completed'
        raise ValueError('未知主动编排阶段：' + stage)
