import copy
import json
from io import BytesIO
import zipfile
import pytest
from bs4 import BeautifulSoup
from sourceloom.checks import freeze,inspect_draft
from sourceloom.config import load_config
from sourceloom.demo import create_demo
from sourceloom.export import export_zip,render
from sourceloom.ingest import intake
from sourceloom.pipeline import Pipeline
from sourceloom.store import Store,Conflict
from sourceloom.versions import append_intake,append_obligations


def prepared(tmp_path,monkeypatch):
    store=Store(tmp_path);old=create_demo(store)
    p=store.revise_sources(old['id'],old['revision'],old['inventory']['digest'],'补充一个遗漏的适用条件')
    def add(p):
        append_obligations(p['inventory'],[dict(id='new-condition',object_id=p['inventory']['objects'][0]['id'],statement='保留原文适用前提')])
        p['inventory']=freeze(p['inventory'])
    p=store.change(p['id'],add)
    pipe=Pipeline(store,load_config()|{'provider':'openai-compatible'})
    monkeypatch.setattr('sourceloom.pipeline.writing_snapshot',lambda c:{'fixture':'isolated synthetic test'})
    new=copy.deepcopy(old['plan']['units'][0])
    new.update(id='new-unit',obligation_ids=['new-condition'],object_ids=[p['inventory']['objects'][0]['id']])
    plan=copy.deepcopy(old['plan']);plan['units']=[new]
    p=pipe.commit(p['id'],'planner',plan,p['revision'],p['inventory']['digest'])
    return store,pipe,old,p


def test_reopen_is_atomic_and_preserves_complete_old_acceptance(tmp_path):
    store=Store(tmp_path);old=create_demo(store)
    old=store.change(old['id'],lambda p:p.update(accepted_revision=p['revision']))
    with pytest.raises(Conflict):store.revise_sources(old['id'],old['revision']+1,old['inventory']['digest'],'why')
    assert store.get(old['id'])==old and store.source_versions(old['id'])==[]
    new=store.revise_sources(old['id'],old['revision'],old['inventory']['digest'],'遗漏')
    assert store.source_versions(old['id'])==[old]
    assert new['draft']==old['draft'] and new['inventory']['objects']==old['inventory']['objects']
    assert new['inventory']['obligations']==old['inventory']['obligations']
    assert new['accepted_revision'] is None and new['review'] is None and new['plan'] is None
    assert new['inventory']['parent_digest']==old['inventory']['digest']
    assert new['budget_usd']==old['budget_usd'] and new['repair_rounds']==old['repair_rounds']


def test_incremental_generation_never_rewrites_completed_blocks(tmp_path,monkeypatch):
    store,pipe,old,p=prepared(tmp_path,monkeypatch)
    assert pipe.payload(p,'planner')['inventory']['obligations'][0]['id']=='new-condition'
    called=[]
    def generate(pid,role,payload,*args):
        called.append(payload['current_unit']['id'])
        return {'blocks':[dict(id='new-block',unit_id='new-unit',kind='source',markdown='遗漏条件的原文',
            obligation_ids=['new-condition'],object_ids=[p['inventory']['objects'][0]['id']],evidence=[])]}
    monkeypatch.setattr(pipe.provider,'call',generate)
    monkeypatch.setattr(pipe.executor,'submit',lambda fn,*a:fn(*a))
    job=pipe.start(p['id'],'generator')
    assert job['status']=='completed' and called==['new-unit']
    result=store.get(p['id'])
    assert result['draft']['blocks'][:-1]==old['draft']['blocks']
    assert not inspect_draft(result['inventory'],result['draft'],result['plan'])
    with zipfile.ZipFile(BytesIO(export_zip(store,result))) as z:
        note=json.loads(z.read('!!!meta.json'))['files'][0]
        audit=next(a for a in note['attachments'] if a['title']=='sourceloom-audit.json')
        assert json.loads(z.read(audit['dataFileName']))['previous_source_versions'][0]['draft']==old['draft']
    pipe.executor.shutdown()


