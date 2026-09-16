import copy
import pytest
from tests.test_production import skill
from bs4 import BeautifulSoup
from sourceloom.media import image_markup,reading_draft
from sourceloom.export import render


def image_project(kind='page'):
    obj={'id':'s1','kind':kind,'text':'OPEN ACCESS\n\nA paragraph & "quoted" text\n\nThe next paragraph',
         'locator':'paper.pdf/page/1','resource_id':'image1'}
    draft={'blocks':[{'id':'b1','unit_id':'u1','kind':'object','object_ids':['s1'],'embedded_object_ids':['s1'],
                     'markdown':'<div align="center">\n\n'+image_markup(obj,legacy=True)+'\n\n</div>'}]}
    return {'inventory':{'objects':[obj]},'draft':draft}


def test_existing_multiline_image_renders_and_original_draft_is_unchanged():
    p=image_project();before=copy.deepcopy(p)
    output=render(p,lambda key:'/files/'+key)
    doc=BeautifulSoup(output,'html.parser')
    assert len(doc.find_all('img'))==1 and doc.img['src']=='/files/image1'
    assert doc.img.find_parent('details') and not doc.details.has_attr('open')
    assert '<img ' not in doc.get_text() and 'OPEN ACCESS' not in doc.get_text()
    assert p==before and reading_draft(p)['blocks'][0]['markdown']!=p['draft']['blocks'][0]['markdown']


def test_real_illustration_is_collapsed_and_keeps_safe_description():
    p=image_project('image');doc=BeautifulSoup(render(p,lambda k:'/files/'+k),'html.parser')
    assert doc.img and doc.img.find_parent('details') and not doc.img.find_parent('details').has_attr('open')
    assert '\n' not in doc.img['alt'] and 'quoted' in doc.img['alt']


def test_table_relative_images_resolve_only_to_saved_unambiguous_resources():
    p=image_project('image');p['inventory']['objects'][0]['target']='../img/picture.png'
    raw='<table><tr><td><img src="../img/picture.png"></td></tr></table>'
    p['inventory']['objects'].append({'id':'table','kind':'table','text':'Picture','raw':raw,'locator':'source/table'})
    p['draft']['blocks'][0].update(markdown=raw,object_ids=['table'],embedded_object_ids=['table'])
    doc=BeautifulSoup(render(p,lambda key:'/files/'+key),'html.parser')
    assert doc.img['src']=='/files/image1'
    p['inventory']['objects'].append(p['inventory']['objects'][0]|{'id':'other','resource_id':'other-image'})
    assert not BeautifulSoup(render(p),'html.parser').img.has_attr('src')


def test_source_image_cannot_turn_an_asset_path_into_an_arbitrary_same_origin_request():
    p=image_project();p['inventory']['objects'][0].update(kind='table',raw='<table><tr><td><img src="assets/../../private"></td></tr></table>')
    p['draft']['blocks'][0]['markdown']=p['inventory']['objects'][0]['raw']
    assert not BeautifulSoup(render(p),'html.parser').img.has_attr('src')


def test_protected_multiline_table_html_keeps_cells_and_removes_active_markup():
    raw='<table><tr><td>A\n\nB</td><td><img src="assets/image1" alt="two\n\nlines" onerror="alert(1)"></td></tr></table>'
    p=image_project();p['inventory']['objects'][0].update(kind='table',raw=raw)
    p['draft']['blocks'][0]['markdown']=raw
    doc=BeautifulSoup(render(p,lambda k:'/files/'+k),'html.parser')
    assert len(doc.select('td'))==2 and doc.img['src']=='/files/image1'
    assert doc.td.get_text()=='A\n\nB' and not doc.img.has_attr('onerror')


def test_image_projection_does_not_change_a_verbatim_code_example():
    p=image_project();p['draft']['blocks'][0]['embedded_object_ids']=[]
    p['draft']['blocks'][0]['markdown']='```html\n'+image_markup(p['inventory']['objects'][0],legacy=True)+'\n```'
    assert reading_draft(p)==p['draft']


def test_reading_projection_is_idempotent_and_legacy_images_are_not_editable():
    from sourceloom.review_context import editable_lines
    p=image_project();projected=p|{'draft':reading_draft(p)}
    assert reading_draft(projected)==projected['draft']
    assert not any('OPEN ACCESS' in row['text'] or 'quoted' in row['text'] for row in editable_lines(p['draft'],p['inventory'],{'b1'}))


