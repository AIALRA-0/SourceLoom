"""Deterministic checks make narrowly scoped claims, never semantic guarantees."""

import copy
import re
from .contracts import Draft, Patch, Plan
from .store import Conflict, digest


def source_quote_matches(quote,text):
    # A truly text-free image container has no characters to quote. Its
    # identity, raw structure and child image still require separate coverage.
    return isinstance(quote,str) and (bool(quote) or text=='') and quote in text


def validate_plan(plan, inventory):
    plan = Plan.model_validate(plan).model_dump()
    obligations = {o["id"] for o in inventory["obligations"]}
    objects = {o["id"] for o in inventory["objects"]}
    assigned = []
    seen_units = set()
    for unit in plan["units"]:
        if unit["id"] in seen_units:
            raise ValueError("教学单元身份重复")
        seen_units.add(unit["id"])
        if not set(unit["object_ids"]) <= objects:
            raise ValueError("规划引用不存在的源对象")
        assigned.extend(unit["obligation_ids"])
    if set(assigned) != obligations:
        raise ValueError("教学规划没有完整分配冻结义务，或引用了不存在的义务")
    return plan


def inspect_draft(inventory, draft, plan=None, require_heading_structure=False, prior_draft=None):
    findings = []
    def error(code, message, block_id="", obligation_id=""):
        findings.append(dict(id=f"mechanical-{len(findings)+1}", severity="error", code=code,
                             block_id=block_id, obligation_id=obligation_id, message=message, expected=""))
    if not inventory or not inventory["frozen"]:
        error("unfrozen", "清单尚未冻结")
        return findings
    try:
        parsed = Draft.model_validate(draft).model_dump()
    except ValueError:
        error("shape", "候选结构不符合约定")
        return findings
    sources = {o["id"]: o for o in inventory["objects"]}
    obligations = {o["id"]: o for o in inventory["obligations"]}
    units = {u["id"] for u in plan["units"]} if plan else None
    resources = {r["id"] for r in inventory["resources"]}
    earlier=set()
    if prior_draft:
        from .writing import protected_objects
        from .media import source_literals
        for sid,literal in protected_objects(inventory).items():
            if any(sid in b.get('embedded_object_ids',[]) and sid in b['object_ids'] and any(v in b['markdown'] for v in source_literals(sources[sid],literal)) for b in prior_draft['blocks']):
                earlier.add(sid)
    covered, object_coverage, identities = set(), set(), set()
    for b in parsed["blocks"]:
        if not set(b.get('embedded_object_ids',[])) <= set(b['object_ids']):
            error('embedded_object','正文内嵌对象没有原对象关系',b['id'])
        if b.get('embedded_object_ids'):
            from .writing import protected_objects
            literals=protected_objects(inventory)
            for src in inventory['objects']:
                if src['kind'] in {'text','heading'}:
                    literals[src['id']]=''.join('> '+line for line in src['text'].splitlines(keepends=True))
            for sid in b['embedded_object_ids']:
                from .media import source_literals
                if sid not in literals or not any(text in b['markdown'] for text in source_literals(sources[sid],literals[sid])):
                    error('embedded_bytes','正文内嵌原对象的字符发生变化',b['id'])
        if b["id"] in identities:
            error("duplicate", "候选段落身份重复", b["id"])
        identities.add(b["id"])
        if units is not None and b["unit_id"] not in units:
            error("unit", "候选引用不存在的教学单元", b["id"])
        if not b["markdown"].strip() and not b["object_ids"]:
            error("empty", "空段落不能代替内容覆盖", b["id"])
        for oid in b["obligation_ids"]:
            if oid not in obligations:
                error("unknown_obligation", "候选引用不存在的义务", b["id"], oid)
            else:
                covered.add(oid)
                source_id = obligations[oid]["object_id"]
                if source_id not in {e["source_id"] for e in b["evidence"]} and source_id not in b["object_ids"]:
                    error("unmapped", "义务没有指向对应原文或原对象", b["id"], oid)
        for e in b["evidence"]:
            src = sources.get(e["source_id"])
            if not src or not source_quote_matches(e['quote'],src['text']):
                error("quote", "来源片段与保存的原文不符", b["id"])
        for sid in b["object_ids"]:
            if sid not in sources:
                error("object", "引用不存在的原始对象", b["id"])
            else:
                object_coverage.add(sid)
                src = sources[sid]
                if src.get("resource_id") and src["resource_id"] not in resources:
                    error("resource", "原对象资源缺失", b["id"])
    for oid in obligations.keys() - covered:
        error("omission", "冻结义务在候选中没有落点", obligation_id=oid)
    for sid, src in sources.items():
        from .visual_sources import decorative_resource
        if decorative_resource(src):
            if src.get('resource_id') not in resources:
                error('resource','排版资源没有保存在原件资源清单中')
            continue
        if src["kind"] in {"image", "table", "code", "formula", "link", "page", "attachment", "footnote"} and sid not in object_coverage and sid not in earlier:
            error("protected_object", "受保护对象没有插入候选", obligation_id=next((o["id"] for o in obligations.values() if o["object_id"]==sid), ""))
        if require_heading_structure and src['kind']=='heading' and not any(
                sid in b['object_ids'] and (
                    (b['kind']=='source' and not b['markdown'].strip()) or
                    any(re.match(r'^ {0,3}#{1,6}\s+\S',line)
                        for line in b['markdown'].splitlines()))
                for b in parsed['blocks']):
            error('heading_structure','原文标题没有关联到改写正文的标题位置',
                  obligation_id=next((o['id'] for o in obligations.values() if o['object_id']==sid),''))
    return findings