def test_manual_incremental_result_cannot_replace_old_prose(tmp_path,monkeypatch):
    store,pipe,old,p=prepared(tmp_path,monkeypatch)
    wrong=copy.deepcopy(old['draft']);wrong['blocks'][0]['markdown']='unauthorized rewrite'
    with pytest.raises(Conflict):pipe.commit(p['id'],'generator',wrong,p['revision'],p['inventory']['digest'])
    assert store.get(p['id'])['draft']==old['draft']
    pipe.executor.shutdown()


def test_append_same_filenames_and_footnotes_keep_distinct_occurrences(tmp_path):
    store=Store(tmp_path)
    first=intake(store,[('note.html',b'<p>First<a href="#fn1">1</a></p><p id="fn1">First exception</p>')])
    second=intake(store,[('note.html',b'<p>Second<a href="#fn1">1</a></p><p id="fn1">Second exception</p>')])
    inv=freeze(append_intake(first,second))
    assert len({o['id'] for o in inv['objects']})==len(inv['objects'])
    p={'inventory':inv,'draft':{'blocks':[dict(id=str(i),unit_id='u',kind='source',markdown='',object_ids=[o['id']],obligation_ids=[],evidence=[]) for i,o in enumerate(inv['objects'])]}}
    doc=BeautifulSoup(render(p),'html.parser')
    links=doc.select('a[href^="#"]')
    targets=[doc.find(id=a['href'][1:]).get_text().strip() for a in links]
    assert targets==['First exception','First exception','Second exception','Second exception']
    assert [store.read_blob(o['sha256']) for o in inv['originals']]==[store.read_blob(o['sha256']) for o in first['originals']+second['originals']]


def test_resume_only_calls_unfinished_unit_and_keeps_original_identity(tmp_path,monkeypatch):
    store=Store(tmp_path);p=create_demo(store)
    units=[]
    for i,ob in enumerate(p['inventory']['obligations']):
        u=copy.deepcopy(p['plan']['units'][0]);u.update(id='u'+str(i),obligation_ids=[ob['id']],object_ids=[ob['object_id']]);units.append(u)
    p=store.change(p['id'],lambda p:p.update(draft=None,review=None,plan=p['plan']|{'units':units}))
    pipe=Pipeline(store,load_config()|{'provider':'openai-compatible'})
    monkeypatch.setattr('sourceloom.pipeline.writing_snapshot',lambda c:{'fixture':'test'})
    monkeypatch.setattr(pipe.executor,'submit',lambda fn,*a:fn(*a))
    counts={}
    def call(pid,role,payload,*args):
        u=payload['current_unit'];counts[u['id']]=counts.get(u['id'],0)+1
        if u['id']=='u1' and counts['u1']==1:raise Conflict('known failure before submission')
        return {'blocks':[dict(id='b-'+u['id'],unit_id=u['id'],kind='source',markdown='原始内容',obligation_ids=u['obligation_ids'],object_ids=u['object_ids'],evidence=[])]}
    monkeypatch.setattr(pipe.provider,'call',call)
    job=pipe.start(p['id'],'generator')
    assert job['status']=='paused' and job['completed_unit_ids']==['u0']
    with pytest.raises(Conflict):pipe.start(p['id'],'generator')
    with pytest.raises(Conflict):pipe.start(p['id'],'planner')
    with pytest.raises(Conflict):pipe.commit(p['id'],'planner',p['plan'],p['revision'],p['inventory']['digest'])
    resumed=pipe.resume(job['id'])
    assert resumed['status']=='completed' and counts['u0']==1 and counts['u1']==2
    assert len(store.jobs(p['id']))==1
    pipe.executor.shutdown()


def test_unknown_submission_cannot_be_resumed_as_new_call(tmp_path):
    store=Store(tmp_path);p=create_demo(store);pipe=Pipeline(store,load_config())
    store.put_job(dict(id='unknown',project=p['id'],role='generator',status='uncertain',created=1,calls=[]))
    with pytest.raises(Conflict):pipe.resume('unknown')
    with pytest.raises(Conflict):store.revise_sources(p['id'],p['revision'],p['inventory']['digest'],'追加')
    pipe.executor.shutdown()
