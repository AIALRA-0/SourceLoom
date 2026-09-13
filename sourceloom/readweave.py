"""Native import with durable identity and independently read attachment bytes."""
import json
import time
from io import BytesIO
import zipfile
import httpx
from bs4 import BeautifulSoup
from .export import export_zip
from .store import Conflict,digest


def compare_html(expected,actual):
    a,b=BeautifulSoup(expected,'html.parser'),BeautifulSoup(actual,'html.parser')
    norm=lambda s:' '.join(s.split())
    return {'visible_text':norm(a.get_text(' '))==norm(b.get_text(' ')),
            'table_cells':[[norm(c.get_text()),c.get('rowspan','1'),c.get('colspan','1')] for c in a.find_all(['td','th'])]
                          ==[[norm(c.get_text()),c.get('rowspan','1'),c.get('colspan','1')] for c in b.find_all(['td','th'])],
            'code_blocks':[c.get_text() for c in a.find_all('pre')]==[c.get_text() for c in b.find_all('pre')],
            'anchor_ids':[n.get('data-readweave-anchor-id') for n in a.select('[data-readweave-anchor-id]')]
                         ==[n.get('data-readweave-anchor-id') for n in b.select('[data-readweave-anchor-id]')],
            'image_count':len(a.find_all('img'))==len(b.find_all('img')),
            'local_fragment_targets':all(b.find(id=n['href'][1:]) is not None for n in b.select('a[href^="#"]') if not n['href'].startswith('#root/')),
            'external_links':[n['href'] for n in a.select('a[href]') if n['href'].startswith(('https://','http://'))]
                             ==[n['href'] for n in b.select('a[href]') if n['href'].startswith(('https://','http://'))]}


def import_candidate(store,config,pid):
    if not all(config.get(k) for k in ['readweave_url','readweave_token','readweave_parent']):
        raise Conflict('先配置 ReadWeave 的地址、接口凭据与专用候选父笔记')
    p=store.get(pid);raw=export_zip(store,p)
    key=f'{pid}:{p["revision"]}';parent=config['readweave_parent']
    with zipfile.ZipFile(BytesIO(raw)) as z:
        expected=z.read('material.html').decode()
        meta=json.loads(z.read('!!!meta.json'))
        attachment_hashes={x['title']:digest(z.read(x['dataFileName'])) for x in meta['files'][0]['attachments']}
    work=store.root/'readweave';work.mkdir(exist_ok=True)
    record=work/(pid+'-'+str(p['revision'])+'.json')
    previous=json.loads(record.read_text(encoding='utf-8')) if record.exists() else None
    with httpx.Client(base_url=config['readweave_url'].rstrip('/')+'/etapi/',headers={'Authorization':config['readweave_token']},timeout=45,follow_redirects=False) as c:
        # Native import is not idempotent: query the exact label before every write.
        r=c.get('notes',params={'search':'#sourceloomCandidate','limit':1000});r.raise_for_status()
        matches=[n for n in r.json()['results'] if parent in n.get('parentNoteIds',[]) and
                 any(a.get('name')=='sourceloomCandidate' and a.get('value')==key for a in n.get('attributes',[]))]
        if len(matches)>1:raise Conflict('同一候选存在多个导入副本，需要先核对远端身份')
        if matches:
            nid=matches[0]['noteId']
        elif previous:
            if not previous.get('note_id'):
                raise Conflict('前次导入结果尚不确定，未再次提交导入；请核对原始远端记录')
            nid=previous['note_id']
        else:
            record.write_text(json.dumps({'status':'submitted','candidate':key,'parent':parent,'created':time.time()}),encoding='utf-8')
            r=c.post(f'notes/{parent}/import',content=raw,headers={'Content-Type':'application/octet-stream','Content-Transfer-Encoding':'binary'})
            r.raise_for_status();nid=r.json()['note']['noteId']
            record.write_text(json.dumps({'status':'imported','candidate':key,'parent':parent,'note_id':nid}),encoding='utf-8')
        content=c.get(f'notes/{nid}/content');content.raise_for_status()
        attachments=c.get(f'notes/{nid}/attachments');attachments.raise_for_status()
        remote=attachments.json()
        if isinstance(remote,dict):remote=remote.get('results',remote.get('attachments',[]))
        measured={}
        for a in remote:
            r=c.get('attachments/'+a['attachmentId']+'/content');r.raise_for_status()
            measured[a['title']]=digest(r.content)
        checks=compare_html(expected,content.text)
        checks['all_attachment_bytes']=all(measured.get(title)==value for title,value in attachment_hashes.items())
        checks['attachment_count']=len(remote)==len(attachment_hashes)
        # Close and reopen the HTTP client to avoid mistaking a local buffer for readback.
    with httpx.Client(timeout=20,follow_redirects=False) as c:
        second=c.get(config['readweave_url'].rstrip('/')+'/etapi/notes/'+nid+'/content',headers={'Authorization':config['readweave_token']})
        second.raise_for_status()
        checks['new_connection_readback']=second.content==content.content
    result={'status':'readback_passed' if all(checks.values()) else 'readback_gaps','candidate':key,'note_id':nid,
            'checks':checks,'editor_save_reopen':'not_verified','user_accepted':False,'created':time.time()}
    record.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    store.event(pid,'readweave_readback',result)
    store.change(pid,lambda p:p.update(readweave=result))
    return result