def test_explicit_bibliographic_section_folds_without_changing_saved_words_or_anchors():
    p=image_project();p['draft']['blocks'][0].update(kind='explanation',object_ids=[],embedded_object_ids=[],
        markdown='## Author Affiliations\n\nA. Researcher <sup>1</sup>\n\n1 Example University')
    before=copy.deepcopy(p);doc=BeautifulSoup(render(p),'html.parser')
    assert doc.section['data-readweave-anchor-id']=='b1'
    assert doc.details and not doc.details.has_attr('open')
    assert doc.details.h2.text=='Author Affiliations' and doc.details.sup.text=='1'
    assert 'Example University' in doc.details.get_text() and p==before
    projection=reading_draft(p)
    assert reading_draft(p|{'draft':projection})==projection
    for text in ['## Ordinary English\n\nA long source paragraph',
                 '## Author Affiliations\n\nA\n\n## Research Findings\n\nB',
                 '```markdown\n## Author Affiliations\n```']:
        p['draft']['blocks'][0]['markdown']=text
        assert reading_draft(p)==p['draft']


def test_native_money_never_relabels_historical_dollars_or_subscription_allocation():
    from sourceloom.money import summary,usage_cost
    assert usage_cost({'prompt_tokens':1000,'completion_tokens':200,'prompt_cache_hit_tokens':500}, {'input':9,'cached_input':.3,'output':27})==pytest.approx(.01005)
    rows=[{'actual':1,'body':{}},{'actual':0,'body':{'channel':'subscription','actual_cny':0,'subscription_allocation_usd':.4}},
          {'actual':.01,'body':{'actual_cny':.08}},{'actual':None,'body':{'reserved_cny':.1}}]
    assert summary(rows)=={'known_cny':.08,'reserved_cny':.1,'legacy_currency_records':1,'subscription_calls':1}


def test_native_reservations_are_atomic_and_subscription_does_not_spend_cash_budget(tmp_path):
    from sourceloom.store import Store,Conflict
    s=Store(tmp_path);p=s.create('Budget',budget=1);s.change(p['id'],lambda p:p.update(budget_cny=.1))
    s.reserve(p['id'],'a',.01,{'reserved_cny':.06})
    with pytest.raises(Conflict,match='人民币费用保护'):s.reserve(p['id'],'b',.01,{'reserved_cny':.06})
    s.reserve(p['id'],'sub',0,{'channel':'subscription','reserved_cny':0})
    s.settle('a',.005,{'actual_cny':.03})
    s.reserve(p['id'],'c',.01,{'reserved_cny':.06})


def test_review_evidence_ids_resolve_exact_lines_and_reject_invented_quotations():
    from sourceloom.style_parts import evidence_catalog,decode_indexed
    from jsonschema import ValidationError
    draft={'blocks':[{'id':'b1','markdown':'原句一\n\n原句二'}]}
    payload={'draft':draft,'evidence_catalog':evidence_catalog(draft),'rule_catalog':['FMT-044'],'mechanical_findings':[],'mechanical_candidates':[]}
    raw={'findings':[],'rules_by_id':{'FMT-044':{'status':'fail','reason':'实际缺陷','evidence_ids':['e1-3']}}}
    result=decode_indexed(raw,payload)
    assert result['assessments'][0]['quotes']==['原句二'] and result['assessments'][0]['block_ids']==['b1']
    assert raw['rules_by_id']['FMT-044']['evidence_ids']==['e1-3']
    raw['rules_by_id']['FMT-044']['evidence_ids']=['invented']
    with pytest.raises(ValidationError):decode_indexed(raw,payload)


@pytest.mark.parametrize('status,code,host,expected',[(429,'rate_limit_exceeded','opencode.ai',False),(429,'quota_exceeded','opencode.ai',True),(402,'quota_exhausted','opencode.ai',True),(500,'quota_exhausted','opencode.ai',False),(429,'quota_exceeded','example.com',False)])
def test_only_explicit_subscription_exhaustion_can_trigger_paid_fallback(status,code,host,expected):
    import httpx
    from sourceloom.providers import confirmed_subscription_exhaustion
    response=httpx.Response(status,json={'error':{'code':code}})
    assert confirmed_subscription_exhaustion(response,'https://'+host+'/v1/chat/completions')==expected


def test_documented_go_error_envelope_is_distinct_from_generic_rate_limiting():
    import httpx
    from sourceloom.providers import confirmed_subscription_exhaustion
    endpoint='https://opencode.ai/zen/go/v1/chat/completions'
    assert confirmed_subscription_exhaustion(httpx.Response(429,json={'type':'GoUsageLimitError','message':'5-hour usage limit reached'}),endpoint)
    assert not confirmed_subscription_exhaustion(httpx.Response(429,json={'type':'RateLimitError','message':'Rate limit exceeded'}),endpoint)


