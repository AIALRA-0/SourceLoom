"""Bound review output while every part still reads the whole skill and draft."""
import copy
import json
from .production_contracts import StyleReview


class IndexedReviewSchema:
    """Put assigned identities in required object keys, not model-authored arrays."""
    def __init__(self,payload):self.payload=payload

    def model_json_schema(self):
        schema=copy.deepcopy(StyleReview.model_json_schema())
        definitions=schema['$defs']
        blocks=self.payload.get('draft',{}).get('blocks',[])
        if blocks:
            ids=[b['id'] for b in blocks]
            definitions['RuleAssessment']['properties']['block_ids']['items']={'type':'string','enum':ids}
            definitions['Finding']['properties']['block_id']={'type':'string','enum':ids}
        executions=list(self.payload.get('execution_evidence',{}))
        if executions:
            definitions['RuleAssessment']['properties']['execution_ids']['items']={'type':'string','enum':executions}
        if self.payload.get('evidence_catalog'):
            rule=definitions['RuleAssessment']
            rule['properties'].pop('quotes',None);rule['properties'].pop('block_ids',None)
            rule['required']=[k for k in rule['required'] if k not in {'quotes','block_ids'}]+['evidence_ids']
            rule['properties']['evidence_ids']={'type':'array','items':{'type':'string','enum':list(self.payload['evidence_catalog'])}}
        for name,identity in [('RuleAssessment','rule_ids'),('CandidateAssessment','candidate_id')]:
            definitions[name]['properties'].pop(identity)
            definitions[name]['required'].remove(identity)
        properties={'findings':schema['properties']['findings']}
        rules=self.payload['rule_catalog']
        candidates=[row['id'] for key in ('mechanical_findings','mechanical_candidates') for row in self.payload[key]]
        for name,ids,definition in [('rules_by_id',rules,'RuleAssessment'),('checks_by_id',candidates,'CandidateAssessment')]:
            if ids:
                properties[name]={'type':'object','properties':{key:{'$ref':'#/$defs/'+definition} for key in ids},
                    'required':ids,'additionalProperties':False}
        schema.update(properties=properties,required=list(properties),additionalProperties=False)
        return schema


def decode_indexed(result,payload):
    from jsonschema import validate
    # Some providers repeat the top-level empty findings inside assessments.
    # Accept only the exact empty list; never discard substantive feedback.
    result=copy.deepcopy(result)
    _drop_unassigned_empty_groups(result,payload)
    # A model can echo these two harmless root-schema annotations beside the
    # requested object. Remove only their exact schema values; every review
    # verdict, finding and unknown remains subject to the closed schema.
    if result.get('title')=='StyleReview':result.pop('title')
    if result.get('additionalProperties') is False:result.pop('additionalProperties')
    # Extra positive observations have no authority over another partition.
    # Ignore only known, unassigned pass/NA rows; retain every finding and
    # reject extra negative/unknown judgments instead of losing criticism.
    rules=result.get('rules_by_id',{}) if isinstance(result,dict) else {}
    had_extra_rows=bool(rules)
    if isinstance(rules,dict):
        for rid,row in list(rules.items()):
            if (rid not in payload['rule_catalog'] and rid in payload.get('rule_definitions',{})
                    and isinstance(row,dict) and row.get('status') in {'pass','not_applicable'}
                    and set(row)<={'status','reason','evidence_ids','execution_ids','quotes','block_ids'}):
                del rules[rid]
        if had_extra_rows and not rules and not payload['rule_catalog']:result.pop('rules_by_id',None)
    for group in ('rules_by_id','checks_by_id'):
        rows=result.get(group,{}) if isinstance(result,dict) else {}
        if isinstance(rows,dict):
            for row in rows.values():
                if isinstance(row,dict) and row.get('findings')==[]:
                    del row['findings']
    validate(result,IndexedReviewSchema(payload).model_json_schema())
    if payload.get('evidence_catalog'):
        for row in result.get('rules_by_id',{}).values():
            records=[payload['evidence_catalog'][key] for key in row.pop('evidence_ids')]
            row['block_ids']=list(dict.fromkeys(r['block_id'] for r in records))
            row['quotes']=[r['text'] for r in records]
    rules=[row|{'rule_ids':[key]} for key,row in result.get('rules_by_id',{}).items()]
    candidates=[row|{'candidate_id':key} for key,row in result.get('checks_by_id',{}).items()]
    return validate_part({'assessments':rules,'mechanical_assessments':candidates,'findings':result['findings']},payload)


def evidence_catalog(draft):
    """Immutable line references prevent reviewers from rewriting their quotations."""
    return {f"e{block_index+1}-{line_index+1}":{'block_id':block['id'],'text':line}
        for block_index,block in enumerate(draft['blocks'])
        for line_index,line in enumerate(block['markdown'].splitlines()) if line.strip()}


