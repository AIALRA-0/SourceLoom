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
from .active_resources import Resources
from .checks import freeze, inspect_draft
from .contracts import Plan
from .providers import Provider, Uncertain
from .skills import load_bundle
from .source_context import classify_inert_markup, classify_layout_tables, inventory_groups
from .store import Conflict, digest
from .writing import canonical, compose, protected_objects, repair, scan, trim_block_edges, tighten_list_spacing

PIPELINE = 'active_composition_v1'


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
    """Resolve PDF line wrapping only, returning actual source characters."""
    if not quote or not quote.strip():return None
    if quote in source:return quote
    # The ordinary path permits whitespace only, never spelling or numbers.
    pattern=r'\s+'.join(re.escape(word) for word in quote.split())
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


def initialize(job):
    role_names=('active_plan','active_write','active_review','active_format','active_revision','active_patch','active_protocol','active_visual','rewrite_scope')
    policy={name:(Path(__file__).parent/'roles'/f'{name}.md').read_text(encoding='utf8') for name in role_names}
    job.update(pipeline=PIPELINE, stage='active_index', teaching_version=0,
               active_plans=[], active_partition_index=0, unit_index=0,
               knowledge_memory=[], visual_cards=[], quality_issues=[],role_policy=policy,role_policy_digest=digest(policy))
    return job


def unique(values, label):
    if len(values) != len(set(values)) or '' in values:
        raise ValueError(label + '身份为空或重复')


def format_signature(text):
    """Only spacing, presentation punctuation and letter case may change here."""
    numeric=tuple(re.findall(r'(?<!\w)[+-]?\d+(?:[.,]\d+)*(?:[eE][+-]?\d+)?%?',text))
    return ''.join(c.casefold() for c in text if unicodedata.category(c)[0] in {'L','N','S'}),numeric


def normalize_authored_spacing(draft,inventory):
    changed=copy.deepcopy(draft)
    literals=protected_objects(inventory)
    for block in changed['blocks']:
        if block['kind'] in {'object','document_info','source'}:continue
        stripped=block|{'markdown':block['markdown'].strip('\r\n')}
        proposal,_=tighten_list_spacing({'blocks':[stripped]})
        text=proposal['blocks'][0]['markdown']
        if all(block['markdown'].count(v)==text.count(v) for v in literals.values() if v):
            block['markdown']=text
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


def validate_plan(value, source, assigned, prior=(), mode='rewrite', node_limit=6500,require_spans=False):
    from .production import bind_evidence_layout
    value=bind_evidence_layout(value,source)
    plan = A.CompositionPlan.model_validate(value).model_dump()
    objects = {o['id']: o for o in source['objects']}
    assigned = set(assigned)
    obligations = {o['id']: o for o in plan['obligations']}
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
    nodes = plan['nodes']
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
        for cid in node['requires_concepts']:
            if cid not in established and cid not in explicitly_established and cid in concepts:
                if set(concepts[cid]['requires'])<=established:
                    node['establishes_concepts'].append(cid)
                    explicitly_established.add(cid)
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
        if re.search(r'[A-Za-z]',node['title']) and not re.search(r'[\u3400-\u9fff]',node['title']):
            raise ValueError('面向中文读者的编排标题不能照搬未解释的英文标题')
        node_spans={s for f in node['obligation_ids'] for s in obligations[f].get('source_span_ids',[])}
        size=sum(spans[s]['end']-spans[s]['start'] for s in node_spans) if node_spans else sum(len(obligations[f]['quote']) for f in node['obligation_ids'])
        if size > node_limit:
            raise ValueError('单元负责的信息过长，需要按实际主题拆分义务，不能删减')
        new = set(node['establishes_concepts'])
        if not new <= concepts.keys() or new & established:
            raise ValueError('概念首次解释位置重复或不存在')
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
    if mode == 'rewrite' and plan['contract']['depth'] == 'progressive':
        raise ValueError('改写契约扩大了用户授权范围')
    if prior and plan['contract'] != prior[0]['contract']:
        raise ValueError('后续分组改变了全篇改写契约')
    return plan


def writing_batches(plans, source, limit=6500):
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
        if current and size+extra>limit:
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


