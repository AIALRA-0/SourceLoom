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


def test_failed_web_image_remains_a_gap_not_a_silent_omission(tmp_path,monkeypatch):
    def fetch(url,*args):
        if args:raise ValueError('refused non-public address')
        return b'<img src="https://127.0.0.1/private.png">','text/html',url
    monkeypatch.setattr(network,'fetch',fetch)
    uploads,url,aliases,failures=network.fetch_bundle('https://example.invalid/chapter')
    inv=intake(Store(tmp_path),uploads,url,aliases)
    assert failures and inv['unknown'] and inv['objects'][0]['resource_id'] is None
