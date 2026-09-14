from io import BytesIO
import copy
import json
import pytest

from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from sourceloom.app import create_app
from sourceloom.config import load_config
from sourceloom.demo import create_demo
from sourceloom.durable import Queue
from sourceloom.ingest import intake
from sourceloom.library_api import source_locations
from sourceloom.production import draft_text_view,bind_uncertainty_evidence,check_scoped_plan_addition,apply_coverage_patch,plan_review_needs_repair,unit_limit_for_inventory,restore_frozen_markdown_fences
from sourceloom.providers import parse_json
from sourceloom.store import Store
from sourceloom.writing import expand_response,protected_objects


def test_tree_projects_only_display_fields_and_keeps_trash_filter(tmp_path):
    store = Store(tmp_path)
    queue = Queue(store)
    project = store.create('长材料')
    store.change(project['id'], lambda p: p.update(inventory={'objects': [{'text': 'data' * 200000}]}))
    row = next(x for x in queue.tree()['documents'] if x['id'] == project['id'])
    assert row['title'] == '长材料'
    assert 'inventory' not in row
    assert row['active_job'] is None
    queue.edit_document(project['id'], 0, trashed=True)
    assert not queue.tree()['documents']
    assert queue.tree(True)['documents'][0]['id'] == project['id']


def test_long_pdf_is_rejected_before_job_creation(tmp_path):
    store = Store(tmp_path)
    queue = Queue(store)
    pdf = canvas.Canvas(BytesIO())
    stream = pdf._filename
    for page in range(26):
        pdf.drawString(40, 700, f'Page {page + 1}')
        pdf.showPage()
    pdf.save()
    inventory = intake(store, [('long.pdf', stream.getvalue())])
    estimate = queue.estimate(inventory)
    assert estimate['visual_batches'] == 9
    assert estimate['minimum_calls'] == 26
    assert not estimate['feasible']
    project = store.create('长论文')
    store.change(project['id'], lambda p: p.update(inventory=inventory))
    from sourceloom.store import Conflict
    import pytest
    with pytest.raises(Conflict, match='超过单篇'):
        queue.enqueue(project['id'], {'root': 'unused', 'package_digest': 'unused', 'instruction_digest': 'unused'})
    assert store.get(project['id'])['active_job'] is None


def test_subscription_limit_counts_accepted_calls_but_keeps_rejected_ledger_rows(tmp_path):
    from sourceloom.store import Conflict
    import pytest

    store=Store(tmp_path)
    project=store.create('订阅调用账本')
    store.reserve(project['id'],'rejected-request',0,{'channel':'router'},subscription_calls=1)
    store.settle('rejected-request',0,{'channel':'router','status':'rejected','http_status':400})
    store.reserve(project['id'],'accepted-request',0,{'channel':'router'},subscription_calls=1)
    store.settle('accepted-request',None,{'channel':'subscription','status':'succeeded'})
    with pytest.raises(Conflict,match='订阅测试请求次数'):
        store.reserve(project['id'],'next-request',0,{'channel':'router'},subscription_calls=1)
    assert len(store.costs(project['id']))==2


def test_source_locations_support_text_and_pdf_page(tmp_path):
    store = Store(tmp_path)
    demo = create_demo(store)
    locations = source_locations(demo)
    assert locations['block-1']['file'] == 'example.md'
    assert locations['block-1']['quote']
    assert locations['block-1']['page'] is None
    pdf = {'inventory': {'originals': [{'name': 'paper.pdf', 'sha256': 'key'}],
                         'objects': [{'id': 'page-one', 'kind': 'page', 'locator': 'paper.pdf/page[1]', 'text': 'First page'}]},
           'draft': {'blocks': [{'id': 'b', 'evidence': [{'source_id': 'page-one', 'quote': 'First page'}]}]}}
    assert source_locations(pdf)['b']['page'] == 1


