"""Checkpointed composition: plan first, write with continuity, repair locally.

Structural delivery checks are deliberately not an independent semantic verdict.
Legacy jobs retain their executor; a job's pipeline identity never changes on resume.
"""
import copy
import json
import re
import time
import unicodedata
from pathlib import Path
from . import active_contracts as A
from .active_resources import Resources, canonical_url
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


def bounded_prior_context(blocks, limit=8000):
    """Keep a small continuity window only when the plan identifies a risk."""
    selected=[];used=0
    for block in reversed(blocks):
        value=block.get('markdown','')
        if selected and used+len(value)>limit:break
        text=value[-limit:] if not selected and len(value)>limit else value
        selected.append(text);used+=len(text)
    return list(reversed(selected))


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


def repair_one_missing_json_object_closer(raw):
    """Repair one missing `}` before `]`; never edit a JSON string or value."""
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
                heading['id'] in following['depends_on']):
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
    definitions=len(re.findall(r'(?m)^\s*[-*]\s+[^\n]{1,90}（[^\n]{1,90}）：',output))
    mixed=len(originals)!=len(objects)
    if not mixed:
        return definitions>len(prose) and len(output)>source_chars*2.5
    code_lines=sum(len(obj['text'].splitlines()) for obj in objects if obj['kind']=='code')
    images=sum(obj['kind']=='image' for obj in objects)
    links=sum(obj['kind']=='link' for obj in objects)
    allowance=source_chars*2.5+code_lines*50+images*200+links*100
    return definitions>=max(3,(len(prose)+1)//2) and len(output)>allowance


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
    policy={name:(Path(__file__).parent/'roles'/f'{name}.md').read_text(encoding='utf8') for name in role_names}
    job.update(pipeline=pipeline, stage='active_index', teaching_version=0,
               active_plans=[], active_partition_index=0, unit_index=0,
               knowledge_memory=[], visual_cards=[], quality_issues=[],
               delivery_state='draft', cross_batch_review_required=False,
               content_patch_default=1, content_patch_hard_limit=2,
               format_patch_hard_limit=2,
               role_policy=policy,role_policy_digest=digest(policy))
    return job


def normalize_visual_card_lists(value):
    """Repair a schema-only scalar list without another paid model call"""
    result=copy.deepcopy(value)
    for card in result.get('cards',[]) if isinstance(result,dict) else []:
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
    for concept in result.get('concepts',[]) if isinstance(result,dict) else []:
        if 'requires' not in concept:
            removed.append(concept.get('id',''))
            concept['requires']=[]
        if 'naming_status_effective' in concept and 'naming_status' in concept:
            removed.append(concept.get('id',''))
            concept.pop('naming_status_effective',None)
    for node in result.get('nodes',[]) if isinstance(result,dict) else []:
        if node.get('explanation_placeholder')=='':
            removed.append(node.get('id',''))
            node.pop('explanation_placeholder',None)
    return cleaned,removed


def discard_known_review_protocol_extras(raw):
    """Remove an empty duplicate explanation field from a validation copy."""
    cleaned=copy.deepcopy(raw);removed=[]
    result=cleaned.get('result') if isinstance(cleaned,dict) else None
    for decision in result.get('format_decisions',[]) if isinstance(result,dict) else []:
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


def normalize_authored_spacing(draft,inventory):
    changed=copy.deepcopy(draft)
    literals=protected_objects(inventory)
    for block in changed['blocks']:
        if block['kind'] in {'object','document_info','source'}:continue
        if re.search(r'(?m)^\s*(?:```|~~~)',block['markdown']):continue
        stripped=block|{'markdown':block['markdown'].strip('\r\n')}
        proposal,_=tighten_list_spacing({'blocks':[stripped]})
        text=re.sub(r'\n{3,}', '\n\n',proposal['blocks'][0]['markdown'])
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


def preceding_plan_context(plans):
    """Carry cross-partition identities without replaying completed plans."""
    concept_keys={'id','name','chinese_name','english_name','abbreviations','requires'}
    node_keys={'id','title','requires_concepts','establishes_concepts','depends_on','prepares_for'}
    return [dict(
        concepts=[{k:v for k,v in concept.items() if k in concept_keys}
                  for concept in plan.get('concepts',[])],
        nodes=[{k:v for k,v in node.items() if k in node_keys}
               for node in plan.get('nodes',[])],
    ) for plan in plans]


def writer_node_context(node):
    """Keep writer decisions while removing repeated batch-planning prose."""
    result=copy.deepcopy(node)
    section_keys={'id','title','heading_level','purpose','source_ids','obligation_ids',
                  'requires_concepts','establishes_concepts','explanation'}
    result['section_outline']=[{k:v for k,v in section.items() if k in section_keys}
                               for section in result.get('section_outline',[])]
    for key in ('depends_on','cross_batch_risks'):
        if not result.get(key):result.pop(key,None)
    return result


def prompt_obligations(obligations):
    """Use opened source text once; keep every planned semantic obligation."""
    return [{k:v for k,v in obligation.items() if k!='quote'} for obligation in obligations]


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


def missing_catalogued_numeronyms(source, assigned, concepts):
    """Catch evidence-backed names such as i18n before prose is written."""
    from .terminology import CATALOG
    known={entry['abbr'].casefold() for entry in CATALOG if re.fullmatch(
        r'[A-Za-z][0-9]{1,2}[A-Za-z]',entry.get('abbr',''))}
    objects={obj['id']:obj for obj in source['objects']}
    used={match.group().casefold() for sid in assigned if objects[sid]['kind'] in
        {'text','heading','link'} for match in re.finditer(
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
        if absent:missing.append(dict(concept_id=concept['id'],names=absent,
                                       chinese_name=concept['chinese_name'],
                                       source_ids=concept.get('source_ids',[])))
    return missing


def insert_verified_name_at_unique_first_use(draft, delta, concepts):
    """Add a verified English label only at an unambiguous authored Chinese use."""
    receipts=[]
    existing=canonical(draft).casefold()
    for concept in concepts:
        chinese=concept.get('chinese_name','').strip()
        english=concept.get('english_name','').strip()
        if (concept.get('naming_status')!='verified' or not chinese or not english or
                english.casefold() in existing or not re.search(r'[\u3400-\u9fff]',chinese)):
            continue
        matches=[block for block in draft['blocks']
                 if block['kind'] not in {'source','object','document_info'}
                 and not block.get('embedded_object_ids')
                 and chinese in block['markdown']]
        if not matches:continue
        block=matches[0]
        if block['markdown'].count(chinese)!=1:continue
        offset=block['markdown'].index(chinese)+len(chinese)
        if block['markdown'][offset:offset+1]=='（':continue
        replacement=chinese+'（'+english+'）'
        block['markdown']=block['markdown'].replace(chinese,replacement,1)
        for evidence in delta.get('concept_evidence',[]):
            if (evidence['block_id']==block['id'] and chinese in evidence['output_quote']
                    and english not in evidence['output_quote']):
                evidence['output_quote']=evidence['output_quote'].replace(chinese,replacement,1)
        receipts.append(dict(concept_id=concept['id'],block_id=block['id'],
                             operation='insert_verified_english_name_at_unique_first_use'))
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
    blocks={b['id']:b['markdown'] for b in draft['blocks']}
    normalized=[];rejected=[]
    for edit in proposal['edits']:
        old,new=edit['old_text'],edit['new_text']
        if old==new:
            rejected.append(dict(block_id=edit['block_id'],reason='no_change'))
            continue
        if any(literal and literal in old and literal not in new for literal in literals):
            rejected.append(dict(block_id=edit['block_id'],reason='protected_original_changed'))
            continue
        inline_code=re.findall(r'(?<!`)`[^`\n]+`(?!`)',old)
        if any(new.count(code)<old.count(code) for code in set(inline_code)):
            rejected.append(dict(block_id=edit['block_id'],reason='authored_inline_code_removed'))
            continue
        before=blocks.get(edit['block_id'],'')
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
                  concept_limit=7):
    from .production import bind_evidence_layout
    value=bind_evidence_layout(value,source)
    plan = A.CompositionPlan.model_validate(value).model_dump()
    objects = {o['id']: o for o in source['objects']}
    assigned = set(assigned)
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
    current_page=urlsplit(source.get('source_url',''))
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
        if ((path in {'donate','donation','contribute','contributing','contact','privacy','terms','license'}
             or path.startswith('donat')) and
            any(word in label for word in ('donat','contribut','contact','privacy','terms','license'))):
            brief.update(role='administrative',topic='',connection='',destination='',
                         limitation='',evidence=[],unavailable_reason='')
        if 'site-comments' in target.path and any(word in label for word in ('comment','feedback','意见')):
            brief.update(role='administrative',topic='',connection='',destination='',
                         limitation='',evidence=[],unavailable_reason='')
        if (brief['role']=='navigation' and obj.get('source_scope') not in
                {'site_chrome','source_metadata'} and not same_page_anchor and not (
                target.hostname==current_page.hostname and
                current_page.path.startswith(target.path.rstrip('/')+'/') and
                any(word in label for word in ('home','index','tips','目录','主页')))):
            resource_id=(resources.state.get('url_index',{}).get(
                canonical_url(obj.get('target',''))) if resources else None)
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
            raise ValueError('正文知识链接不能仅按导航链接跳过目标内容：'+brief['source_id'])
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
        # Site navigation, footer links and author metadata have a verifiable
        # DOM role. Keep them in the source inventory, but do not spend a model
        # turn asking for invented destination explanations.
        planned_ids={item['source_id'] for item in briefs}
        for sid in sorted(link_ids-planned_ids):
            item=objects[sid];scope=item.get('source_scope');target=item.get('target','')
            parsed=urlsplit(target);path=parsed.path.casefold()
            image_wrapper=(not item.get('text','').strip() and
                (parsed.hostname or '').casefold().endswith('substackcdn.com') or (not item.get('text','').strip() and
                 path.endswith(('.png','.jpg','.jpeg','.gif','.webp','.svg'))))
            duplicate_empty=(not item.get('text','').strip() and any(
                objects[other].get('target')==target and objects[other].get('text','').strip()
                for other in link_ids if other!=sid))
            if scope in {'site_chrome','source_metadata'} or image_wrapper or duplicate_empty:
                briefs.append(dict(source_id=sid,
                    role='administrative' if scope=='source_metadata' or image_wrapper else 'navigation',
                    topic='',connection='',destination='',limitation='',evidence=[],
                    unavailable_reason=''))
        unique([item['source_id'] for item in briefs], '链接解释责任')
        if {item['source_id'] for item in briefs}!=link_ids:
            raise ValueError('每个原文链接都需要明确内容或导航职责')
        for brief in briefs:
            if brief['role']!='content':continue
            if not all(brief[k].strip() for k in ('topic','connection')):
                raise ValueError('内容链接缺少主题、当前位置关联或目标内容：'+brief['source_id'])
            target=objects[brief['source_id']].get('target','')
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
    claimed=set();dropped={};kept=[]
    for node in nodes:
        unique_obligations=[fid for fid in node['obligation_ids'] if fid not in claimed]
        if unique_obligations!=node['obligation_ids']:
            if not unique_obligations and node['establishes_concepts']:
                raise ValueError('重复义务节点仍承担新概念，不能自动删除：'+node['id'])
            node['obligation_ids']=unique_obligations
            node['source_ids']=list(dict.fromkeys(obligations[fid]['source_id'] for fid in unique_obligations))
        if not node['obligation_ids']:
            dropped[node['id']]=list(node['depends_on'])
            continue
        claimed.update(node['obligation_ids']);kept.append(node)
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
        if all(objects[s]['kind']=='heading' for s in node['source_ids']):
            raise ValueError('单独标题不能消耗一个写作单元，应与实际正文一起安排')
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
    missing_numeronyms=missing_catalogued_numeronyms(source,assigned,plan['concepts'])
    if missing_numeronyms:
        raise ValueError('原文名称内部的缩写缺少已查证全称规划：'+', '.join(missing_numeronyms))
    if mode == 'rewrite' and plan['contract']['depth'] == 'progressive':
        raise ValueError('改写契约扩大了用户授权范围')
    if prior and plan['contract'] != prior[0]['contract']:
        raise ValueError('后续分组改变了全篇改写契约')
    return plan


def writing_batches(plans, source, limit=6500, concept_limit=10, object_limit=20):
    """Keep the section plan while scheduling adjacent sections in bounded calls."""
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
    for node in [n for p in plans for n in p['nodes']]:
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


def legacy_artifacts(plans, source, execution_nodes=None):
    facts = [f for part in plans for f in part['obligations']]
    inv = copy.deepcopy(source)
    inv['obligations'] = [dict(id=f['id'], object_id=f['source_id'], statement=f['meaning'],
                              conditions=f['conditions'], quantities=f['quantities'],
                              negations=f['negations'], status='unreviewed') for f in facts]
    # Do not invent the independent inventory-review receipt expected by legacy releases
    inv['inventory_review'] = None
    inv = freeze(inv)
    nodes = execution_nodes or [n for p in plans for n in p['nodes']]
    plan = Plan.model_validate(dict(title=nodes[0]['title'], objective=plans[0]['contract']['purpose'],
        research_gaps=[], units=[dict(id=n['id'], title=n['title'], objective=n['purpose'],
            obligation_ids=n['obligation_ids'], prerequisites=n['requires_concepts'],
            stages=n['explanation']['reasoning_steps'] or [n['purpose']], object_ids=n['source_ids'],
            proof_questions=[], follows_units=n['depends_on'], bridge_reason=n['transition_from'],
            entry_knowledge=[n['explanation']['known_start']], reader_question=n['explanation']['obstacle'])
            for n in nodes])).model_dump()
    return inv, plan


def validate_written(value, node, inventory, bundle, prior, source_obligations=(),concepts=()):
    body = A.WrittenUnit.model_validate(value).model_dump()
    sources={o['id']:o for o in inventory['objects']}
    literals=protected_objects(inventory)
    fact_quotes={f['id']:f for f in source_obligations}
    blocks=[]
    for raw in body['blocks']:
        prefixes=[node['id']]+[n['id'] for n in node.get('section_outline',[])]
        if not any(raw['id'].startswith(prefix+'-') for prefix in prefixes):
            raise ValueError('段落身份缺少单元前缀')
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
            if sid not in literals or sid not in node['source_ids']:
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
        binding['block_id']=aliases.get(binding['block_id'],binding['block_id'])
    draft={'blocks':merged}
    draft=planned_heading_depth(draft,node.get('heading_level',2),inventory)
    name_alignments=insert_verified_name_at_unique_first_use(
        draft,body['knowledge_delta'],concepts)
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
    if (set(delta['established_concepts']) != set(node['establishes_concepts'])
            or set(delta['explained_obligations']) != allowed or delta['unresolved_prerequisites']):
        raise ValueError('实际知识增量与规划不符，或仍有未解决的前提')
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
        job['stage']='active_write' if job['unit_index']<len(nodes) else 'active_deliver'
        return 'queued'

    def call(self, job, key, role, payload, schema):
        if key in job['results']:
            return job['results'][key]
        if key in job.get('active_invalid_json', {}):
            fixed=repair_one_missing_json_object_closer(job['active_invalid_json'][key])
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
        if (self.config.get('job_timeout',0)>0 and job.get('started') and
                time.time()-job['started']>=self.config['job_timeout']):
            raise Conflict('本篇已达到处理时间上限，已保存全部完成结果')
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
            if key in job.get('transport_fallback_steps',{}):
                fallback=self.config.get('fallback_providers',{}).get(role,{})
                if fallback:
                    cfg |= fallback
                    call_role=role+'__fallback'
            if role == 'active_visual' and role not in self.config.get('role_providers', {}):
                cfg |= self.config.get('role_providers', {}).get('visual_extract', {})
            cfg=apply_stream_timeout(cfg)
            if self.config.get('job_timeout',0)>0 and job.get('started'):
                remaining=self.config['job_timeout']-(time.time()-job['started'])
                if remaining<=0:
                    raise Conflict('本篇已达到处理时间上限，已保存全部完成结果')
                cfg['call_timeout']=min(float(cfg.get('call_timeout',90)),remaining)
            cfg['deadline_at']=time.time()+float(cfg.get('call_timeout',90))
            cfg['role_providers'] = {}
            before = len(job['calls'])
            try:
                value = Provider(self.store, cfg).call(job['project'], call_role, payload,
                    schema.model_json_schema(), job, lambda: self.queue.cancelled(job['id'], self.owner))
            except json.JSONDecodeError:
                return self.repair_json(job,key,role,payload,schema)
            except Exception:
                if len(job['calls']) == before:
                    job.pop('pending', None)
                    self.store.put_job(job)
                raise
        job['results'][key] = value
        job.pop('pending', None)
        self.store.put_job(job)
        return value

    def repair_json(self,job,key,role,payload,schema):
        if role=='active_protocol':
            raise ValueError('返回格式修正一次后仍不是有效数据，原响应已保存')
        call=job['calls'][-1]
        if call.get('finish_reason') not in {'stop','tool_calls'} or not call.get('response_blob'):
            raise Uncertain('未取得完整响应，不重发未知请求')
        response=json.loads(self.store.read_blob(call['response_blob']))
        message=response['choices'][0]['message']
        text=message.get('content') or message['tool_calls'][0]['function']['arguments']
        job.setdefault('active_invalid_json',{})[key]=text
        job.pop('pending',None)
        self.store.put_job(job)
        return self.call(job,key,role,payload,schema)

    def turn(self, job, key, role, schema, source, ids, payload, validate):
        sessions = job.setdefault('active_sessions', {})
        session = sessions.setdefault(key, {'round': 0, 'corrections': 0})
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
                and not session.get('direct_link_prefetch_complete')):
            from urllib.parse import urlsplit
            current=urlsplit(source.get('source_url',''))
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
            request = payload | dict(catalog=prompt_resource_catalog(catalog), opened_resources=resources.context(),
                                     previous_action_results=session.get('action_results', []),
                                     action_history=session.get('action_history', []))
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
            raw = self.call(job, key + '-turn-' + str(session['round']), role, request, A.Turn[schema])
            try:
                if is_v2(job) and role=='active_plan':
                    checked_raw,removed_extras=discard_known_plan_protocol_extras(raw)
                elif is_v2(job) and role=='active_review':
                    checked_raw,removed_extras=discard_known_review_protocol_extras(raw)
                else:
                    checked_raw,removed_extras=raw,[]
                if removed_extras:
                    job.setdefault('nonblocking_protocol_notes',[]).append(dict(
                        step=key,reason=('redundant naming_status_effective removed from validation copy'
                            if role=='active_plan' else 'empty reason_note removed from validation copy'),
                        object_ids=removed_extras))
                response = A.Turn[schema].model_validate(checked_raw).model_dump()
                response,reused=discard_redundant_reads_with_result(response,resources)
                if reused:
                    session.setdefault('redundant_result_reads',[]).append(dict(
                        turn=session['round'],resource_ids=reused,
                        operation='already_opened_source_reads_omitted'))
                if response['actions']:
                    if response['result'] is not None or not response['gaps']:
                        raise ValueError('读取动作必须说明缺口，不能同时提交结果')
                    session['action_results'] = []
                    for action in response['actions']:
                        if is_v2(job) and action['kind'] in {'search','page','image'}:
                            gap_id=action.get('gap_id','')
                            if role!='active_plan' or not gap_id or gap_id not in response['gaps']:
                                raise ValueError('v2 外部检索必须绑定当前 Turn 明确声明的 EvidenceGap')
                            source_id=action.get('source_id','')
                            source_objects={obj['id']:obj for obj in source['objects']}
                            source_obj=source_objects.get(source_id)
                            if not source_obj or source_id not in ids:
                                raise ValueError('v2 外部检索必须绑定当前分组的原文义务来源')
                            if action['kind']=='search' and source_obj.get('kind')=='link' and source_obj.get('target'):
                                raise ValueError('已有直接 URL 时必须优先读取目标页，不能先搜索')
                            if action['kind']=='page' and source_obj.get('target'):
                                if canonical_url(action.get('url','')) != canonical_url(source_obj['target']):
                                    raise ValueError('直接 URL 证据必须先读取该原文链接目标')
                            if gap_id not in session['declared_gap_ids']:
                                session['declared_gap_ids'].append(gap_id)
                        if is_v2(job) and action['kind'] in {'search','page','image'}:
                            counts=session.setdefault('external_action_counts',{'search':0,'open':0})
                            counter='search' if action['kind']=='search' else 'open'
                            limit=int(self.config.get('evidence_query_limit',2) if counter=='search'
                                      else self.config.get('evidence_open_limit',4))
                            if counts[counter]>=max(0,limit):
                                raise ValueError('v2 证据动作达到当前设置上限：'+counter)
                            counts[counter]+=1
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
                        dict(action=a,result=r) for a,r in zip(response['actions'],session['action_results']))
                    session['round'] += 1
                    session.pop('correction', None)
                    continue
                if (response['result'] is None or response['gaps'] or
                        (not response['ready_reason'].strip() and not is_v2(job))):
                    raise ValueError('资料未齐全，不能提交结果')
                if is_v2(job) and not response['ready_reason'].strip():
                    job.setdefault('nonblocking_protocol_notes',[]).append(dict(
                        step=key,reason='complete structured result returned without a ready_reason'))
                if is_v2(job) and role=='active_plan':
                    returned_gap_ids={gap['id'] for gap in response['result'].get('evidence_gaps',[])}
                    declared_gap_ids=set(session.get('declared_gap_ids',[]))
                    if not declared_gap_ids <= returned_gap_ids:
                        raise ValueError('规划没有保存本轮声明的 EvidenceGap')
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
                session['resources'] = resources.state
                job.setdefault('external_resources', {}).update({k: v for k, v in resources.state['entries'].items()
                                                                 if v['kind'] == 'external'})
                session['complete'] = True
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
                correction_limit=(max(0,min(1,int(self.config.get('max_plan_repairs',1))))
                                  if role=='active_plan' else
                                  max(1,min(2,int(self.config.get('active_structure_correction_limit',2)))))
                if session['corrections'] >= correction_limit:
                    raise ValueError('当前阶段结构修正后仍不成立：' + str(error)) from error
                session['corrections'] += 1
                if role in {'active_write','active_review'} and isinstance(raw,dict) and isinstance(raw.get('result'),dict):
                    session['previous_invalid_result']=raw['result']
                # The exact rejected response already lives in the provider
                # call receipt. Replaying a full invalid plan can push a
                # correction over relay limits; the deterministic validation
                # error is sufficient for the planner to regenerate it.
                job.setdefault('active_correction_receipts',[]).append(dict(
                    step=key,role=role,error=str(error),received_digest=digest(raw)))
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
                if role=='active_write' and '标题照搬了未解释的英文' in str(error):
                    session['correction']['instruction']=(
                        'Rewrite every displayed heading as natural Chinese. Preserve necessary official '
                        'English names in parentheses at first use and explain unfamiliar abbreviations; do '
                        'not leave any heading as an unexplained copy of the English source title. Preserve '
                        'all body content, source bindings, images and factual qualifications')
                elif role=='active_write' and '正文图片缺少与原图绑定的说明' in str(error):
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
            from .materials import require_complete_web_materials
            require_complete_web_materials(source)
            for original in source.get('originals', []):
                self.store.read_blob(original['sha256'])
            for resource in source.get('resources', []):
                self.store.read_blob(resource.get('sha256', resource['id']))
            job['source'] = source
            job['visual_count']=sum(o['kind'] in {'image','page','media'} and bool(o.get('resource_id'))
                and not o.get('visual_classification') and o.get('source_scope') not in
                {'site_chrome','source_metadata'} for o in source['objects'])
            job['stage'] = 'active_visual'
            return 'queued'
        source = job['source']
        if stage == 'active_visual':
            from .visual_sources import image_resources
            from .source_context import classify_web_chrome
            source=classify_web_chrome(self.store,source)
            job['source']=source
            done = {card['source_id'] for card in job['visual_cards']}
            pending = [o for o in source['objects'] if o['kind'] in {'image','page','media'}
                       and o.get('resource_id') and not o.get('visual_classification')
                       and o.get('source_scope') not in {'site_chrome','source_metadata'}
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
                        if [c['source_id'] for c in detail['cards']] != [page['id']] or detail['cards'][0]['blocking_uncertainty']:
                            raise ValueError('图像仍有无法确定的内容，原图与识别结果已保存')
                        card = detail['cards'][0]
                    page['original_extracted_text'] = page.get('text', '')
                    page['text'] = card['source_text'] or card['visible_content']
                    page['visual_card'] = card
                    job['visual_cards'].append(card)
                job['visual_index']=len(job['visual_cards'])
                visited={page['id'] for page in batch}
                source['unknown'] = [g for g in source.get('unknown', []) if g['object_id'] not in visited]
                return 'queued'
            active_ids={o['id'] for o in source['objects'] if o.get('source_scope') not in
                {'site_chrome','source_metadata'}}
            unresolved=[g for g in source.get('unknown',[]) if g['object_id'] in active_ids]
            if unresolved:
                raise ValueError('原件正文仍有无法读取的对象，尚未开始改写：' + json.dumps(unresolved, ensure_ascii=False))
            from .visual_sources import decorative_resource
            job['archived_layout_source_ids']=[o['id'] for o in source['objects'] if decorative_resource(o)]
            plan_chars=(self.config.get('active_v2_plan_source_chars',9000) if is_v2(job)
                        else self.config.get('active_plan_source_chars',12000))
            plan_objects=(self.config.get('active_v2_plan_source_objects',24) if is_v2(job)
                          else self.config.get('active_plan_source_objects',10))
            job['active_groups'] = [[o['id'] for o in group] for group in inventory_groups(
                [o for o in source['objects'] if not decorative_resource(o)],
                plan_chars,plan_objects)]
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
                job['active_partition_count']=len(job['active_groups'])
                job['web_chrome_scope_version']=2
            index = job['active_partition_index']
            if index==len(job['active_groups']):
                retired=retire_unanchored_abbreviations(job['active_plans'],source)
                if retired:job.setdefault('unanchored_formal_concepts',[]).extend(retired)
                job['unanchored_abbreviations_checked']=True
                job['writing_batches']=writing_batches(job['active_plans'],source,
                    node_chars,self.config.get('active_node_concept_limit',10),node_objects)
                if is_v2(job):
                    job['cross_batch_review_required']=any(
                        bool(node.get('cross_batch_risks'))
                        for part in job['active_plans'] for node in part.get('nodes',[]))
                job['inventory'],job['plan']=legacy_artifacts(
                    job['active_plans'],source,job['writing_batches'])
                job['draft']={'blocks':[]}
                job['stage']='active_write'
                return 'queued'
            ids = job['active_groups'][index]
            prefix = 'p' + str(index+1)
            prior = job['active_plans']
            payload = dict(goal=job['goal'], task_mode=job['transformation_mode'], assigned_source_ids=ids,
                **plan_document_preview(source,ids,job['active_groups'][:index]),
                source_spans=prompt_source_spans(source,ids),
                partition_prefix=prefix, node_source_char_limit=node_chars,
                node_concept_limit=self.config.get('active_node_concept_limit',10),
                immutable_contract=prior[0]['contract'] if prior else None,
                preceding_plans=preceding_plan_context(prior),
                visual_cards=[{k:v for k,v in c.items() if k!='source_text'} for c in job['visual_cards'] if c['source_id'] in ids])
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
                plan=validate_plan(value|({'contract':prior[0]['contract']} if prior else {}),source,ids,prior,
                    job['transformation_mode'],node_chars,
                    require_spans=True,resources=resources,
                    require_link_briefs=bool(job.get('link_contract_version')),
                    archived_ids=job.get('archived_layout_source_ids',[]),
                    concept_limit=self.config.get('active_node_concept_limit',10))
                # Drop a proposed formal acronym that the cited original never
                # uses before checking its name. A model may otherwise spend its
                # entire evidence budget proving an unnecessary added glossary
                # term and leave a short rewrite unable to start.
                retired=retire_unanchored_abbreviations([plan],source)
                if retired:
                    job.setdefault('unanchored_formal_concepts',[]).extend(retired)
                return validate_names(plan,resources,allow_unverified_downgrade=is_v2(job)) if job.get('naming_contract_version') else plan
            result = self.turn(job, 'active-plan-'+prefix, 'active_plan', A.CompositionPart if prior else A.CompositionPlan, source, ids, payload,validate_planning)
            job['active_plans'].append(result)
            job['active_partition_index'] += 1
            if job['active_partition_index'] == len(job['active_groups']):
                retired=retire_unanchored_abbreviations(job['active_plans'],source)
                if retired:job.setdefault('unanchored_formal_concepts',[]).extend(retired)
                job['unanchored_abbreviations_checked']=True
                job['writing_batches']=writing_batches(job['active_plans'],source,
                    node_chars,
                    self.config.get('active_node_concept_limit',10),
                    node_objects)
                if is_v2(job):
                    job['cross_batch_review_required']=any(
                        bool(node.get('cross_batch_risks'))
                        for part in job['active_plans'] for node in part.get('nodes',[]))
                job['inventory'], job['plan'] = legacy_artifacts(job['active_plans'], source,job['writing_batches'])
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
                job['inventory'],job['plan']=legacy_artifacts(job['active_plans'],source,job['writing_batches'])
            job['unanchored_abbreviations_checked']=True
        nodes = job.get('writing_batches') or [n for p in job['active_plans'] for n in p['nodes']]
        if stage == 'active_write':
            node = nodes[job['unit_index']]
            job['current_unit_id'] = node['id']
            obligations = [o for p in job['active_plans'] for o in p['obligations'] if o['id'] in node['obligation_ids']]
            risk_context=(bounded_prior_context(job['draft']['blocks'])
                          if is_v2(job) and node.get('cross_batch_risks') else [])
            payload = dict(contract=writing_batch_contract(job['active_plans'][0]['contract'],node,job.get('goal','')), node=writer_node_context(node), obligations=prompt_obligations(obligations),
                obligation_source_rule='Resolve each obligation through source_id and source_span_ids in opened_resources.',
                displayed_heading_rule=('Every displayed heading must contain natural Chinese. Keep necessary official '
                    'English names in parentheses after the Chinese wording; never copy an English-only source heading.'),
                source_binding_rule=('Every authored block source_ids entry must come only from node.source_ids. Prior-tail '
                    'context is for transitions and must never be rebound as evidence for this batch.'),
                link_guides=link_guides(job,node['source_ids']),
                protected_object_catalog=[dict(source_id=sid,kind=next(o['kind'] for o in source['objects'] if o['id']==sid),
                    insert_marker='{{source:'+sid+'}}') for sid in protected_objects(job['inventory']) if sid in node['source_ids']],
                composition_instructions='section_outline is the original planned order inside this writing batch, not separate API calls. Keep consecutive list items inside one block. Source metadata belongs in unchanged, collapsed end matter, never a prose tour of HTML scripts. Insert each required protected object by its given marker; source_ids alone is an evidence mapping, not a displayed object.',
                global_spine=[dict(id=n['id'], title=n['title'], purpose=n['purpose']) for n in nodes],
                concepts=[c for p in job['active_plans'] for c in p['concepts']
                          if c['id'] in node['requires_concepts']+node['establishes_concepts']],
                established_memory=[dict(node_id=m['node_id'],draft_digest=m['draft_digest'],resource_id=m.get('resource_id'),
                    established=[c for c in m['established'] if c['id'] in node['requires_concepts']])
                    for m in job['knowledge_memory'] if any(c['id'] in node['requires_concepts'] for c in m['established'])],
                previous_final_tail=[b['markdown'] for b in job['draft']['blocks'][-2:]],
                risk_review_context=risk_context,
                already_placed_source_ids=list({sid for b in job['draft']['blocks'] for sid in b.get('embedded_object_ids',[])}),
                next_node=nodes[job['unit_index']+1] if job['unit_index']+1<len(nodes) else None,
                visual_cards=[{k:v for k,v in c.items() if k!='source_text'} for c in job['visual_cards'] if c['source_id'] in node['source_ids']])
            def validate(value, resources):
                result=validate_written(value, node, job['inventory'], bundle, job['draft'],
                    obligations,payload['concepts'])
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
                    [c for c in payload['concepts'] if c['id'] in node['establishes_concepts']],draft))
            job['stage'] = 'active_review'
            return 'queued'
        if stage=='active_review':
            node=nodes[job['unit_index']];candidate=job['active_candidate']
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
                            raise ValueError('核对意见引用了其他单元的原文')
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
                        if exact is None:raise ValueError('核对意见未准确引用当前原文')
                        if exact!=finding['source_quote']:
                            result.setdefault('source_quote_alignments',[]).append(dict(source_id=sid,
                                submitted_quote=finding['source_quote'],actual_quote=exact,
                                operation='source_pdf_wrap_alignment' if pdf_wrap else 'source_whitespace_only'))
                            finding['source_quote']=exact
                # Deterministic omissions enter the same bounded local repair
                # transaction as semantic findings, instead of paying for a
                # second full writer response to discover one absent name.
                for omission in missing_concept_names(
                    [c for part in job['active_plans'] for c in part['concepts']
                     if c['id'] in node['establishes_concepts']],draft):
                    sid=next((sid for sid in omission['source_ids'] if sid in node['source_ids']),None)
                    if not sid:continue
                    block=next((b for b in draft['blocks'] if any(
                        token and token in b['markdown'] for token in
                        (omission['chinese_name'],*omission['names']))),None)
                    if not block:continue
                    quote=next((line for line in block['markdown'].splitlines()
                                if omission['chinese_name'] in line),block['markdown'].splitlines()[0])
                    if any(f['block_id']==block['id'] and '术语' in f['problem'] for f in result['findings']):continue
                    result['findings'].append(dict(block_id=block['id'],output_quote=quote,
                        source_id=sid,source_quote=objects[sid]['text'],
                        problem='已查证术语在实际正文中缺少名称：'+', '.join(omission['names']),
                        required_change='仅在本术语首次定义处补齐已查证的中英文名称和缩写展开，保持原句主张及其余正文不变'))
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
                result['protected_reference_notes']=[f for f in result['findings'] if reference_only(f)]
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
                         if c['id'] in node['establishes_concepts']],draft),
                    format_preflight=review_format_context(format_issues),
                    format_decisions_required=bool(job.get('joint_review_contract_version')),
                    visual_cards=visual_cards,
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
            if result['findings']:
                candidate['content_findings']=result['findings']
                if patch_rounds(candidate)>=candidate.get('patch_limit',2) or round_>=self.config.get('active_revision_limit',2):
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
            candidate = job['active_candidate']
            reject_empty_claimed_blocks(candidate['draft'])
            retire_refuted_formal_concepts(job,candidate)
            if is_v2(job):
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
                              if c['id'] in node['establishes_concepts']], draft)
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
                allowed=set(candidate['revision_blocks'])
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
                changed,proposal=self.turn(job,'active-patch-'+node['id']+'-'+str(attempt)+'-'+digest([canonical(previous),writing_batch_contract(job['active_plans'][0]['contract'],node,job.get('goal',''))])[:16],'active_patch',LocalRepair,
                    source,node['source_ids'],dict(contract=writing_batch_contract(job['active_plans'][0]['contract'],node,job.get('goal','')),node=node,
                        document_digest=digest(canonical(previous).encode()),original_draft=previous,
                        editable_block_ids=sorted(allowed),issues=candidate['revision_issues'],
                        link_guides=link_guides(job,node['source_ids']),
                        visual_cards=[{k:v for k,v in c.items() if k!='source_text'} for c in job.get('visual_cards',[]) if c['source_id'] in node['source_ids']],
                        obligations=[f for part in job['active_plans'] for f in part['obligations'] if f['id'] in node['obligation_ids']],
                        previous_final_tail=[b['markdown'] for b in job['draft']['blocks'][-2:]]),validate_patch)
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
                job['stage']='active_format' if is_v2(job) else 'active_review'
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
        if stage == 'active_deliver':
            reject_empty_claimed_blocks(job['draft'])
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
