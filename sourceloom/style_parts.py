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
    for group in ('rules_by_id','checks_by_id'):
        rows=result.get(group,{}) if isinstance(result,dict) else {}
        if isinstance(rows,dict):
            for row in rows.values():
                if isinstance(row,dict) and row.get('findings')==[]:
                    del row['findings']
    validate(result,IndexedReviewSchema(payload).model_json_schema())
    rules=[row|{'rule_ids':[key]} for key,row in result.get('rules_by_id',{}).items()]
    candidates=[row|{'candidate_id':key} for key,row in result.get('checks_by_id',{}).items()]
    return validate_part({'assessments':rules,'mechanical_assessments':candidates,'findings':result['findings']},payload)


def missing_assignments(result,payload):
    """Select missing identities only; malformed or extra answers remain errors."""
    from jsonschema import Draft202012Validator
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


def partitions(payload,limit=40):
    tasks=[('rule_catalog',rid) for rid in payload['rule_catalog']]
    tasks += [(key,row) for key in ('mechanical_findings','mechanical_candidates') for row in payload[key]]
    count=max(1,(len(tasks)+limit-1)//limit)
    result=[]
    for index in range(count):
        part=copy.deepcopy(payload)
        for key in ('rule_catalog','mechanical_findings','mechanical_candidates'):part[key]=[]
        for key,value in tasks[index*limit:(index+1)*limit]:part[key].append(value)
        part['review_partition']={'index':index+1,'count':count,
            'instruction':'Read the complete skill and whole actual draft. Return judgments only for this assigned rule_catalog and these mechanical items, each exactly once; unassigned fields are empty arrays. Other parts cover all remaining IDs. Never summarize or omit source text. Findings must relate to this part\'s assigned checks.'}
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