def missing_assignments(result,payload):
    """Select missing identities only; malformed or extra answers remain errors."""
    from jsonschema import Draft202012Validator
    result=copy.deepcopy(result)
    _drop_unassigned_empty_groups(result,payload)
    errors=list(Draft202012Validator(IndexedReviewSchema(payload).model_json_schema()).iter_errors(result))
    if not errors or any(e.validator!='required' for e in errors) or 'findings' not in result:
        raise ValueError('审核返回结构不是可单独补齐的检查项遗漏')
    missing=copy.deepcopy(payload)
    missing['rule_catalog']=[rid for rid in payload['rule_catalog'] if rid not in result.get('rules_by_id',{})]
    for key in ('mechanical_findings','mechanical_candidates'):
        missing[key]=[row for row in payload[key] if row['id'] not in result.get('checks_by_id',{})]
    if not any(missing[key] for key in ('rule_catalog','mechanical_findings','mechanical_candidates')):
        raise ValueError('已有检查项内部缺字段，不能冒充整项遗漏')
    return missing


def merge_indexed(original,supplement,payload):
    missing=missing_assignments(original,payload)
    decode_indexed(supplement,missing)
    result=copy.deepcopy(original)
    for key in ('rules_by_id','checks_by_id'):
        if supplement.get(key):result.setdefault(key,{}).update(copy.deepcopy(supplement[key]))
    result['findings'].extend(copy.deepcopy(supplement['findings']))
    return decode_indexed(result,payload)


def invalid_assignments(result,payload):
    """Reassess only malformed rows; preserve valid verdicts and all findings."""
    from jsonschema import Draft202012Validator
    result=copy.deepcopy(result)
    _drop_unassigned_empty_groups(result,payload)
    errors=list(Draft202012Validator(IndexedReviewSchema(payload).model_json_schema()).iter_errors(result))
    if not errors:raise ValueError('没有需要修正的审核结构')
    rows=set()
    for error in errors:
        path=list(error.path)
        if len(path)<2 or path[0] not in {'rules_by_id','checks_by_id'}:
            raise ValueError('审核错误不局限于可独立重审的检查项')
        rows.add((path[0],path[1]))
    retained=copy.deepcopy(result)
    for group,key in rows:del retained[group][key]
    return retained,missing_assignments(retained,payload)


def _drop_unassigned_empty_groups(result,payload):
    """Ignore only an exact empty object echoed for a group absent from this partition."""
    expected={
        'rules_by_id':bool(payload['rule_catalog']),
        'checks_by_id':any(payload[key] for key in ('mechanical_findings','mechanical_candidates')),
    }
    if isinstance(result,dict):
        for group,assigned in expected.items():
            if not assigned and result.get(group)=={}:
                result.pop(group)


def partitions(payload,limit=40):
    tasks=[('rule_catalog',rid) for rid in payload['rule_catalog']]
    tasks += [(key,row) for key in ('mechanical_findings','mechanical_candidates') for row in payload[key]]
    count=max(1,(len(tasks)+limit-1)//limit)
    result=[]
    for index in range(count):
        part=copy.deepcopy(payload)
        for key in ('rule_catalog','mechanical_findings','mechanical_candidates'):part[key]=[]
        for key,value in tasks[index*limit:(index+1)*limit]:part[key].append(value)
        if isinstance(part.get('rule_definitions'),dict):
            part['rule_definitions']={rid:part['rule_definitions'][rid] for rid in part['rule_catalog'] if rid in part['rule_definitions']}
        part['review_partition']={'index':index+1,'count':count,
            'instruction':'Read the complete skill, whole actual draft and every supplied protected source literal. Return judgments only for this assigned rule_catalog and these mechanical items, each exactly once; unassigned fields are empty arrays. Other parts cover all remaining IDs. Never summarize or omit candidate text or supplied protected source spans. Complete original meaning is covered by the separate fidelity review. Findings must relate to this part\'s assigned checks.'}
        result.append(part)
    return result


def validate_part(result,payload):
    result=StyleReview.model_validate(result).model_dump()
    rules=[rid for a in result['assessments'] for rid in a['rule_ids']]
    actual=[a['candidate_id'] for a in result['mechanical_assessments']]
    expected=[r['id'] for key in ('mechanical_findings','mechanical_candidates') for r in payload[key]]
    if len(rules)!=len(set(rules)) or set(rules)!=set(payload['rule_catalog']):raise ValueError('本组写作审核没有准确覆盖指定规则')
    if len(actual)!=len(set(actual)) or set(actual)!=set(expected):raise ValueError('本组写作审核没有准确覆盖指定检查项')
    return result


def merge(parts):
    combined={'assessments':[],'mechanical_assessments':[],'findings':[]};seen=set()
    for index,part in enumerate(parts):
        for key in ('assessments','mechanical_assessments'):combined[key].extend(part[key])
        for finding in part['findings']:
            identity=json.dumps({k:v for k,v in finding.items() if k!='id'},ensure_ascii=False,sort_keys=True)
            if identity in seen:continue
            seen.add(identity);combined['findings'].append(finding|{'id':f'part-{index+1}-'+finding['id']})
    return combined
