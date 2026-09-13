import pytest
from .corpus import SEEDS,sample
from sourceloom.store import Store
from sourceloom.ingest import intake

@pytest.mark.parametrize('domain,text',SEEDS,ids=[x[0] for x in SEEDS])
@pytest.mark.parametrize('format',['txt','md','html','docx','pdf'])
def test_multidomain_format_intake_preserves_original_and_tail(tmp_path,domain,text,format):
    raw=sample(domain,text,format);store=Store(tmp_path)
    inv=intake(store,[(domain+'.'+format,raw)])
    assert store.read_blob(inv['originals'][0]['sha256'])==raw
    actual=' '.join(o['text'] for o in inv['objects'])
    assert 'Tail exception: all numbers are teaching assumptions.' in actual
    assert text.replace('\n',' ') in actual.replace('\n',' ')
    assert len(inv['obligations'])==len(inv['objects'])
    if format in {'pdf','docx'}:assert inv['unknown']
