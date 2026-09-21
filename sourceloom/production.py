"""Automatic, role-separated production with immutable call checkpoints."""

import copy
import json
import re
import sqlite3
from pathlib import Path
import threading
import time

from . import contracts as C
from . import production_contracts as P
from .checks import freeze, inspect_draft, validate_plan,source_quote_matches
from .durable import Queue
from .export import render
from .ingest import decode, attach_markdown_fences
from .providers import Provider, Uncertain, ReasoningExhausted, apply_stream_timeout
from .skills import load_bundle, rule_catalog
from .store import Conflict, digest, identity
from .source_context import objects_view,inventory_groups,merge_inventories,patch_inventory,classify_inert_markup
from .writing import (available_draft, canonical, compose, expand_response, protected_objects,
                      repair, scan, validate_layout_repair)
from .pedagogy import (TeachingReview, validate_teaching_plan, unmark_nonmetadata_document_info, mark_known_document_metadata, teaching_issues,
                       arrange_document_info, repair_units, validate_replan, teaching_review_contract)


def draft_text_view(draft, unit_ids=None):
    """Keep every authored word and block identity; omit duplicated audit bindings."""
    wanted=set(unit_ids) if unit_ids is not None else None
    return {'blocks':[{k:b[k] for k in ('id','unit_id','kind','markdown')}
                      for b in draft['blocks'] if wanted is None or b['unit_id'] in wanted]}


def recoverable_delivery(job):
    """Build a publishable best-effort result from saved work and untouched source."""
    inventory=copy.deepcopy(job.get('inventory') or job.get('source'))
    if not inventory or not inventory.get('objects'):
        raise ValueError('没有可恢复的原件对象')
    draft=copy.deepcopy(available_draft(job) or {'blocks':[]})
    represented={sid for block in draft['blocks'] for sid in block.get('object_ids',[])}
    obligations=inventory.get('obligations',[])
    by_object={}
    for obligation in obligations:
        by_object.setdefault(obligation['object_id'],[]).append(obligation['id'])
    literals=protected_objects(inventory)
    used={block['id'] for block in draft['blocks']}
    for index,obj in enumerate(inventory['objects'],1):
        sid=obj['id']
        if sid in represented:
            continue
        markdown=literals.get(sid)
        if markdown is None:
            markdown=obj.get('text') or obj.get('raw') or ''
        if not markdown.strip():
            continue
        block_id='recovered-'+sid
        if block_id in used:
            block_id=f'recovered-{index}-{sid}'
        used.add(block_id)
        draft['blocks'].append(dict(id=block_id,unit_id='recovered-source',kind='source',
            markdown=markdown,obligation_ids=by_object.get(sid,[]),object_ids=[sid],
            evidence=[{'source_id':sid,'quote':obj.get('text','')}],embedded_object_ids=[sid]))
    if not draft['blocks']:
        raise ValueError('原件没有可显示的内容')
    plan=copy.deepcopy(job.get('plan'))
    if not plan:
        plan=dict(title='原件保留稿',objective='按原顺序保留当前材料，供继续编辑或重新生成',
            research_gaps=[],units=[dict(id='recovered-source',title='原件内容',
                objective='完整保留当前材料',obligation_ids=[o['id'] for o in obligations],
                prerequisites=[],stages=['按原顺序保留'],object_ids=[o['id'] for o in inventory['objects']],
                proof_questions=[],reader_question='',entry_knowledge=[],example_thread='',
                learning_result='可查看、编辑和重新生成',follows_units=[],bridge_reason='',
                document_info_ids=[])],teaching_functions=[])
    return inventory,plan,draft


def retryable_v2_gateway_timeout(job, error, limit=1):
    """Release one explicit gateway timeout so the saved stage can continue.

    A returned 502/503/504/524 page contains no model artifact to recover.  The
    completed visual, planning, and writing checkpoints stay in the job; only
    the current step receives a new call identity.  Unknown socket timeouts are
    deliberately excluded because their delivery state cannot be established.
    """
    call=(job.get('calls') or [{}])[-1]
    key=job.get('pending')
    retries=job.setdefault('transient_gateway_retries',{})
    if (job.get('pipeline')!='active_composition_v2' or not isinstance(error,Uncertain)
            or not key or call.get('step_key')!=key or call.get('status')!='uncertain'
            or call.get('http_status') not in {502,503,504,524}
            or not call.get('response_blob') or int(retries.get(key,{}).get('attempts',0))>=limit):
        return False
    retries[key]=dict(attempts=int(retries.get(key,{}).get('attempts',0))+1,
        previous_call=call.get('id'),http_status=call.get('http_status'),
        operation='retry_current_saved_stage_after_explicit_gateway_response')
    job.setdefault('internal_recoveries',[]).append(dict(stage=job.get('stage'),step=key,
        type='transient_gateway_response',http_status=call.get('http_status'),at=time.time()))
    job.pop('pending',None)
    job.pop('error',None)
    job.pop('error_type',None)
    return True


def style_review_payload(job, source, catalog, report):
    """Give style review the whole draft and every displayed source literal.

    Full source semantics belong to the separately partitioned fidelity review.
    This avoids sending the unchanged source a second time while preserving the
    exact code, links, tables, images and other source bytes visible in the draft.
    """
    from .review_context import protected_context
    from .style_parts import evidence_catalog
    return dict(draft=draft_text_view(job['draft']),rule_catalog=list(catalog),rule_definitions=catalog,
        verified_terminology=job.get('verified_terminology',[]),retrieved_background=job.get('terminology_background',{}),
        mechanical_findings=report['format']['findings'],mechanical_candidates=report['format']['candidates'],
        execution_evidence=report['execution_evidence'],protected_originals=protected_context(source,job['draft']),
        evidence_catalog=evidence_catalog(job['draft']),
        review_scope=('The complete candidate is supplied. protected_originals contains every exact source literal actually displayed '
            'inside it. Complete source meaning and fact preservation are assessed independently by the fidelity role against the '
            'complete original; style review must not invent a source-loss verdict for source text outside the candidate.'))


def check_scoped_plan_addition(previous, candidate):
    """A coverage-only repair may add assignments, never remove saved ones."""
    old_units=previous['units']
    new_units=candidate['units']
    if [u['id'] for u in old_units]!=[u['id'] for u in new_units]:
        raise ValueError('补漏规划改变了原有教学单元身份或顺序')
    for old,new in zip(old_units,new_units):
        if not set(old['obligation_ids'])<=set(new['obligation_ids']):
            raise ValueError('补漏规划删掉了已有原文事实分配')
        if not set(old['object_ids'])<=set(new['object_ids']):
            raise ValueError('补漏规划删掉了已有原文对象分配')


def apply_coverage_patch(plan, patch, missing_fact_ids, facts):
    """Apply a tiny reviewed-role assignment to the saved full plan."""
    patch=P.CoveragePatch.model_validate(patch).model_dump()
    result=copy.deepcopy(plan)
    units={u['id']:u for u in result['units']}
    source_by_fact={f['id']:f['source_id'] for f in facts}
    assigned=[item['fact_id'] for item in patch['assignments']]
    if len(assigned)!=len(set(assigned)) or set(assigned)!=set(missing_fact_ids):
        raise ValueError('规划补漏没有准确分配全部遗漏事实')
    for item in patch['assignments']:
        if item['unit_id'] not in units or item['fact_id'] not in source_by_fact:
            raise ValueError('规划补漏引用不存在的事实或单元')
        unit=units[item['unit_id']]
        unit['obligation_ids'].append(item['fact_id'])
        source_id=source_by_fact[item['fact_id']]
        if source_id not in unit['object_ids']:
            unit['object_ids'].append(source_id)
    if len(patch['stage_additions'])>len(missing_fact_ids):
        raise ValueError('规划补漏新增了过多无关阶段')
    for item in patch['stage_additions']:
        if item['unit_id'] not in units:
            raise ValueError('规划补漏引用不存在的教学单元')
        stages=units[item['unit_id']]['stages']
        if item['new_stage'] in stages or (item['after_stage'] and item['after_stage'] not in stages):
            raise ValueError('规划补漏的教学阶段不能定位')
        position=stages.index(item['after_stage'])+1 if item['after_stage'] else len(stages)
        stages.insert(position,item['new_stage'])
    check_scoped_plan_addition(plan,result)
    return result


def plan_review_needs_repair(review, confirmed_issues, unit_count, max_units):
    """A review label cannot outweigh its independently rejected claims."""
    return (confirmed_issues or unit_count>max_units or
            (review['status']=='needs_repair' and not review['issues']))


def unit_limit_for_inventory(inventory, configured_limit, mode=None):
    """Small text lessons should not consume a six-unit route by default."""
    source_chars=sum(len(o.get('text','')) for o in inventory['objects'])
    has_complex_layout=any(o['kind'] in {'page','image','table','formula'} for o in inventory['objects'])
    return min(configured_limit,1 if mode=='rewrite' else 3) if source_chars<=5000 and not has_complex_layout else configured_limit


def restore_frozen_markdown_fences(unit_inventory, frozen_inventory, store):
    """Recover literal code fences from saved original bytes without changing facts."""
    missing={o['id'] for o in unit_inventory['objects'] if o['kind']=='code' and not o.get('fence_raw')}
    if not missing:
        return []
    recovered=[]
    for original in frozen_inventory['originals']:
        name=original['name']
        if Path(name).suffix.lower() not in {'.md','.markdown'}:
            continue
        if not any(o['id'] in missing and o['locator'].startswith(name+'/') for o in unit_inventory['objects']):
            continue
        all_from_file=copy.deepcopy([o for o in frozen_inventory['objects'] if o['locator'].startswith(name+'/')])
        attach_markdown_fences(all_from_file,decode(store.read_blob(original['sha256'])))
        exact={o['id']:o for o in all_from_file if o.get('fence_raw')}
        for source in unit_inventory['objects']:
            if source['id'] in missing and source['id'] in exact:
                source['fence_raw']=exact[source['id']]['fence_raw']
                source['fence_info']=exact[source['id']]['fence_info']
                recovered.append(source['id'])
    return recovered


def validate_facts(body, source):
    source_map={o['id']:o for o in source['objects']}
    if set(body['assessed_source_ids'])!=set(source_map):
        raise ValueError('生产前信息清点没有准确覆盖全部源对象')
    ids=set()
    for f in body['facts']:
        if f['id'] in ids or f['source_id'] not in source_map or not source_quote_matches(f['quote'],source_map[f['source_id']]['text']):
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


def merge_inventory_additions(facts,additions,source):
    """Replay a saved audit idempotently without altering conflicting facts."""
    merged=bind_source_quotes(facts,source);extra=bind_source_quotes({'facts':additions},source)
    by_id={};ordered=[]
    for fact in [*merged['facts'],*extra['facts']]:
        if fact['id'] in by_id:
            if by_id[fact['id']]!=fact:raise ValueError('同一原子信息身份对应不同内容，未覆盖任何事实')
            continue
        by_id[fact['id']]=fact;ordered.append(fact)
    merged['facts']=ordered;validate_facts(merged,source)
    return merged


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


