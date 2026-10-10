import pytest
from fastapi.testclient import TestClient
from sourceloom.app import create_app
from sourceloom.math_render import markdown_renderer


@pytest.fixture
def context(tmp_path):
    app=create_app(dict(data_dir=str(tmp_path),auth_mode='local',provider='manual',external_worker=True))
    with TestClient(app,headers={'X-SourceLoom':'1'}) as c:
        p=c.post('/api/processor/projects',json={'title':'Return fixture'}).json()
        pid=p['id']
        p=c.post(f'/api/processor/projects/{pid}/upload',files={'files':('source.md',b'# Source\n\nFull fixture.','text/markdown')}).json()
        yield c,app.state.processor_library,pid,p['processor']['source_digest']


def upload(c,pid,source,raw=b'# Returned\r\n\r\nAll original bytes.',token='manual-upload-fixture-0001',base=''):
    return c.post(f'/api/processor/projects/{pid}/return-upload',
        data={'upload_id':token,'base_version':base,'source_digest':source},
        files={'file':('return.md',raw,'text/markdown')})


def test_return_upload_saves_once_and_preserves_raw_file(context):
    c,store,pid,source=context
    raw=b'\xef\xbb\xbf# Returned\r\n\r\nAll original bytes.'
    first=upload(c,pid,source,raw);assert first.status_code==200,first.text
    second=upload(c,pid,source,raw);assert second.status_code==200,second.text
    assert second.json()['duplicate'] and first.json()['version_id']==second.json()['version_id']
    p=store.get(pid);assert len(p['processor']['versions'])==1
    version=p['processor']['versions'][0]
    assert version['markdown']==raw.decode('utf-8-sig')
    assert store.read_blob(version['manual_return']['sha256'])==raw
    download=c.get(f"/api/processor/projects/{pid}/files/{version['manual_return']['sha256']}")
    assert download.status_code==200 and download.content==raw
    assert p['processor']['requests']==[]
    different=upload(c,pid,source,b'# Different')
    assert different.status_code==409
    assert len(store.get(pid)['processor']['versions'])==1


def test_return_upload_conflict_keeps_raw_without_overwriting(context):
    c,store,pid,source=context
    assert upload(c,pid,source).status_code==200
    raw=b'# A second full returned draft'
    conflict=upload(c,pid,source,raw,token='manual-upload-fixture-0002')
    assert conflict.status_code==409
    p=store.get(pid);assert len(p['processor']['versions'])==1
    receipt=p['processor']['manual_return_uploads'][-1]
    assert store.read_blob(receipt['sha256'])==raw


def test_concurrent_acknowledgements_share_one_version(context):
    from concurrent.futures import ThreadPoolExecutor
    c,store,pid,source=context
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _:upload(c,pid,source),range(2)))
    assert all(r.status_code==200 for r in responses)
    assert len(store.get(pid)['processor']['versions'])==1


def test_trashed_material_rejects_return_write(context):
    c,store,pid,source=context
    node=next(n for n in c.get('/api/processor/tree').json()['nodes'] if n['id']==pid)
    response=c.post('/api/processor/tree/actions',json={'action':'trash','items':[node],'request_id':'trash-return-fixture'})
    assert response.status_code==200,response.text
    assert upload(c,pid,source).status_code==409
    assert store.get(pid)['processor']['versions']==[]


@pytest.mark.parametrize('raw',[b'',b'\xff\xfe',b'\x00binary',b'x'*(4*1024*1024+1)],ids=['empty','encoding','binary','oversized'])
def test_invalid_return_does_not_create_version(context,raw):
    c,store,pid,source=context
    assert upload(c,pid,source,raw).status_code==400
    assert store.get(pid)['processor']['versions']==[]


def test_history_does_not_compile_or_build_reading_geometry(context,monkeypatch):
    c,store,pid,source=context
    assert upload(c,pid,source).status_code==200
    from sourceloom import processor
    monkeypatch.setattr(processor,'compile_result',lambda *a,**k:pytest.fail('history must not compile'))
    monkeypatch.setattr(processor,'presentation_project',lambda *a,**k:pytest.fail('history must not rebuild source'))
    response=c.get(f'/api/processor/projects/{pid}/version-history')
    assert response.status_code==200 and len(response.json()['versions'])==1
    assert 'markdown' not in response.json()['versions'][0]


FORMULA=r'\bar{T}_{\mathrm{available}} = \frac{1}{n}\sum_{i\in S}T_i = \frac{12+14+16+18}{4} = 15\,{}^\circ\mathrm{C}.'


@pytest.mark.parametrize('source',[
    '$$\n'+FORMULA+'\n$$',
    '```\n$$\n'+FORMULA+'\n$$\n```',
    '```math\n'+FORMULA+'\n```',
    '    $$\n    '+FORMULA+'\n    $$',
])
def test_display_formula_in_return_containers_renders_without_source_edits(source):
    rendered=markdown_renderer().render(source)
    assert '<math' in rendered and '<mfrac>' in rendered and '<pre>' not in rendered
    assert FORMULA in rendered
    native=markdown_renderer('readweave').render(source)
    assert 'math-tex' in native


def test_programming_and_mixed_text_code_stays_literal():
    for source in ['```python\n$$x$$\n```','```\nExample:\n$$x$$\n```','`$x$`']:
        result=markdown_renderer().render(source)
        assert '<code' in result and '<math' not in result


def test_unrenderable_math_fence_retains_expression():
    source='```math\n'+('x'*12001)+'\n```'
    result=markdown_renderer().render(source)
    assert '公式暂无法排版' in result and 'x'*12001 in result