def test_resolved_markdown_link_keeps_its_original_relative_target():
    from markdown_it import MarkdownIt

    inventory={'objects':[{'id':'link-one','kind':'link','text':'函数参考',
                           'original_target':'../Reference/Functions',
                           'target':'https://example.org/Reference/Functions'}]}
    literal=protected_objects(inventory)['link-one']
    rendered=MarkdownIt('commonmark').render(literal)
    assert 'https://example.org/Reference/Functions' in rendered
    assert '原始链接目标：../Reference/Functions' in rendered


def test_source_heading_must_map_to_a_real_heading_position():
    from sourceloom.checks import inspect_draft

    inventory={'frozen':True,'objects':[{'id':'source-heading','kind':'heading','text':'See also'}],
               'obligations':[],'resources':[]}
    block={'id':'b','unit_id':'u','kind':'source','markdown':'这里提到 See also',
           'obligation_ids':[],'object_ids':['source-heading'],'evidence':[]}
    issue_codes={f['code'] for f in inspect_draft(inventory,{'blocks':[block]},require_heading_structure=True)}
    assert 'heading_structure' in issue_codes
    block['markdown']='## 更多资料\n\n这里提到 See also'
    assert not any(f['code']=='heading_structure' for f in inspect_draft(inventory,{'blocks':[block]},require_heading_structure=True))


def test_cross_block_section_reference_keeps_every_block():
    def block(id, content):
        return {'id': id, 'unit_id': 'u', 'kind': 'explanation', 'obligation_ids': [],
                'object_ids': [], 'evidence': [], 'content': content}
    draft = {'encoding': 'flat_nodes_v1', 'blocks': [
        block('first', [{'type': 'section', 'node_id': 'heading', 'parent_id': '', 'heading': '如何开始'},
                        {'type': 'paragraph', 'node_id': 'intro', 'parent_id': 'heading', 'text': '先观察材料'}]),
        block('later', [{'type': 'paragraph', 'node_id': 'continuation', 'parent_id': 'heading',
                         'text': '接着记录结果'}])]}
    rendered = expand_response(draft)
    assert len(rendered['blocks']) == 2
    assert rendered['blocks'][0]['content'][0]['blocks'][0]['text'] == '先观察材料'
    assert rendered['blocks'][1]['content'][0]['text'] == '接着记录结果'


def test_missing_list_container_preserves_item_text():
    draft={'encoding':'flat_nodes_v1','blocks':[{
        'id':'item','unit_id':'u','kind':'explanation','content':[
            {'type':'list_item','node_id':'i','parent_id':'earlier-list','text':'只保留这一项'}]}]}
    rendered=expand_response(draft)
    assert rendered['blocks'][0]['content']==[{'type':'list','ordered':False,'items':[
        {'text':'只保留这一项'}]}]


def test_replan_view_preserves_all_authored_text_without_duplicate_bindings():
    original={'blocks':[{'id':'one','unit_id':'u1','kind':'explanation','markdown':'我们先观察',
                         'evidence':[{'source_id':'source','quote':'我们先观察'}]},
                        {'id':'two','unit_id':'u2','kind':'example','markdown':'接着处理例子',
                         'evidence':[{'source_id':'source','quote':'接着处理例子'}]}]}
    view=draft_text_view(original)
    assert [b['markdown'] for b in view['blocks']]==[b['markdown'] for b in original['blocks']]
    assert all('evidence' not in b for b in view['blocks'])
    assert [b['id'] for b in draft_text_view(original,['u2'])['blocks']]==['two']


def test_inexact_uncertainty_excerpt_binds_untouched_source_object():
    source={'objects':[{'id':'s','text':'15. URI\'s  . . . . . . 13'}]}
    received={'uncertainty_assessments':[{'id':'u','evidence':[
        {'source_id':'s','quote':'15. URI\'s . . . . 13'}]}]}
    corrected,record=bind_uncertainty_evidence(received,source)
    assert corrected['uncertainty_assessments'][0]['evidence'][0]['quote']==source['objects'][0]['text']
    assert record==[{'uncertainty_id':'u','source_id':'s'}]
    assert received['uncertainty_assessments'][0]['evidence'][0]['quote']!='15. URI\'s  . . . . . . 13'


