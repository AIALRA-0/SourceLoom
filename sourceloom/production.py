"""Automatic, role-separated production with immutable call checkpoints."""

import copy
import json
import re
from pathlib import Path
import threading
import time

from . import contracts as C
from . import production_contracts as P
from .checks import freeze, inspect_draft, validate_plan
from .durable import Queue
from .export import render
from .ingest import decode
from .providers import Provider, Uncertain
from .skills import load_bundle, rule_catalog
from .store import Conflict, digest, identity
from .writing import canonical, compose, expand_response, protected_objects, repair, scan, validate_layout_repair
from .pedagogy import (TeachingReview, validate_teaching_plan, teaching_issues,
                       arrange_document_info, repair_units, validate_replan, teaching_review_contract)


def validate_facts(body, source):
    source_map={o['id']:o for o in source['objects']}
    if set(body['assessed_source_ids'])!=set(source_map):
        raise ValueError('生产前信息清点没有准确覆盖全部源对象')
    ids=set()
    for f in body['facts']:
        if f['id'] in ids or f['source_id'] not in source_map or f['quote'] not in source_map[f['source_id']]['text']:
            raise ValueError('原子信息身份重复或原文证据不匹配')
        ids.add(f['id'])
    if {f['source_id'] for f in body['facts']}!=set(source_map):
        raise ValueError('有源对象没有任何生产前信息记录')


def bind_source_quotes(body, source):
    """Bind evidence from immutable source IDs; models never author source bytes."""
    result=copy.deepcopy(body)
    sources={o['id']:o['text'] for o in source['objects']}
    for fact in result['facts']:
        if fact['source_id'] not in sources:
            raise ValueError('清点引用不存在的原对象')
        fact['quote']=sources[fact['source_id']]
    return result


def bind_evidence_layout(value, source):
    """Resolve one whitespace-only excerpt to its exact original byte-level text."""
    texts={o['id']:o['text'] for o in source['objects']}
    def bind(item):
        if isinstance(item,list):
            return [bind(x) for x in item]
        if not isinstance(item,dict):
            return item
        out={k:bind(v) for k,v in item.items()}
        if out.get('source_id') in texts and isinstance(out.get('quote'),str):
            text=texts[out['source_id']]
            quote=out['quote']
            if quote and quote not in text:
                parts=re.split(r'(\s+)',quote)
                pattern=''.join(r'\s+' if p.isspace() else re.escape(p) for p in parts)
                matches=list(re.finditer(pattern,text))
                if len(matches)==1:
                    out['quote']=matches[0].group()
        return out
    return bind(value)


def decision_evidence(response,source):
    """Reviewer selects source IDs; application binds the immutable full objects.

    Earlier raw responses with copied excerpts remain in results/call blobs. They are
    not used as source bytes, nor treated as proof that the reasoning is correct.
    """
    value=copy.deepcopy(response)
    for decision in value['decisions']:
        decision['evidence']=[{'source_id':e['source_id']} for e in decision['evidence']]
    decisions=P.InventoryDecisions.model_validate(value).model_dump()['decisions']
    sources={o['id']:o['text'] for o in source['objects']}
    for decision in decisions:
        for evidence in decision['evidence']:
            if evidence['source_id'] not in sources:
                raise ValueError('审核争议引用不存在的原件')
            evidence['quote']=sources[evidence['source_id']]
    return decisions


def visual_decision(response):
    """Derive failures from typed comparisons, never from free-form observations."""
    if response.get('pages') and 'checks' not in response['pages'][0]:
        return P.VisualAudit.model_validate(response).model_dump()
    pages=P.VisualAuditV2.model_validate(response).model_dump()['pages']
    expected={'text','reading_order','tables','formulas','figures','captions','footnotes'}
    result=[]
    for page in pages:
        categories=[c['category'] for c in page['checks']]
        if len(categories)!=len(expected) or set(categories)!=expected:
            raise ValueError('视觉核对没有完整覆盖文字、顺序及全部对象类型')
        gaps=[c['region']+'：'+c['observation'] for c in page['checks'] if c['status'] not in {'preserved','not_applicable'}]
        result.append(dict(source_id=page['source_id'],status='needs_correction' if gaps else 'verified',
                           discrepancies=gaps,region_evidence=[c['region']+'：'+c['observation'] for c in page['checks']],
                           checks=page['checks']))
    return {'pages':result}


