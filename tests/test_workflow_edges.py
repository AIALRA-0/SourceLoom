import copy
from io import BytesIO
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
import pytest
from bs4 import BeautifulSoup
from sourceloom.checks import review_complete
from sourceloom.config import load_config
from sourceloom.demo import create_demo
from sourceloom.export import render
from sourceloom.parse_worker import isolated_intake
from sourceloom.pipeline import Pipeline
from sourceloom.readweave import compare_html
from sourceloom.store import Store,Conflict
from sourceloom.ingest import intake
from sourceloom.checks import freeze,inspect_draft


def test_claiming_every_id_without_proof_cannot_release(tmp_path):
    p=create_demo(Store(tmp_path))
    p['review']={'revision':p['revision'],'inventory_digest':p['inventory']['digest'],'body':{
        'findings':[],'assessed_obligation_ids':[o['id'] for o in p['inventory']['obligations']],
        'assessed_block_ids':[b['id'] for b in p['draft']['blocks']],'proofs':[]}}
    assert not review_complete(p)


def test_protected_source_is_visible_inside_quote(tmp_path):
    p=create_demo(Store(tmp_path));o=next(o for o in p['inventory']['objects'] if o['kind']=='text')
    b=p['draft']['blocks'][0];b.update(kind='source',markdown='原文',object_ids=[o['id']])
    html=BeautifulSoup(render(p),'html.parser')
    assert o['text'] in html.blockquote.get_text()


def test_cross_project_reservation_has_one_atomic_daily_ceiling(tmp_path):
    store=Store(tmp_path);projects=[store.create('A',budget=1),store.create('B',budget=1)]
    def run(i):
        try:store.reserve(projects[i]['id'],str(i),0.6,{},daily_budget=1);return True
        except Conflict:return False
    with ThreadPoolExecutor(2) as ex:assert sum(ex.map(run,[0,1]))==1


def test_parser_runs_separately_and_preserves_received_bytes(tmp_path):
    store=Store(tmp_path);raw='条件：不包含启动开销\n\n尾注：测试值为合成值'.encode()
    inv=isolated_intake(store,[('source.txt',raw)])
    assert store.read_blob(inv['originals'][0]['sha256'])==raw
    assert '合成值' in inv['objects'][-1]['text']


def test_readback_detects_text_table_link_and_anchor_drift():
    source='<section data-readweave-anchor-id="b1"><p>Only if A</p><table><tr><td rowspan="2">3</td></tr></table><a href="https://example.invalid/a">A</a></section>'
    changed=source.replace('Only if A','Always').replace('rowspan="2"','rowspan="1"').replace('b1','b2').replace('/a"','/b"')
    result=compare_html(source,changed)
    assert not result['visible_text'] and not result['table_cells'] and not result['anchor_ids'] and not result['external_links']


def test_recovery_only_reads_original_artifact_and_never_calls_model(tmp_path,monkeypatch):
    store=Store(tmp_path);p=create_demo(store);pipe=Pipeline(store,load_config()|{'provider':'codex-cli'})
    monkeypatch.setattr(pipe.provider,'call',lambda *a,**k:pytest.fail('A recovery must not call a model'))
    work=store.root/'calls'/'known';work.mkdir(parents=True)
    body={'findings':[],'assessed_obligation_ids':[],'assessed_block_ids':[],'proofs':[],'teaching_notes':[]}
    (work/'result.json').write_text(json.dumps(body),encoding='utf-8')
    (work/'events.jsonl').write_text('{"type":"turn.completed"}\n',encoding='utf-8')
    job={'id':'recovery','project':p['id'],'role':'reviewer','status':'uncertain','created':1,
         'base_revision':p['revision'],'inventory_digest':p['inventory']['digest'],
         'calls':[{'id':'known','channel':'codex-cli'}]}
    store.put_job(job)
    assert pipe.recover(job['id'])['status']=='completed'
    assert not review_complete(store.get(p['id']))
    pipe.executor.shutdown()


def test_native_footnote_target_survives_reorganization(tmp_path):
    store=Store(tmp_path);p=create_demo(store)
    inv=freeze(intake(store,[('note.html',b'<p>Claim<a href="#fn1">1</a></p><p id="fn1" role="doc-footnote">Exception applies</p>')]))
    p['inventory']=inv
    p['draft']={'blocks':[{'id':'b','unit_id':'u','kind':'source','markdown':'',
        'obligation_ids':[o['id'] for o in inv['obligations']],
        'object_ids':[o['id'] for o in inv['objects']],'evidence':[]}]}
    html=BeautifulSoup(render(p),'html.parser')
    for link in html.select('a[href^="#"]'):
        assert html.find(id=link['href'][1:]) is not None
    assert any(o['kind']=='footnote' for o in inv['objects'])


def test_markdown_formula_gets_an_explicit_protected_obligation(tmp_path):
    inv=freeze(intake(Store(tmp_path),[('formula.md',b'Assume x > 0\n\n$$y = 1 / x$$\n\n```text\n$not math$\n```')]))
    formulas=[o for o in inv['objects'] if o['kind']=='formula']
    assert [o['text'] for o in formulas]==['$$y = 1 / x$$']
    draft={'blocks':[{'id':'b','unit_id':'u','kind':'explanation','markdown':'a summary',
                     'obligation_ids':[o['id'] for o in inv['obligations']],'object_ids':[],'evidence':[]}]}
    assert any(f['code']=='protected_object' for f in inspect_draft(inv,draft))
