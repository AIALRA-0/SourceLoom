"""Native import with durable identity and independently read attachment bytes."""
import json
import os
import time
import re
from uuid import uuid4
from collections import Counter
from io import BytesIO
import zipfile
from urllib.parse import parse_qs, quote, urlsplit
import httpx
from bs4 import BeautifulSoup
from .export import export_zip
from .store import Conflict,digest


def note_url(config, note_id):
    """Build an editor route only from a configured instance and a real note ID."""
    base=str(config.get('readweave_url') or '').rstrip('/')
    parsed=urlsplit(base)
    if (parsed.scheme not in {'http','https'} or not parsed.netloc or parsed.username or
            parsed.password or parsed.query or parsed.fragment or not note_id):
        return None
    return base+'/#root/'+quote(str(note_id),safe='')


def _instance(config):
    return str(config.get('readweave_url') or '').rstrip('/')


def _candidate_key(raw):
    with zipfile.ZipFile(BytesIO(raw)) as z:
        meta=json.loads(z.read('!!!meta.json'))
    return next(a['value'] for a in meta['files'][0]['attributes'] if a['name']=='sourceloomCandidate')


def _receipt_path(store, pid, revision, key):
    return store.root/'readweave'/(pid+'-'+str(revision)+'-'+key.rsplit(':',1)[-1]+'.json')


def _save_receipt(path, payload):
    """Expose each confirmed phase as a complete JSON document to polling tabs."""
    temporary=path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    try:
        with temporary.open('x',encoding='utf-8') as output:
            json.dump(payload,output,ensure_ascii=False,indent=2)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_receipt(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise Conflict('ReadWeave 导入身份已占用，但回执尚不可读；未重复提交') from exc


def import_status(store, config, pid):
    """Read the exact current candidate's durable receipt without contacting ReadWeave.

    A submitted receipt without a note ID is intentionally unconfirmed.  This
    endpoint never retries a native import or invents a target URL.
    """
    p=store.get(pid)
    if not all(config.get(k) for k in ('readweave_url','readweave_token','readweave_parent')):
        return {'status':'not_configured','phase':'configuration','note_url':None}
    if note_url(config,'configured-note') is None:
        return {'status':'invalid_configuration','phase':'configuration','note_url':None}
    try:
        if 'processor' in p:
            from .processor import export_package
            raw=export_package(store,p)
        else:
            raw=export_zip(store,p)
    except Conflict as exc:
        return {'status':'not_ready','phase':'candidate','note_url':None,'message':str(exc)}
    key=_candidate_key(raw)
    record=_receipt_path(store,pid,p['revision'],key)
    if not record.exists():
        return {'status':'not_submitted','phase':'ready','candidate':key,'note_url':None}
    try:
        receipt=_read_receipt(record)
    except Conflict:
        return {'status':'receipt_unavailable','phase':'identity_claimed',
                'candidate':key,'note_url':None}
    if receipt.get('candidate')!=key:
        raise Conflict('ReadWeave 导入回执身份与当前候选不一致')
    target_matches=(receipt.get('parent')==config['readweave_parent'] and
                    receipt.get('instance')==_instance(config))
    receipt['note_url']=(note_url(config,receipt.get('note_id')) if target_matches and
                         receipt.get('status') in {'readback_passed','readback_gaps'} else None)
    if not target_matches:
        receipt['target_verification']='required'
    return receipt


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
            'image_descriptions':[n.get('alt','') for n in a.find_all('img')]==[n.get('alt','') for n in b.find_all('img')],
            'formula_source':[n.get_text() for n in a.select('.math-tex')]==[n.get_text() for n in b.select('.math-tex')],
            'local_fragment_targets':all(b.find(id=n['href'][1:]) is not None for n in b.select('a[href^="#"]') if not n['href'].startswith('#root/')),
            'external_links':[n['href'] for n in a.select('a[href]') if n['href'].startswith(('https://','http://'))]
                             ==[n['href'] for n in b.select('a[href]') if n['href'].startswith(('https://','http://'))]}


def _image_attachment_navigation(raw, note_id, verified_entities):
    """Project this note's verified image links to the real image endpoint.

    Native ZIP import turns attachment hrefs into an attachment-manager view,
    which some native editor builds cannot display. No text, images, source
    relationships or attachment bytes change in this navigation projection.
    """
    doc = BeautifulSoup(raw, 'html.parser')
    changes = []
    for link in doc.select('a[href]'):
        old = link['href']
        parsed = urlsplit(old)
        route, _, query = parsed.fragment.partition('?')
        parameters = parse_qs(query)
        if (parsed.scheme or parsed.netloc or parsed.path or route != 'root/'+note_id
                or parameters.get('viewMode') != ['attachments']
                or len(parameters.get('attachmentId', [])) != 1):
            continue
        attachment_id = parameters['attachmentId'][0]
        entity = verified_entities.get(attachment_id)
        if not entity or entity[1] != 'image' or not entity[2].startswith('image/'):
            continue
        new = 'api/attachments/'+quote(attachment_id, safe='')+'/image/'+quote(entity[0], safe='')
        link['href'] = new
        changes.append(dict(old_href=old, new_href=new, attachment_id=attachment_id,
                            attachment_sha256=entity[3]))
    return (str(doc) if changes else raw), changes