def test_saved_plan_review_recheck_keeps_job_and_call_ledger(tmp_path):
    store=Store(tmp_path)
    queue=Queue(store)
    project=store.create('修复规划')
    store.change(project['id'],lambda p:p.update(inventory={'objects':[{'id':'s','kind':'text','text':'来源'}],
                                                 'digest':'saved'}))
    job=queue.enqueue(project['id'],{'root':'saved','package_digest':'saved','instruction_digest':'saved'})
    job.update(status='needs_attention',stage='teaching_replan_review',repair_rounds=1,
               results={'teaching-replan-review-1':{'status':'needs_repair'}},quality_issues=['旧稿仍有问题'])
    with store.connect() as cx:
        p=store.get(project['id'])
        p.update(active_job=None,state='needs_attention')
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),p['id']))
        cx.execute("UPDATE jobs SET status='needs_attention',body=? WHERE id=?",
                   (json.dumps(job,ensure_ascii=False),job['id']))
        cx.execute("UPDATE production_control SET status='needs_attention' WHERE id=?",(job['id'],))
    resumed=queue.recheck_plan_review(job['id'])
    assert resumed['id']==job['id'] and resumed['calls']==[]
    assert resumed['plan_review_rechecks'][0]['old_issues']==['旧稿仍有问题']
    assert store.get(project['id'])['active_job']==job['id']
    from sourceloom.store import Conflict
    import pytest
    with pytest.raises(Conflict):queue.recheck_plan_review(job['id'])
    again=store.job(job['id'])
    again.update(status='needs_attention',results=again['results']|{
        'teaching-replan-review-1-route-v2':{'status':'ready','issues':['新规划覆盖了旧问题']}})
    with store.connect() as cx:
        p=store.get(project['id'])
        p.update(active_job=None,state='needs_attention')
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),p['id']))
        cx.execute("UPDATE jobs SET status='needs_attention',body=? WHERE id=?",
                   (json.dumps(again,ensure_ascii=False),again['id']))
        cx.execute("UPDATE production_control SET status='needs_attention' WHERE id=?",(again['id'],))
    adjudication=queue.recheck_plan_review(job['id'])
    assert adjudication['plan_claim_recheck']['old_issues']==['新规划覆盖了旧问题']
    with pytest.raises(Conflict):queue.recheck_plan_review(job['id'])
    again=store.job(job['id'])
    again.update(status='needs_attention',results=again['results']|{
        'teaching-replan-decision-1':{'decisions':[{'claim_index':0,'verdict':'unknown'}]}})
    with store.connect() as cx:
        p=store.get(project['id'])
        p.update(active_job=None,state='needs_attention')
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),p['id']))
        cx.execute("UPDATE jobs SET status='needs_attention',body=? WHERE id=?",
                   (json.dumps(again,ensure_ascii=False),again['id']))
        cx.execute("UPDATE production_control SET status='needs_attention' WHERE id=?",(again['id'],))
    original_check=queue.recheck_plan_review(job['id'])
    assert original_check['plan_original_recheck']['unknown_indices']==[0]
    with pytest.raises(Conflict):queue.recheck_plan_review(job['id'])


def test_recheck_uses_exact_saved_candidate_as_new_version_baseline(tmp_path):
    store=Store(tmp_path)
    queue=Queue(store)
    project=store.create('候选继续')
    original={'objects':[{'id':'s','kind':'text','text':'来源'}],'digest':'original'}
    store.change(project['id'],lambda p:p.update(inventory=original))
    job=queue.enqueue(project['id'],{'root':'saved','package_digest':'saved','instruction_digest':'saved'})
    candidate={'blocks':[{'id':'b','markdown':'仍需复核'}]}
    new_inventory=original|{'digest':'after-review'}
    job.update(status='needs_attention',stage='teaching_replan_review',repair_rounds=1,
               results={'teaching-replan-review-1':{'status':'needs_repair'}},
               draft=candidate,inventory=new_inventory,quality_issues=['旧稿问题'])
    with store.connect() as cx:
        p=store.get(project['id'])
        p.update(active_job=None,state='needs_attention',revision=1,draft=candidate,inventory=new_inventory,
                 production={'job':job['id'],'status':'needs_attention','revision':1})
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),p['id']))
        cx.execute("UPDATE jobs SET status='needs_attention',body=? WHERE id=?",
                   (json.dumps(job,ensure_ascii=False),job['id']))
        cx.execute("UPDATE production_control SET status='needs_attention' WHERE id=?",(job['id'],))
    resumed=queue.recheck_plan_review(job['id'])
    assert resumed['base_revision']==1 and resumed['source_digest']=='after-review'
    assert resumed['continued_from_saved_candidate']['revision']==0


