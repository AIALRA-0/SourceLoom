"""The processor's native import receipt is recoverable without a second import."""
import json
import zipfile
from io import BytesIO

import httpx
import pytest

from sourceloom import processor
from sourceloom.readweave import import_candidate, import_status, note_url
from sourceloom.store import Conflict, Store, digest


def candidate(tmp_path):
    store=Store(tmp_path)
    project=processor.create(store,'ReadWeave receipt test')
    project=processor.prepare(store,project['id'],[('source.txt',b'Original source material.')])
    project=processor.save_result(store,project['id'],'# Reading copy\n\nOriginal source material.')
    return store,project


def config():
    return {'readweave_url':'https://reader.example','readweave_token':'private-synthetic-token',
            'readweave_parent':'test-parent'}


def test_processor_readweave_status_is_exact_and_import_is_idempotent(tmp_path,monkeypatch):
    store,p=candidate(tmp_path)
    assert import_status(store,config(),p['id'])['status']=='not_submitted'
    sent=[]
    remote={}
    def handler(request):
        path=request.url.path
        if path=='/etapi/notes' and request.method=='GET':
            return httpx.Response(200,json={'results':[remote['entry']] if remote else []})
        if path=='/etapi/notes/test-parent/import' and request.method=='POST':
            sent.append(request.content)
            with zipfile.ZipFile(BytesIO(request.content)) as z:
                meta=json.loads(z.read('!!!meta.json'))['files'][0]
                remote['html']=z.read('material.html')
                remote['attachments']=[dict(attachmentId='a'+str(i),title=a['title'],
                                            role=a['role'],mime=a['mime']) for i,a in enumerate(meta['attachments'])]
                remote['bytes']={'a'+str(i):z.read(a['dataFileName']) for i,a in enumerate(meta['attachments'])}
                marker=next(a['value'] for a in meta['attributes'] if a['name']=='sourceloomCandidate')
                remote['entry']=dict(noteId='noteA',parentNoteIds=['test-parent'],
                                     attributes=[{'name':'sourceloomCandidate','value':marker}])
            return httpx.Response(200,json={'note':{'noteId':'noteA'}})
        if path=='/etapi/notes/noteA/content':
            return httpx.Response(200,content=remote['html'])
        if path=='/etapi/notes/noteA/attachments':
            return httpx.Response(200,json=remote['attachments'])
        if path.startswith('/etapi/attachments/') and path.endswith('/content'):
            return httpx.Response(200,content=remote['bytes'][path.split('/')[3]])
        raise AssertionError(f'Unexpected request: {request.method} {path}')
    client=httpx.Client
    monkeypatch.setattr(httpx,'Client',lambda **kw:client(**kw,transport=httpx.MockTransport(handler)))
    first=import_candidate(store,config(),p['id'])
    assert first['status']=='readback_passed'
    assert first['phase']=='complete'
    assert first['note_id']=='noteA'
    assert first['note_url']=='https://reader.example/#root/noteA'
    assert first['semantic_status']=='not_reviewed' and first['user_accepted'] is False
    assert len(sent)==1
    assert import_status(store,config(),p['id'])==first
    assert import_candidate(store,config(),p['id'])['note_id']=='noteA'
    assert len(sent)==1
    assert 'private-synthetic-token' not in json.dumps(import_status(store,config(),p['id']))
    remote['entry']['noteId']='different-note'
    with pytest.raises(Conflict,match='身份不一致'):
        import_candidate(store,config(),p['id'])
    assert import_status(store,config(),p['id'])['note_id']=='noteA'
    assert len(sent)==1


def test_unknown_native_import_remains_queryable_without_second_post(tmp_path,monkeypatch):
    store,p=candidate(tmp_path)
    posts=[]
    found=False
    def handler(request):
        nonlocal found
        path=request.url.path
        if path=='/etapi/notes' and request.method=='GET':
            return httpx.Response(200,json={'results':[]})
        if path=='/etapi/notes/test-parent/import' and request.method=='POST':
            posts.append(digest(request.content))
            raise httpx.ReadTimeout('The request may have reached ReadWeave')
        raise AssertionError(f'Unexpected request: {request.method} {path}')
    client=httpx.Client
    monkeypatch.setattr(httpx,'Client',lambda **kw:client(**kw,transport=httpx.MockTransport(handler)))
    with pytest.raises(httpx.ReadTimeout):
        import_candidate(store,config(),p['id'])
    status=import_status(store,config(),p['id'])
    assert status['status']=='submitted' and status['phase']=='remote_import'
    assert status['note_url'] is None
    with pytest.raises(Conflict,match='不确定'):
        import_candidate(store,config(),p['id'])
    assert len(posts)==1


@pytest.mark.parametrize('fault',['post500_committed','post500_committed_readback500',
                                  'post500_absent','attachment500'])
