"""Role orchestration with frozen inputs, independent outputs and local recovery."""

import copy
import json
from pathlib import Path
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import contracts as C
from .checks import apply_patch, freeze, inspect_draft, validate_plan
from .config import writing_snapshot
from .providers import Provider, Uncertain
from .store import Conflict, digest, identity
from .versions import pending_obligations

SCHEMAS = {"inventory":C.InventoryReview,"planner":C.Plan,"generator":C.Draft,
           "reviewer":C.Review,"repairer":C.Patch,"research":C.ResearchPlan}


class Pipeline:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self.provider = Provider(store, config)
        self.executor = ThreadPoolExecutor(max_workers=1,thread_name_prefix="sourceloom")
        self.lock = threading.Lock()
        self.cancelled = set()
        # A restarted process cannot know whether an external request completed.
        for p in ([] if config.get('external_worker') else store.list()):
            if p.get("active_job"):
                job = store.job(p["active_job"])
                job["status"]="uncertain"
                job["error"]="进程中断，请查询原任务或导回原结果，不自动重发"
                store.put_job(job)
                store.change(p["id"],lambda p:p.update(active_job=None,state="paused"))

    def payload(self, p, role):
        inv = p.get("inventory")
        if role != "research" and not inv:
            raise Conflict("先接入材料")
        if role not in {"inventory","research"} and not inv["frozen"]:
            raise Conflict("先冻结清单")
        if role in {"generator","reviewer","repairer"} and not p["plan"]:
            raise Conflict("先形成教学规划")
        if role in {"reviewer","repairer"} and not p["draft"]:
            raise Conflict("先形成候选")
        if role == "repairer" and (not p["review"] or p["review"]["revision"] != p["revision"]):
            raise Conflict("修复需要当前版本的问题定位")
        payload = dict(goal=p["goal"],mode=p["mode"],inventory=inv,
                       plan=p["plan"] if role in {"generator","reviewer","repairer"} else None,
                       draft=p["draft"] if role in {"reviewer","repairer"} else None,
                       review=p["review"] if role=="repairer" else None,base_revision=p["revision"])
        if role=='planner' and p.get('incremental_pending') and p['baseline'].get('draft'):
            current=copy.deepcopy(inv)
            current['obligations']=pending_obligations(p)
            wanted={o['object_id'] for o in current['obligations']}
            current['objects']=[o for o in current['objects'] if o['id'] in wanted]
            payload['inventory']=current
            payload['retained_units']=p['baseline']['plan']['units']
            payload['incremental_instruction']='Plan only the supplied additional obligations. The application retains existing units and blocks unchanged. Use new unit IDs; explain dependencies on retained units explicitly.'
        if role == "repairer":
            payload["allowed_block_ids"] = list({f["block_id"] for f in p["review"]["body"]["findings"] if f["block_id"]})
        if role in {"reviewer","repairer"}:
            from .export import render
            from bs4 import BeautifulSoup
            import re
            payload["rendered_candidate_html"]=render(p)
            visible=BeautifulSoup(payload['rendered_candidate_html'],'html.parser').get_text()
            payload['mechanical_evidence']={'visible_han_characters':len(re.findall('[\u3400-\u9fff]',visible)),
                'visible_characters_including_latin_numbers_whitespace':len(visible),
                'count_method':'Unicode U+3400..U+9FFF matches in parsed rendered HTML text, including protected source insertions',
                'limit_interpretation':'A limit stated as Chinese characters refers to the Han count; do not estimate it by eye'}
        if self.config.get('writing_skill_dir'):
            snapshot = writing_snapshot(self.config)
            payload["writing_policy"] = snapshot
            payload["writing_policy_digest"] = digest(snapshot)
        return payload

    def task_pack(self,pid,role):
        p=self.store.get(pid)
        payload=self.payload(p,role)
        if role=='generator' and p.get('incremental_pending') and p['baseline'].get('draft'):
            payload['retained_blocks']=p['baseline']['draft']['blocks']
            payload['manual_incremental_instruction']='Return these retained blocks exactly unchanged, followed by blocks for only the new units. Existing block IDs, text, metadata, and order are immutable.'
        return dict(schema="sourceloom-task/1",project=pid,role=role,base_revision=p["revision"],
                    inventory_digest=p["inventory"]["digest"] if p.get("inventory") else None,
                    instructions=(Path(__file__).parent/"roles"/f"{role}.md").read_text(encoding="utf-8"),
                    payload=payload,response_schema=SCHEMAS[role].model_json_schema())

    def start(self,pid,role):
        if role not in SCHEMAS:
            raise ValueError("未知角色")
        with self.lock:
            p=self.store.get(pid)
            self.payload(p,role)
            if p["active_job"]:
                return self.store.job(p["active_job"])
            if any(j["status"]=="uncertain" for j in self.store.jobs(pid)):
                raise Conflict("存在结果不确定的任务，请先查询原任务或导回原结果")
            if any(j['status']=='paused' for j in self.store.jobs(pid)):
                raise Conflict('已有部分完成的生成任务，请继续原任务，期间保留原规划与清单')
            if self.config["provider"]=="manual":
                raise Conflict("当前是人工任务包通道，请下载角色任务包")
            job=dict(id=identity(),project=pid,role=role,status="queued",created=time.time(),calls=[],
                     base_revision=p["revision"],inventory_digest=p["inventory"]["digest"] if p["inventory"] else None)
            self.store.put_job(job)
            self.store.change(pid,lambda p:p.update(active_job=job["id"]))
            self.executor.submit(self.run,job)
            return job

    def run(self,job):
        pid,role=job["project"],job["role"]
        try:
            job["status"]="running"
            self.store.put_job(job)
            p=self.store.get(pid)
            started=time.monotonic()
            if role=="generator":
                retained=p.get('baseline',{}).get('draft') if p.get('incremental_pending') else None
                blocks=copy.deepcopy(job.get('partial_result', retained or {'blocks':[]})['blocks'])
                completed=set(job.get('completed_unit_ids',[]))
                if retained:
                    completed.update(u['id'] for u in p['baseline']['plan']['units'])
                    job.setdefault('partial_result',copy.deepcopy(retained))
                job['completed_unit_ids']=list(completed)
                for unit in p["plan"]["units"]:
                    if unit['id'] in completed:continue
                    if self.config.get("job_timeout",0)>0 and time.monotonic()-started > self.config["job_timeout"]:
                        raise Conflict("达到整项生成等待上限，已完成单元保存在任务结果中")
                    payload=self.payload(p,role)
                    current=copy.deepcopy(p["inventory"])
                    needed=set(unit["obligation_ids"])
                    current["obligations"]=[o for o in current["obligations"] if o["id"] in needed]
                    object_ids={o["object_id"] for o in current["obligations"]}|set(unit["object_ids"])
                    current["objects"]=[o for o in current["objects"] if o["id"] in object_ids]
                    payload["inventory"]=current
                    payload["current_unit"]=unit
                    job['current_unit_id']=unit['id']
                    self.store.put_job(job)
                    result=self.provider.call(pid,role,payload,C.Draft.model_json_schema(),job,lambda:job["id"] in self.cancelled)
                    result=C.Draft.model_validate(result).model_dump()
                    if inspect_draft(current,result,{'units':[unit]}):
                        raise ValueError('当前单元存在遗漏或来源错误，已停止后续调用')
                    if {b['id'] for b in blocks}&{b['id'] for b in result['blocks']}:
                        raise ValueError('当前单元重复使用已有段落身份，未覆盖旧结果')
                    blocks.extend(result['blocks'])
                    job["partial_result"]={"blocks":blocks}
                    job.setdefault('completed_unit_ids',[]).append(unit['id'])
                    completed.add(unit['id'])
                    self.store.put_job(job)
                result={"blocks":blocks}
            else:
                result=self.provider.call(pid,role,self.payload(p,role),SCHEMAS[role].model_json_schema(),job,lambda:job["id"] in self.cancelled)
            if job["id"] in self.cancelled:
                raise Conflict("任务已取消，未改变候选")
            job["result"]=result
            self.commit(pid,role,result,job["base_revision"],job["inventory_digest"],job["id"])
            job["status"]="completed"
        except Exception as exc:
            job["status"]="uncertain" if isinstance(exc,Uncertain) else "cancelled" if job["id"] in self.cancelled else "failed"
            if job['status'] in {'failed','cancelled'} and role=='generator' and job.get('partial_result') and 'result' not in job:
                job['status']='paused'
            job["error"]=str(exc)[:500] if isinstance(exc,(ValueError,Conflict,Uncertain)) else "阶段失败，原件与既有产物保留"
        finally:
            job["finished"]=time.time()
            self.store.put_job(job)
            self.store.change(pid,lambda p:p.update(active_job=None))
            self.store.event(pid,"role_finished",dict(job=job["id"],role=role,status=job["status"]))

    def commit(self,pid,role,result,revision,inventory_digest,job_id="manual"):
        result=SCHEMAS[role].model_validate(result).model_dump()
        if any(j['status']=='paused' and j['id']!=job_id for j in self.store.jobs(pid)):
            raise Conflict('部分生成任务尚未结束，不能改写其依赖产物')
        def change(p):
            if p["active_job"] and p["active_job"]!=job_id:
                raise Conflict("另一个角色仍在运行")
            inv=p["inventory"]
            if inv and inv["digest"]!=inventory_digest:
                raise Conflict("清单版本已变化，角色产物未写入")
            if role=="inventory":
                if inv["frozen"]:
                    raise Conflict("冻结清单只能通过新版本补漏")
                known={o["id"]:o for o in inv["objects"]}
                if set(result["assessed_object_ids"])!=set(known):
                    raise ValueError("清单审核没有覆盖全部已接入对象")
                ids={o["id"] for o in inv["obligations"]}
                for addition in result["additions"]:
                    if addition["id"] in ids or addition["object_id"] not in known:
                        raise ValueError("补充义务身份重复或源对象不存在")
                    ids.add(addition["id"])
                inv["obligations"].extend(result["additions"])
                inv["inventory_review"]={"job":job_id,"body":result}
                # Extraction gaps cannot be cleared by a text-only reviewer.
                for oid in result["unresolved_object_ids"]:
                    if oid not in known:
                        raise ValueError("未知项指向不存在的对象")
                    inv["unknown"].append(dict(id=identity(),object_id=oid,reason="独立清单审核仍不确定",locator=known[oid]["locator"]))
            elif role=="planner":
                if p["draft"] and not p.get('incremental_pending'):
                    raise Conflict("已有候选，调整规划需要新项目版本")
                if p.get('incremental_pending') and p['baseline'].get('draft'):
                    extra=copy.deepcopy(inv)
                    extra['obligations']=pending_obligations(p)
                    # A delta plan cannot rewrite old units or claim old obligations.
                    validate_plan(result,extra)
                    result['units']=copy.deepcopy(p['baseline']['plan']['units'])+result['units']
                p["plan"]=validate_plan(result,inv)
                p["state"]="planned"
            elif role=="generator":
                if p["draft"] and not p.get('incremental_pending'):
                    raise Conflict("已有候选，请使用局部修改或建立新版本")
                if p.get('incremental_pending') and p['baseline'].get('draft'):
                    old=p['baseline']['draft']['blocks']
                    normalized=C.Draft.model_validate({'blocks':old}).model_dump()['blocks']
                    if result['blocks'][:len(old)]!=normalized:
                        raise Conflict('增量生成改变了原有段落，候选整笔未写入')
                    result['blocks'][:len(old)]=copy.deepcopy(old)
                findings=inspect_draft(inv,result,p["plan"])
                if findings:
                    raise ValueError("候选未通过结构核对："+"；".join(f["message"] for f in findings[:4]))
                p.update(draft=result,revision=p["revision"]+1,review=None,accepted_revision=None,state="generated",incremental_pending=False)
            elif role=="reviewer":
                source={o["id"]:o["text"] for o in inv["objects"]}
                block_ids={b["id"] for b in p["draft"]["blocks"]}
                for proof in result["proofs"]:
                    if proof["block_id"] not in block_ids:
                        raise ValueError("证明记录引用未知段落")
                    for ev in proof["evidence"]:
                        from .checks import source_quote_matches
                        if ev["source_id"] not in source or not source_quote_matches(ev['quote'],source[ev['source_id']]):
                            raise ValueError("证明来源片段不匹配")
                p["review"]={"revision":p["revision"],"inventory_digest":inv["digest"],"job":job_id,"body":result}
                p["state"]="reviewed"
            elif role=="repairer":
                allowed={f["block_id"] for f in p["review"]["body"]["findings"] if f["block_id"]}
                p["draft"]=apply_patch(p,result,allowed)
                p.update(revision=p["revision"]+1,repair_rounds=p["repair_rounds"]+1,review=None,accepted_revision=None,state="generated")
            elif role=="research":
                p["research"]=result
                p["state"]="researching"
        updated=self.store.change(pid,change,revision)
        self.store.event(pid,"artifact_committed",dict(role=role,revision=updated["revision"],job=job_id))
        return updated

    def cancel(self,jid):
        self.cancelled.add(jid)
        job=self.store.job(jid)
        self.store.event(job["project"],"cancel_requested",dict(job=jid))
        return job

    def recover(self,jid):
        with self.lock:
            job=self.store.job(jid)
            if job['status']!='uncertain':raise Conflict('仅查询结果不确定的原任务')
            p=self.store.get(job['project'])
            if p['active_job']:raise Conflict('另一个角色正在执行')
            result=self.provider.recover(job)
            if result is None:return job
            if job['role']=='generator':
                completed=set(job.get('completed_unit_ids',[]))
                blocks=job.get('partial_result',{'blocks':[]})['blocks']
                if job.get('current_unit_id') not in completed:
                    blocks+=C.Draft.model_validate(result).model_dump()['blocks']
                    completed.add(job['current_unit_id'])
                job['partial_result']={'blocks':blocks}
                job['completed_unit_ids']=list(completed)
                if completed!={u['id'] for u in p['plan']['units']}:
                    job['status']='paused'
                    job['error']='原调用结果已找回，仍有未生成单元，保存的单元不会重发'
                    self.store.put_job(job)
                    return job
                result={'blocks':blocks}
            self.commit(p['id'],job['role'],result,job['base_revision'],job['inventory_digest'],job['id'])
            job.update(result=result,status='completed',recovered=True,error=None)
            self.store.put_job(job)
            return job

    def resume(self,jid):
        with self.lock:
            job=self.store.job(jid)
            if job['status']!='paused' or job['role']!='generator':
                raise Conflict('仅继续已确认原调用结果的部分生成任务')
            p=self.store.get(job['project'])
            if p['active_job'] or p['revision']!=job['base_revision'] or p['inventory']['digest']!=job['inventory_digest']:
                raise Conflict('任务基线已变化或已有角色执行，保留原结果')
            if any(j['status']=='uncertain' for j in self.store.jobs(p['id'])):
                raise Conflict('先核对结果不确定的原调用')
            if job.get('resume_count',0)>=2:
                raise Conflict('已达到两次继续上限，请保留候选并核对具体缺口')
            job.update(status='queued',resume_count=job.get('resume_count',0)+1,error=None)
            self.cancelled.discard(jid)
            self.store.put_job(job)
            self.store.change(p['id'],lambda p:p.update(active_job=jid))
            self.executor.submit(self.run,job)
            return job