@pytest.mark.parametrize('fresh',[False,True])
def test_saved_teaching_review_recheck_preserves_candidate_and_prior_calls(tmp_path,fresh):
    from sourceloom.store import Conflict
    import pytest
    store=Store(tmp_path)
    queue=Queue(store)
    project=store.create('相邻关系复核')
    original={'objects':[{'id':'s','kind':'metadata','text':'页面登记'}],'digest':'source'}
    store.change(project['id'],lambda p:p.update(inventory=original))
    job=queue.enqueue(project['id'],{'root':'saved','package_digest':'saved','instruction_digest':'saved'})
    candidate={'blocks':[{'id':'b','kind':'source','markdown':'页面登记'}]}
    job.update(status='needs_attention',stage='teaching',draft=candidate,inventory=original,
               calls=[{'id':'old','status':'completed'}],
               results={'teaching-contract-0-0':{'assessed_block_ids':['b']}},
               quality_issues=['教学审查证据仍无法核对：相邻关系缺失'])
    with store.connect() as cx:
        p=store.get(project['id'])
        p.update(active_job=None,state='needs_attention',revision=1,draft=candidate,inventory=original,
                 production={'job':job['id'],'status':'needs_attention','revision':1})
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),p['id']))
        cx.execute("UPDATE jobs SET status='needs_attention',body=? WHERE id=?",(json.dumps(job,ensure_ascii=False),job['id']))
        cx.execute("UPDATE production_control SET status='needs_attention' WHERE id=?",(job['id'],))
    resumed=queue.recheck_teaching_review(job['id'],fresh=fresh)
    assert resumed['id']==job['id'] and resumed['calls']==job['calls']
    assert resumed['base_revision']==1
    assert bool(resumed.get('use_saved_teaching_contract')) is not fresh
    assert bool(resumed.get('teaching_evidence_retry')) is fresh
    assert resumed['teaching_contract_rechecks'][0]['saved_key']=='teaching-contract-0-0'
    with pytest.raises(Conflict):queue.recheck_teaching_review(job['id'])


def test_rejected_teaching_replan_can_be_repaired_once_without_erasing_the_candidate(tmp_path):
    from sourceloom.store import Conflict
    import pytest

    store=Store(tmp_path)
    queue=Queue(store)
    project=store.create('修复后的规划仍有缺口')
    inventory={'objects':[{'id':'s','kind':'text','text':'来源'}],'digest':'frozen'}
    store.change(project['id'],lambda p:p.update(inventory=inventory))
    job=queue.enqueue(project['id'],{'root':'saved','package_digest':'saved','instruction_digest':'saved'})
    draft={'blocks':[{'id':'b','markdown':'完整候选'}]}
    job.update(status='needs_attention',stage='teaching_replan_review',repair_rounds=1,
               draft=draft,inventory=inventory,quality_issues=['规划没有真正调整讲解顺序'],
               regeneration={'unit_ids':['u1'],'original_plan':{},'original_draft':draft,'findings':[]},
               results={'teaching-replan-review-1-route-v2':{'status':'needs_repair'}})
    with store.connect() as cx:
        p=store.get(project['id'])
        p.update(active_job=None,state='needs_attention',revision=1,draft=draft,
                 production={'job':job['id'],'status':'needs_attention','revision':1})
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),p['id']))
        cx.execute("UPDATE jobs SET status='needs_attention',body=? WHERE id=?",
                   (json.dumps(job,ensure_ascii=False),job['id']))
        cx.execute("UPDATE production_control SET status='needs_attention' WHERE id=?",(job['id'],))
    resumed=queue.repair_teaching_replan(job['id'])
    assert resumed['stage']=='teaching_replan_repair'
    assert resumed['draft']==draft and resumed['base_revision']==1
    assert resumed['teaching_replan_attempts']==1
    assert resumed['teaching_replan_repair_issues']==['规划没有真正调整讲解顺序']
    assert store.get(project['id'])['active_job']==job['id']
    with pytest.raises(Conflict):queue.repair_teaching_replan(job['id'])