def review_complete(project):
    production=project.get('production') or {}
    if production.get('pipeline')=='active_composition_v1':
        # Completion means a generated, structurally checked candidate, not an independent semantic pass
        review=project.get('independent_review') or {}
        from .writing import canonical
        return (review.get('status')=='passed' and review.get('revision')==project['revision']
                and review.get('canonical_digest')==digest(canonical(project['draft']).encode()))
    if production.get('automatic') and production.get('status')=='completed':
        from .writing import canonical
        return (production.get('revision')==project['revision'] and production.get('manual_edits')==0
                and not production.get('issues') and bool(production.get('skill_digest'))
                and production.get('canonical_digest')==digest(canonical(project['draft']).encode()))
    review = project.get("review")
    if not review or review.get("revision") != project["revision"] or review.get("inventory_digest") != project["inventory"]["digest"]:
        return False
    body = review["body"]
    required_obligations = {o["id"] for o in project["inventory"]["obligations"]}
    required_blocks = {b["id"] for b in project["draft"]["blocks"]}
    return (not any(f["severity"]=="error" for f in body["findings"])
            and required_obligations == set(body["assessed_obligation_ids"])
            and required_blocks == set(body["assessed_block_ids"])
            and required_blocks == {p["block_id"] for p in body["proofs"]}
            and len({p["id"] for p in body["proofs"]}) == len(body["proofs"])
            and all(p["status"]=="supported" and p["claim"].strip() and p["method"].strip()
                    and (p["evidence"] or p["kind"] in {"mathematical", "example", "design"})
                    for p in body["proofs"]))


def release_issues(project):
    issues = inspect_draft(project["inventory"], project["draft"], project.get("plan"),
                           require_heading_structure=(project.get('production') or {}).get('teaching_version',0)>=2)
    if project["inventory"]["unknown"]:
        issues.append(dict(code="unknown",message="仍有接入或对象未知项"))
    if not project["inventory"].get("inventory_review"):
        issues.append(dict(code="inventory_review",message="清单尚未独立核对"))
    if not review_complete(project):
        issues.append(dict(code="review",message="当前版本尚未完成独立语义审核"))
    if project["accepted_revision"] != project["revision"]:
        issues.append(dict(code="acceptance",message="用户尚未接受当前版本"))
    return issues


def apply_patch(project, patch, allowed):
    patch = Patch.model_validate(patch).model_dump()
    if patch["base_revision"] != project["revision"]:
        raise Conflict("补丁基线过期，已拒绝覆盖")
    if project["repair_rounds"] >= 2:
        raise Conflict("已到两轮局部修复上限，候选与缺口保留")
    result = copy.deepcopy(project["draft"])
    blocks = {b["id"]: b for b in result["blocks"]}
    seen = set()
    for edit in patch["edits"]:
        bid = edit["block_id"]
        if bid not in allowed or bid not in blocks or bid in seen:
            raise Conflict("补丁重复、越界或段落不存在")
        if blocks[bid]["markdown"] != edit["old_markdown"]:
            raise Conflict("补丁旧文不匹配，整笔未写入")
        seen.add(bid)
        blocks[bid]["markdown"] = edit["new_markdown"]
    # Only the specified text changes; object and evidence identities remain locked.
    return result


def freeze(inventory):
    inv = copy.deepcopy(inventory)
    if not inv["objects"]:
        raise ValueError("没有已接入的源对象")
    inv["frozen"] = True
    inv["digest"] = digest({k:v for k,v in inv.items() if k!="digest"})
    return inv
