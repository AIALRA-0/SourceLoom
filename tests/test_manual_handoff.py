import copy
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest
from fastapi.testclient import TestClient

from sourceloom import processor
from sourceloom.app import create_app
from sourceloom.config import load_config
from sourceloom.manual_handoff import START_MESSAGE, download, pack_zip, view
from sourceloom.store import Store


@pytest.fixture
def handoff(tmp_path):
    store=Store(tmp_path)
    project=processor.create(store, 'Complete source')
    processor.prepare(store,project['id'],[('source.txt',b'Full original source; no summary.')])
    processor.save_result(store,project['id'],'Full English baseline remains unchanged.')
    before=copy.deepcopy(store.get(project['id']))
    pack=processor.task_pack(store,project['id'])
    root=store.root/'manual-handoffs'/pack['source_digest']
    folder=root/'batch-1';folder.mkdir(parents=True)
    (folder/'rules.md').write_bytes(pack['prompt'].encode('utf8'))
    manifest=dict(source_digest=pack['source_digest'],template_digest=pack['template_digest'],
                  preferences='',resource_selection=pack['resource_selection'],
                  files=[dict(id='rules',name='完整任务要求.md',path='batch-1/rules.md',mime='text/markdown',role='format',batch='first')],
                  batches=[dict(id='first',title='Upload source',folder='batch-1',files=['rules'],message='Receive attachments only')],
                  chat_message_short='Read attached rules; wait for all images.',start_message='Start full rewrite.',continuation_message='Continue natural sections.')
    (root/'manifest.json').write_text(json.dumps(manifest),encoding='utf8')
    return store,project['id'],before,root,pack


def test_prepared_downloads_keep_source_baseline_and_requests_unchanged(handoff):
    store,pid,before,root,pack=handoff
    guide=view(store,pid)
    assert guide['package_url']==f'/api/processor/projects/{pid}/pack.zip'
    assert guide['start_message']==START_MESSAGE
    assert 'batches' not in guide
    path,row=download(store,pid,'rules')
    assert path.read_text(encoding='utf8')==pack['prompt']
    assert store.get(pid)==before
    assert store.jobs(pid)==[]


def test_changed_task_does_not_reuse_stale_manual_rules(handoff):
    store,pid,before,root,pack=handoff
    store.change(pid,lambda p:p['processor'].update(preferences='Different output requirements'))
    assert view(store,pid)['pack_digest']!=pack['digest']
    with pytest.raises(KeyError):download(store,pid,'rules')


def test_manual_file_cannot_escape_manifest_directory(handoff,tmp_path):
    store,pid,before,root,pack=handoff
    outside=root.parent/'outside.txt';outside.write_text('Not an attachment',encoding='utf8')
    path=root/'manifest.json';data=json.loads(path.read_text(encoding='utf8'))
    data['files'][0]['path']='../outside.txt';path.write_text(json.dumps(data),encoding='utf8')
    with pytest.raises(KeyError):download(store,pid,'rules')
    with pytest.raises(KeyError):download(store,pid,'unlisted-id')


def test_different_source_cannot_read_another_handoff(handoff):
    store,pid,before,root,pack=handoff
    other=processor.create(store,'Different material')
    processor.prepare(store,other['id'],[('different.txt',b'Completely different material.')])
    assert view(store,other['id'])['source_digest']!=pack['source_digest']
    with pytest.raises(KeyError):download(store,other['id'],'rules')


def test_normal_workbench_download_routes_serve_real_utf8_rules(handoff):
    store,pid,before,root,pack=handoff
    config=load_config();config['data_dir']=str(store.root)
    with TestClient(create_app(config)) as client:
        guide=client.get(f'/api/processor/projects/{pid}/manual-handoff').json()
        assert guide['package_url'].endswith('/pack.zip')
        package=client.get(guide['package_url'])
        assert package.status_code==200
        with zipfile.ZipFile(BytesIO(package.content)) as archive:
            assert pack['prompt'] in archive.read('START_HERE.md').decode('utf8')
            assert archive.read('source-text.txt').decode('utf8')==pack['source_text']
        response=client.get(f'/api/processor/projects/{pid}/manual-handoff/files/rules')
        assert response.status_code==200
        assert response.content.decode('utf8')==pack['prompt']
        assert 'attachment;' in response.headers['content-disposition']
        assert client.get(f'/api/processor/projects/{pid}/manual-handoff/files/no-such-file').status_code==404
    assert store.get(pid)==before


