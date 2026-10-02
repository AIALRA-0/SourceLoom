"""Native attachment links use verified image bytes without changing content."""
from io import BytesIO
import json
import zipfile

from bs4 import BeautifulSoup
import httpx
import pytest
from reportlab.pdfgen import canvas

from sourceloom import processor
from sourceloom.readweave import _confirmed_navigation, _image_attachment_navigation, import_candidate, import_status
from sourceloom.store import Conflict, Store, digest


def test_navigation_is_limited_to_this_notes_verified_image_attachments():
    links = ['#root/note?viewMode=attachments&attachmentId=image',
             '#root/other?viewMode=attachments&attachmentId=image',
             '#root/note?viewMode=attachments&attachmentId=file',
             '#root/note?viewMode=attachments&attachmentId=unknown',
             'https://outside.example/#root/note?viewMode=attachments&attachmentId=image']
    raw = '<p>Exact text <code>a &lt; b</code></p>' + ''.join('<a href="'+url+'">Page label</a>' for url in links)
    entities = {'image': ('page one.png', 'image', 'image/png', 'a'*64),
                'file': ('source.pdf', 'file', 'application/pdf', 'b'*64)}
    projected, changes = _image_attachment_navigation(raw, 'note', entities)
    before, after = BeautifulSoup(raw, 'html.parser'), BeautifulSoup(projected, 'html.parser')
    assert before.get_text() == after.get_text()
    assert before.code.get_text() == after.code.get_text()
    assert [a['href'] for a in after.find_all('a')] == ['api/attachments/image/image/page%20one.png'] + links[1:]
    assert changes == [dict(old_href=links[0], new_href='api/attachments/image/image/page%20one.png',
                            attachment_id='image', attachment_sha256='a'*64)]


def test_native_void_tag_serialization_matches_exact_projection_but_changes_do_not():
    projected = '<p>Original page <a href="api/attachments/page/image/page-1.png">Page 1</a></p>'
    projected += ''.join('<img alt="Figure '+str(i)+'" src="api/attachments/img/image/image.png"/>' for i in range(7))
    claim = dict(status='UNKNOWN', projected_content_sha256=digest(projected.encode()))
    readback = projected.replace('/>', '>').encode()
    confirmed = _confirmed_navigation(readback, claim)
    assert confirmed['status'] == 'confirmed_by_readback'
    assert confirmed['readback_content_sha256'] != claim['projected_content_sha256']
    assert confirmed['readback_canonical_sha256'] == claim['projected_content_sha256']
    assert _confirmed_navigation(readback.replace(b'Original page', b'Different page'), claim) is None
    assert _confirmed_navigation(readback.replace(b'page-1.png', b'page-2.png'), claim) is None
    assert _confirmed_navigation(readback.replace(b'image.png', b'other.png'), claim) is None