def fidelity_issues(review, facts, draft, source):
    issues=[]
    source_map={o['id']:o['text'] for o in source['objects']}
    fact_map={f['id']:f for f in facts['facts']}
    blocks={b['id']:b for b in draft['blocks']}
    checks=review['fact_checks']
    if set(review['assessed_source_ids'])!=set(source_map) or review['missing_inventory_information']:
        issues.append('独立对照发现清点遗漏或没有核对完整原文')
    if len(checks)!=len(fact_map) or {x['fact_id'] for x in checks}!=set(fact_map):
        issues.append('独立对照没有逐项核对全部原子信息')
    for x in checks:
        fact=fact_map.get(x['fact_id'])
        block=blocks.get(x['block_id'])
        if (not fact or not block or x['source_quote'] not in source_map[fact['source_id']]
            or not x['output_quote'] or x['output_quote'] not in block['markdown']
            or x['status']!='preserved' or not x['person_preserved'] or not x['referents_preserved']):
            issues.append('原信息、人称或指代尚未确认保留：'+x['fact_id'])
    if {r['block_id'] for r in review['reverse_checks']}!=set(blocks):
        issues.append('成稿反查没有覆盖全部正文块')
    for x in review['reverse_checks']:
        block=blocks.get(x['block_id'])
        if not block or x['output_quote'] not in block['markdown'] or x['status']!='supported':
            issues.append('成稿存在无依据或不确定的主张')
        if x['kind']=='source' and not x['evidence']:
            issues.append('原文主张缺少来源证据')
        for ev in x['evidence']:
            if ev['source_id'] not in source_map or ev['quote'] not in source_map[ev['source_id']]:
                issues.append('反查证据与原件不符')
    issues.extend(f['message'] for f in review['findings'])
    return issues


def style_issues(review, catalog, draft, report):
    issues=[]
    assessed=[rid for a in review['assessments'] for rid in a['rule_ids']]
    if len(assessed)!=len(catalog) or set(assessed)!=set(catalog):
        issues.append('写作审核未逐条覆盖完整规则，或重复声明规则通过')
    blocks={b['id']:b['markdown'] for b in draft['blocks']}
    text=canonical(draft)
    for a in review['assessments']:
        if a['status'] in {'fail','unknown'}:
            issues.append('写作规则尚未满足：'+','.join(a['rule_ids']))
        if not set(a['block_ids'])<=set(blocks):
            issues.append('写作审核引用不存在的正文块')
        if a['status']=='pass' and (not a['quotes'] or not a['block_ids']):
            issues.append('写作规则通过声明缺少正文证据')
        if any(q not in text or not q for q in a['quotes']):
            issues.append('写作审核引用的成稿原句不匹配')
    expected={x['id'] for kind in ('findings','candidates') for x in report['format'][kind]}
    adjudicated=review['mechanical_assessments']
    if len(adjudicated)!=len(expected) or {x['candidate_id'] for x in adjudicated}!=expected:
        issues.append('实际检查器的发现和疑点尚未全部裁决')
    if any(x['verdict']!='not_violation' for x in adjudicated):
        issues.append('实际检查仍有写作违规或未裁决项')
    issues.extend(f['message'] for f in review['findings'])
    return issues


