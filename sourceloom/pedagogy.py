"""Reader progression contracts, scoped regeneration and evidence validation."""

import copy
from pydantic import Field
from typing import Literal
from .contracts import Strict
from .checks import validate_plan


class TeachingCheck(Strict):
    category: Literal['prerequisites','concrete_example','progression','term_use','scope','objects','document_info']
    status: Literal['pass','fail','unknown','not_applicable']
    block_ids: list[str]
    quotes: list[str]
    reason: str = Field(min_length=1)


class Transition(Strict):
    before_id: str
    after_id: str
    before_quote: str
    after_quote: str
    relationship: str = Field(min_length=1)
    status: Literal['connected','missing_explanation','wrong_order','unknown']


class TeachingFinding(Strict):
    unit_ids: list[str] = Field(min_length=1)
    block_ids: list[str] = Field(min_length=1)
    quotes: list[str] = Field(min_length=1)
    missing_understanding: str = Field(min_length=1)
    repair_direction: str = Field(min_length=1)


class TeachingReview(Strict):
    assessed_block_ids: list[str]
    checks: list[TeachingCheck]
    transitions: list[Transition]
    findings: list[TeachingFinding]


def validate_teaching_plan(plan, inventory):
    plan = copy.deepcopy(plan)
    source_objects = {o['id']:o for o in inventory['objects']}
    # Selecting a metadata object for end matter selects its recorded facts too.
    # This is reference binding, never authored content or a semantic waiver.
    for unit in plan['units']:
        for sid in unit.get('document_info_ids',[]):
            if sid not in source_objects or source_objects[sid]['kind']!='metadata':
                raise ValueError('主题原信息不能被移作页面资料')
            if sid not in unit['object_ids']:unit['object_ids'].append(sid)
            for fact in inventory['obligations']:
                if fact['object_id']==sid and fact['id'] not in unit['obligation_ids']:
                    unit['obligation_ids'].append(fact['id'])
    plan = validate_plan(plan, inventory)
    seen = set()
    objects = {o['id']: o for o in inventory['objects']}
    for unit in plan['units']:
        if not unit['reader_question'].strip() or not unit['learning_result'].strip():
            raise ValueError('教学规划缺少读者的具体问题或学完后的判断')
        if not set(unit['follows_units']) <= seen:
            raise ValueError('教学前提引用后面的单元或不存在的单元')
        if unit['follows_units'] and not unit['bridge_reason'].strip():
            raise ValueError('相邻主题的依赖没有说明原因')
        if not set(unit['document_info_ids']) <= set(unit['object_ids']):
            raise ValueError('文末资料没有分配到当前单元')
        if any(objects[s]['kind'] != 'metadata' for s in unit['document_info_ids']):
            raise ValueError('主题原信息不能被移作页面资料')
        seen.add(unit['id'])
    functions = plan['teaching_functions']
    expected = {'problem','foundations','concepts','mechanism','example','transfer','extension'}
    if len(functions) != 7 or {f['kind'] for f in functions} != expected:
        raise ValueError('七类教学要求尚未逐项安排，不要求七个固定章节')
    for f in functions:
        if not set(f['unit_ids']) <= seen or (f['treatment']=='included' and not f['unit_ids']):
            raise ValueError('教学要求没有对应实际单元')
        if f['kind'] in {'problem','foundations','concepts'} and f['treatment'] != 'included':
            raise ValueError('理解目标与必要前提不能省略')
    return plan


def arrange_document_info(draft, plan):
    """Move only explicitly authored info blocks; never move or rewrite source facts."""
    result = copy.deepcopy(draft)
    allowed = {s for u in plan['units'] for s in u.get('document_info_ids', [])}
    for b in result['blocks']:
        if b['kind']=='document_info' and (not b['evidence'] or
                not {e['source_id'] for e in b['evidence']} <= allowed):
            raise ValueError('文末资料包含未批准移出教学主线的内容')
    result['blocks'].sort(key=lambda b: b['kind']=='document_info')
    return result


