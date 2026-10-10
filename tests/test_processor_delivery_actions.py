"""A real imported note is openable; stale or untrusted receipts are not links."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def delivery(receipt, clear=False):
    script = r'''
    const fs=require('node:fs'),vm=require('node:vm'),arg=JSON.parse(fs.readFileSync(0,'utf8'));
    const source=fs.readFileSync(arg.root+'/sourceloom/static/processor.js','utf8');
    const code=source.slice(source.indexOf('const isWebURL ='),source.indexOf('async function loadReadweaveStatus('));
    const nodes={},q=id=>nodes[id]||(nodes[id]={hidden:false,removeAttribute(k){delete this[k];}});
    const ctx={URL,$:q,project:{library:{}},readweaveReceipt:null,readweaveURL:'',locked:false,
      capabilities:{},activeChecks:()=>({ok:true}),activeVersion:()=>({id:'v1',mechanical_pass:true}),processor:()=>({})};
    vm.createContext(ctx);vm.runInContext(fs.readFileSync(arg.root+'/sourceloom/static/processor_readweave.js','utf8').replace(/export /g,''),ctx);vm.runInContext(code,ctx);ctx.renderReadweaveStatus(arg.receipt);
    if(arg.clear)ctx.renderReadweaveStatus(null);
    process.stdout.write(JSON.stringify({open:q('#open-readweave'),send:q('#send-readweave'),query:q('#query-readweave'),url:ctx.readweaveURL}));
    '''
    result = subprocess.run([shutil.which('node'), '-e', script], input=json.dumps(dict(root=str(ROOT),receipt=receipt,clear=clear)),text=True,encoding='utf-8',capture_output=True,check=True,timeout=10)
    return json.loads(result.stdout)


@pytest.mark.parametrize('status', ['readback_passed','imported','readback_gaps'])
def test_actual_note_has_primary_open_action_without_reimport(status):
    value = delivery(dict(status=status,note_id='known-note',note_url='https://notes.example/n/known-note'))
    assert value['open']['hidden'] is False
    assert value['open']['href']=='https://notes.example/n/known-note'
    assert value['send']['hidden'] is True
    assert value['send']['disabled'] is True


@pytest.mark.parametrize('receipt', [
    dict(status='readback_passed',note_url='https://notes.example/n/guess'),
    dict(status='submitted',note_id='pending',note_url='https://notes.example/n/pending'),
    dict(status='readback_passed',note_id='n',note_url='javascript:alert(1)'),
])
def test_incomplete_or_untrusted_receipt_is_not_an_open_link(receipt):
    value=delivery(receipt)
    assert value['open']['hidden'] is True
    assert 'href' not in value['open']
    assert not value['url']


def test_new_material_or_version_clears_previous_note_action():
    value=delivery(dict(status='readback_passed',note_id='previous',note_url='https://notes.example/n/previous'),clear=True)
    assert value['open']['hidden'] is True
    assert 'href' not in value['open']
    assert value['send']['hidden'] is False
    assert value['send']['textContent']=='导入 ReadWeave'