def _confirmed_navigation(raw, navigation):
    """Accept only the exact submitted projection, including native serialization.

    Native content saves serialize HTML void tags without their closing slash.
    The projection was serialized by this same parser before submission, so an
    exact canonical digest can identify it without relaxing any HTML checks.
    """
    raw_sha = digest(raw)
    try:
        canonical_sha = digest(str(BeautifulSoup(raw.decode('utf-8'), 'html.parser')).encode())
    except UnicodeError:
        canonical_sha = None
    expected = navigation.get('projected_content_sha256')
    if expected not in {raw_sha, canonical_sha}:
        return None
    return dict(navigation, status='confirmed_by_readback',
                readback_content_sha256=raw_sha,
                readback_canonical_sha256=canonical_sha)


def import_candidate(store,config,pid):
    if not all(config.get(k) for k in ['readweave_url','readweave_token','readweave_parent']):
        raise Conflict('先配置 ReadWeave 的地址、接口凭据与专用候选父笔记')
    if note_url(config,'configured-note') is None:
        raise Conflict('ReadWeave 地址必须是不带凭据、查询或片段的 HTTP(S) 实例地址')
    p=store.get(pid)
    from .checks import can_publish
    if (p.get('production') or {}).get('pipeline')=='active_composition_v2' and not can_publish(p):
        raise Conflict('只有已完成语义核对并由用户接受的当前版本才能导入 ReadWeave')
    if 'processor' in p:
        from .processor import export_package
        raw=export_package(store,p)
    else:
        raw=export_zip(store,p)
    parent=config['readweave_parent']
    with zipfile.ZipFile(BytesIO(raw)) as z:
        expected=z.read('material.html').decode()
        meta=json.loads(z.read('!!!meta.json'))
        key=next(a['value'] for a in meta['files'][0]['attributes'] if a['name']=='sourceloomCandidate')
        attachment_hashes={x['title']:digest(z.read(x['dataFileName'])) for x in meta['files'][0]['attachments']}
        expected_attachments=[(a['title'],a['role'],a['mime'],digest(z.read(a['dataFileName']))) for a in meta['files'][0]['attachments']]
        image_files={a['dataFileName']:digest(z.read(a['dataFileName'])) for a in meta['files'][0]['attachments'] if a['role']=='image'}
        expected_images=[image_files.get(n.get('src','')) for n in BeautifulSoup(expected,'html.parser').find_all('img')]
    work=store.root/'readweave';work.mkdir(exist_ok=True)
    legacy_record=work/(pid+'-'+str(p['revision'])+'.json')
    record=_receipt_path(store,pid,p['revision'],key)
    previous=_read_receipt(record)
    legacy=_read_receipt(legacy_record)
    if previous and ((previous.get('parent') and previous['parent']!=parent) or
                     (previous.get('instance') and previous['instance']!=_instance(config))):
        raise Conflict('候选已关联另一个 ReadWeave 实例或父笔记，未自动建立第二份导入')
    with httpx.Client(base_url=config['readweave_url'].rstrip('/')+'/etapi/',headers={'Authorization':config['readweave_token']},timeout=45,follow_redirects=False) as c:
        # Native import is not idempotent: query the exact label before every write.
        r=c.get('notes',params={'search':'#sourceloomCandidate','limit':1000});r.raise_for_status()
        matches=[n for n in r.json()['results'] if parent in n.get('parentNoteIds',[]) and
                 any(a.get('name')=='sourceloomCandidate' and a.get('value')==key for a in n.get('attributes',[]))]
        if len(matches)>1:raise Conflict('同一候选存在多个导入副本，需要先核对远端身份')
        if matches:
            nid=matches[0]['noteId']
            if previous and previous.get('note_id') and previous['note_id']!=nid:
                raise Conflict('候选回执与远端笔记身份不一致，未改写原回执')
            if not previous or not previous.get('note_id'):
                _save_receipt(record,{'status':'imported','phase':'readback',
                                      'candidate':key,'parent':parent,'instance':_instance(config),
                                      'note_id':nid,'created':time.time()})
        elif previous:
            if not previous.get('note_id'):
                raise Conflict('前次导入结果尚不确定，未再次提交导入；请核对原始远端记录')
            if not previous.get('parent') or not previous.get('instance'):
                raise Conflict('历史导入回执缺少目标身份，远端未找到原候选，未重复提交')
            nid=previous['note_id']
        else:
            if legacy and not legacy.get('note_id'):
                raise Conflict('旧版导入送达尚不确定，保留原记录，未因导出格式变化重复提交')
            try:
                with record.open('x',encoding='utf-8') as claim:
                    claim.write(json.dumps({'status':'submitted','phase':'remote_import',
                                           'candidate':key,'parent':parent,'instance':_instance(config),
                                           'created':time.time()}))
            except FileExistsError:
                raise Conflict('该导入已被另一请求提交，先查询原记录，未重复导入')
            r=c.post(f'notes/{parent}/import',content=raw,headers={'Content-Type':'application/octet-stream','Content-Transfer-Encoding':'binary'})
            r.raise_for_status();nid=r.json()['note']['noteId']
            _save_receipt(record,{'status':'imported','phase':'readback',
                                  'candidate':key,'parent':parent,'instance':_instance(config),
                                  'note_id':nid})
        content=c.get(f'notes/{nid}/content');content.raise_for_status()
        attachments=c.get(f'notes/{nid}/attachments');attachments.raise_for_status()
        remote=attachments.json()
        if isinstance(remote,dict):remote=remote.get('results',remote.get('attachments',[]))
        measured={}
        measured_entities={}
        for a in remote:
            r=c.get('attachments/'+a['attachmentId']+'/content');r.raise_for_status()
            measured[a['title']]=digest(r.content)
            measured_entities[a['attachmentId']]=(a['title'],a['role'],a['mime'],digest(r.content))
        navigation = (_read_receipt(record) or {}).get('navigation_projection')
        if navigation and navigation.get('status') == 'UNKNOWN':
            # A PUT may have committed despite a lost response. Resolve only
            # by reading its exact expected content; never repeat that write.
            confirmed = _confirmed_navigation(content.content, navigation)
            if confirmed is None:
                raise Conflict('原页链接更新结果尚不确定；已保留笔记与回执，未重复提交')
            navigation = confirmed
        elif Counter(measured_entities.values()) == Counter(expected_attachments):
            projected, links = _image_attachment_navigation(content.text, nid, measured_entities)
            if links:
                if not all(compare_html(content.text, projected).values()):
                    raise Conflict('原页链接投影会改变正文或资源关系，未提交')
                navigation = dict(status='UNKNOWN', original_content_sha256=digest(content.content),
                                  projected_content_sha256=digest(projected.encode()), links=links)
                claim = _read_receipt(record) or {}
                _save_receipt(record, dict(claim, navigation_projection=navigation))
                update = c.put(f'notes/{nid}/content', content=projected.encode(),
                               headers={'Content-Type': 'text/plain; charset=utf-8'})
                update.raise_for_status()
                content = c.get(f'notes/{nid}/content');content.raise_for_status()
                confirmed = _confirmed_navigation(content.content, navigation)
                if confirmed is None:
                    raise Conflict('原页链接更新后读回不一致；保留未知回执，未重投')
                navigation = confirmed
        checks=compare_html(expected,content.text)
        checks['all_attachment_bytes']=all(measured.get(title)==value for title,value in attachment_hashes.items())
        checks['attachment_count']=len(remote)==len(expected_attachments)
        checks['attachment_identity_role_and_bytes']=Counter(measured_entities.values())==Counter(expected_attachments)
        images=[]
        for pic in BeautifulSoup(content.text,'html.parser').find_all('img'):
            match=re.match(r'^/?api/attachments/([^/]+)/image/',pic.get('src',''))
            entity=measured_entities.get(match[1]) if match else None
            images.append(entity[3] if entity and entity[1]=='image' and entity[2].startswith('image/') else None)
        checks['rendered_image_targets']=None not in images and None not in expected_images and images==expected_images
        if navigation:
            current_links = [a['href'] for a in BeautifulSoup(content.text,'html.parser').select('a[href]')]
            checks['image_attachment_navigation'] = all(link['new_href'] in current_links for link in navigation['links'])
        # Close and reopen the HTTP client to avoid mistaking a local buffer for readback.
    with httpx.Client(timeout=20,follow_redirects=False) as c:
        second=c.get(config['readweave_url'].rstrip('/')+'/etapi/notes/'+nid+'/content',headers={'Authorization':config['readweave_token']})
        second.raise_for_status()
        checks['new_connection_readback']=second.content==content.content
    passed=all(checks.values())
    result={'status':'readback_passed' if passed else 'readback_gaps','phase':'complete' if passed else 'readback_gaps',
            'candidate':key,'parent':parent,'instance':_instance(config),
            'note_id':nid,'note_url':note_url(config,nid),
            'checks':checks,'editor_save_reopen':'not_verified','user_accepted':True,'created':time.time()}
    if 'processor' in p:
        result['semantic_status']='not_reviewed'
        result['user_accepted']=False
    if navigation:
        result['navigation_projection'] = navigation
    _save_receipt(record,result)
    store.event(pid,'readweave_readback',result)
    def save(project):
        project['readweave']=result
        if passed and (project.get('production') or {}).get('pipeline')=='active_composition_v2':
            from .checks import transition_delivery
            transition_delivery(project,'publish')
    store.change(pid,save)
    return result