def teaching_review_contract(review, draft, plan):
    """Reject invented citations before interpreting a review as teaching feedback."""
    review=TeachingReview.model_validate(review).model_dump()
    blocks={b['id']:b for b in draft['blocks']}
    errors=[]
    def exact(ids,quotes):
        return bool(ids and quotes) and set(ids)<=set(blocks) and all(q and any(q in blocks[i]['markdown'] for i in ids) for q in quotes)
    if len(review['assessed_block_ids'])!=len(blocks) or set(review['assessed_block_ids'])!=set(blocks):
        errors.append('assessed_block_ids must cover each actual block exactly once')
    for c in review['checks']:
        if c['status']!='not_applicable' and not exact(c['block_ids'],c['quotes']):
            errors.append('check '+c['category']+' contains a non-verbatim quote or unknown block')
    body=[b for b in draft['blocks'] if b['kind']!='document_info']
    pairs={(a['id'],b['id']) for a,b in zip(body,body[1:])}
    actual=[(t['before_id'],t['after_id']) for t in review['transitions']]
    if len(actual)!=len(pairs) or set(actual)!=pairs:
        errors.append('transitions must cover only every adjacent NON-document_info pair exactly once')
    for t in review['transitions']:
        if not exact([t['before_id']],[t['before_quote']]) or not exact([t['after_id']],[t['after_quote']]):
            errors.append('transition '+t['before_id']+' -> '+t['after_id']+' has a non-verbatim quote')
    for n,f in enumerate(review['findings']):
        if not exact(f['block_ids'],f['quotes']):
            errors.append('finding '+str(n)+' has a non-verbatim quote or unknown block')
        elif {blocks[i]['unit_id'] for i in f['block_ids']}!=set(f['unit_ids']):
            errors.append('finding '+str(n)+' unit_ids do not match its cited blocks')
    return errors


def teaching_issues(review, draft, plan):
    review = TeachingReview.model_validate(review).model_dump()
    blocks = {b['id']: b for b in draft['blocks']}
    issues = []
    if len(review['assessed_block_ids']) != len(blocks) or set(review['assessed_block_ids']) != set(blocks):
        issues.append('教学审查没有读完全部正文块')
    expected = {'prerequisites','concrete_example','progression','term_use','scope','objects','document_info'}
    if len(review['checks']) != 7 or {c['category'] for c in review['checks']} != expected:
        issues.append('教学审查缺少必要检查类别')
    def evidenced(ids, quotes):
        return bool(ids and quotes) and set(ids)<=set(blocks) and all(
            q and any(q in blocks[i]['markdown'] for i in ids) for q in quotes)
    for c in review['checks']:
        if c['status'] in {'fail','unknown'}:
            issues.append(c['category']+'：'+c['reason'])
        if c['status']=='not_applicable' and c['category'] in {'prerequisites','progression','term_use','scope'}:
            issues.append('必要教学检查不能用不适用跳过：'+c['category'])
        if c['status']!='not_applicable' and not evidenced(c['block_ids'],c['quotes']):
            issues.append('教学判断没有对应实际正文证据')
    body = [b for b in draft['blocks'] if b['kind']!='document_info']
    pairs = {(a['id'],b['id']) for a,b in zip(body,body[1:])}
    transitions = review['transitions']
    if len(transitions)!=len(pairs) or {(t['before_id'],t['after_id']) for t in transitions}!=pairs:
        issues.append('教学审查未逐一检查前后内容的连接')
    for t in transitions:
        if (t['status']!='connected' or not evidenced([t['before_id']],[t['before_quote']])
                or not evidenced([t['after_id']],[t['after_quote']])):
            issues.append('前后讲解有缺口：'+t['relationship'])
    units = {u['id'] for u in plan['units']}
    for f in review['findings']:
        if not evidenced(f['block_ids'],f['quotes']) or not set(f['unit_ids'])<=units:
            raise ValueError('教学修复依据没有匹配实际正文')
        actual = {blocks[i]['unit_id'] for i in f['block_ids']}
        if actual != set(f['unit_ids']):
            raise ValueError('教学修复范围与引用段落不一致')
        issues.append(f['missing_understanding'])
    return issues


def repair_units(review, plan):
    ids = {u for f in review['findings'] for u in f['unit_ids']}
    order = [u['id'] for u in plan['units']]
    if not ids:
        raise ValueError('教学审查未提供可定位的修复范围')
    positions = [order.index(u) for u in ids]
    if set(range(min(positions),max(positions)+1)) != set(positions):
        raise ValueError('教学修复范围不连续，不能顺带改写无关主题')
    return [u for u in order if u in ids]


def validate_replan(candidate, original, selected, inventory):
    candidate = validate_teaching_plan(candidate, inventory)
    if [u['id'] for u in candidate['units']] != [u['id'] for u in original['units']]:
        raise ValueError('局部教学修复不能增加、删除或移动无关单元')
    for old,new in zip(original['units'],candidate['units']):
        if old['id'] not in selected and old != new:
            raise ValueError('局部教学修复改动了无关单元')
        if set(old['obligation_ids'])!=set(new['obligation_ids']) or set(old['object_ids'])!=set(new['object_ids']):
            raise ValueError('局部教学修复改变了原信息分配')
    return candidate