def test_known_truncation_retries_once_only_with_higher_cap(tmp_path):
    store=Store(tmp_path)
    queue=Queue(store)
    project=store.create('长清单',budget=.20)
    store.change(project['id'],lambda p:p.update(inventory={'objects':[{'id':'s','kind':'text','text':'来源'}],
                                                 'digest':'saved'}))
    job=queue.enqueue(project['id'],{'root':'saved','package_digest':'saved','instruction_digest':'saved'})
    store.reserve(project['id'],'paid-call',.02,{'channel':'openai-compatible'})
    store.settle('paid-call',.01,{'status':'truncated'})
    wire=store.blob(json.dumps({'max_tokens':14000}).encode())
    job.update(status='failed',stage='inventory',pending='inventory',
               calls=[{'id':'paid-call','status':'truncated','finish_reason':'length','wire_request_blob':wire}])
    with store.connect() as cx:
        p=store.get(project['id'])
        p.update(active_job=None,state='failed')
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),p['id']))
        cx.execute("UPDATE jobs SET status='failed',body=? WHERE id=?",(json.dumps(job,ensure_ascii=False),job['id']))
        cx.execute("UPDATE production_control SET status='failed' WHERE id=?",(job['id'],))
    from sourceloom.store import Conflict
    import pytest
    with pytest.raises(Conflict):queue.retry_truncated_inventory(job['id'],14000)
    resumed=queue.retry_truncated_inventory(job['id'],40000)
    assert resumed['id']==job['id'] and len(resumed['calls'])==1
    assert resumed['inventory_cap_retry']['prior_limit']==14000
    with pytest.raises(Conflict):queue.retry_truncated_inventory(job['id'],50000)


def test_static_assets_revalidate_and_estimate_is_exposed(tmp_path):
    app = create_app(load_config() | {'data_dir': str(tmp_path), 'provider': 'manual'})
    with TestClient(app) as client:
        response = client.get('/static/source-focus.js')
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'private, max-age=0, must-revalidate'
        cached = client.get('/static/source-focus.js', headers={'If-None-Match': response.headers['etag']})
        assert cached.status_code == 304
        demo = client.post('/api/demo', headers={'X-SourceLoom': '1'}).json()
        estimate = client.get('/api/projects/' + demo['id'] + '/estimate').json()
        assert estimate['source_chars'] > 0 and estimate['feasible']


def test_running_revision_summary_matches_the_saved_draft_actually_served(tmp_path):
    from sourceloom.store import digest
    app=create_app(load_config()|{'data_dir':str(tmp_path),'provider':'manual'})
    store=Store(tmp_path)
    project=store.create('正在修订')
    old={'blocks':[{'id':'old','markdown':'已经保存的完整候选'}]}
    partial={'blocks':[{'id':'new','markdown':'正在生成的局部内容'}]}
    job={'id':'revising','project':project['id'],'role':'production','status':'running',
         'stage':'writer','created':1,'calls':[],'draft':partial}
    with store.connect() as cx:
        saved=store.get(project['id'])
        saved.update(draft=old,active_job=job['id'])
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(saved,ensure_ascii=False),project['id']))
        cx.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',
                   (job['id'],project['id'],'production','running',1,json.dumps(job,ensure_ascii=False)))
    with TestClient(app) as client:
        headers={'X-SourceLoom':'1'}
        summary=client.get('/api/projects/'+project['id']+'/production',headers=headers)
        assert summary.status_code==200
        assert summary.json()['output_digest']==digest('已经保存的完整候选\n'.encode())
        output=client.get('/api/projects/'+project['id']+'/output?format=markdown',headers=headers)
        assert output.status_code==200 and output.text=='已经保存的完整候选\n'