def test_every_material_has_complete_single_package_without_prepared_folders(tmp_path):
    from PIL import Image
    store=Store(tmp_path)
    picture=BytesIO();Image.new('RGB',(80,60),'blue').save(picture,'PNG')
    original_text='完整文字、数值 42.17 和引用归属必须保留。'.encode('utf8')
    p=processor.create(store,'真实原件',preferences='完整保留历史时点与所有附录')
    processor.prepare(store,p['id'],[('source.txt',original_text),('figure.png',picture.getvalue())])
    pack=processor.task_pack(store,p['id']);before=copy.deepcopy(store.get(p['id']))
    with zipfile.ZipFile(BytesIO(pack_zip(store,p['id']))) as archive:
        start=archive.read('START_HERE.md').decode('utf8')
        assert pack['prompt'] in start
        assert pack['template_version'] in start
        assert 'xhigh' in start and '自行要求切换 Pro' in start
        assert '实际图像工具打开对应附件' in start
        assert archive.read('source-text.txt').decode('utf8')==pack['source_text']
        manifest=json.loads(archive.read('manifest.json'))
        assert manifest['digest']==pack['digest']
        attachments=json.loads(archive.read('attachment-index.json'))
        assert len(attachments)==len(pack['attachments'])
        for row in attachments:
            assert archive.read(row['package_path'])==store.read_blob(row['sha256'])
        resources=json.loads(archive.read('resource-index.json'))
        assert [r['id'] for r in resources]==[r['id'] for r in pack['resources'] if r['usage']!='exclude']
        for row in resources:
            if row.get('sha256'):
                assert archive.read(row['package_path'])==store.read_blob(row['sha256'])
        assert any(archive.read(row['package_path'])==original_text for row in attachments)
        assert any(archive.read(row['package_path'])==picture.getvalue() for row in attachments)
    assert store.get(p['id'])==before
    assert store.jobs(p['id'])==[]
    assert view(store,p['id'])['channel_validation']=='pending_target_file_and_image_tools'


def test_single_package_uses_current_requirements_instead_of_old_prepared_prompt(handoff):
    store,pid,before,root,pack=handoff
    store.change(pid,lambda p:p['processor'].update(preferences='新偏好只用于本次材料，不得返回过期要求'))
    with zipfile.ZipFile(BytesIO(pack_zip(store,pid))) as archive:
        assert '新偏好只用于本次材料' in archive.read('START_HERE.md').decode('utf8')
        assert json.loads(archive.read('manifest.json'))['digest']==view(store,pid)['pack_digest']


@pytest.mark.parametrize('late_response', [False, True])
def test_default_manual_ui_has_three_actions_and_rejects_late_material_response(late_response):
    script=r'''
    const fs=require('node:fs'),vm=require('node:vm');
    const arg=JSON.parse(fs.readFileSync(0,'utf8'));
    class Element {
      constructor(tag,text=''){this.tag=tag;this.textContent=text||'';this.children=[];this.listeners={};}
      append(...nodes){for(const node of nodes){if(node.parent)node.parent.children=node.parent.children.filter(n=>n!==node);node.parent=this;this.children.push(node);}}
      prepend(node){this.append(node);this.children.unshift(this.children.pop());}
      replaceChildren(){for(const node of this.children)node.parent=null;this.children=[];}
      addEventListener(event,fn){this.listeners[event]=fn;}
    }
    const panel=new Element('section'),download=new Element('a'),copy=new Element('button');
    panel.append(download,copy);
    const prompt=new Element('details'),attachments=new Element('details');panel.append(prompt,attachments);
    const document={querySelector:s=>({'#manual-handoff':panel,'#task-pack-download':download,'#copy-prompt':copy,'#prompt-details':prompt,'#attachment-details':attachments}[s])};
    const source=fs.readFileSync(arg.root+'/sourceloom/static/processor_manual_handoff.js','utf8').replace(/export /g,'');
    const context={document,console};vm.createContext(context);vm.runInContext(source+';this.ManualHandoff=ManualHandoff;',context);
    let current='S6',resolve,returned=0;
    const guide=new context.ManualHandoff({element:(tag,text)=>new Element(tag,text),current:()=>current,
      api:()=>new Promise(r=>resolve=r),notify(){throw Error('unexpected warning');},showResult(){returned++;}});
    const pending=guide.update('S6','digest6');
    if(arg.late_response)current='S5';
    resolve({pack_digest:'digest6',package_url:'/S6/pack.zip'});
    (async()=>{
      await pending;
      const actions=guide.root.children.find(n=>n.tag==='div');
      if(actions.children.length!==3)throw Error('default workflow has extra actions');
      const back=actions.children[2];back.listeners.click();
      const text=guide.root.children.flatMap(n=>[n.textContent,...n.children.map(c=>c.textContent)]).join('\n');
      if(prompt.parent!==attachments.parent)throw Error('existing details lost');
      process.stdout.write(JSON.stringify({data:guide.data,download:download.href||null,
        labels:actions.children.map(n=>n.textContent),message:guide.message(),text,returned}));
    })().catch(e=>{console.error(e);process.exit(1);});
    '''
    result=subprocess.run([shutil.which('node'),'-e',script],input=json.dumps(dict(
        root=str(Path(__file__).parents[1]),late_response=late_response)),text=True,encoding='utf8',capture_output=True,
        check=True,timeout=10)
    ui=json.loads(result.stdout)
    assert ui['labels'][1:]==['复制开始指令','上传 GPT 成稿']
    assert ui['message']==START_MESSAGE
    assert 'GPT 对话中上传' in ui['text'] and '完整 .md 或 .txt' in ui['text']
    assert '第 1 组' not in ui['text']
    assert ui['returned']==1
    if late_response:
        assert ui['data'] is None and ui['download'] is None
    else:
        assert ui['data']['pack_digest']=='digest6' and ui['download']=='/S6/pack.zip'