@pytest.mark.parametrize('put_outcome', ['success', 'response_lost_after_commit', 'response_lost_before_commit', 'changed_text', 'changed_href'])
def test_native_link_projection_and_unknown_write_do_not_repeat_import_or_put(tmp_path, monkeypatch, put_outcome):
    store = Store(tmp_path)
    source = BytesIO()
    pdf = canvas.Canvas(source)
    pdf.drawString(50, 750, 'Synthetic original page navigation')
    pdf.save()
    project = processor.create(store, 'Synthetic native page reference')
    project = processor.prepare(store, project['id'], [('original.pdf', source.getvalue())])
    markdown = '# Page reference\n\n'+'\n\n'.join(r['marker'] for r in project['processor']['resources'])
    project = processor.save_result(store, project['id'], markdown)
    frozen_version = processor.active_version(project)
    config = dict(readweave_url='https://reader.example', readweave_parent='test-parent', readweave_token='synthetic-private-token')
    remote, imports, puts = {}, [], []

    def handler(request):
        path = request.url.path
        if path == '/etapi/notes':
            return httpx.Response(200, json={'results': [remote['entry']] if remote else []})
        if path == '/etapi/notes/test-parent/import':
            imports.append(request.content)
            with zipfile.ZipFile(BytesIO(request.content)) as archive:
                meta = json.loads(archive.read('!!!meta.json'))['files'][0]
                remote['attachments'] = [dict(a, attachmentId='entity'+str(i)) for i,a in enumerate(meta['attachments'])]
                remote['bytes'] = {a['attachmentId']: archive.read(a['dataFileName']) for a in remote['attachments']}
                doc = BeautifulSoup(archive.read('material.html'), 'html.parser')
                for a in doc.find_all('a', href=True):
                    entry = next(row for row in remote['attachments'] if row['dataFileName'] == a['href'])
                    a['href'] = '#root/note?viewMode=attachments&attachmentId='+entry['attachmentId']
                remote['html'] = str(doc).encode()
                key = next(a['value'] for a in meta['attributes'] if a['name']=='sourceloomCandidate')
                remote['entry'] = dict(noteId='note', parentNoteIds=['test-parent'],
                                       attributes=[dict(name='sourceloomCandidate', value=key)])
            return httpx.Response(200, json={'note': {'noteId': 'note'}})
        if path == '/etapi/notes/note/content':
            if request.method == 'PUT':
                # The deployed instance's served OpenAPI declares a string
                # body parsed as text/plain, even for an HTML note. GET uses
                # text/html; confusing them caused the real C4 HTTP 500.
                if request.headers.get('content-type') != 'text/plain; charset=utf-8':
                    return httpx.Response(500, json={'code':'TEXT_BODY_REQUIRED'})
                puts.append(request.content)
                if put_outcome != 'response_lost_before_commit':
                    remote['html'] = request.content
                if put_outcome == 'changed_text':
                    remote['html'] = remote['html'].replace(b'Page reference', b'Changed reference')
                elif put_outcome == 'changed_href':
                    remote['html'] = remote['html'].replace(b'/image/', b'/different/')
                if put_outcome.startswith('response_lost'):
                    raise httpx.ReadTimeout('Native projection response was lost')
            return httpx.Response(200, content=remote['html'])
        if path == '/etapi/notes/note/attachments':
            return httpx.Response(200, json=remote['attachments'])
        if path.startswith('/etapi/attachments/'):
            return httpx.Response(200, content=remote['bytes'][path.split('/')[3]])
        raise AssertionError((request.method, path))

    real_client = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: real_client(**kw, transport=httpx.MockTransport(handler)))
    if put_outcome == 'success':
        result = import_candidate(store, config, project['id'])
        assert result['status'] == 'readback_passed'
        assert result['navigation_projection']['status'] == 'confirmed_by_readback'
        assert result['checks']['image_attachment_navigation']
        assert import_candidate(store, config, project['id'])['status'] == 'readback_passed'
    elif put_outcome.startswith('response_lost'):
        with pytest.raises(httpx.ReadTimeout):
            import_candidate(store, config, project['id'])
        assert import_status(store, config, project['id'])['navigation_projection']['status'] == 'UNKNOWN'
        if put_outcome == 'response_lost_after_commit':
            assert import_candidate(store, config, project['id'])['status'] == 'readback_passed'
        else:
            with pytest.raises(Conflict, match='尚不确定'):
                import_candidate(store, config, project['id'])
    else:
        with pytest.raises(Conflict, match='读回不一致'):
            import_candidate(store, config, project['id'])
        assert import_status(store, config, project['id'])['navigation_projection']['status'] == 'UNKNOWN'
        with pytest.raises(Conflict, match='尚不确定'):
            import_candidate(store, config, project['id'])
    assert len(imports) == len(puts) == 1
    assert processor.active_version(store.get(project['id'])) == frozen_version
    doc = BeautifulSoup(remote['html'], 'html.parser')
    if put_outcome != 'changed_text':
        assert doc.get_text(' ',strip=True) == BeautifulSoup(puts[0], 'html.parser').get_text(' ',strip=True)
    with zipfile.ZipFile(BytesIO(imports[0])) as archive:
        assert all(remote['bytes'][a['attachmentId']] == archive.read(a['dataFileName'])
                   for a in remote['attachments'])