def test_strict_tool_surplus_closing_brace_can_be_recovered_without_resubmission():
    assert parse_json('{"decisions":[]} }') == {'decisions': []}
    import pytest
    with pytest.raises(json.JSONDecodeError):
        parse_json('{"decisions":[]} trailing words')
    with pytest.raises(json.JSONDecodeError):
        parse_json('{"decisions":[]}}}')


def test_coverage_repair_cannot_remove_previous_fact_or_object_assignment():
    old={'units':[{'id':'U1','obligation_ids':['f1'],'object_ids':['s1']},
                  {'id':'U2','obligation_ids':['f2'],'object_ids':['s2']}]}
    added={'units':[{'id':'U1','obligation_ids':['f1','f3'],'object_ids':['s1','s3']},
                    {'id':'U2','obligation_ids':['f2'],'object_ids':['s2']}]}
    check_scoped_plan_addition(old,added)
    import pytest
    removed=json.loads(json.dumps(added))
    removed['units'][0]['obligation_ids']=['f3']
    with pytest.raises(ValueError,match='删掉'):check_scoped_plan_addition(old,removed)
    reordered={'units':list(reversed(added['units']))}
    with pytest.raises(ValueError,match='身份或顺序'):check_scoped_plan_addition(old,reordered)


def test_rfc_plain_text_page_furniture_keeps_exact_bytes_as_metadata(tmp_path):
    raw=('Network Working Group                           A. Author\n'
         'Request for Comments: 1234                      January 2001\n'
         '\n'
         'A real technical requirement remains here.\n'
         '\n'
         'Author          Best Current Practice          [Page 1]\n'
         '\n'
         'RFC 1234                    Example             January 2001\n').encode()
    inventory=intake(Store(tmp_path),[('sample.txt',raw)])
    objects=inventory['objects']
    assert [o['kind'] for o in objects]==['metadata','text','metadata','metadata']
    assert '\n\n'.join(o['text'] for o in objects)==raw.decode()
    ordinary=intake(Store(tmp_path/'ordinary'),[('note.txt',b'Example          [Page 1]\n')])
    assert ordinary['objects'][0]['kind']=='text'


def test_markdown_code_fence_keeps_extra_original_info_after_language(tmp_path):
    raw=b'## Example\n\n```js-nolint example-bad\nfunction bad() {}\n```\n'
    inventory=intake(Store(tmp_path),[('lesson.md',raw)])
    code=next(o for o in inventory['objects'] if o['kind']=='code')
    assert code['language']=='js-nolint'
    assert code['fence_info']=='js-nolint example-bad'
    assert code['fence_raw']==b'```js-nolint example-bad\nfunction bad() {}\n```\n'.decode()
    assert protected_objects(inventory)[code['id']]==code['fence_raw']


def test_old_frozen_inventory_recovers_fence_only_in_the_rendering_copy(tmp_path):
    store=Store(tmp_path)
    raw=b'```js-nolint example-bad\nfunction bad() {}\n```\n'
    frozen=intake(store,[('lesson.md',raw)])
    old_code=next(o for o in frozen['objects'] if o['kind']=='code')
    old_code.pop('fence_raw');old_code.pop('fence_info')
    unit={'objects':[copy.deepcopy(old_code)]}
    recovered=restore_frozen_markdown_fences(unit,frozen,store)
    assert recovered==[old_code['id']]
    assert unit['objects'][0]['fence_raw']==raw.decode()
    assert 'fence_raw' not in old_code