def test_invalid_review_quote_rechecks_only_its_row_without_overwriting_valid_failures():
    from sourceloom.style_parts import invalid_assignments,merge_indexed,evidence_catalog
    draft={'blocks':[{'id':'b','markdown':'Actual text'}]}
    payload={'draft':draft,'rule_catalog':['a','b'],'mechanical_findings':[],'mechanical_candidates':[],'evidence_catalog':evidence_catalog(draft)}
    valid={'status':'fail','reason':'Keep this genuine failure','evidence_ids':['e1-1']}
    original={'rules_by_id':{'a':valid,'b':{'status':'pass','reason':'Wrong address','evidence_ids':['invented']}},'findings':[]}
    retained,missing=invalid_assignments(original,payload)
    assert missing['rule_catalog']==['b'] and retained['rules_by_id']['a']==valid
    fixed={'rules_by_id':{'b':{'status':'unknown','reason':'Cannot prove compliance','evidence_ids':['e1-1']}},'findings':[]}
    result=merge_indexed(retained,fixed,payload)
    assert [r['status'] for r in result['assessments']]==['fail','unknown']
    assert original['rules_by_id']['b']['evidence_ids']==['invented']


def test_native_budget_can_be_changed_without_rewriting_document_or_losing_revision_guard(tmp_path):
    from sourceloom.store import Store,Conflict
    from sourceloom.durable import Queue
    store=Store(tmp_path);p=store.create('Document');queue=Queue(store)
    changed=queue.edit_document(p['id'],0,budget_cny=5)
    assert changed['budget_cny']==5 and changed['revision']==p['revision']
    with pytest.raises(Conflict):queue.edit_document(p['id'],0,budget_cny=10)
    with pytest.raises(ValueError):queue.edit_document(p['id'],1,budget_cny=-1)
    assert store.get(p['id'])['budget_cny']==5


@pytest.mark.parametrize('uncertain',[False,True])
def test_quota_fallback_has_separate_ledger_and_keeps_full_skill(tmp_path,skill,monkeypatch,uncertain):
    import json,httpx
    from tests.test_production import prepared
    from sourceloom.providers import Provider,Uncertain
    from sourceloom.config import load_config
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle);captured=[]
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,**kwargs):
            captured.append((url,kwargs))
            if len(captured)==1:return httpx.Response(429,json={'type':'GoUsageLimitError'})
            if uncertain:raise httpx.ReadTimeout('Unknown delivery')
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}],'usage':{'prompt_tokens':100,'completion_tokens':10}})
    monkeypatch.setattr('sourceloom.providers.httpx.AsyncClient',Client)
    config=load_config()|{'provider':'openai-compatible','base_url':'https://opencode.ai/zen/go/v1','api_key':'synthetic-go-key','billing_mode':'subscription','role_providers':{},'max_input_bytes':100000,'writing_skill_dir':str(skill),'structured_output':'json_object',
        'quota_fallback':{'base_url':'https://api.deepseek.com/v1','api_key':'synthetic-official-key','billing_mode':'metered','pricing_cny':{'input':9,'cached_input':.3,'output':27}}}
    payload={'source':'Actual preserved input'};original=copy.deepcopy(payload)
    if uncertain:
        with pytest.raises(Uncertain):Provider(store,config).call(p['id'],'writer',payload,{'type':'object'},job)
    else:assert Provider(store,config).call(p['id'],'writer',payload,{'type':'object'},job)=={'ok':True}
    assert len(captured)==2 and payload==original
    assert captured[0][1]['headers']['User-Agent']=='SourceLoom/0.1'
    assert 'x-opencode-session' in captured[0][1]['headers'] and 'x-opencode-session' not in captured[1][1]['headers']
    for _,request in captured:
        for text in bundle['instructions'].values():assert text in request['json']['messages'][0]['content']
    with store.connect() as cx:rows=[dict(r)|{'body':json.loads(r['body'])} for r in cx.execute('SELECT * FROM spending ORDER BY created')]
    assert rows[0]['actual']==0 and rows[0]['body']['status']=='quota_rejected'
    assert rows[1]['actual'] is None if uncertain else rows[1]['body']['actual_cny']==pytest.approx(.00117)
    assert [c['status'] for c in job['calls']]==['rejected','uncertain' if uncertain else 'completed']
