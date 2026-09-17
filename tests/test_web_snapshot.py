from io import BytesIO
from PIL import Image
from sourceloom import network
from sourceloom.ingest import intake
from sourceloom.store import Store


def test_web_snapshot_keeps_original_bytes_and_resolves_duplicate_image_positions(tmp_path,monkeypatch):
    raw=b'<base href="https://assets.example.invalid/book/"><p>We retain both pictures.</p><img src="plot.png"><img src="plot.png">'
    image=BytesIO();Image.new('RGB',(2,2),'white').save(image,'PNG')
    calls=[]
    def fetch(url,*args):
        calls.append(url)
        return (raw,'text/html',url) if len(calls)==1 else (image.getvalue(),'image/png',url)
    monkeypatch.setattr(network,'fetch',fetch)
    uploads,url,aliases,failures=network.fetch_bundle('https://example.invalid/chapter')
    store=Store(tmp_path);inv=intake(store,uploads,url,aliases)
    assert failures==[] and len(calls)==2
    assert store.read_blob(inv['originals'][0]['sha256'])==raw
    pics=[o for o in inv['objects'] if o['kind']=='image']
    assert len(pics)==2 and pics[0]['resource_id']==pics[1]['resource_id']
    assert not inv['unknown']


def test_fetched_page_asset_is_archived_without_a_detached_article_image(tmp_path):
    picture=BytesIO();Image.new('RGB',(2,2),'white').save(picture,'PNG')
    store=Store(tmp_path)
    inv=intake(store,[('snapshot.html',b'<main><p>Article text</p></main>'),
                      ('web-assets/unreferenced.png',picture.getvalue())],
               source_url='https://example.org/article')
    assert len(inv['originals'])==2
    assert any(item['name']=='web-assets/unreferenced.png' for item in inv['resources'])
    assert not any(obj['kind']=='image' for obj in inv['objects'])


def test_failed_web_image_remains_a_gap_not_a_silent_omission(tmp_path,monkeypatch):
    def fetch(url,*args):
        if args:raise ValueError('refused non-public address')
        return b'<img src="https://127.0.0.1/private.png">','text/html',url
    monkeypatch.setattr(network,'fetch',fetch)
    uploads,url,aliases,failures=network.fetch_bundle('https://example.invalid/chapter')
    inv=intake(Store(tmp_path),uploads,url,aliases)
    assert failures and inv['unknown'] and inv['objects'][0]['resource_id'] is None


def test_markdown_snapshot_fetches_raster_and_rasterizes_svg_badge(tmp_path,monkeypatch):
    raw=(b'# Real project\n\n[![Build][badge]][status]\n\n![Portrait](images/person.png)\n\n'
         b'[badge]: https://badges.example.invalid/build.svg\n'
         b'[status]: https://example.invalid/build\n')
    svg=(b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
         b'<rect width="40" height="20" fill="green"/></svg>')
    portrait=BytesIO();Image.new('RGB',(3,3),'white').save(portrait,'PNG')
    base='https://raw.example.invalid/project/README.md'
    seen=[]
    def fetch(url,*args):
        seen.append(url)
        if url==base:return raw,'text/plain',url
        if url.endswith('.svg'):return svg,'image/svg+xml',url
        return portrait.getvalue(),'image/png',url
    monkeypatch.setattr(network,'fetch',fetch)
    uploads,final,aliases,failures=network.fetch_bundle(base)
    inv=intake(Store(tmp_path),uploads,final,aliases)
    assert failures==[] and len(seen)==3 and not inv['unknown']
    assert len([o for o in inv['objects'] if o['kind']=='image'])==2
    assert any(name.endswith('.png') and data.startswith(b'\x89PNG') for name,data in uploads[1:])


def test_svg_stylesheet_import_is_rejected_before_rasterization(monkeypatch):
    raw=b'# Page\n\n![Diagram](image.svg)\n'
    svg=(b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
         b'<style>@import url(https://example.invalid/hidden.css);</style></svg>')
    def fetch(url,*args):
        return (svg,'image/svg+xml',url) if url.endswith('.svg') else (raw,'text/plain',url)
    monkeypatch.setattr(network,'fetch',fetch)
    uploads,_,_,failures=network.fetch_bundle('https://example.invalid/page.md')
    assert len(uploads)==1 and len(failures)==1
    assert failures[0]['reason']=='ValueError'


def test_markdown_snapshot_fetches_literal_html_images(tmp_path,monkeypatch):
    raw=b'# Project\n\n<img src="docs/diagram.png" alt="Diagram">\n'
    picture=BytesIO();Image.new('RGB',(3,3),'white').save(picture,'PNG')
    base='https://raw.example.invalid/project/README.md'
    seen=[]
    def fetch(url,*args):
        seen.append(url)
        return (raw,'text/plain',url) if url==base else (picture.getvalue(),'image/png',url)
    monkeypatch.setattr(network,'fetch',fetch)
    uploads,final,aliases,failures=network.fetch_bundle(base)
    inv=intake(Store(tmp_path),uploads,final,aliases)
    assert failures==[] and seen==[base,'https://raw.example.invalid/project/docs/diagram.png']
    assert len([o for o in inv['objects'] if o['kind']=='image' and o['resource_id']])==1
    assert not inv['unknown']