def validate_written(value, node, inventory, bundle, prior, source_obligations=()):
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
            if sid not in literals or sid not in node['source_ids']:
                raise ValueError('原对象插入标记不存在，或把普通文字当作原文搬移')
            embedded.append(sid)
            return literals[sid]
        text=re.sub(r'\{\{source:([^{}]+)\}\}',insert,raw['markdown'])
        if '{{source:' in text:raise ValueError('原对象插入标记未完整闭合')
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
    allowed = set(node['obligation_ids'])
    if any(not set(b['obligation_ids']) <= allowed for b in draft['blocks']):
        raise ValueError('写作把其他单元的事实声明为已经覆盖')
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
    if (set(delta['established_concepts']) != set(node['establishes_concepts'])
            or set(delta['explained_obligations']) != allowed or delta['unresolved_prerequisites']):
        raise ValueError('实际知识增量与规划不符，或仍有未解决的前提')
    unique([c['concept_id'] for c in delta['concept_evidence']], '概念正文证据')
    if not {c['concept_id'] for c in delta['concept_evidence']} <= set(node['establishes_concepts']):
        raise ValueError('概念记忆引用了未安排的概念')
    for item in delta['concept_evidence']:
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

    def call(self, job, key, role, payload, schema):
        if key in job['results']:
            return job['results'][key]
        if key in job.get('active_invalid_json', {}):
            result = self.call(job, key+'-protocol', 'active_protocol',
                dict(original_request=payload, returned_text=job['active_invalid_json'][key],
                     instruction='Repair JSON encoding and requested structure only; preserve all words, values and claims'), schema)
            job['results'][key]=result
            self.store.put_job(job)
            return result
        if self.queue.cancelled(job['id'], self.owner):
            raise Conflict('任务已取消')
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
            if role == 'active_visual' and role not in self.config.get('role_providers', {}):
                cfg |= self.config.get('role_providers', {}).get('visual_extract', {})
            cfg['role_providers'] = {}
            before = len(job['calls'])
            try:
                value = Provider(self.store, cfg).call(job['project'], role, payload,
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
        resources = Resources(self.store, source, session.get('resources'))
        if 'resources' not in session:
            for sid in ids:
                resources.read(sid)
            # Previous research remains addressable without repeatedly placing all of it in context
            for entry in job.get('external_resources', {}).values():
                resources.state['entries'][entry['id']] = entry
            for entry in job.get('generated_resources', {}).values():
                resources.state['entries'][entry['id']] = entry
        cap = self.config.get('active_resource_rounds', 8)
        while session['round'] <= cap:
            session['resources'] = resources.state
            self.store.put_job(job)
            request = payload | dict(catalog=resources.catalog(), opened_resources=resources.context(),
                                     previous_action_results=session.get('action_results', []))
            if session.get('correction'):
                request['protocol_correction'] = session['correction']
            raw = self.call(job, key + '-turn-' + str(session['round']), role, request, A.Turn[schema])
            try:
                response = A.Turn[schema].model_validate(raw).model_dump()
                if response['actions']:
                    if response['result'] is not None or not response['gaps']:
                        raise ValueError('读取动作必须说明缺口，不能同时提交结果')
                    session['action_results'] = [resources.execute(a) for a in response['actions']]
                    session['round'] += 1
                    session.pop('correction', None)
                    continue
                if response['result'] is None or response['gaps'] or not response['ready_reason'].strip():
                    raise ValueError('资料未齐全，不能提交结果')
                if not all(resources.fully_read(sid) for sid in ids):
                    raise ValueError('当前来源仍有未读取部分')
                value = validate(response['result'], resources)
                session['resources'] = resources.state
                job.setdefault('external_resources', {}).update({k: v for k, v in resources.state['entries'].items()
                                                                 if v['kind'] == 'external'})
                session['complete'] = True
                self.store.put_job(job)
                return value
            except (ValueError, KeyError) as error:
                # One protocol/structure correction per stage, not an audit loop
                if session['corrections'] >= 1:
                    raise ValueError('当前阶段结构修正后仍不成立：' + str(error)) from error
                session['corrections'] += 1
                session['correction'] = dict(error=str(error), received=raw,
                    instruction='Correct only this invalid artifact; preserve all valid content and bindings')
                session['round'] += 1
        raise ValueError('按需取材仍未收敛，已保存资料与具体缺口，没有生成替代稿')

    def step(self, job):
        stage = job['stage']
        bundle = load_bundle(job['writing_skill']['root'], job['writing_skill']['package_digest'])
        if stage == 'active_index':
            from .word_structures import resolve_word_structures
            from .visual_sources import classify_transparent
            source = resolve_word_structures(self.store, classify_layout_tables(classify_inert_markup(job['source'])))
            source = classify_transparent(self.store, source)
            for original in source.get('originals', []):
                self.store.read_blob(original['sha256'])
            for resource in source.get('resources', []):
                self.store.read_blob(resource.get('sha256', resource['id']))
            job['source'] = source
            job['visual_count']=sum(o['kind'] in {'image','page'} and bool(o.get('resource_id')) and not o.get('visual_classification') for o in source['objects'])
            job['stage'] = 'active_visual'
            return 'queued'
        source = job['source']
        if stage == 'active_visual':
            from .visual_sources import image_resources
            done = {card['source_id'] for card in job['visual_cards']}
            pending = [o for o in source['objects'] if o['kind'] in {'image','page'}
                       and o.get('resource_id') and not o.get('visual_classification') and o['id'] not in done]
            if pending:
                page = pending[0]
                key = 'active-visual-' + page['id']
                result = A.VisualCards.model_validate(self.call(job, key, 'active_visual',
                    dict(pages=[page], _image_resources=image_resources(self.store, [page], source)), A.VisualCards)).model_dump()
                if [c['source_id'] for c in result['cards']] != [page['id']]:
                    raise ValueError('视觉卡没有准确对应原对象')
                card = result['cards'][0]
                if not set(card['blocking_uncertainty'])<=set(card['uncertainty']):
                    raise ValueError('视觉阻断项必须引用实际无法确定的内容')
                if card['blocking_uncertainty']:
                    # One targeted read of the same image, not a mandatory second reviewer
                    result = A.VisualCards.model_validate(self.call(job, key+'-detail', 'active_visual',
                        dict(pages=[page], previous_card=card,
                             _image_resources=image_resources(self.store, [page], source, [page['id']])), A.VisualCards)).model_dump()
                    if [c['source_id'] for c in result['cards']] != [page['id']] or result['cards'][0]['blocking_uncertainty']:
                        raise ValueError('图像仍有无法确定的内容，原图与识别结果已保存')
                    card = result['cards'][0]
                page['original_extracted_text'] = page.get('text', '')
                page['text'] = card['source_text'] or card['visible_content']
                page['visual_card'] = card
                job['visual_cards'].append(card)
                job['visual_index']=len(job['visual_cards'])
                source['unknown'] = [g for g in source.get('unknown', []) if g['object_id'] != page['id']]
                return 'queued'
            if source.get('unknown'):
                raise ValueError('原件仍有无法读取的对象，尚未开始改写：' + json.dumps(source['unknown'], ensure_ascii=False))
            job['active_groups'] = [[o['id'] for o in group] for group in inventory_groups(
                source['objects'], self.config.get('active_plan_source_chars', 12000))]
            job['active_partition_count']=len(job['active_groups'])
            job['stage'] = 'active_plan'
            return 'queued'
        if stage == 'active_plan':
            index = job['active_partition_index']
            ids = job['active_groups'][index]
            prefix = 'p' + str(index+1)
            prior = job['active_plans']
            payload = dict(goal=job['goal'], task_mode=job['transformation_mode'], assigned_source_ids=ids,
                whole_document_source_ids=[o['id'] for o in source['objects']],
                whole_document_files=[o['name'] for o in source.get('originals',[])],
                source_spans=source_spans(source,ids),
                partition_prefix=prefix, node_source_char_limit=self.config.get('active_node_source_chars',6500),
                immutable_contract=prior[0]['contract'] if prior else None,
                preceding_plans=[dict(contract=p['contract'], concepts=p['concepts'], nodes=p['nodes']) for p in prior],
                future_headings=[dict(id=o['id'], text=o['text']) for o in source['objects']
                                 if o['kind']=='heading' and o['id'] not in {s for g in job['active_groups'][:index+1] for s in g}],
                visual_cards=[{k:v for k,v in c.items() if k!='source_text'} for c in job['visual_cards'] if c['source_id'] in ids])
            result = self.turn(job, 'active-plan-'+prefix, 'active_plan', A.CompositionPart if prior else A.CompositionPlan, source, ids, payload,
                lambda value, resources: validate_plan(value|({'contract':prior[0]['contract']} if prior else {}), source, ids, prior, job['transformation_mode'],
                                                       self.config.get('active_node_source_chars',6500),require_spans=True))
            job['active_plans'].append(result)
            job['active_partition_index'] += 1
            if job['active_partition_index'] == len(job['active_groups']):
                job['writing_batches']=writing_batches(job['active_plans'],source,self.config.get('active_node_source_chars',6500))
                job['inventory'], job['plan'] = legacy_artifacts(job['active_plans'], source,job['writing_batches'])
                job['draft'] = {'blocks': []}
                job['stage'] = 'active_write'
            return 'queued'
        nodes = job.get('writing_batches') or [n for p in job['active_plans'] for n in p['nodes']]
        if stage == 'active_write':
            node = nodes[job['unit_index']]
            job['current_unit_id'] = node['id']
            obligations = [o for p in job['active_plans'] for o in p['obligations'] if o['id'] in node['obligation_ids']]
            payload = dict(contract=job['active_plans'][0]['contract'], node=node, obligations=obligations,
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
                already_placed_source_ids=list({sid for b in job['draft']['blocks'] for sid in b.get('embedded_object_ids',[])}),
                next_node=nodes[job['unit_index']+1] if job['unit_index']+1<len(nodes) else None,
                visual_cards=[{k:v for k,v in c.items() if k!='source_text'} for c in job['visual_cards'] if c['source_id'] in node['source_ids']])
            def validate(value, resources):
                return validate_written(value, node, job['inventory'], bundle, job['draft'],obligations)
            draft, coverage, delta = self.turn(job, 'active-write-'+node['id'], 'active_write', A.WrittenUnit,
                source, node['source_ids'], payload, validate)
            job['active_candidate'] = dict(draft=draft, coverage=coverage, delta=delta, rounds=0)
            job['stage'] = 'active_review'
            return 'queued'
        if stage=='active_review':
            node=nodes[job['unit_index']];candidate=job['active_candidate']
            draft=candidate['draft'];round_=int(candidate.get('content_review_round',0))
            obligations=[f for p in job['active_plans'] for f in p['obligations'] if f['id'] in node['obligation_ids']]
            review_ids=set(node['obligation_ids'])
            patch=candidate.get('last_patch')
            if patch and any(set(r['checked_obligation_ids'])==review_ids for r in candidate.get('content_reviews',[])):
                changed_ids={e['block_id'] for e in patch['edits']}
                review_ids={fid for b in draft['blocks'] if b['id'] in changed_ids for fid in b['obligation_ids']}
            def validate_review(value,resources):
                result=A.ContentReview.model_validate(value).model_dump()
                checked=set(result['checked_obligation_ids'])
                if not review_ids<=checked<=set(node['obligation_ids']):
                    raise ValueError('核对遗漏本次影响的原信息，或引用了其他单元')
                blocks={b['id']:b for b in draft['blocks']};objects={o['id']:o for o in source['objects']}
                literals=protected_objects(job['inventory'])
                for finding in result['findings']:
                    if finding['block_id'] not in blocks or finding['output_quote'] not in blocks[finding['block_id']]['markdown']:
                        raise ValueError('核对意见未准确引用实际正文')
                    if finding['source_id']:
                        sid=finding['source_id']
                        if sid not in node['source_ids']:raise ValueError('核对意见引用了其他单元的原文')
                        exact=next((q for text in (objects[sid]['text'],literals.get(sid,''))
                            if (q:=exact_source_quote(finding['source_quote'],text,objects[sid]['kind']=='page')) is not None),None)
                        if exact is None:raise ValueError('核对意见未准确引用当前原文')
                        if exact!=finding['source_quote']:
                            result.setdefault('source_quote_alignments',[]).append(dict(source_id=sid,
                                submitted_quote=finding['source_quote'],actual_quote=exact,
                                operation='source_pdf_wrap_alignment' if objects[sid]['kind']=='page' else 'source_whitespace_only'))
                            finding['source_quote']=exact
                # A collapsed facsimile is an original reference, not a new
                # teaching diagram whose immutable bytes a writer should edit
                references={b['id'] for b in draft['blocks'] if b['kind']=='document_info'
                    and b['id']==node['id']+'-source-'+next(iter(b.get('object_ids',[])),'')
                    and all(objects[s]['kind'] in {'page','metadata'} for s in b.get('object_ids',[]))}
                # An exact compiler-owned literal is not authored prose. Keep
                # comments about it as evidence without asking the writer to
                # mutate that object. Its bytes are checked by the compiler.
                def reference_only(f):
                    return f['block_id'] in references or any(f['output_quote']==literal for literal in literals.values() if literal)
                result['protected_reference_notes']=[f for f in result['findings'] if reference_only(f)]
                result['findings']=[f for f in result['findings'] if not reference_only(f)]
                return result
            base='active-review-'+node['id']+'-'+str(round_)
            review_key=base+'-'+digest(canonical(draft).encode())[:16]
            # Compatibility for already-returned reviews: replay only after the
            # archived request proves it reviewed this exact candidate.
            old_calls=[c for c in job['calls'] if c.get('step_key','').startswith(base+'-turn-') and c.get('wire_request_blob')]
            if old_calls:
                same=True
                for c in old_calls:
                    wire=json.loads(self.store.read_blob(c['wire_request_blob']))
                    messages=[m for m in wire.get('messages',[]) if m.get('role')=='user']
                    try:same=same and json.loads(messages[-1]['content']).get('actual_draft')==draft
                    except (ValueError,KeyError,IndexError,TypeError):same=False
                if same:review_key=base
            result=self.turn(job,review_key,'active_review',A.ContentReview,
                source,node['source_ids'],dict(contract=job['active_plans'][0]['contract'],node=node,
                    actual_draft=draft,obligations=obligations,prior_findings=candidate.get('content_findings',[]),
                    review_obligation_ids=sorted(review_ids),
                    exact_revision=candidate.get('last_patch'),
                    protected_originals=protected_objects(job['inventory']),
                    previous_final_tail=[b['markdown'] for b in job['draft']['blocks'][-2:]]),validate_review)
            record=dict(draft_digest=digest(canonical(draft).encode()),**result)
            reviews=candidate.setdefault('content_reviews',[])
            if not reviews or reviews[-1]!=record:reviews.append(record)
            if result['findings']:
                candidate['content_findings']=result['findings']
                if round_>=self.config.get('active_revision_limit',4):
                    raise ValueError('定向修订后仍有具体内容问题：'+json.dumps(result['findings'],ensure_ascii=False))
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
                candidate['delta']['concept_evidence']=[e for e in candidate['delta'].get('concept_evidence',[]) if e['output_quote'] in by_id[e['block_id']]['markdown']]
            report = respect_original_format(scan(bundle, draft, self.store.root/'production'/job['id']/node['id']),draft,job['inventory'])
            candidate['original_format_exemptions']=report['original_exemptions']
            findings = report['format']['findings']
            candidates = [c for c in report['format']['candidates'] if candidate_key(c,draft) not in candidate.get('dismissed_keys', [])]
            if findings or candidates:
                if candidate['format_rounds'] >= 2:
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
                    proposal = dict(document_digest=result['document_digest'], edits=[dict(
                        block_id=e['block_id'], old_text=e['old_text'], new_text=e['new_text'], reason=e['rule']) for e in result['edits']])
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
            candidate['draft'] = draft
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
                    if not proposal['edits']:raise ValueError('已确认的问题需要实际补丁')
                    changed=repair(bundle,previous,proposal,allowed,
                        self.store.root/'production'/job['id']/node['id']/('content-patch-'+str(attempt)))
                    for literal in protected_objects(job['inventory']).values():
                        if literal and canonical(previous).count(literal)!=canonical(changed).count(literal):
                            raise ValueError('精确修订不能改变原对象')
                    return changed,proposal
                changed,proposal=self.turn(job,'active-patch-'+node['id']+'-'+str(attempt)+'-'+digest(canonical(previous).encode())[:16],'active_patch',LocalRepair,
                    source,node['source_ids'],dict(contract=job['active_plans'][0]['contract'],node=node,
                        document_digest=digest(canonical(previous).encode()),original_draft=previous,
                        editable_block_ids=sorted(allowed),issues=candidate['revision_issues'],
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
                job['stage']='active_review'
                return 'queued'
            revision_count=len(candidate.get('revision_history',[]))
            if revision_count>=self.config.get('active_revision_limit',4):raise ValueError('当前单元定向修订仍未解决问题，已保存候选与具体意见')
            key='active-revision-'+node['id']+('-'+str(revision_count+1) if revision_count else '')
            previous=candidate['draft']
            payload=dict(contract=job['active_plans'][0]['contract'],node=node,
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
            job['stage']='active_review' if candidate.get('content_findings') else 'active_format'
            return 'queued'
        if stage == 'active_deliver':
            findings = inspect_draft(job['inventory'], job['draft'], job['plan'])
            if findings:
                raise ValueError('交付结构检查失败：'+json.dumps(findings, ensure_ascii=False))
            if len(job['active_checkpoints']) != len(nodes):
                raise ValueError('部分写作单元缺少检查点')
            for point in job['active_checkpoints']:
                actual = {'blocks': [b for b in job['draft']['blocks'] if b['unit_id']==point['node_id']]}
                if digest(canonical(actual).encode()) != point['draft_digest']:
                    raise ValueError('交付正文与已检查单元版本不同')
            job['delivery_checks'] = dict(source_digest=job['source_digest'],
                draft_digest=digest(canonical(job['draft']).encode()), source_objects=len(source['objects']),
                obligation_count=len(job['inventory']['obligations']), unit_count=len(nodes),
                structural_status='passed', semantic_status='not_independently_reviewed',
                unit_review_status='model_checked' if all(p.get('content_reviews') for p in job['active_checkpoints']) else 'not_requested',
                skill_delivery='unabridged_inline', manual_edits=0)
            return 'completed'
        raise ValueError('未知主动编排阶段：' + stage)
