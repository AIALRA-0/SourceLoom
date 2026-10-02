from sourceloom.active_composition import (
    validate_plan, discard_known_plan_protocol_extras, exact_review_quote_fragment,
    writing_batches,
)
from tests.test_active_composition import plan, source
from tests.test_production import prepared, skill
from sourceloom.active_composition import ActiveComposition
from sourceloom.production import Production
from sourceloom.durable import Queue


def test_uploaded_document_without_url_keeps_fragment_link_as_navigation():
    material=source()
    material['source_url']=None
    material['objects']=[dict(id='link',kind='link',text='Contents',
                             locator='chapter.pdf/page[1]/a[1]',target='#contents')]
    proposal=plan()
    proposal['obligations']=proposal['obligations'][:1]
    proposal['obligations'][0].update(source_id='link',quote='Contents')
    proposal['nodes'][0].update(source_ids=['link'],obligation_ids=['f1'])
    proposal['link_briefs']=[dict(source_id='link',role='navigation')]
    result=validate_plan(proposal,material,['link'],require_link_briefs=True)
    assert result['link_briefs'][0]['role']=='navigation'


def test_bad_plan_nodes_are_left_for_schema_repair_instead_of_crashing():
    raw={'result':{'nodes':'not a node list','concepts':['not a concept'],
                   'cross_batch_risks':['unknown']}}
    clean,removed=discard_known_plan_protocol_extras(raw)
    assert clean==raw and removed==[]
    raw={'result':{'nodes':['not a node'],'concepts':[],
                   'cross_batch_risks':['unknown']}}
    clean,removed=discard_known_plan_protocol_extras(raw)
    assert clean==raw and removed==[]


def test_review_quote_alignment_only_trims_non_substantive_edges():
    block='它与制定互联网标准的 IESG 不同，只管研究领域'
    assert exact_review_quote_fragment('这与制定互联网标准的 IESG 不同，只管研究领域',block)==(
        '与制定互联网标准的 IESG 不同，只管研究领域')
    assert exact_review_quote_fragment('不与制定互联网标准的 IESG 不同，只管研究领域',block) is None
    assert exact_review_quote_fragment('这与制定互联网标准的 IESG 不同，只管标准领域',block) is None


def test_heading_at_partition_boundary_joins_next_actual_content():
    material={'objects':[{'id':'h','kind':'heading','text':'Results'},
                         {'id':'t','kind':'text','text':'Measured result.'}]}
    def node(node_id,source_id,obligation_id,title):
        return dict(id=node_id,source_ids=[source_id],obligation_ids=[obligation_id],
                    establishes_concepts=[],requires_concepts=[],depends_on=[],
                    title=title,transition_from='',prepares_for='')
    parts=[{'nodes':[node('heading','h','fh','Results')],
            'obligations':[dict(id='fh',quote='Results',source_span_ids=[])]},
           {'nodes':[node('body','t','ft','Body')],
            'obligations':[dict(id='ft',quote='Measured result.',source_span_ids=[])]}]
    batches=writing_batches(parts,material)
    assert len(batches)==1
    assert batches[0]['source_ids']==['h','t']
    assert batches[0]['obligation_ids']==['fh','ft']
    assert batches[0]['title']=='Results'


def test_transport_fallback_uses_the_actual_provider_identity(tmp_path, skill, monkeypatch):
    from pydantic import BaseModel
    class Shape(BaseModel):
        answer:str
    store,_,project,bundle=prepared(tmp_path,skill)
    job=Queue(store,pipeline='active_composition_v2').enqueue(project['id'],bundle)
    job['core_chain_version']=0
    job['transport_fallback_steps']={'stage':'earlier-uncertain-call'}
    store.put_job(job)
    seen={}
    def call(provider,pid,role,payload,schema,claimed,cancelled):
        seen.update(role=role,provider_id=provider.config.get('provider_id'),
                    base_url=provider.config.get('base_url'))
        return {'answer':'ok'}
    monkeypatch.setattr('sourceloom.active_composition.Provider.call',call)
    engine=ActiveComposition(Production(store,{
        'provider':'openai-compatible','provider_id':'kuafu',
        'base_url':'https://api.kuafushe.cc/v1',
        'fallback_providers':{'active_plan':{
            'provider':'openai-compatible','base_url':'https://opencode.ai/zen/go/v1'}}}))
    monkeypatch.setattr(engine.queue,'cancelled',lambda *args:False)
    assert engine.call(job,'stage','active_plan',{},Shape)=={'answer':'ok'}
    assert seen=={'role':'active_plan__fallback','provider_id':'opencode-go',
                  'base_url':'https://opencode.ai/zen/go/v1'}


def test_uncertain_kuafu_stage_does_not_degrade_later_same_role(tmp_path, skill, monkeypatch):
    from pydantic import BaseModel
    class Shape(BaseModel):
        answer:str
    store,_,project,bundle=prepared(tmp_path,skill)
    job=Queue(store,pipeline='active_composition_v2').enqueue(project['id'],bundle)
    job['transport_fallback_steps']={'prior-stage':'uncertain-call'}
    job['calls']=[{'id':'uncertain-call','role':'active_plan','status':'uncertain',
                   'upstream_base':'https://api.kuafushe.cc/v1'}]
    seen={}
    def call(provider,pid,role,payload,schema,claimed,cancelled):
        seen.update(role=role,base_url=provider.config.get('base_url'))
        return {'answer':'ok'}
    monkeypatch.setattr('sourceloom.active_composition.Provider.call',call)
    engine=ActiveComposition(Production(store,{
        'provider':'openai-compatible','provider_id':'kuafu',
        'base_url':'https://api.kuafushe.cc/v1',
        'fallback_providers':{'active_plan':{
            'provider':'openai-compatible','base_url':'https://opencode.ai/zen/go/v1'}}}))
    monkeypatch.setattr(engine.queue,'cancelled',lambda *args:False)
    assert engine.call(job,'next-stage','active_plan',{},Shape)=={'answer':'ok'}
    assert seen=={'role':'active_plan','base_url':'https://api.kuafushe.cc/v1'}
    assert 'active_plan' not in job.get('transport_role_degradations',{})