def test_ambiguous_or_partial_import_recovers_by_read_only_lookup(tmp_path,monkeypatch,fault):
    store,p=candidate(tmp_path)
    sent=[]
    remote={}
    failed_once=False
    def handler(request):
        nonlocal failed_once
        path=request.url.path
        if path=='/etapi/notes' and request.method=='GET':
            return httpx.Response(200,json={'results':[remote['entry']] if 'entry' in remote else []})
        if path=='/etapi/notes/test-parent/import' and request.method=='POST':
            sent.append(digest(request.content))
            with zipfile.ZipFile(BytesIO(request.content)) as z:
                meta=json.loads(z.read('!!!meta.json'))['files'][0]
                remote['html']=z.read('material.html')
                remote['attachments']=[dict(attachmentId='a'+str(i),title=a['title'],
                    role=a['role'],mime=a['mime']) for i,a in enumerate(meta['attachments'])]
                remote['bytes']={'a'+str(i):z.read(a['dataFileName'])
                                 for i,a in enumerate(meta['attachments'])}
                key=next(a['value'] for a in meta['attributes'] if a['name']=='sourceloomCandidate')
                if fault!='post500_absent':
                    remote['entry']=dict(noteId='noteA',parentNoteIds=['test-parent'],
                        attributes=[{'name':'sourceloomCandidate','value':key}])
            if fault.startswith('post500'):
                return httpx.Response(500)
            return httpx.Response(200,json={'note':{'noteId':'noteA'}})
        if path=='/etapi/notes/noteA/content':
            if fault=='post500_committed_readback500' and not failed_once:
                failed_once=True
                return httpx.Response(500)
            return httpx.Response(200,content=remote['html'])
        if path=='/etapi/notes/noteA/attachments':
            if fault=='attachment500' and not failed_once:
                failed_once=True
                return httpx.Response(500)
            return httpx.Response(200,json=remote['attachments'])
        if path.startswith('/etapi/attachments/') and path.endswith('/content'):
            return httpx.Response(200,content=remote['bytes'][path.split('/')[3]])
        raise AssertionError(f'Unexpected request: {request.method} {path}')
    client=httpx.Client
    monkeypatch.setattr(httpx,'Client',lambda **kw:client(**kw,transport=httpx.MockTransport(handler)))
    with pytest.raises(httpx.HTTPStatusError):
        import_candidate(store,config(),p['id'])
    pending=import_status(store,config(),p['id'])
    assert pending['status']==('imported' if fault=='attachment500' else 'submitted')
    assert pending['note_url'] is None
    if fault=='post500_absent':
        with pytest.raises(Conflict,match='不确定'):
            import_candidate(store,config(),p['id'])
    else:
        if fault=='post500_committed_readback500':
            with pytest.raises(httpx.HTTPStatusError):
                import_candidate(store,config(),p['id'])
            known=import_status(store,config(),p['id'])
            assert known['status']=='imported' and known['note_id']=='noteA'
            assert known['note_url'] is None
        recovered=import_candidate(store,config(),p['id'])
        assert recovered['status']=='readback_passed'
        assert recovered['note_id']=='noteA'
    assert len(sent)==1


def test_note_url_requires_real_usable_target():
    assert note_url(config(),'note A')=='https://reader.example/#root/note%20A'
    assert note_url(config(),'') is None
    assert note_url({'readweave_url':'https://reader.example/?token=secret'},'noteA') is None
    assert note_url({'readweave_url':'javascript:alert(1)'},'noteA') is None


def test_status_does_not_claim_a_note_exists_before_candidate_is_ready(tmp_path):
    store=Store(tmp_path)
    p=processor.create(store,'Not generated yet')
    result=import_status(store,config(),p['id'])
    assert result['status']=='not_ready' and result['note_url'] is None


def test_incomplete_claim_stays_unconfirmed_and_cannot_dispatch(tmp_path,monkeypatch):
    store,p=candidate(tmp_path)
    key=import_status(store,config(),p['id'])['candidate']
    path=store.root/'readweave'/(p['id']+'-'+str(p['revision'])+'-'+key.rsplit(':',1)[-1]+'.json')
    path.parent.mkdir(exist_ok=True)
    path.write_text('{',encoding='utf-8')
    assert import_status(store,config(),p['id'])['status']=='receipt_unavailable'
    def forbidden(*args,**kwargs):
        raise AssertionError('No remote request may follow an incomplete claim')
    monkeypatch.setattr(httpx,'Client',forbidden)
    with pytest.raises(Conflict,match='未重复提交'):
        import_candidate(store,config(),p['id'])


def test_known_note_is_not_offered_at_a_different_instance_or_parent(tmp_path):
    store,p=candidate(tmp_path)
    status=import_status(store,config(),p['id'])
    key=status['candidate']
    path=store.root/'readweave'/(p['id']+'-'+str(p['revision'])+'-'+key.rsplit(':',1)[-1]+'.json')
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(dict(candidate=key,status='readback_passed',phase='complete',
                                    parent='other-parent',instance='https://other.example',
                                    note_id='other-note')),encoding='utf-8')
    result=import_status(store,config(),p['id'])
    assert result['note_url'] is None and result['target_verification']=='required'
    with pytest.raises(Conflict,match='另一个 ReadWeave'):
        import_candidate(store,config(),p['id'])