def bind_uncertainty_evidence(response,source):
    """Replace inaccurate excerpts with the exact complete cited source object."""
    result=copy.deepcopy(response)
    texts={o['id']:o['text'] for o in source['objects']}
    replacements=[]
    for assessment in result.get('uncertainty_assessments',[]):
        for evidence in assessment['evidence']:
            sid=evidence['source_id']
            if sid not in texts or not texts[sid]:
                raise ValueError('清点疑点引用不存在或没有文字的源对象')
            if not source_quote_matches(evidence['quote'],texts[sid]):
                evidence['quote']=texts[sid]
                replacements.append({'uncertainty_id':assessment['id'],'source_id':sid})
    return result,replacements


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
        if (not fact or not block or not source_quote_matches(x['source_quote'],source_map[fact['source_id']])
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
            if ev['source_id'] not in source_map or not source_quote_matches(ev['quote'],source_map[ev['source_id']]):
                issues.append('反查证据与原件不符')
    issues.extend(f['message'] for f in review['findings'])
    return issues


def style_issues(review, catalog, draft, report):
    from .review_context import PROCESS_RULES
    issues=[]
    assessed=[rid for a in review['assessments'] for rid in a['rule_ids']]
    if len(assessed)!=len(catalog) or set(assessed)!=set(catalog):
        issues.append('写作审核未逐条覆盖完整规则，或重复声明规则通过')
    blocks={b['id']:b['markdown'] for b in draft['blocks']}
    for a in review['assessments']:
        if a['status'] in {'fail','unknown'}:
            issues.append('写作规则尚未满足：'+','.join(a['rule_ids']))
        if not set(a['block_ids'])<=set(blocks):
            issues.append('写作审核引用不存在的正文块')
        execution=report.get('execution_evidence',{})
        process=set(a['rule_ids'])<=PROCESS_RULES and bool(a.get('execution_ids'))
        if process:
            records=[execution.get(key,{}) for key in a['execution_ids']]
            if (any(r.get('status')!='pass' for r in records) or
                    not set(a['rule_ids'])<=set(rid for r in records for rid in r.get('rule_ids',[]))):
                issues.append('写作过程声明缺少可验证执行记录')
        if a['status']=='pass' and not process and (not a['quotes'] or not a['block_ids']):
            issues.append('写作规则通过声明缺少正文证据')
        if any(not q or not any(q in blocks.get(bid,'') for bid in a['block_ids']) for q in a['quotes']):
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
        self.queue=Queue(store,config.get('worker_concurrency',2))
        self.provider=Provider(store,config)
        self.owner=identity()
        self.project=project

    def repair_limit(self):
        """Bound adaptive repair while allowing production to finish real cases."""
        return max(2,min(32,int(self.config.get('max_repair_rounds',2))))

    def _regenerate_protected_structure(self,job,source):
        """Regenerate a unit when an exact source object must move rather than be edited."""
        if job.get('teaching_version',0)<2 or job['repair_rounds']>=self.repair_limit():return False
        literals=[text for text in protected_objects(source).values() if text]
        blocks={b['id']:b for b in job['draft']['blocks']}
        strong_markers=('单独一行','独立成行','移回','放回','relocat')
        weak_markers=('位置','归属','顺序','position','order')
        findings=[*job.get('style',{}).get('findings',[]),*job.get('fidelity',{}).get('findings',[])]
        targets=[];selected=[]
        for finding in findings:
            block=blocks.get(finding.get('block_id'))
            detail=' '.join(str(finding.get(k,'')) for k in ('message','expected')).casefold()
            exact_literals=[literal for literal in literals if block and literal in block['markdown']]
            false_absence=exact_literals and any(phrase in detail for phrase in (
                '未逐字出现','没有逐字出现','未原样出现','does not appear verbatim','not appear verbatim'))
            protected_position=(any(marker in detail for marker in strong_markers) or
                (any(marker in detail for marker in weak_markers) and
                 any(literal.casefold() in detail for literal in exact_literals)))
            if (block and any(literal in block['markdown'] for literal in literals)
                    and protected_position and not false_absence):
                targets.append(finding)
                if block['unit_id'] not in selected:selected.append(block['unit_id'])
        if not targets:return False
        job['repair_rounds']+=1
        job['regeneration']=dict(unit_ids=selected,original_plan=copy.deepcopy(job['plan']),
            original_draft=copy.deepcopy(job['draft']),findings=targets,
            reason='A protected source object must be repositioned by regeneration; exact-line repair cannot edit or move it')
        job.setdefault('content_history',[]).append(self.store.blob(json.dumps(job['regeneration'],ensure_ascii=False).encode()))
        job['stage']='teaching_replan'
        return True

    def teaching_inventory(self, inventory):
        # All original objects and obligations are included. Internal reviews are
        # kept in the ledger, not injected as competing instructions to the writer.
        return {k:inventory[k] for k in ('id','version','objects','obligations','resources','frozen','digest')}|{'objects':objects_view(inventory['objects'])}

    def validate_plan(self, job, plan):
        if job.get('teaching_version',0)>=2:
            plan,_=unmark_nonmetadata_document_info(plan,job['inventory'])
            plan,_=mark_known_document_metadata(plan,job['inventory'])
            plan=validate_teaching_plan(plan,job['inventory'],job.get('transformation_mode'))
        else:
            plan=validate_plan(plan,job['inventory'])
        # Every assigned fact already determines its original object exactly.
        # Bind these IDs before reviews, as the writer's source slice already does.
        objects={o['id']:o['object_id'] for o in job['inventory']['obligations']}
        for unit in plan['units']:
            for fid in unit['obligation_ids']:
                sid=objects[fid]
                if sid not in unit['object_ids']:unit['object_ids'].append(sid)
        return plan

    def validated_initial_plan(self,job):
        """Reconstruct the first plan plus its saved contract-only patch."""
        saved=job['results'].get('planner-contract-document-info-1',job['results']['planner'])
        plan,_=unmark_nonmetadata_document_info(saved,job['inventory'])
        plan,_=mark_known_document_metadata(plan,job['inventory'])
        dependencies=job['results'].get('planner-contract-dependencies-1')
        if dependencies:
            from .pedagogy import apply_dependency_patch
            plan=apply_dependency_patch(plan,dependencies)
        patch=job['results'].get('planner-contract-coverage-patch-1')
        if patch:
            record=(job.get('planner_contract_repair') or {}).get('coverage_repair') or {}
            missing=record.get('missing_fact_ids')
            if not missing:
                expected={o['id'] for o in job['inventory']['obligations']}
                missing=sorted(expected-{fid for unit in plan['units'] for fid in unit['obligation_ids']})
            plan=apply_coverage_patch(plan,patch,missing,job['facts']['facts'])
        return self.validate_plan(job,plan)

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
        if self.config.get('job_timeout',0)>0 and time.time()-job.get('started',time.time())>self.config['job_timeout']:
            raise Conflict('本篇已达到处理时间上限，已保存全部完成结果')
        if job.get('pending'):
            if job['pending']!=key:
                raise Conflict('待处理请求与当前阶段不一致')
            if not job['calls'] or job['calls'][-1].get('step_key')!=key:
                raise Uncertain('进程在提交边界中断，没有自动重发；需要查询原提交记录')
            try:
                result=self.provider.recover(job)
            except Uncertain as error:
                return self._returned_service_error_retry(job,key,role,payload,schema,error)
            except (json.JSONDecodeError, ReasoningExhausted):
                return self._invalid_response_fallback(job,key,role,payload,schema)
            except ValueError as error:
                return self._known_truncated_fallback(job,key,role,payload,schema,error)
            if result is None:
                raise Uncertain('原请求尚未取得完整结果，未重新提交')
        else:
            job['pending']=key
            job['current_step_key']=key
            self.store.put_job(job)
            primary_base=key in job.get('primary_base_steps',[]) or role in job.get('primary_base_roles',[])
            config=self.config if primary_base else self.config|self.config.get('role_providers',{}).get(role,{})
            primary_continuation=((role in {'writer','term_preparation'} and job.get('writer_use_primary'))
                or key in job.get('primary_continuation_steps',[])
                or role in job.get('primary_continuation_roles',[]) or primary_base)
            if job.get('quality_fallback_of') and not primary_continuation:
                config.update(self.config.get('quality_fallback_providers',{}).get(role,{}))
            if role=='coverage_patch':
                config['max_output_tokens']=min(config['max_output_tokens'],2000)
            if role.endswith('__fallback'):
                config.update(self.config.get('fallback_providers',{}).get(role.removesuffix('__fallback'),{}))
            config=apply_stream_timeout(config)
            if self.config.get('job_timeout',0)>0:
                remaining=max(1,int(self.config['job_timeout']-(time.time()-job.get('started',time.time()))))
                config['call_timeout']=min(config['call_timeout'],remaining)
            config['deadline_at']=time.time()+config['call_timeout']
            config['role_providers']={}
            try:
                prior_calls=len(job['calls'])
                result=Provider(self.store,config).call(job['project'],role,payload,schema.model_json_schema(),job,
                               lambda:self.queue.cancelled(job['id'],self.owner))
            except Uncertain as error:
                return self._returned_service_error_retry(job,key,role,payload,schema,error)
            except Conflict:
                if len(job['calls'])==prior_calls:
                    job.setdefault('preflight_stops',[]).append(dict(key=key,at=time.time(),dispatched=False))
                    job.pop('pending',None)
                    self.store.put_job(job)
                raise
            except (json.JSONDecodeError, ReasoningExhausted):
                return self._invalid_response_fallback(job,key,role,payload,schema)
            except ValueError as error:
                return self._known_truncated_fallback(job,key,role,payload,schema,error)
        # Persist raw role result before validation, so failed validation never loses the artifact.
        job['results'][key]=result
        job.pop('pending',None)
        self.store.put_job(job)
        return result

    def _indexed_style_result(self,job,key,payload):
        """One smaller assignment pass after a fully returned non-review reply."""
        from .style_parts import IndexedReviewSchema,partitions
        result=self._call(job,key,'style',payload,IndexedReviewSchema(payload))
        if result!={'_incomplete_style_review':True}:return result
        count=sum(len(payload[k]) for k in ('rule_catalog','mechanical_findings','mechanical_candidates'))
        if count<=20:raise ValueError('本组审核未返回检查结果，原响应已保存，未继续重复请求')
        combined={'findings':[]}
        for number,part in enumerate(partitions(payload,20),1):
            part_key=key+f'-smaller-{number}'
            received=self._call(job,part_key,'style',part,IndexedReviewSchema(part))
            from .style_parts import decode_indexed
            from jsonschema import ValidationError
            try:decode_indexed(received,part)
            except ValidationError as error:
                received=self._repair_indexed_style_part(job,part_key,part,received,error)
            combined['findings'].extend(received['findings'])
            for group in ('rules_by_id','checks_by_id'):
                if group in received:
                    if set(combined.get(group,{}))&set(received[group]):raise ValueError('缩小审核分组出现重复判断')
                    combined.setdefault(group,{}).update(received[group])
        return combined

    def _repair_indexed_style_part(self,job,key,payload,received,error):
        """Repair one bounded malformed style partition without dropping criticism."""
        from .style_parts import IndexedReviewSchema,decode_indexed
        from jsonschema import ValidationError
        latest=received;latest_error=error
        for attempt in range(1,3):
            suffix='-contract' if attempt==1 else '-contract-2'
            contract=payload|dict(
                received_review=latest,
                contract_error=str(latest_error),
                response_instruction='Return the exact indexed schema for this same bounded partition. Preserve every real failure, unknown verdict and finding. Correct malformed findings, invalid block identities, missing rows, extra unassigned rows and root fields. Return every assigned identity exactly once. Do not rewrite prose or turn defects into passes.')
            latest=self._call(job,key+suffix,'style_contract_repair',contract,IndexedReviewSchema(payload))
            try:
                decode_indexed(latest,payload)
                return latest
            except ValidationError as next_error:
                latest_error=next_error
        raise latest_error

    def _returned_service_error_retry(self,job,key,role,payload,schema,error):
        """One separately accounted retry of a returned subscription service error.

        A transport timeout without a response never qualifies. The first call
        retains unknown consumption, rather than being relabeled as free.
        """
        from .providers import returned_subscription_service_error
        call=(job.get('calls') or [{}])[-1]
        if key in job.get('service_error_retries',{}) or not returned_subscription_service_error(self.store,call):
            raise error
        job.setdefault('service_error_retries',{})[key]={'original_call':call['id'],
            'response_blob':call['response_blob'],'reason':'One separate retry after a returned subscription service error; first call consumption remains unknown'}
        job.pop('pending',None);self.store.put_job(job)
        return self._call(job,key,role,payload,schema)

    def _known_truncated_fallback(self,job,key,role,payload,schema,error):
        call=(job.get('calls') or [{}])[-1]
        with self.store.connect() as cx:
            billed=cx.execute('SELECT actual FROM spending WHERE id=?',(call.get('id',''),)).fetchone()
        if (role in {'term_preparation','style','style_contract_repair','writer','layout_repair','fidelity','teaching_replan'}
                and role in self.config.get('fallback_providers',{})
                and call.get('status')=='truncated' and call.get('finish_reason')=='length'
                and call.get('response_blob') and billed and billed['actual'] is not None):
            job.setdefault('fallbacks',{})[key]=dict(original_call=call['id'],reason='known_truncated_review')
            job.pop('pending',None);self.store.put_job(job)
            return self._call(job,key,role,payload,schema)
        raise error

    def _invalid_response_fallback(self,job,key,role,payload,schema):
        call=job['calls'][-1]
        empty_reasoning=(call.get('status')=='reasoning_exhausted' and call.get('finish_reason') in {'length','stop'})
        if (role.endswith('__fallback') or role not in self.config.get('fallback_providers',{})
            or (call.get('finish_reason') not in {'stop','tool_calls'} and not empty_reasoning) or not call.get('response_blob')):
            raise ValueError('原响应结构无效，未配置可用的受限备用通道')
        job.setdefault('fallbacks',{})[key]=dict(original_call=call['id'],reason=
            'output_budget_spent_on_reasoning_without_artifact' if empty_reasoning else 'invalid_json_after_normal_finish')
        job.pop('pending',None)
        self.store.put_job(job)
        return self._call(job,key,role,payload,schema)

    def _finish_draft(self,job):
        # A mistaken metadata label cannot remove unapproved content from the
        # main flow. Keep it in place for the following full-content review.
        allowed={sid for u in job['plan']['units'] for sid in u.get('document_info_ids',[])}
        candidate=copy.deepcopy(job['draft']);kept=[]
        for b in candidate['blocks']:
            if b['kind']=='document_info' and (not b['evidence'] or
                    not {e['source_id'] for e in b['evidence']}<=allowed):
                b['kind']='explanation'
                if b['id'] not in kept:kept.append(b['id'])
        candidate=arrange_document_info(candidate,job['plan'])
        regeneration=job.get('regeneration')
        if regeneration:
            untouched=[b for b in regeneration['original_draft']['blocks'] if b['unit_id'] not in regeneration['unit_ids']]
            if untouched!=[b for b in candidate['blocks'] if b['unit_id'] not in regeneration['unit_ids']]:
                raise ValueError('教学修复改变了未命中的正文')
            job.pop('regeneration',None)
        job['draft']=candidate
        job['document_info_kept_in_body']=list(dict.fromkeys(job.get('document_info_kept_in_body',[])+kept))
        job['stage']='teaching' if job.get('teaching_version',0)>=2 and job.get('review_order')!='style_first' else 'style'
        job['initial_draft_blob']=self.store.blob(json.dumps(job['draft'],ensure_ascii=False).encode())
        return 'queued'

    def step(self, job):
        if job.get('pipeline') in {'active_composition_v1','active_composition_v2'}:
            from .active_composition import ActiveComposition
            return ActiveComposition(self).step(job)
        bundle=load_bundle(job['writing_skill']['root'],job['writing_skill']['package_digest'])
        from .word_structures import resolve_word_structures
        source=resolve_word_structures(self.store,classify_inert_markup(job['source']));job['source']=source
        base=dict(goal=job['goal'],source={'objects':objects_view(source['objects'])},
                  required_source_ids=[o['id'] for o in source['objects']])
        base['source_contract']='The supplied files define input scope. HTML locator child indexes count text/whitespace nodes too; li[2] is NOT a claim that an earlier list item exists. Never derive content ordinals or omissions from locator numbers. Raw Markdown templates are literal source syntax, not missing rendered webpage content unless rendered expansion was explicitly requested. Preserve them without inventing expansion. Object text, raw markup and complete original text are complementary views of the same retained material. Source code bytes stay authoritative over inventory annotations about layout.'
        base['original_text_files']=[dict(name=o['name'],text=decode(self.store.read_blob(o['sha256'])))
            for o in source.get('originals',[]) if Path(o['name']).suffix.lower() in {'.md','.txt','.html','.htm'}]
        stage=job['stage']
        if stage in {'visual_extract','visual_audit'}:
            from .visual_sources import classify_transparent,image_resources
            source=classify_transparent(self.store,source);job['source']=source
            visual=[o for o in source['objects'] if o['kind'] in {'page','image'} and o.get('resource_id') and not o.get('visual_classification')]
            index=job.get('visual_index',0)
            pages=visual[index:index+3]
            ids={o['id'] for o in pages}
            if not pages:
                job['stage']='inventory';return 'queued'
            key=f'visual_extract-{index}'
            previous=job['results'].get(key)
            if previous and {p['source_id'] for p in previous.get('pages',[])}!=ids:key+='-'+digest(sorted(ids))[:8]
            def image_view(objects):
                return [{k:o[k] for k in ('id','kind','locator')} for o in objects]
            payload=dict(pages=image_view(pages),_image_resources=image_resources(self.store,pages,source))
            if stage=='visual_extract':
                result=self._call(job,key,'visual_extract',payload,P.VisualExtraction)
                result=P.VisualExtraction.model_validate(result).model_dump()
                if len(result['pages'])!=len(ids) or {p['source_id'] for p in result['pages']}!=ids:
                    raise ValueError('视觉提取没有准确覆盖本批原始页面')
                unresolved={p['source_id'] for p in result['pages'] if p['unresolved']}
                if unresolved:
                    focused=[o for o in pages if o['id'] in unresolved]
                    corrected=self._call(job,key+'-detail-repair','visual_extract',
                        dict(pages=image_view(focused),_image_resources=image_resources(self.store,focused,source),
                             prior_extraction=[p for p in result['pages'] if p['source_id'] in unresolved],
                             instruction='Reinspect these exact original images once at the supplied higher detail. Resolve the listed uncertain glyphs from the actual image; extracted text is fallible. Return complete corrected transcripts for only these IDs, preserving all other readable text. Blank transparency is absence of visible content, not unreadable text. Keep any truly unreadable region unresolved.'),P.VisualExtraction)
                    corrected=P.VisualExtraction.model_validate(corrected).model_dump()
                    if len(corrected['pages'])!=len(unresolved) or {p['source_id'] for p in corrected['pages']}!=unresolved:raise ValueError('视觉局部修正没有覆盖指定原件')
                    by_id={p['source_id']:p for p in corrected['pages']}
                    result={'pages':[by_id.get(p['source_id'],p) for p in result['pages']]}
                    # The independent image reviewer adjudicates these explicit
                    # uncertainties too; an extractor's doubt is not itself a verdict.
                job['visual_candidate']=result
                job['stage']='visual_audit'
            else:
                audit_key=key.replace('visual_extract-','visual_audit-',1)+'-image-v2'
                if job.get('visual_detail_ids'):
                    detail_ids=set(job['visual_detail_ids'])&ids
                    audit_key+='-detail-v1'
                    payload['_image_resources']=image_resources(self.store,pages,source,detail_ids)
                    focused=[o for o in pages if o['id'] in detail_ids]
                    detail=self._call(job,audit_key+'-extraction','visual_extract',
                        dict(pages=image_view(focused),_image_resources=image_resources(self.store,focused,source,detail_ids),
                             instruction='Transcribe these original images using both the whole image and its labeled overlapping detail views. These are the same pixels, not additional content. Read actual visible text, never recall a familiar diagram from memory. Reconcile the overlaps so no label is duplicated or assigned to the wrong spatial group. Prior failed transcripts are deliberately not supplied as visual evidence.'),P.VisualExtraction)
                    detail=P.VisualExtraction.model_validate(detail).model_dump()
                    if len(detail['pages'])!=len(detail_ids) or {p['source_id'] for p in detail['pages']}!=detail_ids:raise ValueError('图片细节提取没有覆盖指定原件')
                    by_id={p['source_id']:p for p in detail['pages']}
                    job['visual_candidate']={'pages':[by_id.get(p['source_id'],p) for p in job['visual_candidate']['pages']]}
                result=self._call(job,audit_key,'visual_audit',payload|dict(extraction=job['visual_candidate']),P.VisualAuditV2)
                result=visual_decision(result)
                inaccessible=any('unsupported image' in d.lower() for p in result['pages'] for d in p['discrepancies'])
                if inaccessible and self.config.get('vision_capability_corrected'):
                    result=self._call(job,f'visual_audit_capability-{index}','visual_audit',
                        payload|dict(extraction=job['visual_candidate'],prior_transport_failure='The previous text-only reviewer could not inspect attached images. This call uses a separately configured verified image-capable channel. Inspect the actual originals independently.'),P.VisualAuditV2)
                    result=visual_decision(result)
                if len(result['pages'])!=len(ids) or {p['source_id'] for p in result['pages']}!=ids:
                    raise ValueError('独立视觉核对没有准确覆盖本批原始页面')
                defects={p['source_id'] for p in result['pages'] if p['status']!='verified' or p['discrepancies']}
                if defects:
                    focused=[p for p in pages if p['id'] in defects]
                    fixed=self._call(job,audit_key+'-repair','visual_extract',
                        dict(pages=image_view(focused),_image_resources=image_resources(self.store,focused,source,job.get('visual_detail_ids',())),
                             prior_extraction=[p for p in job['visual_candidate']['pages'] if p['source_id'] in defects],
                             audit_findings=[p for p in result['pages'] if p['source_id'] in defects],
                             instruction='Correct only the independently identified transcription or structural discrepancies against these actual original images. Keep every readable character and all unrelated content. Return complete transcripts for exactly these source IDs. Do not force resolution when genuinely unreadable.'),P.VisualExtraction)
                    fixed=P.VisualExtraction.model_validate(fixed).model_dump()
                    if len(fixed['pages'])!=len(defects) or {p['source_id'] for p in fixed['pages']}!=defects:raise ValueError('视觉核对修复没有覆盖指定原件')
                    by_id={p['source_id']:p for p in fixed['pages']}
                    repaired={'pages':[by_id.get(p['source_id'],p) for p in job['visual_candidate']['pages']]}
                    result=self._call(job,audit_key+'-recheck','visual_audit',payload|dict(extraction=repaired),P.VisualAuditV2)
                    result=visual_decision(result)
                    if len(result['pages'])!=len(ids) or {p['source_id'] for p in result['pages']}!=ids:raise ValueError('视觉再次核对没有覆盖原件')
                    job['visual_candidate']=repaired
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
                job.pop('visual_detail_ids',None)
                job['stage']='visual_extract' if job['visual_index']<len(visual) else 'inventory'
        elif stage=='inventory':
            groups=inventory_groups(source['objects'])
            if len(groups)>1 and sum(len(o.get('text','')) for o in source['objects'])>12000 and 'inventory' not in job['results']:
                index=job.get('inventory_group_index',0)
                if index<len(groups):
                    group={'objects':groups[index]};payload=base|dict(source={'objects':objects_view(groups[index])},
                        original_text_files=[],required_source_ids=[o['id'] for o in groups[index]],
                        source_group=index+1,source_group_count=len(groups),
                        instruction='Inventory every fact in these complete source objects. Adjacent groups are inventoried separately and the combined result is independently checked against the entire source. Do not infer omissions outside the supplied group. Keep quote to one short exact phrase; the application binds its full original source text automatically.')
                    key=f'inventory-group-{index}'
                    result=self._call(job,key,'fact_inventory',payload,P.FactInventory)
                    try:
                        result=P.FactInventory.model_validate(bind_source_quotes(result,group)).model_dump();validate_facts(result,group)
                    except ValueError as exc:
                        result=self._call(job,key+'-correction','inventory_repair',payload|dict(previous_response=result,validator_error=str(exc)),P.FactInventory)
                        result=P.FactInventory.model_validate(bind_source_quotes(result,group)).model_dump();validate_facts(result,group)
                    job.setdefault('inventory_groups',[]).append(result)
                    job.update(inventory_group_index=index+1,inventory_group_count=len(groups))
                    return 'queued'
                job['facts']=merge_inventories(job['inventory_groups']);validate_facts(job['facts'],source)
                job['stage']='inventory_audit';return 'queued'
            result=self._call(job,'inventory','fact_inventory',base,P.FactInventory)
            result=P.FactInventory.model_validate(bind_source_quotes(result,source)).model_dump()
            try:
                validate_facts(result,source)
            except ValueError as exc:
                result=self._call(job,'inventory_correction-1','inventory_repair',
                                  base|dict(previous_response=result,validator_error=str(exc)),P.FactInventory)
                result=P.FactInventory.model_validate(bind_source_quotes(result,source)).model_dump()
                validate_facts(result,source)
            job['facts']=result
            job['stage']='inventory_audit'
        elif stage=='inventory_audit':
            uncertainties=[dict(id=f'uncertainty-{n+1}',description=x) for n,x in enumerate(job['facts']['unresolved'])]
            audit_round=job.get('inventory_semantic_repairs',0)
            audit_key='inventory_audit' if not audit_round else f'inventory_audit-{audit_round}'
            review_suffix='-recheck-1' if job.get('inventory_review_recheck',{}).get('round')==audit_round else ''
            audit_key+=review_suffix
            if source.get('resolved_word_structure_gaps'):
                audit_key+='-word-structures-v1'
                review_suffix+='-word-structures-v1'
            result=self._call(job,audit_key,'inventory_audit',base|dict(facts=job['facts'],inventory_uncertainties=uncertainties),P.InventoryAudit)
            try:result=P.InventoryAudit.model_validate(result).model_dump()
            except ValueError as exc:
                result=self._call(job,audit_key+'-contract','inventory_audit',
                    base|dict(facts=job['facts'],inventory_uncertainties=uncertainties,
                              invalid_review=result,protocol_error=str(exc),
                              instruction='Return the complete independent review with only declared fields. Preserve every actual error and uncertainty against the source. Put evidence explanations in declared explanation fields, never add evidence_note. Do not modify the fact inventory. This is the sole schema correction for this review.'),P.InventoryAudit)
                result=P.InventoryAudit.model_validate(result).model_dump()
            expected_uncertainties={a['id'] for a in uncertainties}
            actual_uncertainties=[a['id'] for a in result['uncertainty_assessments']]
            if set(actual_uncertainties)!=expected_uncertainties or len(actual_uncertainties)!=len(expected_uncertainties):
                result=self._call(job,f'inventory_audit_protocol-{audit_round}-exact-ids'+review_suffix,'inventory_audit',
                    base|dict(facts=job['facts'],inventory_uncertainties=uncertainties,
                        invalid_review=result,expected_assessment_count=len(uncertainties),
                        protocol_error='Build a NEW uncertainty_assessments array from only inventory_uncertainties, exactly one item per supplied ID. Do NOT preserve the invalid review array or invent IDs for additional observations. Put any real newly found inventory defects in errors/missing_facts instead. An empty supplied list requires an empty assessment list. Independently recheck the actual source and facts, preserving real errors. Do not put passing observations into errors. Source raw markup and target fields are already separately retained; semantic meaning need not duplicate their bytes.'),P.InventoryAudit)
                result=P.InventoryAudit.model_validate(result).model_dump()
            result,bound_evidence=bind_uncertainty_evidence(result,source)
            if bound_evidence:job.setdefault('uncertainty_evidence_bindings',[]).extend(bound_evidence)
            confirmed_errors=list(result['errors'])
            confirmed_unresolved=list(result['unresolved'])
            claims=confirmed_errors+confirmed_unresolved
            if claims:
                decision_key=f'inventory_decision-{audit_round}'+('-all-claims' if confirmed_unresolved else '')
                # A corrected audit can contain different claims in the same
                # round. Never reuse an older verdict for a different question.
                if f'inventory_audit_protocol-{audit_round}-exact-ids' in job['results']:
                    decision_key+='-'+digest(claims)[:12]
                decision_key+=review_suffix
                decisions=self._call(job,decision_key,'inventory_decision',
                    base|dict(facts=job['facts'],uncertainty_assessments=result['uncertainty_assessments'],
                        claims=[dict(claim_index=n,claim=claim) for n,claim in enumerate(claims)]),P.InventoryDecisions)
                decisions=decision_evidence(decisions,source)
                ids=[d['claim_index'] for d in decisions]
                if len(ids)!=len(claims) or set(ids)!=set(range(len(claims))):
                    raise ValueError('审核争议未逐项得到明确裁决')
                sources={o['id']:o['text'] for o in source['objects']}
                if any(e['source_id'] not in sources or not source_quote_matches(e['quote'],sources[e['source_id']])
                       for d in decisions for e in d['evidence']):
                    raise ValueError('审核争议裁决缺少与原件匹配的证据')
                job.setdefault('inventory_decisions',{})[str(audit_round)]=decisions
                confirmed_errors=[claims[d['claim_index']] for d in decisions if d['verdict']!='not_error']
                confirmed_unresolved=[] # Every uncertainty above received an explicit independent decision.
            if (set(result['assessed_source_ids'])!={o['id'] for o in source['objects']}
                or set(result['assessed_fact_ids'])!={f['id'] for f in job['facts']['facts']}
                or confirmed_errors or confirmed_unresolved):
                if audit_round<job.get('inventory_semantic_limit',2):
                    repair_payload=base|dict(previous_response=job['facts'],independent_review=result|dict(errors=confirmed_errors,unresolved=confirmed_unresolved),
                                  validator_error='Resolve the cited inventory errors against the full source, preserving all information. Add actual missing facts. Do not invent facts to satisfy incorrect criticism. Canonical source quotes are substrings of source.objects[*].text, not HTML markup; objects retain raw markup and links separately.')
                    legacy_key=f'inventory_semantic_repair-{audit_round+1}'
                    if len(job['facts']['facts'])>80 and legacy_key not in job['results']:
                        patch=self._call(job,legacy_key+'-patch','inventory_patch',repair_payload,P.InventoryPatch)
                        try:fixed=patch_inventory(job['facts'],patch)
                        except ValueError as exc:
                            patch=self._call(job,legacy_key+'-patch-contract','inventory_patch',repair_payload|dict(
                                received_patch=patch,patch_contract_error=str(exc),
                                instruction='Correct only the patch contract against the ACTUAL previous_response fact IDs. Do not delete hypothetical or previously removed entries. Reread replacement/remove conflicts against source and choose exactly one supported action. unresolved contains ONLY real remaining uncertainties, not statements saying something is resolved or present. Keep every untouched fact and actual source condition.'),P.InventoryPatch)
                            fixed=patch_inventory(job['facts'],patch)
                    else:
                        fixed=self._call(job,legacy_key,'inventory_repair',repair_payload,P.FactInventory)
                    fixed=P.FactInventory.model_validate(bind_source_quotes(fixed,source)).model_dump()
                    validate_facts(fixed,source)
                    job['facts']=fixed
                    job['inventory_semantic_repairs']=audit_round+1
                    return 'queued'
                raise ValueError('独立清单审核发现错误或未知项，尚未进入写作')
            assessments=result['uncertainty_assessments']
            if len(assessments)!=len(uncertainties) or {a['id'] for a in assessments}!={a['id'] for a in uncertainties}:
                raise ValueError('清点疑点尚未逐项得到独立判断')
            # A reviewer may label an item "inventory_error" while also saying
            # its original wording must be preserved. Do not silently recast
            # that contradiction or discard the fact. Ask a separate role to
            # adjudicate only these disputed classifications against the source.
            disputed=[a for a in assessments if a['kind']!='original_ambiguity'
                      or a['treatment']!='preserve_without_invention']
            accepted_disputes=set()
            if disputed:
                dispute_claims=[dict(claim_index=n,claim='The inventory audit classifies '+a['id']+
                                     ' as '+a['kind']+' with treatment '+a['treatment']+'. '
                                     'Determine whether there is a real source extraction gap, missing evidence, or inventory error '
                                     'that blocks faithful rewriting within the supplied input scope. Do not reject a real gap. '
                                     'A reference to an external file does not itself require inventing or explaining its absent contents. Original uncertainty: '+
                                     next(u['description'] for u in uncertainties if u['id']==a['id'])+
                                     '. Audit explanation: '+a['explanation'])
                                for n,a in enumerate(disputed)]
                verdicts=self._call(job,f'inventory_uncertainty_decision-{audit_round}-'+digest(dispute_claims)[:12]+review_suffix,
                                    'inventory_decision',base|dict(facts=job['facts'],
                                    uncertainty_assessments=disputed,claims=dispute_claims),P.InventoryDecisions)
                verdicts=decision_evidence(verdicts,source)
                if len(verdicts)!=len(disputed) or {d['claim_index'] for d in verdicts}!=set(range(len(disputed))):
                    raise ValueError('清点疑点分类争议没有逐项裁决')
                job.setdefault('inventory_uncertainty_decisions',{})[str(audit_round)]=verdicts
                accepted_disputes={disputed[d['claim_index']]['id'] for d in verdicts
                                   if d['verdict']=='not_error'}
            source_text={o['id']:o['text'] for o in source['objects']}
            for a in assessments:
                if (a['id'] not in accepted_disputes and
                        (a['treatment']!='preserve_without_invention' or a['kind']!='original_ambiguity')):
                    raise ValueError('独立清单审核确认仍需补充证据或纠正清点')
                if any(e['source_id'] not in source_text or not source_quote_matches(e['quote'],source_text[e['source_id']]) for e in a['evidence']):
                    raise ValueError('清点疑点的裁决没有匹配原件证据')
            merged=merge_inventory_additions(job['facts'],result['missing_facts'],source)
            if source['unknown']:
                raise ValueError('原件仍有解析或视觉未知项，文本审核不能擅自清除')
            job['facts']=merged
            inv=copy.deepcopy(source)
            inv['obligations']=[dict(id=f['id'],object_id=f['source_id'],statement=f['meaning'],conditions=f['conditions'],
                quantities=f['quantities'],negations=f['negations'],status='reviewed') for f in job['facts']['facts']]
            inv['inventory_review']={'job':job['id'],'body':result}
            job['inventory']=freeze(inv)
            job['stage']='planner'
        elif stage=='planner':
            if job.get('transformation_mode')=='rewrite' and 'verified_terminology' not in job:
                from .terminology import verified_terms
                job['verified_terminology']=verified_terms(self.store,job['source'])
                self.store.put_job(job)
            length=sum(len(o.get('text','')) for o in job['inventory']['objects'])
            job['unit_limit']=unit_limit_for_inventory(job['inventory'],self.config.get('max_units',6),job.get('transformation_mode'))
            result=self._call(job,'planner','planner',dict(goal=job['goal'],inventory=self.teaching_inventory(job['inventory']),
                facts=job['facts'],max_units=job['unit_limit'],
                source_chars=length,working_length_target_chars=round(length*1.8)+400 if length>=500 else None,
                instruction='Use the fewest pedagogically coherent units that fully cover all facts. Each unit must fit one complete response. Do not omit small details to meet the limit.'),C.Plan)
            from pydantic import ValidationError as PydanticValidationError
            try:result=C.Plan.model_validate(result).model_dump()
            except PydanticValidationError as error:
                result=self._call(job,'planner-contract-shape-1','planner_repair',dict(
                    goal=job['goal'],inventory=self.teaching_inventory(job['inventory']),facts=job['facts'],
                    max_units=job['unit_limit'],invalid_plan=result,
                    contract_errors=error.errors(include_url=False),
                    instruction='Repair only the declared plan contract. Preserve the supplied unit order, titles, objectives, stages and every existing fact assignment. Add every missing required field with evidence from the supplied inventory and facts, remove undeclared fields, and return one complete Plan object. Do not omit or reassign existing facts merely to satisfy the schema.'),C.Plan)
                result=C.Plan.model_validate(result).model_dump()
            normalized,removed=unmark_nonmetadata_document_info(result,job['inventory'])
            normalized,marked=mark_known_document_metadata(normalized,job['inventory'])
            seen=set()
            invalid_dependencies=False
            for unit in normalized['units']:
                invalid_dependencies |= not set(unit['follows_units']) <= seen
                seen.add(unit['id'])
            if invalid_dependencies:
                from .pedagogy import apply_dependency_patch
                dependencies=self._call(job,'planner-contract-dependencies-1','planner',dict(
                    goal=job['goal'],plan=normalized,
                    instruction='This request repairs ONLY the dependency field contract of the saved plan. Return DependencyPatch, exactly one entry per existing unit. follows_units means prerequisite units ALREADY READ, never the unit that comes next. Respect the existing unit order. Use an empty array when no earlier unit is required. Explain an actual dependency in bridge_reason, do not invent one. Do not rewrite stages, facts, titles, or assignments. The complete plan will then receive an independent review.'),P.DependencyPatch)
                normalized=apply_dependency_patch(normalized,dependencies)
                job['planner_dependency_repair']='planner-contract-dependencies-1'
            coverage_repair=None
            try:
                job['plan']=self.validate_plan(job,normalized)
            except ValueError as error:
                if str(error)=='教学规划没有完整分配冻结义务，或引用了不存在的义务':
                    expected={o['id'] for o in job['inventory']['obligations']}
                    assigned={fid for unit in normalized['units'] for fid in unit['obligation_ids']}
                    if assigned-expected or not expected-assigned:raise
                    missing=expected-assigned
                    missing_facts=[f for f in job['facts']['facts'] if f['id'] in missing]
                    missing_source_ids={f['source_id'] for f in missing_facts}
                    patch=self._call(job,'planner-contract-coverage-patch-1','coverage_patch',dict(
                        goal=job['goal'],
                        existing_units=[{k:u[k] for k in ('id','title','objective','stages','reader_question','learning_result','bridge_reason')}
                                        for u in normalized['units']],
                        missing_facts=missing_facts,
                        missing_source_objects=[o for o in job['inventory']['objects'] if o['id'] in missing_source_ids],
                        instruction='Assign every supplied missing fact exactly once to an existing topical unit. The program will add source object IDs automatically and retain all prior assignments unchanged. Add only necessary stage text, using an exact existing after_stage or empty string to append. Return only the small CoveragePatch JSON; the program applies it to the saved full plan and validates all frozen facts.'),P.CoveragePatch)
                    complete=apply_coverage_patch(normalized,patch,missing,job['facts']['facts'])
                    job['plan']=self.validate_plan(job,complete)
                    coverage_repair={'missing_fact_ids':sorted(missing),'missing_source_ids':sorted(missing_source_ids),
                                     'patch_key':'planner-contract-coverage-patch-1'}
                else:
                    repaired=self._call(job,'planner-contract-semantic-1','planner_repair',dict(
                        goal=job['goal'],inventory=self.teaching_inventory(job['inventory']),facts=job['facts'],
                        max_units=job['unit_limit'],invalid_plan=normalized,semantic_error=str(error),
                        instruction='Repair only the stated semantic plan defect. Preserve unit identities, order, titles, fact assignments and source object assignments. Fill the reader question, expected learning result, dependency bridge or other named field with concrete material-supported wording. Do not add a lesson scenario, exercise or topic absent from the source. Return one complete Plan object.'),C.Plan)
                    job['plan']=self.validate_plan(job,C.Plan.model_validate(repaired).model_dump())
            if removed or marked or coverage_repair:
                job['planner_contract_repair']={'deterministically_unmarked_ids':removed,
                                                'deterministically_marked_metadata_ids':marked,
                                                'coverage_repair':coverage_repair}
            job['stage']='plan_review'
        elif stage=='plan_review':
            plan_round=job.get('plan_repairs',0)
            # A prior software version could repair a plan based on passing observations
            # misplaced in issues. Re-adjudicate that known result; preserve both versions.
            first_review=job['results'].get('plan_review-0',{})
            if (plan_round and first_review.get('issues') and first_review.get('status')!='needs_sources'
                    and not job.get('original_plan_adjudicated')):
                decision=job['results'].get('plan_decision-0')
                if decision is None:
                    decision,_=self._checked_plan_decisions(job,'original_plan_decision',base|dict(
                        plan=self.validated_initial_plan(job),
                        claims=[dict(claim_index=n,claim=x) for n,x in enumerate(first_review['issues'])]),
                        first_review['issues'],source)
                if self._decisions_pass(decision,first_review['issues'],source):
                    original=self.validated_initial_plan(job)
                    if len(original['units'])<=job.get('unit_limit',self.config.get('max_units',6)):
                        job['superseded_plan']=job['plan']
                        job['plan']=original
                        job['plan_review']=first_review
                        job['optional_source_limits']=first_review['optional_source_limits']
                        job.update(draft={'blocks':[]},unit_index=0,stage='writer',original_plan_adjudicated=True)
                        return 'queued'
                job['original_plan_adjudicated']=True
            result=self._call(job,f'plan_review-{plan_round}','plan_review',base|dict(plan=job['plan'],
                              facts=job['facts'],max_units=job.get('unit_limit',self.config.get('max_units',6))),P.PlanReview)
            result=P.PlanReview.model_validate(result).model_dump()
            job['plan_review']=result
            if result['status']=='needs_sources' or result['essential_missing_sources']:
                raise ValueError('教学需要的额外依据尚未取得：'+'；'.join(result['essential_missing_sources']))
            plan_errors=bool(result['issues'])
            repair_review=result
            if result['issues']:
                decision_payload=base|dict(plan=job['plan'],
                    claims=[dict(claim_index=n,claim=x) for n,x in enumerate(result['issues'])])
                decision,passed=self._checked_plan_decisions(job,f'plan_decision-{plan_round}',
                    decision_payload,result['issues'],source)
                plan_errors=not passed
                # Do not feed rejected allegations back to the repair role.
                # The original review and all independent verdicts stay saved.
                unresolved={d['claim_index'] for d in decision['decisions'] if d['verdict']!='not_error'}
                repair_review=result|{'issues':[issue for n,issue in enumerate(result['issues']) if n in unresolved]}
            if plan_review_needs_repair(result,plan_errors,len(job['plan']['units']),
                                        job.get('unit_limit',self.config.get('max_units',6))):
                plan_limit=max(1,min(6,int(self.config.get('max_plan_repairs',2))))
                if plan_round>=plan_limit:
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
                next_round=plan_round+1
                fixed=self._call(job,f'planner_repair-{next_round}','planner_repair',base|dict(inventory=job['inventory'],
                                 previous_plan=job['plan'],review=repair_review,max_units=job.get('unit_limit',self.config.get('max_units',6))),C.Plan)
                try:
                    job['plan']=self.validate_plan(job,fixed)
                except ValueError as exc:
                    fixed=self._call(job,f'planner_contract_repair-{next_round}','planner_repair',base|dict(
                        inventory=self.teaching_inventory(job['inventory']),previous_plan=job['plan'],
                        invalid_plan=fixed,validator_error=str(exc),review=repair_review,
                        instruction='Repair the returned plan contract without losing any original fact, metadata, or object. Moving document metadata to the end does NOT remove its required obligation/object assignment. Keep the actual teaching improvements. Return the complete corrected plan once.',
                        max_units=job.get('unit_limit',self.config.get('max_units',6))),C.Plan)
                    job['plan']=self.validate_plan(job,fixed)
                job['plan_repairs']=next_round
                return 'queued'
            job['optional_source_limits']=result['optional_source_limits']
            job['draft']={'blocks':[]}
            job['unit_index']=0
            job['stage']='writer'
        elif stage=='writer':
            if job['unit_index']==len(job['plan']['units']):
                return self._finish_draft(job)
            unit=job['plan']['units'][job['unit_index']]
            needed=set(unit['obligation_ids'])
            inv=copy.deepcopy(self.teaching_inventory(job['inventory']))
            inv['_verified_terminology']=job.get('verified_terminology',[])
            inv['obligations']=[o for o in inv['obligations'] if o['id'] in needed]
            wanted={o['object_id'] for o in inv['obligations']}|set(unit['object_ids'])
            inv['objects']=[o for o in inv['objects'] if o['id'] in wanted]
            recovered_fences=restore_frozen_markdown_fences(inv,job['inventory'],self.store)
            if recovered_fences:
                job.setdefault('source_fence_recoveries',[]).append({'unit_id':unit['id'],
                                                                     'source_ids':recovered_fences})
            writing_input={k:v for k,v in inv.items() if k!='obligations'}
            writing_input['obligation_fact_ids']=[o['id'] for o in inv['obligations']]
            unit_source_chars=sum(len(o.get('text','')) for o in inv['objects'])
            payload=dict(goal=job['goal'],inventory=writing_input,current_unit=unit,
                facts=[f for f in job['facts']['facts'] if f['id'] in needed],
                source_chars=unit_source_chars,
                working_length_target_chars=round(unit_source_chars*1.8)+400 if unit_source_chars>=500 else None,
                obligation_binding='Each listed obligation_fact_id is the SAME id in facts. Its original object_id is fact.source_id; statement is fact.meaning; conditions/quantities/negations are the fact fields. The complete pre-production ledger remains saved. Use these IDs in every block obligation_ids.',
                protected_objects=protected_objects(inv),prior_terminology=job.get('terminology',[]),
                optional_source_limits=job.get('optional_source_limits',[]))
            payload['previously_embedded_object_ids']=[sid for sid,literal in payload['protected_objects'].items()
                if any(sid in b.get('embedded_object_ids',[]) and sid in b['object_ids'] and literal in b['markdown'] for b in job['draft']['blocks'])]
            previous_units=[u['id'] for u in job['plan']['units'][:job['unit_index']]]
            immediately_previous=previous_units[-1:] if previous_units else []
            payload.update(full_teaching_route=job['plan'],
                           actual_previous_text=draft_text_view(job['draft'],immediately_previous)['blocks'],
                           actual_surrounding_text=(draft_text_view(job['regeneration']['original_draft'],[unit['id']])
                                                    if job.get('regeneration') else {}),
                           teaching_findings=job.get('regeneration',{}).get('findings',[]))
            if job.get('writing_contract_version',0)>=7 and job.get('transformation_mode')=='rewrite':
                term_key='terms-'+unit['id']
                if job.get('writer_use_primary') and term_key not in job['results']:term_key+='-primary'
                prepared=self._call(job,term_key,'term_preparation',
                    dict(source={'objects':inv['objects']},prior_terminology=job.get('terminology',[]),
                         verified_terminology=job.get('verified_terminology',[])),P.TermPreparation)
                prepared=P.TermPreparation.model_validate(prepared).model_dump()
                valid_ids={o['id'] for o in inv['objects']}
                if any(not set(t['source_ids'])<=valid_ids for t in prepared['terms']):
                    raise ValueError('术语准备引用不存在的原文对象')
                job.setdefault('prepared_terminology',{})[unit['id']]=prepared
                payload['prepared_terminology']=prepared
                from .terminology import retrieve_background
                if unit['id'] not in job.get('terminology_background',{}):
                    job.setdefault('terminology_background',{})[unit['id']]=retrieve_background(self.store,prepared,
                        verified=job.get('verified_terminology',[]))
                    self.store.put_job(job)
                payload['retrieved_background']=job['terminology_background'][unit['id']]
            generation=job.get('content_generation',0)
            key='writer-'+unit['id']+(f'-revision-{generation}' if generation else '')
            result=self._call(job,key,'writer',payload,P.FlatDraft)
            result=bind_evidence_layout(result,inv)
            from .writing import attach_original_pages,invalid_inline_references,apply_reference_patch
            bad_references=invalid_inline_references(result,inv)
            if bad_references:
                patch=self._call(job,key+'-references','reference_repair',dict(source={'objects':objects_view(inv['objects'])},
                    invalid_nodes=bad_references,actual_unit=result,facts=payload['facts']),P.ReferenceTextPatch)
                result=apply_reference_patch(result,patch,inv)
            result=attach_original_pages(result,inv,unit['id'],job['draft'])
            try:
                draft=compose(bundle,result,inv)
            except ValueError as exc:
                fixed=self._call(job,key+'-layout','layout_repair',
                    dict(received=result,validator_error=str(exc)),P.FlatDraft)
                fixed=bind_evidence_layout(fixed,inv)
                try:result=validate_layout_repair(result,fixed)
                except ValueError as contract_error:
                    fixed=self._call(job,key+'-layout-contract','layout_repair',dict(
                        received=result,invalid_layout_repair=fixed,validator_error=str(contract_error),
                        instruction='Return one complete FlatDraft using only the declared node types and fields. Repair only structure needed to make the original received draft renderable. Preserve every original text value, object identity, source binding and order; remove invented placeholder node types instead of replacing prose.'),P.FlatDraft)
                    fixed=bind_evidence_layout(fixed,inv)
                    result=validate_layout_repair(result,fixed)
                draft=compose(bundle,result,inv)
            issues=inspect_draft(inv,draft,{'units':[unit]},prior_draft=job['draft'])
            if any(i['code'] in {'unknown_obligation','unmapped','quote','object','omission'} for i in issues):
                from .writing import apply_binding_patch
                fixed=self._call(job,key+'-bindings-v2','binding_patch',
                    dict(received=result,rendered_draft=draft,inventory=inv,current_unit=unit,
                         facts=payload['facts'],validator_issues=issues),P.BindingPatch)
                result=apply_binding_patch(result,fixed,inv)
                draft=compose(bundle,result,inv)
                issues=inspect_draft(inv,draft,{'units':[unit]},prior_draft=job['draft'])
            completion_key=key+'-complete'
            if issues and {i['code'] for i in issues}<={'omission','protected_object'} and all(i['obligation_id'] for i in issues) and (
                    completion_key in job.get('unit_completion_attempts',[]) or job['repair_rounds']<self.repair_limit()):
                from .writing import append_unit_completion
                if completion_key not in job.setdefault('unit_completion_attempts',[]):
                    job['unit_completion_attempts'].append(completion_key)
                    job['repair_rounds']+=1
                    self.store.put_job(job)
                extra=self._call(job,completion_key,'writer',payload|dict(received=result,
                    completion_only=True,actual_current_text=draft,
                    remaining_fact_ids=list(dict.fromkeys(i['obligation_id'] for i in issues))),P.FlatDraft)
                result=append_unit_completion(result,bind_evidence_layout(extra,inv),unit['id'])
                try:
                    draft=compose(bundle,result,inv)
                except ValueError as exc:
                    fixed=self._call(job,completion_key+'-layout','layout_repair',
                        dict(received=result,validator_error=str(exc)),P.FlatDraft)
                    fixed=bind_evidence_layout(fixed,inv)
                    result=validate_layout_repair(result,fixed)
                    draft=compose(bundle,result,inv)
                issues=inspect_draft(inv,draft,{'units':[unit]},prior_draft=job['draft'])
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
                return self._finish_draft(job)
        elif stage=='teaching':
            job['plan']=self.validate_plan(job,job['plan'])
            generation=job.get('content_generation',0)
            saved_contract_key=f"teaching-contract-{generation}-{job['repair_rounds']}"
            evidence_suffix='-evidence-1' if job.get('teaching_evidence_retry') else ''
            if job.get('use_saved_teaching_contract') and saved_contract_key in job['results']:
                result=job['results'][saved_contract_key]
            else:
                result=self._call(job,f"teaching-{generation}-{job['repair_rounds']}{evidence_suffix}",'teaching_review',
                                 base|dict(plan=job['plan'],draft=job['draft']),TeachingReview)
            result=TeachingReview.model_validate(result).model_dump()
            contract_errors=teaching_review_contract(result,job['draft'],job['plan'],job['inventory'])
            if contract_errors:
                result=self._call(job,f"teaching-contract-{generation}-{job['repair_rounds']}{evidence_suffix}",'teaching_review',
                    base|dict(plan=job['plan'],draft=job['draft'],
                              review_contract_errors=contract_errors,
                              instruction='Re-read all actual blocks and return a complete independent review. Cover every adjacent NON-document_info block pair exactly once. Quotes must be exact contiguous substrings. Reconsider substantive judgments from the draft and source, not from an earlier review. Do not rewrite the draft.'),TeachingReview)
                result=TeachingReview.model_validate(result).model_dump()
                contract_errors=teaching_review_contract(result,job['draft'],job['plan'],job['inventory'])
                if contract_errors:
                    job['quality_issues']=['教学审查证据仍无法核对：'+e for e in contract_errors]
                    return 'needs_attention'
            job['teaching_review']=result
            job['teaching_draft_digest']=digest(canonical(job['draft']).encode())
            job['quality_issues']=teaching_issues(result,job['draft'],job['plan'],job['inventory'])
            if job['quality_issues']:
                if job['repair_rounds']>=self.repair_limit() or not result['findings']:
                    return 'needs_attention'
                selected=repair_units(result,job['plan'])
                job['repair_rounds']+=1
                job['regeneration']=dict(unit_ids=selected,original_plan=copy.deepcopy(job['plan']),
                    original_draft=copy.deepcopy(job['draft']),findings=result['findings'])
                job.setdefault('content_history',[]).append(self.store.blob(json.dumps(job['regeneration'],ensure_ascii=False).encode()))
                job['stage']='teaching_replan'
            else:
                style_current=(job.get('style') and job.get('scan') and
                               job.get('style_draft_digest')==digest(canonical(job['draft']).encode()))
                job['stage']='fidelity' if style_current else 'style'
        elif stage=='teaching_replan':
            regeneration=job['regeneration']
            repair_view=dict(regeneration,original_draft=draft_text_view(regeneration['original_draft']))
            result=self._call(job,f"teaching-replan-{job['repair_rounds']}",'teaching_replan',
                dict(goal=job['goal'],inventory=self.teaching_inventory(job['inventory']),**repair_view),C.Plan)
            normalized=self.validate_plan(job,result)
            job['plan']=validate_replan(normalized,regeneration['original_plan'],regeneration['unit_ids'],job['inventory'],job.get('transformation_mode'))
            job['stage']='teaching_replan_review'
        elif stage=='teaching_replan_repair':
            regeneration=job['regeneration']
            attempt=job['teaching_replan_attempts']
            prior_suffix='' if attempt==1 else f"-repair-{attempt-1}"
            prior_key=f"teaching-replan-review-{job['repair_rounds']}-route-v2{prior_suffix}"
            result=self._call(job,f"teaching-replan-fix-{job['repair_rounds']}-repair-{attempt}",'teaching_replan',
                dict(goal=job['goal'],inventory=self.teaching_inventory(job['inventory']),
                          original_text_files=base['original_text_files'],
                          source_contract=base['source_contract'],
                          selected_unit_ids=regeneration['unit_ids'],
                          actual_draft=draft_text_view(regeneration['original_draft']),
                          current_plan=job['plan'],independent_review=job['results'][prior_key],
                          instruction='Return one complete repaired Plan. Address each verified defect in the actual new stages and proof questions, not merely in an intention sentence. Keep every frozen fact and source-object assignment and all unselected units exactly unchanged. Do not write prose.'),C.Plan)
            normalized=self.validate_plan(job,result)
            job['plan']=validate_replan(normalized,regeneration['original_plan'],regeneration['unit_ids'],job['inventory'],job.get('transformation_mode'))
            job['stage']='teaching_replan_review'
        elif stage=='teaching_replan_review':
            repair_view={k:job['regeneration'][k] for k in ('unit_ids','original_plan','findings')}
            suffix=f"-repair-{job['teaching_replan_attempts']}" if job.get('teaching_replan_attempts') else ''
            result=self._call(job,f"teaching-replan-review-{job['repair_rounds']}-route-v2{suffix}",'plan_review',
                dict(goal=job['goal'],inventory=self.teaching_inventory(job['inventory']),
                     plan=job['plan'],repair_context=repair_view),P.PlanReview)
            result=P.PlanReview.model_validate(result).model_dump()
            plan_errors=bool(result['issues'])
            if result['issues']:
                decision_key=f"teaching-replan-decision-{job['repair_rounds']}{suffix}"
                decision_payload=dict(source=self.teaching_inventory(job['inventory']),plan=job['plan'],
                    claims=[dict(claim_index=n,claim=x) for n,x in enumerate(result['issues'])])
                decision,passed=self._checked_plan_decisions(job,decision_key,decision_payload,
                    result['issues'],source)
                plan_errors=not passed
                unresolved=[d['claim_index'] for d in decision['decisions'] if d['verdict']=='unknown']
                if unresolved and not any(d['verdict']=='confirmed_error' for d in decision['decisions']):
                    claims=[result['issues'][n] for n in unresolved]
                    original_decision,passed=self._checked_plan_decisions(job,f"teaching-replan-original-decision-{job['repair_rounds']}{suffix}",
                        dict(source=self.teaching_inventory(job['inventory']),
                            original_text_files=base['original_text_files'],plan=job['plan'],
                            prior_decisions=decision,
                            instruction='The prior independent decision could not verify exact characters. Read original_text_files as the untouched source. Decide each numbered claim against those exact characters and the plan, citing source object IDs. Do not infer a defect from an old draft.',
                            claims=[dict(claim_index=n,claim=x) for n,x in enumerate(claims)]),
                        claims,source)
                    plan_errors=not passed
            if (result['status']=='needs_sources' or result['essential_missing_sources'] or
                    plan_review_needs_repair(result,plan_errors,len(job['plan']['units']),
                                             job.get('unit_limit',self.config.get('max_units',6)))):
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
            suffix='-reassessment-'+str(len(job['fidelity_reassessments'])) if job.get('fidelity_reassessments') else ''
            if job.get('fidelity_review_version',1)>=2:
                from .fidelity_parts import (partitions,FidelitySchema,decode as decode_review,merge,
                                              invalid_assignments,merge_reassessment)
                parts=[]
                fidelity_limit=max(10,min(45,int(job.get('fidelity_partition_limit',45))))
                groups=partitions(source,job['facts'],job['draft'],fidelity_limit)
                fidelity_tag='' if fidelity_limit==45 else f'-small{fidelity_limit}'
                for number,assigned in enumerate(groups):
                    scoped_facts=job['facts']|{'facts':[f for f in job['facts']['facts'] if f['id'] in assigned['fact_ids']]}
                    payload=base|dict(facts=scoped_facts,draft=draft_text_view(job['draft']),assigned=assigned,
                        review_encoding='indexed_fidelity_v2',verified_terminology=job.get('verified_terminology',[]),
                        retrieved_background=job.get('terminology_background',{}),
                        instruction='Read the complete original, complete candidate and full skill. The fact ledger here contains exactly the assigned fact rows; disjoint requests assess every other ledger row. Source coverage checks compare the original directly with the complete candidate, independently of this fact subset. Return judgments only for the exact assigned source, fact and block keys. Each fact explicitly identifies its original source_id. Select the actual candidate block_id containing that fact; use an empty string for lost facts. The program resolves original and output quotations from these exact immutable objects, so do not retype or invent quotation fields. For reverse checks select actual source IDs supporting this block. IDs establish location only, never semantic correctness. Keep every real change, loss, invented claim or unknown and give actionable localized findings. Required term definitions and Chinese rewriting are authorized; their mere existence is not a genre violation. Judge their factual content and distinguish verified explanatory background from source policy, without inventing source restrictions.')
                    key=f'fidelity-{index}{suffix}{fidelity_tag}-indexed-{number+1}'
                    response=self._call(job,key,'fidelity',payload,FidelitySchema(assigned,source,job['draft']))
                    try:parsed=decode_review(response,assigned,source,job['facts'],job['draft'])
                    except Exception as error:
                        from jsonschema import ValidationError
                        if not isinstance(error,ValidationError):raise
                        retained,missing=invalid_assignments(response,assigned,source,job['draft'])
                        missing_facts=job['facts']|{'facts':[f for f in job['facts']['facts'] if f['id'] in missing['fact_ids']]}
                        supplement=self._call(job,key+'-contract','fidelity',base|dict(
                            facts=missing_facts,draft=draft_text_view(job['draft']),assigned=missing,
                            review_encoding='indexed_fidelity_v2_contract_repair',
                            verified_terminology=job.get('verified_terminology',[]),
                            retrieved_background=job.get('terminology_background',{}),
                            invalid_rows={group:{identity:response.get(group,{}).get(identity) for identity in identities
                                if identity in response.get(group,{})}
                                for group,identities in [('source_checks',missing['source_ids']),
                                    ('fact_checks',missing['fact_ids']),('reverse_checks',missing['block_ids'])]},
                            instruction='Reassess only these malformed keyed judgments against the complete original and complete candidate. Return every supplied assigned key exactly once under the declared schema. Resolve contradictions from evidence; do not repeat valid earlier rows, replace the article, or omit real findings.'),
                            FidelitySchema(missing,source,job['draft']))
                        try:parsed=merge_reassessment(retained,supplement,assigned,source,job['facts'],job['draft'])
                        except ValidationError:
                            contract_retained,contract_missing=invalid_assignments(
                                supplement,missing,source,job['draft'])
                            # A model that has twice lost keyed identities often
                            # echoes the complete bounded partition. Ask for that
                            # shape explicitly on the final contract pass so a
                            # complete saved response can be validated and reused
                            # without another request after a software fix.
                            final=self._call(job,key+'-contract-missing','fidelity',base|dict(
                                facts=scoped_facts,draft=draft_text_view(job['draft']),assigned=assigned,
                                review_encoding='indexed_fidelity_v2_contract_full_partition',
                                verified_terminology=job.get('verified_terminology',[]),
                                retrieved_background=job.get('terminology_background',{}),
                                earlier_valid_rows=contract_retained,
                                still_malformed_assignments=contract_missing,
                                instruction='Return one complete fresh assessment of the original bounded assigned partition. Every supplied source, fact and block key must occur exactly once, including empty keyed groups. Recheck the actual source and candidate. Preserve real failures and unknowns. Do not add commentary fields, replace the article, or omit an assigned key.'),
                                FidelitySchema(assigned,source,job['draft']))
                            parsed=decode_review(final,assigned,source,job['facts'],job['draft'])
                    parts.append(parsed)
                result=merge(parts)
            else:
                result=self._call(job,f'fidelity-{index}{suffix}','fidelity',base|dict(facts=job['facts'],draft=draft_text_view(job['draft'])),P.FidelityReview)
            job['fidelity']=P.FidelityReview.model_validate(result).model_dump()
            job['fidelity_draft_digest']=digest(canonical(job['draft']).encode())
            if not job.get('style') or not job.get('scan'):
                # Resume older checkpoints through the full writing review as well.
                job['stage']='style'
            else:
                issues=fidelity_issues(job['fidelity'],job['facts'],job['draft'],source)
                issues+=style_issues(job['style'],rule_catalog(bundle['instructions']),job['draft'],job['scan'])
                job['quality_issues']=issues
                if issues and job['repair_rounds']>=self.repair_limit():
                    return 'needs_attention'
                if issues and self._regenerate_protected_structure(job,source):
                    return 'queued'
                job['stage']='repair' if issues else 'publish'
        elif stage=='style':
            from .review_context import execution_evidence,protected_context
            from .writing import tighten_list_spacing,trim_block_edges
            # Recover an already submitted style assignment against its exact
            # saved draft before applying any new deterministic normalization.
            if not job.get('pending'):
                before_spacing=digest(canonical(job['draft']).encode())
                tightened,changed_blocks=tighten_list_spacing(job['draft'])
                tightened,edge_blocks=trim_block_edges(tightened)
                changed_blocks=list(dict.fromkeys(changed_blocks+edge_blocks))
                if changed_blocks:
                    job['draft']=tightened
                    job.setdefault('deterministic_format_commits',[]).append({
                        'kind':'block_and_list_spacing','before':before_spacing,
                        'after':digest(canonical(tightened).encode()),'blocks':changed_blocks})
                    for stale in ('style','scan','fidelity','style_draft_digest','fidelity_draft_digest'):
                        job.pop(stale,None)
            report=scan(bundle,job['draft'],self.store.root/'production'/job['id']/'checks')
            report['execution_evidence']=execution_evidence(
                job,bundle,self.store.root/'production'/job['id'],self.repair_limit())
            catalog=rule_catalog(bundle['instructions'])
            payload=style_review_payload(job,source,catalog,report)
            root_key=f"style-{job['repair_rounds']}"
            if not job.get('pending') and not any(k.startswith(root_key) for k in job['results']):
                # Keep every dispatched partition stable; bound only a new round.
                job['style_partition_limit']=min(40,int(job.get('style_partition_limit',40)))
            from .style_parts import partitions,validate_part,merge,IndexedReviewSchema,decode_indexed,missing_assignments,merge_indexed
            divided=root_key not in job['results'] and (job.get('style_parts_enabled') or
                not job.get('pending') and sum(len(payload[k]) for k in ('rule_catalog','mechanical_findings','mechanical_candidates'))>40)
            if divided:
                job['style_parts_enabled']=True
                groups=partitions(payload,max(1,min(80,int(job.get('style_partition_limit',40)))));completed=[]
            else:groups=[payload];completed=[]
            for number,part in enumerate(groups):
                key=root_key+f'-part-{number+1}' if divided else root_key
                job.update(style_part_index=number,style_part_count=len(groups))
                if divided:
                    indexed_key=key+'-indexed-v2'
                    legacy_key=key+'-indexed-v1'
                    if legacy_key in job['results'] or str(job.get('pending','')).startswith(legacy_key):
                        # A previously dispatched request keeps its original schema.
                        indexed_key=legacy_key
                        part={k:v for k,v in part.items() if k!='evidence_catalog'}
                    # Reuse a complete earlier assignment. A malformed old array
                    # cannot become a pass; the new schema fixes identities before
                    # dispatch while retaining every original response and charge.
                    previous=job['results'].get(key+'-contract',job['results'].get(key))
                    if previous is not None and indexed_key not in job['results'] and not job.get('pending'):
                        try:
                            completed.append(validate_part(previous,part));continue
                        except ValueError:pass
                    old_pending={key,key+'__fallback',key+'-contract',key+'-contract__fallback'}
                    if job.get('pending') in old_pending:
                        # Recover the exact already submitted old call first.
                        contract='-contract' in job['pending']
                        result=self._call(job,key+'-contract' if contract else key,
                            'style_contract_repair' if contract else 'style',part,P.StyleReview)
                        parsed=validate_part(result,part)
                    else:
                        indexed_payload=part|{'review_encoding':'indexed_review_v2' if 'evidence_catalog' in part else 'indexed_review_v1',
                            'response_instruction':'Return the exact indexed schema: findings plus rules_by_id and/or checks_by_id ONLY when present in the schema. Each required object key is an assigned identity and must occur exactly once. Do not return assessments or mechanical_assessments arrays. Read the full skill and actual candidate; keep failures and unknowns with exact evidence. Omitted schema fields mean this group has no assignments of that type, not that their rules are waived.'}
                        result=self._indexed_style_result(job,indexed_key,indexed_payload)
                        from jsonschema import ValidationError
                        try:parsed=decode_indexed(result,part)
                        except ValidationError as validation_error:
                            retained=result
                            try:missing=missing_assignments(result,indexed_payload)
                            except ValueError as assignment_error:
                                from .style_parts import invalid_assignments
                                try:retained,missing=invalid_assignments(result,indexed_payload)
                                except ValueError:
                                    # A malformed finding is outside the keyed
                                    # verdict rows. Repair this one bounded
                                    # partition, never the article or other
                                    # completed partitions.
                                    repaired=self._repair_indexed_style_part(
                                        job,indexed_key,indexed_payload,result,validation_error)
                                    parsed=decode_indexed(repaired,part)
                                    completed.append(parsed);continue
                            missing['response_instruction']+=' This is the sole supplement for missing identities. Assess only these remaining supplied items; earlier returned judgments are retained unchanged by the program. Do not repeat them or rewrite the article.'
                            supplement=self._indexed_style_result(job,indexed_key+'-missing',missing)
                            try:parsed=merge_indexed(retained,supplement,part)
                            except ValidationError as merged_error:
                                repaired=self._repair_indexed_style_part(
                                    job,indexed_key,indexed_payload,
                                    {'original':retained,'supplement':supplement},merged_error)
                                parsed=decode_indexed(repaired,part)
                    completed.append(parsed);continue
                result=self._call(job,key,'style',part,P.StyleReview)
                try:parsed=validate_part(result,part) if divided else P.StyleReview.model_validate(result).model_dump()
                except ValueError as exc:
                    result=self._call(job,key+'-contract','style_contract_repair',
                        part|dict(received_review=result,contract_error=str(exc),
                            instruction='Correct this review protocol once, retaining all actual defects and exact evidence. Return exactly the assigned rule and mechanical IDs. Do not rewrite prose or turn failures into passes.'),P.StyleReview)
                    parsed=validate_part(result,part) if divided else P.StyleReview.model_validate(result).model_dump()
                completed.append(parsed)
            job['style']=merge(completed)
            job['scan']=report
            job['style_draft_digest']=digest(canonical(job['draft']).encode())
            issues=style_issues(job['style'],catalog,job['draft'],report)
            if job.get('fidelity'):
                issues+=fidelity_issues(job['fidelity'],job['facts'],job['draft'],source)
            issues += [x['message'] for x in inspect_draft(job['inventory'],job['draft'],job['plan'],
                                                              require_heading_structure=job.get('teaching_version',0)>=2)]
            job['quality_issues']=issues
            if issues:
                if job.get('joint_review_before_repair') and job.get('fidelity_draft_digest')!=digest(canonical(job['draft']).encode()):
                    # Gather independent content feedback on this same draft
                    # before spending either of its bounded repair transactions.
                    job['stage']='fidelity'
                    return 'queued'
                if job['repair_rounds']>=self.repair_limit():
                    return 'needs_attention'
                if self._regenerate_protected_structure(job,source):
                    return 'queued'
                job['stage']='repair'
            else:
                teaching_needed=(job.get('review_order')=='style_first' and job.get('teaching_version',0)>=2 and
                    (not job.get('teaching_review') or job.get('teaching_draft_digest')!=digest(canonical(job['draft']).encode())))
                job['stage']='teaching' if teaching_needed else ('publish' if job.get('fidelity') else 'fidelity')
        elif stage=='repair':
            index=job.get('active_repair_round')
            if index is None and self._regenerate_protected_structure(job,source):
                return 'queued'
            if index is None:
                if job['repair_rounds']>=self.repair_limit():
                    job.setdefault('quality_issues',[]).append(f'已用完 {self.repair_limit()} 轮局部修复，保留当前正文及未解决问题，未发送额外请求')
                    return 'needs_attention'
                index=job['repair_rounds']+1
                # Persist the transaction identity before dispatch. Validation
                # failure or worker recovery must replay this same paid result.
                job.update(active_repair_round=index,repair_rounds=index)
                self.store.put_job(job)
            if index not in set(range(1,self.repair_limit()+1)) or index!=job['repair_rounds']:
                raise ValueError('局部修复轮次记录不一致，未发送请求')
            findings=job['style']['findings']+job.get('fidelity',{}).get('findings',[])
            allowed={f['block_id'] for f in findings if f['block_id']}
            if not allowed:
                raise ValueError('审核存在缺口，但没有足以支持精确修复的定位；未擅自重写')
            received_proposal=False
            try:
                before_digest=digest(canonical(job['draft']).encode())
                if job.get('writing_contract_version',0)>=7:
                    from .review_context import editable_lines,line_proposal,protected_context
                    lines=editable_lines(job['draft'],source,allowed)
                    if not lines:raise Conflict('没有可安全修改的已定位正文行')
                    result=self._call(job,f'line_repair-{index}','line_repair',dict(draft=draft_text_view(job['draft']),
                        source={'objects':objects_view(source['objects'])},
                        findings=findings,document_digest=before_digest,editable_lines=lines,
                        protected_originals=protected_context(source,job['draft']),
                        repair_round=index,repair_limit=self.repair_limit(),
                        prior_rejection=job.get('repair_rejection'),
                        prepared_terminology=job.get('prepared_terminology',{}),
                        verified_terminology=job.get('verified_terminology',[]),
                        retrieved_background=job.get('terminology_background',{})),P.LineRepair)
                    received_proposal=True
                    result=line_proposal(P.LineRepair.model_validate(result).model_dump(),lines)
                else:
                    result=self._call(job,f'local_repair-{index}','local_repair',dict(draft=job['draft'],source=source,
                        findings=findings,document_digest=before_digest),P.LocalRepair)
                    received_proposal=True
                job['draft']=repair(bundle,job['draft'],result,allowed,self.store.root/'production'/job['id']/f'repair-{index}')
            except (ValueError,Conflict) as error:
                if not received_proposal:raise
                job['repair_rejection']=str(error)
                if index<self.repair_limit():
                    job.pop('active_repair_round',None)
                    job['stage']='repair'
                    return 'queued'
                job.setdefault('quality_issues',[]).append('最后一轮局部补丁未能安全提交，正文保持原样：'+str(error))
                return 'needs_attention'
            job.setdefault('repair_commits',[]).append(dict(round=index,before=before_digest,
                after=digest(canonical(job['draft']).encode()),blocks=sorted({e['block_id'] for e in result['edits']}),
                edits=len(result['edits']),committer='full-skill-exact-local-transaction'))
            job.pop('active_repair_round',None)
            job.pop('fidelity',None)
            job.pop('style',None)
            job.pop('scan',None)
            job['stage']='teaching' if job.get('teaching_version',0)>=2 and job.get('review_order')!='style_first' else 'style'
        elif stage=='publish':
            current=digest(canonical(job['draft']).encode())
            if job.get('teaching_version',0)>=2:
                if job.get('teaching_draft_digest')!=current or not job.get('teaching_review'):
                    job['quality_issues']=['当前正文尚未通过同版本的整篇教学审查']
                    return 'needs_attention'
                if teaching_review_contract(job['teaching_review'],job['draft'],job['plan'],job['inventory']) or teaching_issues(job['teaching_review'],job['draft'],job['plan'],job['inventory']):
                    job['quality_issues']=['当前正文仍有教学缺口']
                    return 'needs_attention'
            if (not job.get('style') or not job.get('fidelity') or not job.get('scan')
                or job.get('style_draft_digest')!=current or job.get('fidelity_draft_digest')!=current):
                job['quality_issues']=['当前正文尚无相同版本的完整写作与独立原文核对']
                return 'needs_attention'
            issues=style_issues(job['style'],rule_catalog(bundle['instructions']),job['draft'],job['scan'])
            issues+=fidelity_issues(job['fidelity'],job['facts'],job['draft'],source)
            issues += [x['message'] for x in inspect_draft(job['inventory'],job['draft'],job['plan'],
                                                              require_heading_structure=job.get('teaching_version',0)>=2)]
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
        if any(e['source_id'] not in sources or not source_quote_matches(e['quote'],sources[e['source_id']])
               for d in decisions for e in d['evidence']):
            raise ValueError('审核争议裁决没有匹配的原文证据')
        return all(d['verdict']=='not_error' for d in decisions)

    def _checked_plan_decisions(self,job,key,payload,claims,source):
        """Adjudicate every claim and repair only rows with an invalid contract.

        The inventory itself has an opaque UUID.  It is not a source object and
        therefore must never be accepted as evidence.  Some models copied that
        surrounding UUID even after a generic contract retry.  Expose the exact
        allow-list, keep every valid row, and resend only missing, duplicated, or
        invalid-evidence claim indexes.
        """
        allowed={o['id'] for o in source['objects']}
        decision=self._plan_decisions(job,key,payload)
        last_error=None
        for attempt in range(3):
            try:
                return decision,self._decisions_pass(decision,claims,source)
            except ValueError as error:
                last_error=error
                rows=decision.get('decisions',[])
                counts={n:sum(d.get('claim_index')==n for d in rows) for n in range(len(claims))}
                invalid={n for n,count in counts.items() if count!=1}
                invalid.update(d.get('claim_index') for d in rows
                    if d.get('claim_index') not in counts or
                    any(e.get('source_id') not in allowed for e in d.get('evidence',[])))
                invalid_evidence=any(e.get('source_id') not in allowed
                    for d in rows for e in d.get('evidence',[]))
                invalid={n for n in invalid if isinstance(n,int) and 0<=n<len(claims)}
                if not invalid:
                    invalid=set(range(len(claims)))
                retained=[d for d in rows if d.get('claim_index') not in invalid]
                if attempt>=(2 if invalid_evidence else 1):
                    break
                repair_key=key+'-contract'+('' if attempt==0 else f'-{attempt+1}')
                repair_payload=payload|dict(
                    source={'objects':objects_view(source['objects'])},
                    allowed_source_ids=sorted(allowed),
                    claims=[dict(claim_index=n,claim=claims[n]) for n in sorted(invalid)],
                    valid_decisions=retained,
                    invalid_decision=decision,
                    invalid_decisions=[d for d in rows if d.get('claim_index') in invalid],
                    contract_error=str(error),
                    instruction='Repair only the supplied invalid claim indexes. Return exactly one decision for each supplied claim_index. Evidence source_id must be copied exactly from allowed_source_ids; the inventory UUID is not evidence. Preserve each substantive verdict when valid evidence supports it. Do not force a passing verdict.')
                repaired=self._plan_decisions(job,repair_key,repair_payload)
                decision={'decisions':sorted(retained+repaired['decisions'],key=lambda d:d['claim_index'])}
                job['results'][repair_key+'-merged']=decision
                self.store.put_job(job)
        raise last_error

    def _plan_decisions(self,job,key,payload):
        """Split large dispute sets while preserving every original claim index."""
        payload=copy.deepcopy(payload)
        supplied=payload.get('source',{})
        if supplied.get('objects'):
            payload['source']={'objects':objects_view(supplied['objects'])}
            payload['allowed_source_ids']=[o['id'] for o in payload['source']['objects']]
        claims=payload['claims']
        if len(claims)<=4:
            return P.InventoryDecisions.model_validate(
                self._call(job,key,'plan_decision',payload,P.InventoryDecisions)).model_dump()
        merged=[]
        for start in range(0,len(claims),4):
            part=claims[start:start+4]
            result=P.InventoryDecisions.model_validate(self._call(job,f'{key}-part-{start//4+1}',
                'plan_decision',payload|{'claims':part},P.InventoryDecisions)).model_dump()
            merged.extend(result['decisions'])
        result=P.InventoryDecisions.model_validate({'decisions':merged}).model_dump()
        job['results'][key]=result
        self.store.put_job(job)
        return result

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
            elif self.config.get('job_timeout',0)>0 and time.time()-job.get('started',time.time())>self.config['job_timeout']:
                raise Conflict('本篇已达到处理时间上限，已保存全部完成结果')
            else:
                status=self.step(job)
            publish=None
            if status in {'completed','needs_attention','ready_for_review'}:
                receipt=dict(job=job['id'],status=status,canonical_digest=digest(canonical(job['draft']).encode()),
                    skill_digest=job['writing_skill']['instruction_digest'],issues=job.get('quality_issues',[]),
                    automatic=True,manual_edits=0,revision=job['base_revision']+1,
                    teaching_version=job.get('teaching_version',0))
                if job.get('pipeline') in {'active_composition_v1','active_composition_v2'}:
                    receipt.update(pipeline=job['pipeline'], delivery_checks=job.get('delivery_checks'),
                                   semantic_status=(job.get('delivery_checks') or {}).get(
                                       'semantic_status','not_independently_reviewed'),
                                   delivery_state=job.get('delivery_state','draft'))
                publish=dict(inventory=job['inventory'],plan=job['plan'],draft=job['draft'],production=receipt,
                             review=None,accepted_revision=None,repair_rounds=job['repair_rounds'])
                if job.get('pipeline') == 'active_composition_v2' and status == 'ready_for_review':
                    publish['delivery_state']='ready_for_review'
                    publish['independent_review']=job.get('independent_review')
            self.queue.finish(job,self.owner,status,publish)
        except Exception as exc:
            detail=str(exc).strip()
            cancelled=self.queue.cancelled(job['id'],self.owner)
            if retryable_v2_gateway_timeout(job,exc) and not cancelled:
                self.queue.finish(job,self.owner,'queued')
            elif job.get('pipeline')=='active_composition_v2' and not cancelled:
                try:
                    inventory,plan,draft=recoverable_delivery(job)
                    job.setdefault('internal_failures',[]).append(dict(stage=job.get('stage'),
                        type=type(exc).__name__,detail=(detail[:500] if detail else type(exc).__name__),
                        recovered_at=time.time()))
                    job.update(inventory=inventory,plan=plan,draft=draft,error=None,error_type=None,
                        quality_issues=[],delivery_state='fallback_ready')
                    receipt=dict(job=job['id'],status='ready_for_review',
                        canonical_digest=digest(canonical(draft).encode()),
                        skill_digest=job['writing_skill']['instruction_digest'],issues=[],automatic=True,
                        manual_edits=0,revision=job['base_revision']+1,
                        teaching_version=job.get('teaching_version',0),pipeline=job['pipeline'],
                        delivery_checks=dict(structural_status='source_preserving_fallback',
                            semantic_status='not_independently_reviewed',publication_status='ready_for_review'),
                        semantic_status='not_independently_reviewed',delivery_state='fallback_ready')
                    publish=dict(inventory=inventory,plan=plan,draft=draft,production=receipt,
                        review=None,accepted_revision=None,repair_rounds=job.get('repair_rounds',0),
                        delivery_state='fallback_ready',independent_review=None)
                    self.queue.finish(job,self.owner,'ready_for_review',publish)
                except Exception as recovery_error:
                    job['error']=((detail or type(exc).__name__)+'；自动恢复失败：'+str(recovery_error))[:500]
                    job['error_type']=type(recovery_error).__name__
                    self.queue.finish(job,self.owner,'failed')
            else:
                job['error']=(detail[:500] if detail else type(exc).__name__)
                job['error_type']=type(exc).__name__
                self.queue.finish(job,self.owner,'cancelled' if cancelled else 'failed')
        finally:
            stop.set()
            thread.join(timeout=1)
            from .trials import outcome
            outcome(self.store,self.config,job,job['status'])
        last_call=(job.get('calls') or [{}])[-1]
        if job.get('pipeline') in {'active_composition_v1','active_composition_v2'}:
            return True
        continuation_role=last_call.get('role')
        if (job['status']=='needs_attention' and job.get('stage')=='teaching_replan_review'
                and job.get('quality_issues')):
            try:
                followup=self.queue.repair_teaching_replan(job['id'],self.config)
                self.store.event(job['project'],'teaching_replan_repair_queued',{
                    'job':job['id'],'attempt':followup.get('teaching_replan_attempts'),
                    'issues':len(followup.get('teaching_replan_repair_issues',[]))})
            except Conflict as exc:
                self.store.event(job['project'],'teaching_replan_repair_unavailable',{
                    'job':job['id'],'reason':str(exc)[:300]})
        if (job['status']=='failed' and job.get('quality_fallback_of')
                and last_call.get('status')=='rejected' and not last_call.get('dispatch_started')):
            # A local browser-input guard has consumed neither model work nor
            # money. Route this exact saved step to the configured long-input
            # provider now, rather than requiring a person to resume every
            # later repair round.
            try:
                followup=self.queue.retry_validation(job['id'],self.config)
                self.store.event(job['project'],'local_preflight_continuation_queued',{
                    'job':job['id'],'role':continuation_role,'stage':followup.get('stage')})
            except (Conflict,ValueError) as exc:
                self.store.event(job['project'],'local_preflight_continuation_unavailable',{
                    'job':job['id'],'role':continuation_role,'reason':str(exc)[:300]})
        writer_continuation=(job['status']=='uncertain' and job.get('stage')=='writer'
            and continuation_role in {'writer','term_preparation'}
            and self.config.get('resume_failed_chat_writer_with_primary'))
        review_stages={'style':'style','style_contract_repair':'style','fidelity':'fidelity',
            'line_repair':'repair','teaching_review':'teaching'}
        review_continuation=(job['status']=='uncertain'
            and review_stages.get(continuation_role)==job.get('stage')
            and self.config.get('resume_failed_chat_review_with_primary'))
        already_checked=continuation_role is not None and any(row.get('original_call')==last_call.get('id')
            for row in job.get(continuation_role+'_reassessments',[]))
        if ((writer_continuation or review_continuation) and job.get('quality_fallback_of') and not already_checked):
            import httpx
            try:
                self.queue.recheck_failed_fidelity(job['id'],self.config,role=continuation_role)
                self.store.event(job['project'],'primary_continuation_queued',
                    {'job':job['id'],'role':continuation_role,'saved_units':job.get('unit_index',0)})
            except (Conflict,ValueError,httpx.HTTPError) as exc:
                self.store.event(job['project'],'primary_continuation_unavailable',
                    {'job':job['id'],'role':continuation_role,'reason':str(exc)[:300]})
        if (job['status']=='uncertain' and self.config.get('resume_unqueryable_subscription_once')):
            try:
                result=self.queue.continue_unqueryable_subscription(job['id'],self.config)
                self.store.event(job['project'],'subscription_continuation_queued',
                    {'job':job['id'],'role':continuation_role,'logical_step':result['logical_step']})
            except (Conflict,ValueError) as exc:
                self.store.event(job['project'],'subscription_continuation_unavailable',
                    {'job':job['id'],'role':continuation_role,'reason':str(exc)[:300]})
        if (job['status']=='uncertain' and self.config.get('resume_unqueryable_router_once')):
            try:
                result=self.queue.continue_unqueryable_router(job['id'],self.config)
                self.store.event(job['project'],'router_continuation_queued',
                    {'job':job['id'],'role':continuation_role,'logical_step':result['logical_step']})
            except (Conflict,ValueError) as exc:
                self.store.event(job['project'],'router_continuation_unavailable',
                    {'job':job['id'],'role':continuation_role,'reason':str(exc)[:300]})
        if (job['status']=='needs_attention' and job.get('stage') in {'style','fidelity'}
                and not job.get('quality_fallback_of') and self.config.get('quality_fallback_providers')):
            # One distinct generation after a fully returned failed quality
            # review; never replay an unknown call or reset old repair records.
            try:
                bundle=load_bundle(job['writing_skill']['root'],job['writing_skill']['package_digest'])
                followup=self.queue.rewrite_existing(job['project'],bundle,quality_parent=job['id'])
                self.store.event(job['project'],'quality_fallback_queued',{'previous_job':job['id'],'job':followup['id']})
            except Conflict as exc:
                self.store.event(job['project'],'quality_fallback_skipped',{'previous_job':job['id'],'reason':str(exc)})
        return True


def worker(store, config, once=False):
    from .intake_jobs import intake_worker,IntakeQueue
    if once:IntakeQueue(store,config).run_once()
    else:
        intake_stop=threading.Event()
        threading.Thread(target=intake_worker,args=(store,config,intake_stop),daemon=True).start()
    engine=Production(store,config)
    while True:
        try:
            worked=engine.run_once()
        except sqlite3.OperationalError:
            if once:
                raise
            time.sleep(2)
            continue
        if once:
            return
        if not worked:
            time.sleep(1)
