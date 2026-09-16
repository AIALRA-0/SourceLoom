"""Explicit review assignments; evidence text is resolved from immutable inputs."""
import copy
from jsonschema import validate,Draft202012Validator
from .production_contracts import FidelityReview


def partitions(source,facts,draft,limit=45):
    tasks=[('source_ids',o['id']) for o in source['objects']]
    tasks += [('fact_ids',f['id']) for f in facts['facts']]
    tasks += [('block_ids',b['id']) for b in draft['blocks']]
    parts=[]
    for start in range(0,len(tasks),limit):
        part={key:[] for key in ('source_ids','fact_ids','block_ids')}
        for kind,identity in tasks[start:start+limit]:part[kind].append(identity)
        parts.append(part)
    return parts


class FidelitySchema:
    def __init__(self,assigned,source,draft):
        self.assigned,self.source,self.draft=assigned,source,draft

    def model_json_schema(self):
        schema=copy.deepcopy(FidelityReview.model_json_schema());defs=schema['$defs']
        blocks=[b['id'] for b in self.draft['blocks']];sources=[o['id'] for o in self.source['objects']]
        def row(properties):
            return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}
        explanation={'type':'string','minLength':1}
        fact=row({k:v for k,v in defs['FactCheck']['properties'].items() if k not in {'fact_id','source_quote','output_quote'}})
        fact['properties']['block_id']={'type':'string','enum':['',*blocks]}
        reverse=row({k:v for k,v in defs['ReverseCheck']['properties'].items() if k not in {'block_id','output_quote','evidence'}}|{
            'source_ids':{'type':'array','items':{'type':'string','enum':sources},'uniqueItems':True}})
        source=row({'missing_information':{'type':'array','items':{'type':'string'}},'explanation':explanation})
        def keyed(ids,name):return row({key:{'$ref':'#/$defs/'+name} for key in ids})
        defs['Finding']['properties']['block_id']={'type':'string','enum':['',*blocks]}
        shared={'AssignedSourceCheck':source,'AssignedFactCheck':fact,'AssignedReverseCheck':reverse,
                'Finding':defs['Finding']}
        return row({'source_checks':keyed(self.assigned['source_ids'],'AssignedSourceCheck'),
                    'fact_checks':keyed(self.assigned['fact_ids'],'AssignedFactCheck'),
                    'reverse_checks':keyed(self.assigned['block_ids'],'AssignedReverseCheck'),
                    'findings':{'type':'array','items':{'$ref':'#/$defs/Finding'}}})|{'$defs':shared}


def decode(result,assigned,source,facts,draft):
    result=copy.deepcopy(result)
    # Some structured-output providers append this undeclared empty helper
    # beside the required explanation. It carries no judgment or evidence.
    # Remove only the exact empty value; a non-empty addition still fails.
    for row in result.get('fact_checks',{}).values():
        if isinstance(row,dict) and row.get('explanation_note')=='':row.pop('explanation_note')
    assigned_groups={'source_checks':set(assigned['source_ids']),'fact_checks':set(assigned['fact_ids']),
        'reverse_checks':set(assigned['block_ids'])}
    current_blocks={b['id'] for b in draft['blocks']}
    for group,wanted in assigned_groups.items():
        for key,row in list(result.get(group,{}).items()):
            placeholder=(key not in wanted and isinstance(row,dict) and row.get('explanation') in {'占位','placeholder'}
                and row.get('status') in {None,'unknown'} and not row.get('block_id'))
            stale_block=group=='reverse_checks' and key not in current_blocks
            if placeholder or stale_block:del result[group][key]
    validate(result,FidelitySchema(assigned,source,draft).model_json_schema())
    originals={o['id']:o['text'] for o in source['objects']}
    fact_map={f['id']:f for f in facts['facts']};blocks={b['id']:b['markdown'] for b in draft['blocks']}
    return {'assessed_source_ids':list(result['source_checks']),
        'missing_inventory_information':[sid+': '+message for sid,row in result['source_checks'].items() for message in row['missing_information']],
        'fact_checks':[row|{'fact_id':fid,'source_quote':originals[fact_map[fid]['source_id']],
            'output_quote':blocks.get(row['block_id'],'')} for fid,row in result['fact_checks'].items()],
        'reverse_checks':[{k:v for k,v in row.items() if k!='source_ids'}|{'block_id':bid,'output_quote':blocks[bid],
            'evidence':[{'source_id':sid,'quote':originals[sid]} for sid in row['source_ids']]} for bid,row in result['reverse_checks'].items()],
        'findings':result['findings']}


