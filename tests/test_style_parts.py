import copy
import pytest
from sourceloom.style_parts import partitions,validate_part,merge
from tests.test_production import skill


def test_review_partitions_cover_every_assignment_once_with_whole_source_and_draft():
    payload={'rule_catalog':['FMT-'+str(i) for i in range(7)],
             'mechanical_findings':[{'id':'f1','quote':'exact'}],
             'mechanical_candidates':[{'id':'c'+str(i),'quote':'original'} for i in range(5)],
             'source':{'text':'entire source'},'draft':{'text':'entire candidate'},'protected_originals':['verbatim']}
    original=copy.deepcopy(payload);parts=partitions(payload,4)
    assert len(parts)==4 and payload==original
    for key in ('rule_catalog','mechanical_findings','mechanical_candidates'):
        assert [item for p in parts for item in p[key]]==payload[key]
    for p in parts:
        for key in ('source','draft','protected_originals'):assert p[key]==payload[key]
        assert sum(len(p[k]) for k in ('rule_catalog','mechanical_findings','mechanical_candidates'))<=4


def test_partition_cannot_omit_or_duplicate_an_assigned_check():
    payload={'rule_catalog':['A'],'mechanical_findings':[],'mechanical_candidates':[{'id':'c'}]}
    review={'assessments':[{'rule_ids':['A'],'status':'not_applicable','reason':'Absent','block_ids':[]}],
            'mechanical_assessments':[{'candidate_id':'c','verdict':'not_violation','reason':'Exact original'}],'findings':[]}
    assert validate_part(review,payload)['assessments'][0]['rule_ids']==['A']
    for changed in [{'mechanical_assessments':[]},{'assessments':review['assessments']*2}]:
        with pytest.raises(ValueError):validate_part(review|changed,payload)
    combined=merge([validate_part(review,payload)])
    assert combined['mechanical_assessments']==review['mechanical_assessments']


def test_indexed_review_contract_requires_exact_checks_without_invented_rule_arrays():
    from sourceloom.style_parts import IndexedReviewSchema,decode_indexed
    from jsonschema import ValidationError
    from sourceloom.providers import deepseek_schema
    payload={'rule_catalog':[],'mechanical_findings':[],'mechanical_candidates':[{'id':'last-1'},{'id':'last-2'}]}
    verdict={'verdict':'unknown','reason':'Actual evidence still needs checking'}
    result={'checks_by_id':{'last-1':verdict,'last-2':verdict},'findings':[]}
    parsed=decode_indexed(result,payload)
    assert parsed['assessments']==[] and all(x['verdict']=='unknown' for x in parsed['mechanical_assessments'])
    schema=deepseek_schema(IndexedReviewSchema(payload).model_json_schema())
    assert set(schema['required'])=={'checks_by_id','findings'}
    assert schema['properties']['checks_by_id']['required']==['last-1','last-2']
    for changed in [result|{'assessments':[]},result|{'rules_by_id':{}},result|{'checks_by_id':{'last-1':verdict}}]:
        with pytest.raises(ValidationError):decode_indexed(changed,payload)
    assert result['checks_by_id']['last-1']==verdict


def test_indexed_supplement_preserves_returned_verdicts_and_cannot_replace_them():
    from sourceloom.style_parts import missing_assignments,merge_indexed
    from jsonschema import ValidationError
    payload={'rule_catalog':[],'mechanical_findings':[{'id':'first'}],'mechanical_candidates':[{'id':'last'}]}
    verdict={'verdict':'violation','reason':'Confirmed original defect'}
    original={'findings':[],'checks_by_id':{'first':verdict}}
    missing=missing_assignments(original,payload)
    assert missing['mechanical_findings']==[] and missing['mechanical_candidates']==[{'id':'last'}]
    supplement={'findings':[],'checks_by_id':{'last':{'verdict':'unknown','reason':'Still unclear'}}}
    merged=merge_indexed(original,supplement,payload)
    assert merged['mechanical_assessments'][0]==verdict|{'candidate_id':'first'}
    assert original=={'findings':[],'checks_by_id':{'first':verdict}}
    supplement['checks_by_id']['first']={'verdict':'not_violation','reason':'Attempted overwrite'}
    with pytest.raises(ValidationError):merge_indexed(original,supplement,payload)
    with pytest.raises(ValueError):missing_assignments(original|{'invented':'extra'},payload)


def test_indexed_empty_nested_findings_are_inert_but_real_feedback_is_not_discarded():
    from sourceloom.style_parts import decode_indexed
    from jsonschema import ValidationError
    payload={'rule_catalog':['FMT-044'],'mechanical_findings':[],'mechanical_candidates':[]}
    row={'status':'fail','reason':'Unpaired English remains','block_ids':['b1'],'findings':[]}
    original={'findings':[],'rules_by_id':{'FMT-044':row}}
    before=copy.deepcopy(original)
    result=decode_indexed(original,payload)
    assert result['assessments'][0]['status']=='fail' and original==before
    for extra in [{'findings':[{'description':'Must retain this finding'}]}, {'invented':[]}]:
        invalid=copy.deepcopy(original);invalid['rules_by_id']['FMT-044'].update(extra)
        with pytest.raises(ValidationError):decode_indexed(invalid,payload)


def test_known_billed_truncated_fallback_can_continue_once_with_disjoint_review_parts(tmp_path,skill):
    from tests.test_production import prepared
    s,q,p,bundle=prepared(tmp_path,skill);job=q.enqueue(p['id'],bundle)
    call={'id':'paid','role':'style__fallback','status':'truncated','finish_reason':'length','response_blob':s.blob(b'{}')}
    job.update(status='failed',stage='style',pending='style-0__fallback',calls=[call])
    s.put_job(job);s.change(p['id'],lambda p:p.update(active_job=None))
    with s.connect() as cx:
        cx.execute('INSERT INTO spending(id,project,reserved,actual,created,body) VALUES(?,?,?,?,?,?)',('paid',p['id'],.2,.1,1,'{}'))
    resumed=q.retry_validation(job['id'])
    assert resumed['style_parts_enabled'] and resumed['style_partition_continuation']['original_call']=='paid'
    assert not resumed.get('pending') and resumed['calls']==[call]
    resumed.update(status='failed',pending='style-0-part-1__fallback');s.put_job(resumed)
    s.change(p['id'],lambda p:p.update(active_job=None))
    with pytest.raises(ValueError,match='不能重新发送'):q.retry_validation(job['id'])
