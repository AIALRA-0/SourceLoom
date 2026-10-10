import json
import httpx
import pytest
from fastapi.testclient import TestClient
from sourceloom.app import create_app
from sourceloom.readweave import destinations,target_config,import_status
from sourceloom import processor
from sourceloom.store import Store,Conflict
def config():
    return dict(readweave_url='https://reader.example',readweave_token='private-synthetic-token',readweave_parent='test-parent')

def candidate(path):
    store=Store(path);p=processor.create(store,'Selected target test')
    p=processor.prepare(store,p['id'],[('source.txt',b'Whole original source.')])
    return store,processor.save_result(store,p['id'],'# Reading copy\n\nWhole original source.')

def test_lazy_destinations_filter_protected_and_hidden(monkeypatch):
    seen=[]
    notes={'root':dict(noteId='root',title='Root',type='text',childNoteIds=['folder','secret','_hidden','image']),
           'folder':dict(noteId='folder',title='Research',type='text',childNoteIds=['child']),
           'secret':dict(noteId='secret',title='Protected',type='text',isProtected=True),
           'image':dict(noteId='image',title='Image',type='image')}
    def handler(r):
        assert r.headers['Authorization']=='private-synthetic-token'
        seen.append(r.url.path);return httpx.Response(200,json=notes[r.url.path.rsplit('/',1)[-1]])
    real=httpx.Client;monkeypatch.setattr(httpx,'Client',lambda **kw:real(**kw,transport=httpx.MockTransport(handler)))
    result=destinations(config(),'root')
    assert result['children']==[{'id':'folder','title':'Research','has_children':True}]
    assert '/etapi/notes/child' not in seen and '/etapi/notes/_hidden' not in seen
    assert 'private-synthetic-token' not in json.dumps(result)
    changed=target_config(config(),'folder');assert changed['readweave_parent']=='folder' and config()['readweave_parent']=='test-parent'
    with pytest.raises(Conflict):target_config(config(),'secret')
    with pytest.raises(Conflict):target_config(config(),'../../wrong')

def test_alternate_target_receipt_remains_openable(tmp_path,monkeypatch):
    from sourceloom.readweave import _candidate_key,_receipt_path
    store,p=candidate(tmp_path);raw=processor.export_package(store,p);key=_candidate_key(raw)
    path=_receipt_path(store,p['id'],p['revision'],key);path.parent.mkdir(exist_ok=True)
    receipt={'candidate':key,'parent':'folder','instance':config()['readweave_url'],'note_id':'actual-note','status':'readback_passed'}
    path.write_text(json.dumps(receipt))
    result=import_status(store,config(),p['id'])
    assert result['note_url']=='https://reader.example/#root/actual-note' and result['parent']=='folder'

def test_import_target_snapshot_rejects_stale_version_before_remote(tmp_path,monkeypatch):
    conf=dict(config(),data_dir=str(tmp_path),auth_mode='local',provider='manual',external_worker=True,writing_skill_dir='')
    with TestClient(create_app(conf),headers={'X-SourceLoom':'1'}) as c:
        p=c.post('/api/processor/projects',json={'title':'Stale target snapshot'}).json();pid=p['id']
        c.post('/api/processor/projects/'+pid+'/upload',files={'files':('source.txt',b'Whole original')})
        c.post('/api/processor/projects/'+pid+'/result',json={'markdown':'# Whole original'})
        def forbidden(*a,**kw):raise AssertionError('No external request allowed')
        monkeypatch.setattr(httpx,'Client',forbidden)
        r=c.post('/api/processor/projects/'+pid+'/readweave',json={'parent_id':'folder','version_id':'expired-version'})
        assert r.status_code==409 and '版本已改变' in r.text


def test_destination_pagination_keeps_all_children_reachable(monkeypatch):
    ids=['n'+str(i) for i in range(45)]
    def handler(r):
        nid=r.url.path.rsplit('/',1)[-1]
        return httpx.Response(200,json=dict(noteId=nid,title=nid,type='text',childNoteIds=ids if nid=='root' else []))
    real=httpx.Client;monkeypatch.setattr(httpx,'Client',lambda **kw:real(**kw,transport=httpx.MockTransport(handler)))
    first=destinations(config(),'root');second=destinations(config(),'root',first['next_offset'])
    assert [n['id'] for n in first['children']+second['children']]==ids
    assert first['next_offset']==40 and second['next_offset'] is None
    with pytest.raises(Conflict):destinations(config(),'root',-1)