class Production:
    def __init__(self, store, config, project=None):
        self.store,self.config=store,config
        self.queue=Queue(store)
        self.provider=Provider(store,config)
        self.owner=identity()
        self.project=project

    def teaching_inventory(self, inventory):
        # All original objects and obligations are included. Internal reviews are
        # kept in the ledger, not injected as competing instructions to the writer.
        return {k:inventory[k] for k in ('id','version','objects','obligations','resources','frozen','digest')}

    def validate_plan(self, job, plan):
        validator=validate_teaching_plan if job.get('teaching_version',0)>=2 else validate_plan
        return validator(plan,job['inventory'])

    def _call(self, job, key, role, payload, schema):
        if key in job['results']:
            return job['results'][key]
        if key in job.get('fallbacks',{}):
            result=self._call(job,key+'__fallback',role+'__fallback',payload,schema)
            job['results'][key]=result
            self.store.put_job(job)
            return result
        if self.queue.cancelled(job['id'],self.owner):
            raise Conflict('后台任务已取消')
        if self.config.get('job_timeout',0)>0 and time.time()-job['started']>self.config['job_timeout']:
            raise Conflict('本篇已达到处理时间上限，已保存全部完成结果')
        if job.get('pending'):
            if job['pending']!=key:
                raise Conflict('待处理请求与当前阶段不一致')
            if not job['calls'] or job['calls'][-1].get('step_key')!=key:
                raise Uncertain('进程在提交边界中断，没有自动重发；需要查询原提交记录')
            try:
                result=self.provider.recover(job)
            except json.JSONDecodeError:
                return self._invalid_response_fallback(job,key,role,payload,schema)
            if result is None:
                raise Uncertain('原请求尚未取得完整结果，未重新提交')
        else:
            job['pending']=key
            job['current_step_key']=key
            self.store.put_job(job)
            config=self.config|self.config.get('role_providers',{}).get(role,{})
            if role.endswith('__fallback'):
                config.update(self.config.get('fallback_providers',{}).get(role.removesuffix('__fallback'),{}))
            if self.config.get('job_timeout',0)>0:
                remaining=max(1,int(self.config['job_timeout']-(time.time()-job['started'])))
                config['call_timeout']=min(config['call_timeout'],remaining)
            config['deadline_at']=time.time()+config['call_timeout']
            config['role_providers']={}
            try:
                prior_calls=len(job['calls'])
                result=Provider(self.store,config).call(job['project'],role,payload,schema.model_json_schema(),job,
                               lambda:self.queue.cancelled(job['id'],self.owner))
            except Conflict:
                if len(job['calls'])==prior_calls:
                    job.setdefault('preflight_stops',[]).append(dict(key=key,at=time.time(),dispatched=False))
                    job.pop('pending',None)
                    self.store.put_job(job)
                raise
            except json.JSONDecodeError:
                return self._invalid_response_fallback(job,key,role,payload,schema)
        # Persist raw role result before validation, so failed validation never loses the artifact.
        job['results'][key]=result
        job.pop('pending',None)
        self.store.put_job(job)
        return result

    def _invalid_response_fallback(self,job,key,role,payload,schema):
        call=job['calls'][-1]
        if (role.endswith('__fallback') or role not in self.config.get('fallback_providers',{})
            or call.get('finish_reason') not in {'stop','tool_calls'} or not call.get('response_blob')):
            raise ValueError('原响应结构无效，未配置可用的受限备用通道')
        job.setdefault('fallbacks',{})[key]=dict(original_call=call['id'],reason='invalid_json_after_normal_finish')
        job.pop('pending',None)
        self.store.put_job(job)
        return self._call(job,key,role,payload,schema)

    def step(self, job):
        bundle=load_bundle(job['writing_skill']['root'],job['writing_skill']['package_digest'])
        source=job['source']
        base=dict(goal=job['goal'],source={'objects':source['objects']},
                  required_source_ids=[o['id'] for o in source['objects']])
        base['source_contract']='The supplied files define input scope. HTML locator child indexes count text/whitespace nodes too; li[2] is NOT a claim that an earlier list item exists. Never derive content ordinals or omissions from locator numbers. Raw Markdown templates are literal source syntax, not missing rendered webpage content unless rendered expansion was explicitly requested. Preserve them without inventing expansion. Object text, raw markup and complete original text are complementary views of the same retained material. Source code bytes stay authoritative over inventory annotations about layout.'
        base['original_text_files']=[dict(name=o['name'],text=decode(self.store.read_blob(o['sha256'])))
            for o in source.get('originals',[]) if Path(o['name']).suffix.lower() in {'.md','.txt','.html','.htm'}]
        stage=job['stage']
        if stage in {'visual_extract','visual_audit'}:
            visual=[o for o in source['objects'] if o['kind'] in {'page','image'} and o.get('resource_id')]
            index=job.get('visual_index',0)
            pages=visual[index:index+3]
            ids={o['id'] for o in pages}
            payload=dict(pages=pages,_image_resources=[dict(source_id=o['id'],sha256=o['resource_id']) for o in pages])
            if stage=='visual_extract':
                result=self._call(job,f'visual_extract-{index}','visual_extract',payload,P.VisualExtraction)
                result=P.VisualExtraction.model_validate(result).model_dump()
                if len(result['pages'])!=len(ids) or {p['source_id'] for p in result['pages']}!=ids:
                    raise ValueError('视觉提取没有准确覆盖本批原始页面')
                if any(p['unresolved'] for p in result['pages']):
                    raise ValueError('原始页面仍有不可辨认内容，保留原件，未猜测补齐')
                job['visual_candidate']=result
                job['stage']='visual_audit'
            else:
                result=self._call(job,f'visual_audit-{index}','visual_audit',payload|dict(extraction=job['visual_candidate']),P.VisualAuditV2)
                result=visual_decision(result)
                inaccessible=any('unsupported image' in d.lower() for p in result['pages'] for d in p['discrepancies'])
                if inaccessible and self.config.get('vision_capability_corrected'):
                    result=self._call(job,f'visual_audit_capability-{index}','visual_audit',
                        payload|dict(extraction=job['visual_candidate'],prior_transport_failure='The previous text-only reviewer could not inspect attached images. This call uses a separately configured verified image-capable channel. Inspect the actual originals independently.'),P.VisualAuditV2)
                    result=visual_decision(result)
                if len(result['pages'])!=len(ids) or {p['source_id'] for p in result['pages']}!=ids:
                    raise ValueError('独立视觉核对没有准确覆盖本批原始页面')
                if any(p['status']!='verified' or p['discrepancies'] for p in result['pages']):
                    raise ValueError('独立视觉核对仍有缺口，未进入教学改写')
                extracted={p['source_id']:p for p in job['visual_candidate']['pages']}
                source=copy.deepcopy(source)
                for obj in source['objects']:
                    if obj['id'] not in ids:
                        continue
                    obj['original_extracted_text']=obj['text']
                    obj['text']=extracted[obj['id']]['source_markdown'] or obj['text'] or obj['locator']
                    obj['visual_figure_descriptions']=extracted[obj['id']]['figure_descriptions']
                    obj['visual_audit']=dict(job=job['id'],result=next(p for p in result['pages'] if p['source_id']==obj['id']))
                source['resolved_visual_gaps']=source.get('resolved_visual_gaps',[])+[g for g in source['unknown'] if g['object_id'] in ids]
                source['unknown']=[g for g in source['unknown'] if g['object_id'] not in ids]
                job['source']=source
                job['visual_index']=index+len(pages)
                job['stage']='visual_extract' if job['visual_index']<len(visual) else 'inventory'
        elif stage=='inventory':
            result=self._call(job,'inventory','fact_inventory',base,P.FactInventory)
            result=P.FactInventory.model_validate(result).model_dump()
            result=bind_source_quotes(result,source)
            try:
                validate_facts(result,source)
            except ValueError as exc:
                result=self._call(job,'inventory_correction-1','inventory_repair',
                                  base|dict(previous_response=result,validator_error=str(exc)),P.FactInventory)
                result=P.FactInventory.model_validate(result).model_dump()
                result=bind_source_quotes(result,source)
                validate_facts(result,source)
            job['facts']=result
            job['stage']='inventory_audit'
        elif stage=='inventory_audit':
            uncertainties=[dict(id=f'uncertainty-{n+1}',description=x) for n,x in enumerate(job['facts']['unresolved'])]
            audit_round=job.get('inventory_semantic_repairs',0)
            audit_key='inventory_audit' if not audit_round else f'inventory_audit-{audit_round}'
            result=self._call(job,audit_key,'inventory_audit',base|dict(facts=job['facts'],inventory_uncertainties=uncertainties),P.InventoryAudit)
            result=P.InventoryAudit.model_validate(result).model_dump()
            expected_uncertainties={a['id'] for a in uncertainties}
            actual_uncertainties=[a['id'] for a in result['uncertainty_assessments']]
            if set(actual_uncertainties)!=expected_uncertainties or len(actual_uncertainties)!=len(expected_uncertainties):
                result=self._call(job,f'inventory_audit_protocol-{audit_round}','inventory_audit',
                    base|dict(facts=job['facts'],inventory_uncertainties=uncertainties,
                        invalid_review=result,protocol_error='The review used nonexistent or duplicate uncertainty IDs. Independently recheck the actual source and facts. Return assessments for exactly the supplied inventory_uncertainties IDs; an empty input list requires an empty assessment list. Do not put passing observations into errors. Source raw markup and target fields are already separately retained; semantic meaning need not duplicate their bytes.'),P.InventoryAudit)
                result=P.InventoryAudit.model_validate(result).model_dump()
            confirmed_errors=list(result['errors'])
            confirmed_unresolved=list(result['unresolved'])
            claims=confirmed_errors+confirmed_unresolved
            if claims:
                decision_key=f'inventory_decision-{audit_round}'+('-all-claims' if confirmed_unresolved else '')
                decisions=self._call(job,decision_key,'inventory_decision',
                    base|dict(facts=job['facts'],uncertainty_assessments=result['uncertainty_assessments'],
                        claims=[dict(claim_index=n,claim=claim) for n,claim in enumerate(claims)]),P.InventoryDecisions)
                decisions=decision_evidence(decisions,source)
                ids=[d['claim_index'] for d in decisions]
                if len(ids)!=len(claims) or set(ids)!=set(range(len(claims))):
                    raise ValueError('审核争议未逐项得到明确裁决')
                sources={o['id']:o['text'] for o in source['objects']}
                if any(e['source_id'] not in sources or e['quote'] not in sources[e['source_id']]
                       for d in decisions for e in d['evidence']):
                    raise ValueError('审核争议裁决缺少与原件匹配的证据')
                job.setdefault('inventory_decisions',{})[str(audit_round)]=decisions
                confirmed_errors=[claims[d['claim_index']] for d in decisions if d['verdict']!='not_error']
                confirmed_unresolved=[] # Every uncertainty above received an explicit independent decision.
            if (set(result['assessed_source_ids'])!={o['id'] for o in source['objects']}
                or set(result['assessed_fact_ids'])!={f['id'] for f in job['facts']['facts']}
                or confirmed_errors or confirmed_unresolved):
                if audit_round<2:
                    fixed=self._call(job,f'inventory_semantic_repair-{audit_round+1}','inventory_repair',
                        base|dict(previous_response=job['facts'],independent_review=result|dict(errors=confirmed_errors,unresolved=confirmed_unresolved),
                                  validator_error='Resolve the cited inventory errors against the full source, preserving all information. Add actual missing facts. Do not invent facts to satisfy incorrect criticism. Canonical source quotes are substrings of source.objects[*].text, not HTML markup; objects retain raw markup and links separately.'),P.FactInventory)
                    fixed=P.FactInventory.model_validate(fixed).model_dump()
                    fixed=bind_source_quotes(fixed,source)
                    validate_facts(fixed,source)
                    job['facts']=fixed
                    job['inventory_semantic_repairs']=audit_round+1
                    return 'queued'
                raise ValueError('独立清单审核发现错误或未知项，尚未进入写作')
            assessments=result['uncertainty_assessments']
            if len(assessments)!=len(uncertainties) or {a['id'] for a in assessments}!={a['id'] for a in uncertainties}:
                raise ValueError('清点疑点尚未逐项得到独立判断')
            source_text={o['id']:o['text'] for o in source['objects']}
            for a in assessments:
                if a['kind']!='original_ambiguity' or a['treatment']!='preserve_without_invention':
                    raise ValueError('独立清单审核确认仍需补充证据或纠正清点')
                if any(e['source_id'] not in source_text or e['quote'] not in source_text[e['source_id']] for e in a['evidence']):
                    raise ValueError('清点疑点的裁决没有匹配原件证据')
            additions=bind_source_quotes({'facts':result['missing_facts']},source)
            job['facts']['facts'].extend(additions['facts'])
            validate_facts(job['facts'],source)
            if source['unknown']:
                raise ValueError('原件仍有解析或视觉未知项，文本审核不能擅自清除')
            inv=copy.deepcopy(source)
            inv['obligations']=[dict(id=f['id'],object_id=f['source_id'],statement=f['meaning'],conditions=f['conditions'],
                quantities=f['quantities'],negations=f['negations'],status='reviewed') for f in job['facts']['facts']]
            inv['inventory_review']={'job':job['id'],'body':result}
            job['inventory']=freeze(inv)
            job['stage']='planner'
        elif stage=='planner':
            result=self._call(job,'planner','planner',dict(goal=job['goal'],inventory=self.teaching_inventory(job['inventory']),
                facts=job['facts'],max_units=self.config.get('max_units',6),
                instruction='Use the fewest pedagogically coherent units that fully cover all facts. Each unit must fit one complete response. Do not omit small details to meet the limit.'),C.Plan)
            job['plan']=self.validate_plan(job,result)
            job['stage']='plan_review'
        elif stage=='plan_review':
            plan_round=job.get('plan_repairs',0)
            # A prior software version could repair a plan based on passing observations
            # misplaced in issues. Re-adjudicate that known result; preserve both versions.
            first_review=job['results'].get('plan_review-0',{})
            if plan_round and first_review.get('status')=='ready' and first_review.get('issues') and not job.get('original_plan_adjudicated'):
                decision=self._call(job,'original_plan_decision','plan_decision',base|dict(plan=job['results']['planner'],
                    claims=[dict(claim_index=n,claim=x) for n,x in enumerate(first_review['issues'])]),P.InventoryDecisions)
                if self._decisions_pass(decision,first_review['issues'],source):
                    job['superseded_plan']=job['plan']
                    job['plan']=self.validate_plan(job,job['results']['planner'])
                    job['plan_review']=first_review
                    job['optional_source_limits']=first_review['optional_source_limits']
                    job.update(draft={'blocks':[]},unit_index=0,stage='writer',original_plan_adjudicated=True)
                    return 'queued'
                job['original_plan_adjudicated']=True
            result=self._call(job,f'plan_review-{plan_round}','plan_review',base|dict(plan=job['plan'],
                              facts=job['facts'],max_units=self.config.get('max_units',6)),P.PlanReview)
            result=P.PlanReview.model_validate(result).model_dump()
            job['plan_review']=result
            if result['status']=='needs_sources' or result['essential_missing_sources']:
                raise ValueError('教学需要的额外依据尚未取得：'+'；'.join(result['essential_missing_sources']))
            plan_errors=bool(result['issues'])
            if result['issues']:
                decision=self._call(job,f'plan_decision-{plan_round}','plan_decision',base|dict(plan=job['plan'],
                    claims=[dict(claim_index=n,claim=x) for n,x in enumerate(result['issues'])]),P.InventoryDecisions)
                plan_errors=not self._decisions_pass(decision,result['issues'],source)
            if result['status']=='needs_repair' or plan_errors or len(job['plan']['units'])>self.config.get('max_units',6):
                if plan_round:
                    if self.config.get('plan_contract_arbiter'):
                        decision=self._call(job,'plan_contract_review','plan_contract_review',base|dict(
                            original_plan=job['results']['planner'],repaired_plan=job['plan'],
                            original_review=first_review,current_review=result),P.PlanSelection)
                        decision=P.PlanSelection.model_validate(decision).model_dump()
                        job['plan_contract_decision']=decision
                        if decision['status']=='ready' and not decision['defects'] and decision['selected']!='none':
                            if decision['selected']=='original':
                                job['superseded_plan']=job['plan']
                                job['plan']=self.validate_plan(job,job['results']['planner'])
                            job['optional_source_limits']=result['optional_source_limits']
                            job.update(draft={'blocks':[]},unit_index=0,stage='writer')
                            return 'queued'
                    raise ValueError('规划修正后仍未满足实际教学目标，已停止后续调用')
                fixed=self._call(job,'planner_repair-1','planner_repair',base|dict(inventory=job['inventory'],
                                 previous_plan=job['plan'],review=result,max_units=self.config.get('max_units',6)),C.Plan)
                try:
                    job['plan']=self.validate_plan(job,fixed)
                except ValueError as exc:
                    fixed=self._call(job,'planner_contract_repair-1','planner_repair',base|dict(
                        inventory=self.teaching_inventory(job['inventory']),previous_plan=job['plan'],
                        invalid_plan=fixed,validator_error=str(exc),review=result,
                        instruction='Repair the returned plan contract without losing any original fact, metadata, or object. Moving document metadata to the end does NOT remove its required obligation/object assignment. Keep the actual teaching improvements. Return the complete corrected plan once.',
                        max_units=self.config.get('max_units',6)),C.Plan)
                    job['plan']=self.validate_plan(job,fixed)
                job['plan_repairs']=1
                return 'queued'
            job['optional_source_limits']=result['optional_source_limits']
            job['draft']={'blocks':[]}
            job['unit_index']=0
            job['stage']='writer'
        elif stage=='writer':
            unit=job['plan']['units'][job['unit_index']]
            needed=set(unit['obligation_ids'])
            inv=copy.deepcopy(self.teaching_inventory(job['inventory']))
            inv['obligations']=[o for o in inv['obligations'] if o['id'] in needed]
            wanted={o['object_id'] for o in inv['obligations']}|set(unit['object_ids'])
            inv['objects']=[o for o in inv['objects'] if o['id'] in wanted]
            writing_input={k:v for k,v in inv.items() if k!='obligations'}
            writing_input['obligation_fact_ids']=[o['id'] for o in inv['obligations']]
            payload=dict(goal=job['goal'],inventory=writing_input,current_unit=unit,
                facts=[f for f in job['facts']['facts'] if f['id'] in needed],
                obligation_binding='Each listed obligation_fact_id is the SAME id in facts. Its original object_id is fact.source_id; statement is fact.meaning; conditions/quantities/negations are the fact fields. The complete pre-production ledger remains saved. Use these IDs in every block obligation_ids.',
                protected_objects=protected_objects(inv),prior_terminology=job.get('terminology',[]),
                optional_source_limits=job.get('optional_source_limits',[]))
            payload.update(full_teaching_route=job['plan'],
                           actual_previous_text=[b for b in job['draft']['blocks'] if b['kind']!='document_info'],
                           actual_surrounding_text=job.get('regeneration',{}).get('original_draft',{}),
                           teaching_findings=job.get('regeneration',{}).get('findings',[]))
            generation=job.get('content_generation',0)
            key='writer-'+unit['id']+(f'-revision-{generation}' if generation else '')
            result=self._call(job,key,'writer',payload,P.FlatDraft)
            result=bind_evidence_layout(result,inv)
            try:
                draft=compose(bundle,result,inv)
            except ValueError as exc:
                fixed=self._call(job,key+'-layout','layout_repair',
                    dict(received=result,validator_error=str(exc)),P.FlatDraft)
                result=validate_layout_repair(result,fixed)
                draft=compose(bundle,result,inv)
            issues=inspect_draft(inv,draft,{'units':[unit]})
            if issues and any(i['code']!='omission' for i in issues) and {i['code'] for i in issues}<={'unknown_obligation','unmapped','quote','object','omission'}:
                from .writing import validate_binding_repair
                fixed=self._call(job,key+'-bindings','binding_repair',
                    dict(received=result,rendered_draft=draft,inventory=inv,current_unit=unit,
                         facts=payload['facts'],validator_issues=issues),P.FlatDraft)
                result=validate_binding_repair(result,bind_evidence_layout(fixed,inv),inv)
                draft=compose(bundle,result,inv)
                issues=inspect_draft(inv,draft,{'units':[unit]})
            completion_key=key+'-complete'
            if issues and {i['code'] for i in issues}=={'omission'} and (
                    completion_key in job.get('unit_completion_attempts',[]) or job['repair_rounds']<2):
                from .writing import append_unit_completion
                if completion_key not in job.setdefault('unit_completion_attempts',[]):
                    job['unit_completion_attempts'].append(completion_key)
                    job['repair_rounds']+=1
                    self.store.put_job(job)
                extra=self._call(job,completion_key,'writer',payload|dict(received=result,
                    completion_only=True,actual_current_text=draft,
                    remaining_fact_ids=[i['obligation_id'] for i in issues]),P.FlatDraft)
                result=append_unit_completion(result,bind_evidence_layout(extra,inv),unit['id'])
                try:
                    draft=compose(bundle,result,inv)
                except ValueError as exc:
                    fixed=self._call(job,completion_key+'-layout','layout_repair',
                        dict(received=result,validator_error=str(exc)),P.FlatDraft)
                    result=validate_layout_repair(result,fixed)
                    draft=compose(bundle,result,inv)
                issues=inspect_draft(inv,draft,{'units':[unit]})
            if issues:
                raise ValueError('单元结构或原对象不完整：'+'；'.join(x['message'] for x in issues[:3]))
            if {b['id'] for b in draft['blocks']} & {b['id'] for b in job['draft']['blocks']}:
                raise ValueError('教学单元使用了重复的正文身份')
            job['draft']['blocks'].extend(draft['blocks'])
            def terms(nodes):
                for n in nodes:
                    if n['type']=='term':
                        yield {k:n.get(k) for k in ('zh','en','abbr','definition')}
                    if n['type']=='section':
                        yield from terms(n['blocks'])
            job.setdefault('terminology',[]).extend(t for b in expand_response(result)['blocks'] for t in terms(b['content']))
            job['unit_index']+=1
            regeneration=job.get('regeneration')
            if regeneration:
                while job['unit_index']<len(job['plan']['units']) and job['plan']['units'][job['unit_index']]['id'] not in regeneration['unit_ids']:
                    next_id=job['plan']['units'][job['unit_index']]['id']
                    job['draft']['blocks'].extend(copy.deepcopy([b for b in regeneration['original_draft']['blocks'] if b['unit_id']==next_id]))
                    job['unit_index']+=1
            if job['unit_index']==len(job['plan']['units']):
                job['draft']=arrange_document_info(job['draft'],job['plan'])
                if regeneration:
                    untouched=[b for b in regeneration['original_draft']['blocks'] if b['unit_id'] not in regeneration['unit_ids']]
                    if untouched!=[b for b in job['draft']['blocks'] if b['unit_id'] not in regeneration['unit_ids']]:
                        raise ValueError('教学修复改变了未命中的正文')
                    job.pop('regeneration',None)
                job['stage']='teaching' if job.get('teaching_version',0)>=2 else 'style'
                job['initial_draft_blob']=self.store.blob(json.dumps(job['draft'],ensure_ascii=False).encode())
        elif stage=='teaching':
            generation=job.get('content_generation',0)
            result=self._call(job,f"teaching-{generation}-{job['repair_rounds']}",'teaching_review',
                             base|dict(plan=job['plan'],draft=job['draft']),TeachingReview)
            result=TeachingReview.model_validate(result).model_dump()
            contract_errors=teaching_review_contract(result,job['draft'],job['plan'])
            if contract_errors:
                result=self._call(job,f"teaching-contract-{generation}-{job['repair_rounds']}",'teaching_review',
                    base|dict(plan=job['plan'],draft=job['draft'],previous_review=result,
                              review_contract_errors=contract_errors,
                              instruction='Re-read actual blocks and correct the review. Quotes must be exact contiguous substrings, never reconstructed summaries. Reconsider substantive judgments using actual evidence. Do not rewrite the draft.'),TeachingReview)
                result=TeachingReview.model_validate(result).model_dump()
                contract_errors=teaching_review_contract(result,job['draft'],job['plan'])
                if contract_errors:
                    job['quality_issues']=['教学审查证据仍无法核对：'+e for e in contract_errors]
                    return 'needs_attention'
            job['teaching_review']=result
            job['teaching_draft_digest']=digest(canonical(job['draft']).encode())
            job['quality_issues']=teaching_issues(result,job['draft'],job['plan'])
            if job['quality_issues']:
                if job['repair_rounds']>=2 or not result['findings']:
                    return 'needs_attention'
                selected=repair_units(result,job['plan'])
                job['repair_rounds']+=1
                job['regeneration']=dict(unit_ids=selected,original_plan=copy.deepcopy(job['plan']),
                    original_draft=copy.deepcopy(job['draft']),findings=result['findings'])
                job.setdefault('content_history',[]).append(self.store.blob(json.dumps(job['regeneration'],ensure_ascii=False).encode()))
                job['stage']='teaching_replan'
            else:
                job['stage']='style'
        elif stage=='teaching_replan':
            regeneration=job['regeneration']
            result=self._call(job,f"teaching-replan-{job['repair_rounds']}",'teaching_replan',
                base|dict(inventory=job['inventory'],**regeneration),C.Plan)
            job['plan']=validate_replan(result,regeneration['original_plan'],regeneration['unit_ids'],job['inventory'])
            job['stage']='teaching_replan_review'
        elif stage=='teaching_replan_review':
            result=self._call(job,f"teaching-replan-review-{job['repair_rounds']}",'plan_review',
                base|dict(plan=job['plan'],facts=job['facts'],repair_context=job['regeneration']),P.PlanReview)
            result=P.PlanReview.model_validate(result).model_dump()
            if result['status']!='ready' or result['issues'] or result['essential_missing_sources']:
                job['quality_issues']=result['issues']+result['essential_missing_sources'] or ['教学修复规划尚未通过独立检查']
                return 'needs_attention'
            regeneration=job['regeneration']
            start=next(i for i,u in enumerate(job['plan']['units']) if u['id'] in regeneration['unit_ids'])
            previous={u['id'] for u in job['plan']['units'][:start]}
            job['draft']={'blocks':[b for b in regeneration['original_draft']['blocks'] if b['unit_id'] in previous]}
            job.update(unit_index=start,content_generation=job.get('content_generation',0)+1,terminology=[],stage='writer')
            for key in ('teaching_review','teaching_draft_digest','style','scan','fidelity'):
                job.pop(key,None)
        elif stage=='fidelity':
            index=job['repair_rounds']
            result=self._call(job,f'fidelity-{index}','fidelity',base|dict(facts=job['facts'],draft=job['draft']),P.FidelityReview)
            job['fidelity']=P.FidelityReview.model_validate(result).model_dump()
            job['fidelity_draft_digest']=digest(canonical(job['draft']).encode())
            if not job.get('style') or not job.get('scan'):
                # Resume older checkpoints through the full writing review as well.
                job['stage']='style'
            else:
                issues=fidelity_issues(job['fidelity'],job['facts'],job['draft'],source)
                issues+=style_issues(job['style'],rule_catalog(bundle['instructions']),job['draft'],job['scan'])
                job['quality_issues']=issues
                if issues and job['repair_rounds']>=2:
                    return 'needs_attention'
                job['stage']='repair' if issues else 'publish'
        elif stage=='style':
            report=scan(bundle,job['draft'],self.store.root/'production'/job['id']/'checks')
            catalog=rule_catalog(bundle['instructions'])
            payload=dict(draft=job['draft'],source={'objects':source['objects']},rule_catalog=list(catalog),
                         mechanical_findings=report['format']['findings'],mechanical_candidates=report['format']['candidates'])
            result=self._call(job,f"style-{job['repair_rounds']}",'style',payload,P.StyleReview)
            job['style']=P.StyleReview.model_validate(result).model_dump()
            job['scan']=report
            job['style_draft_digest']=digest(canonical(job['draft']).encode())
            issues=style_issues(job['style'],catalog,job['draft'],report)
            if job.get('fidelity'):
                issues+=fidelity_issues(job['fidelity'],job['facts'],job['draft'],source)
            issues += [x['message'] for x in inspect_draft(job['inventory'],job['draft'],job['plan'])]
            job['quality_issues']=issues
            if issues:
                if job['repair_rounds']>=2:
                    return 'needs_attention'
                job['stage']='repair'
            else:
                job['stage']='publish' if job.get('fidelity') else 'fidelity'
        elif stage=='repair':
            findings=job['style']['findings']+job.get('fidelity',{}).get('findings',[])
            allowed={f['block_id'] for f in findings if f['block_id']}
            if not allowed:
                raise ValueError('审核存在缺口，但没有足以支持精确修复的定位；未擅自重写')
            index=job['repair_rounds']+1
            result=self._call(job,f'local_repair-{index}','local_repair',dict(draft=job['draft'],source=source,
                findings=findings,document_digest=digest(canonical(job['draft']).encode())),P.LocalRepair)
            job['repair_rounds']=index
            self.store.put_job(job)
            job['draft']=repair(bundle,job['draft'],result,allowed,self.store.root/'production'/job['id']/f'repair-{index}')
            job.pop('fidelity',None)
            job.pop('style',None)
            job.pop('scan',None)
            job['stage']='teaching' if job.get('teaching_version',0)>=2 else 'style'
        elif stage=='publish':
            current=digest(canonical(job['draft']).encode())
            if job.get('teaching_version',0)>=2:
                if job.get('teaching_draft_digest')!=current or not job.get('teaching_review'):
                    job['quality_issues']=['当前正文尚未通过同版本的整篇教学审查']
                    return 'needs_attention'
                if teaching_review_contract(job['teaching_review'],job['draft'],job['plan']) or teaching_issues(job['teaching_review'],job['draft'],job['plan']):
                    job['quality_issues']=['当前正文仍有教学缺口']
                    return 'needs_attention'
            if (not job.get('style') or not job.get('fidelity') or not job.get('scan')
                or job.get('style_draft_digest')!=current or job.get('fidelity_draft_digest')!=current):
                job['quality_issues']=['当前正文尚无相同版本的完整写作与独立原文核对']
                return 'needs_attention'
            issues=style_issues(job['style'],rule_catalog(bundle['instructions']),job['draft'],job['scan'])
            issues+=fidelity_issues(job['fidelity'],job['facts'],job['draft'],source)
            issues += [x['message'] for x in inspect_draft(job['inventory'],job['draft'],job['plan'])]
            job['quality_issues']=issues
            if issues:return 'needs_attention'
            return 'completed'
        else:
            raise ValueError('未知生产阶段')
        return 'queued'

    def _decisions_pass(self, response, claims, source):
        decisions=decision_evidence(response,source)
        ids=[d['claim_index'] for d in decisions]
        if len(ids)!=len(claims) or set(ids)!=set(range(len(claims))):
            raise ValueError('审核争议未逐项得到明确裁决')
        sources={o['id']:o['text'] for o in source['objects']}
        if any(e['source_id'] not in sources or e['quote'] not in sources[e['source_id']]
               for d in decisions for e in d['evidence']):
            raise ValueError('审核争议裁决没有匹配的原文证据')
        return all(d['verdict']=='not_error' for d in decisions)

    def run_once(self):
        if self.config.get('generation_pause_reason'):
            return False
        job=self.queue.claim(self.owner,project=self.project)
        if not job:
            return False
        stop=threading.Event()
        def renew():
            while not stop.wait(10):
                if not self.queue.heartbeat(job['id'],self.owner):
                    break
        thread=threading.Thread(target=renew,daemon=True)
        thread.start()
        try:
            if self.queue.cancelled(job['id'],self.owner):
                status='cancelled'
            elif self.config.get('job_timeout',0)>0 and time.time()-job['started']>self.config['job_timeout']:
                raise Conflict('本篇已达到处理时间上限，已保存全部完成结果')
            else:
                status=self.step(job)
            publish=None
            if status in {'completed','needs_attention'}:
                receipt=dict(job=job['id'],status=status,canonical_digest=digest(canonical(job['draft']).encode()),
                    skill_digest=job['writing_skill']['instruction_digest'],issues=job.get('quality_issues',[]),
                    automatic=True,manual_edits=0,revision=job['base_revision']+1)
                publish=dict(inventory=job['inventory'],plan=job['plan'],draft=job['draft'],production=receipt,
                             review=None,accepted_revision=None,repair_rounds=job['repair_rounds'])
            self.queue.finish(job,self.owner,status,publish)
        except Exception as exc:
            job['error']=str(exc)[:500] if isinstance(exc,(ValueError,Uncertain)) else type(exc).__name__
            status='uncertain' if isinstance(exc,Uncertain) else 'cancelled' if self.queue.cancelled(job['id'],self.owner) else 'failed'
            self.queue.finish(job,self.owner,status)
        finally:
            stop.set()
            thread.join(timeout=1)
            from .trials import outcome
            outcome(self.store,self.config,job,job['status'])
        return True


def worker(store, config, once=False):
    engine=Production(store,config)
    while True:
        worked=engine.run_once()
        if once:
            return
        if not worked:
            time.sleep(1)