def test_small_coverage_patch_preserves_full_plan_and_binds_missing_sources():
    plan={'units':[{'id':'U1','stages':['先看问题','解释办法'],'obligation_ids':['f1'],'object_ids':['s1']},
                   {'id':'U2','stages':['接着验证'],'obligation_ids':['f2'],'object_ids':['s2']}]}
    patch={'assignments':[{'fact_id':'f3','unit_id':'U1'},{'fact_id':'f4','unit_id':'U1'}],
           'stage_additions':[{'unit_id':'U1','after_stage':'先看问题','new_stage':'补齐被遗漏的前提'}],
           'rationale':'两个事实共同说明第一单元的前提'}
    result=apply_coverage_patch(plan,patch,{'f3','f4'},[
        {'id':'f3','source_id':'s3'},{'id':'f4','source_id':'s4'}])
    assert result['units'][0]['stages']==['先看问题','补齐被遗漏的前提','解释办法']
    assert result['units'][0]['obligation_ids']==['f1','f3','f4']
    assert result['units'][0]['object_ids']==['s1','s3','s4']
    assert plan['units'][0]['obligation_ids']==['f1']
    import pytest
    with pytest.raises(ValueError,match='全部遗漏事实'):
        apply_coverage_patch(plan,patch,{'f3'},[{'id':'f3','source_id':'s3'},{'id':'f4','source_id':'s4'}])


def test_known_zero_dispatch_campaign_circuit_can_resume_same_job(tmp_path):
    store=Store(tmp_path)
    queue=Queue(store)
    project=store.create('未发送的真实材料')
    store.change(project['id'],lambda p:p.update(inventory={'objects':[{'id':'s','kind':'text','text':'内容'}],
                                                 'digest':'saved'}))
    job=queue.enqueue(project['id'],{'root':'saved','package_digest':'saved','instruction_digest':'saved'})
    job.update(status='failed',pending='inventory',error='本批同类失败已连续发生两次，先修正原因，未发送新请求')
    with store.connect() as cx:
        p=store.get(project['id'])
        p.update(active_job=None,state='failed')
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),p['id']))
        cx.execute("UPDATE jobs SET status='failed',body=? WHERE id=?",(json.dumps(job,ensure_ascii=False),job['id']))
        cx.execute("UPDATE production_control SET status='failed' WHERE id=?",(job['id'],))
    resumed=queue.retry_validation(job['id'])
    assert resumed['id']==job['id'] and resumed['status']=='queued'
    assert resumed['calls']==[] and 'pending' not in resumed
    assert resumed['preflight_stops'][0]['dispatched'] is False


def test_zero_dispatch_circuit_after_pending_cleanup_can_resume(tmp_path):
    store=Store(tmp_path)
    queue=Queue(store)
    project=store.create('已清理待发标记')
    store.change(project['id'],lambda p:p.update(inventory={'objects':[{'id':'s','kind':'text','text':'内容'}],
                                                 'digest':'saved'}))
    job=queue.enqueue(project['id'],{'root':'saved','package_digest':'saved','instruction_digest':'saved'})
    job.update(status='failed',error='本批同类失败已连续发生两次，先修正原因，未发送新请求',
               preflight_stops=[{'key':'inventory','dispatched':False}])
    with store.connect() as cx:
        p=store.get(project['id'])
        p.update(active_job=None,state='failed')
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),p['id']))
        cx.execute("UPDATE jobs SET status='failed',body=? WHERE id=?",(json.dumps(job,ensure_ascii=False),job['id']))
        cx.execute("UPDATE production_control SET status='failed' WHERE id=?",(job['id'],))
    assert queue.retry_validation(job['id'])['status']=='queued'


def test_plan_review_status_cannot_override_independently_rejected_issue():
    review={'status':'needs_repair','issues':['一个被裁定不成立的问题']}
    assert not plan_review_needs_repair(review,False,6,6)
    assert plan_review_needs_repair(review,True,6,6)
    assert plan_review_needs_repair(review,False,7,6)
    assert plan_review_needs_repair({'status':'needs_repair','issues':[]},False,6,6)


def test_short_text_route_uses_fewer_units_without_restricting_complex_or_long_sources():
    short={'objects':[{'kind':'text','text':'文字'*1400}]}
    long={'objects':[{'kind':'text','text':'文字'*2600}]}
    visual={'objects':[{'kind':'text','text':'文字'*1400},{'kind':'image','text':''}]}
    assert unit_limit_for_inventory(short,6)==3
    assert unit_limit_for_inventory(long,6)==6
    assert unit_limit_for_inventory(visual,6)==6