def invalid_assignments(result,assigned,source,draft):
    """Keep valid keyed judgments and isolate only malformed keyed rows."""
    errors=list(Draft202012Validator(FidelitySchema(assigned,source,draft).model_json_schema()).iter_errors(result))
    if not errors:raise ValueError('没有需要重新核对的事实判断')
    rows=set()
    whole_part=False
    for error in errors:
        path=list(error.path)
        if (len(path)==1 and path[0] in {'source_checks','fact_checks','reverse_checks'}
                and error.validator=='required'):
            mapping={'source_checks':'source_ids','fact_checks':'fact_ids','reverse_checks':'block_ids'}
            group=path[0]
            for identity in assigned[mapping[group]]:
                if identity not in result.get(group,{}):rows.add((group,identity))
            continue
        if len(path)<2 or path[0] not in {'source_checks','fact_checks','reverse_checks'}:
            # A wrong group shape, a malformed findings array, or an extra
            # root field cannot be assigned to one row. Reassess this bounded
            # partition only; other completed partitions remain untouched.
            whole_part=True
            continue
        rows.add((path[0],path[1]))
    if whole_part:
        return {'source_checks':{},'fact_checks':{},'reverse_checks':{},'findings':[]},copy.deepcopy(assigned)
    retained=copy.deepcopy(result);missing={key:[] for key in ('source_ids','fact_ids','block_ids')}
    mapping={'source_checks':'source_ids','fact_checks':'fact_ids','reverse_checks':'block_ids'}
    for group,key in rows:
        if key in retained.get(group,{}):del retained[group][key]
        missing[mapping[group]].append(key)
    return retained,missing


def merge_raw_reassessment(retained,supplement,assigned,source,draft):
    """Complete one malformed contract repair without decoding it early."""
    validate(supplement,FidelitySchema({
        'source_ids':[x for x in assigned['source_ids'] if x not in retained['source_checks']],
        'fact_ids':[x for x in assigned['fact_ids'] if x not in retained['fact_checks']],
        'block_ids':[x for x in assigned['block_ids'] if x not in retained['reverse_checks']]},source,draft).model_json_schema())
    combined=copy.deepcopy(retained)
    for group in ('source_checks','fact_checks','reverse_checks'):
        overlap=set(combined[group])&set(supplement[group])
        if overlap:raise ValueError('事实核对协议补充覆盖了已经保留的判断')
        combined[group].update(copy.deepcopy(supplement[group]))
    combined['findings'].extend(copy.deepcopy(supplement['findings']))
    validate(combined,FidelitySchema(assigned,source,draft).model_json_schema())
    return combined


def merge_reassessment(retained,supplement,assigned,source,facts,draft):
    """Merge a disjoint reassessment without replacing earlier valid rows."""
    missing={kind:[identity for identity in ids if identity not in retained[group]] for kind,ids,group in (
        ('source_ids',assigned['source_ids'],'source_checks'),('fact_ids',assigned['fact_ids'],'fact_checks'),
        ('block_ids',assigned['block_ids'],'reverse_checks'))}
    validate(supplement,FidelitySchema(missing,source,draft).model_json_schema())
    combined=copy.deepcopy(retained)
    for group in ('source_checks','fact_checks','reverse_checks'):
        overlap=set(combined[group])&set(supplement[group])
        if overlap:raise ValueError('事实核对补充结果覆盖了已经保留的判断')
        combined[group].update(copy.deepcopy(supplement[group]))
    combined['findings'].extend(copy.deepcopy(supplement['findings']))
    return decode(combined,assigned,source,facts,draft)


def merge(parts):
    return {key:[item for part in parts for item in part[key]] for key in ('assessed_source_ids','missing_inventory_information','fact_checks','reverse_checks','findings')}
