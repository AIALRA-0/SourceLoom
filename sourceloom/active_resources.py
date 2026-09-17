"""Addressable, immutable resources with explicit reads and recoverable releases."""
import copy
import re
import time
from bs4 import BeautifulSoup
from .store import digest


class Resources:
    def __init__(self, store, source, state=None):
        self.store = store
        self.state = copy.deepcopy(state or {'entries': {}, 'opened': {}, 'reads': [], 'spans': {}})
        self.order = [o['id'] for o in source['objects']]
        for obj in source['objects']:
            targets={k:obj[k] for k in ('target','original_target') if k in obj}
            old=self.state['entries'].get(obj['id'])
            # Older checkpoints omitted addresses from the catalog. Fill only
            # absent metadata after verifying that the source text is unchanged.
            if old and old['blob']==digest(obj.get('text','').encode()):
                for key,value in targets.items():old.setdefault(key,value)
            self.add(obj['id'], obj.get('text', ''), kind=obj['kind'], locator=obj['locator'],
                     resource_id=obj.get('resource_id'), raw=obj.get('raw', ''), **targets)

    def add(self, key, text, **metadata):
        blob = self.store.blob(text.encode())
        entry = dict(id=key, blob=blob, chars=len(text), **metadata)
        old = self.state['entries'].get(key)
        if old and old != entry:
            raise ValueError('资源版本发生变化，不能复用旧读取记录：' + key)
        self.state['entries'][key] = entry
        return key

    def text(self, key):
        return self.store.read_blob(self.state['entries'][key]['blob']).decode()

    def catalog(self):
        return [{k: v for k, v in row.items() if k != 'raw'} for row in self.state['entries'].values()]

    def read(self, key, start=0, end=None):
        value = self.text(key)
        end = len(value) if end is None else end
        if not 0 <= start <= end <= len(value):
            raise ValueError('资源读取范围无效：' + key)
        text = value[start:end]
        address = digest(text.encode())
        opened = self.state['opened'].setdefault(address, {'text': text, 'addresses': []})
        location = dict(id=key, start=start, end=end)
        if location not in opened['addresses']:
            opened['addresses'].append(location)
        self.state['spans'].setdefault(key, []).append([start, end])
        receipt = dict(kind='read', **location, content_digest=address)
        self.state['reads'].append(receipt)
        return receipt

    def fully_read(self, key):
        cursor = 0
        for start, end in sorted(self.state['spans'].get(key, [])):
            if start > cursor:
                return False
            cursor = max(cursor, end)
        return key in self.state['spans'] and cursor >= self.state['entries'][key]['chars']

    def context(self):
        return list(self.state['opened'].values())

    def execute(self, action, allow_external=True):
        kind, key = action['kind'], action.get('resource_id', '')
        if kind == 'read':
            return self.read(key, action.get('start', 0), action.get('end'))
        if kind == 'release':
            for address, item in list(self.state['opened'].items()):
                item['addresses'] = [a for a in item['addresses'] if a['id'] != key]
                if not item['addresses']:
                    del self.state['opened'][address]
            result = dict(kind=kind, id=key, retained_in_archive=True)
        elif kind == 'neighbors':
            index = self.order.index(key)
            return [self.read(k) for k in self.order[max(0, index-1):index+2]]
        elif kind == 'find':
            needle = action.get('query', '')
            if not needle:
                raise ValueError('查找内容不能为空')
            targets = [key] if key else list(self.state['entries'])
            matches = []
            more=False
            for target in targets:
                value = self.text(target)
                for match in re.finditer(re.escape(needle),value,re.IGNORECASE):
                    if len(matches)>=100:more=True;break
                    matches.append(dict(id=target, start=match.start(), end=match.end()))
                if more:break
            result = dict(kind=kind, query=needle, matches=matches,has_more=more)
        elif kind=='search':
            if not allow_external:raise ValueError('当前任务禁止外部查证')
            from urllib.parse import urlencode,urlsplit
            from defusedxml import ElementTree
            from .network import fetch
            query=action.get('query','').strip()
            if not query:raise ValueError('搜索词不能为空')
            url='https://www.bing.com/search?'+urlencode({'format':'rss','q':query})
            raw,_,_=fetch(url,{'text/xml','application/xml','application/rss+xml'},timeout=15)
            root=ElementTree.fromstring(raw)
            matches=[]
            for item in root.findall('./channel/item'):
                target=item.findtext('link','')
                if urlsplit(target).scheme!='https':continue
                matches.append(dict(title=item.findtext('title',''),url=target,
                                    snippet=item.findtext('description','')))
            result=dict(kind=kind,query=query,matches=matches,snapshot_blob=self.store.blob(raw),
                evidence_status='discovery_only; open the primary page before citing its claims')
        elif kind == 'page':
            if not allow_external:
                raise ValueError('当前任务禁止外部查证')
            from .network import fetch
            from urllib.parse import urlsplit,urlunsplit
            url = action['url']
            key = 'external-' + digest(url.encode())[:20]
            if key not in self.state['entries']:
                try:
                    parsed=urlsplit(url)
                    # Historical source pages often link to their own HTTP
                    # archive. Fetch the same host/path through HTTPS while
                    # keeping the literal original link and recording the
                    # actual fetched address separately.
                    request_url=urlunsplit(parsed._replace(scheme='https')) if parsed.scheme=='http' else url
                    raw, mime, final = fetch(request_url, {'text/html', 'text/plain', 'text/markdown'}, timeout=15)
                except (ValueError,OSError,TimeoutError) as error:
                    result=dict(kind='page',url=url,status='unavailable',
                                reason=str(error)[:240],fetched_at=time.time())
                    self.state['reads'].append(result)
                    return result
                if mime == 'text/html':
                    from urllib.parse import urljoin
                    doc = BeautifulSoup(raw, 'html.parser')
                    page_title=doc.title.get_text(' ',strip=True) if doc.title else ''
                    if (page_title=='Client Challenge' and
                            'A required part of this site' in doc.get_text(' ',strip=True)):
                        result=dict(kind='page',url=url,status='unavailable',
                            reason='目标站返回 Client Challenge 客户端校验页，未返回项目正文',
                            snapshot_blob=self.store.blob(raw),fetched_at=time.time())
                        self.state['reads'].append(result)
                        return result
                    image_refs=[]
                    for image in doc.find_all('img'):
                        address=image.get('src') or image.get('data-src') or ''
                        if not address or address.startswith('data:'):continue
                        target=urljoin(final,address)
                        if target not in {item['url'] for item in image_refs}:
                            image_refs.append(dict(url=target,alt=image.get('alt',''),
                                nearby_text=image.parent.get_text(' ',strip=True)[:160] if image.parent else ''))
                    for node in doc.select('script,style,nav,header,footer,noscript'):
                        node.decompose()
                    text = doc.get_text('\n', strip=True)
                    if image_refs:
                        text += '\n\nLinked page image references (images require a separate image action):\n'
                        text += '\n'.join(f"{item['url']} | alt={item['alt']} | nearby={item['nearby_text']}"
                                          for item in image_refs[:12])
                else:
                    text = raw.decode('utf-8', errors='replace')
                self.add(key, text, kind='external', locator=final,
                         original_url=url, snapshot_blob=self.store.blob(raw), fetched_at=time.time(),
                         image_refs=image_refs[:12] if mime=='text/html' else [])
            # Long references are catalogued, never silently truncated as full pages
            length = self.state['entries'][key]['chars']
            result = self.read(key, 0, min(length, 6000))
            result['complete'] = length <= 6000
            result['image_refs']=self.state['entries'][key].get('image_refs',[])
        elif kind=='image':
            if not allow_external:raise ValueError('当前任务禁止外部查证')
            from .network import fetch
            url=action['url']
            parent=self.state['entries'].get(key)
            if parent and url not in {item['url'] for item in parent.get('image_refs',[])}:
                raise ValueError('图片地址不属于已经读取的目标页')
            parent_page=parent.get('original_url',parent.get('locator','')) if parent else ''
            key='external-image-'+digest(url.encode())[:20]
            if key in self.state['entries']:
                result=self.read(key)
                result['reused_visual_evidence']=True
            else:
                raw,mime,final=fetch(url,{'image/png','image/jpeg','image/webp','image/gif'},timeout=15)
                result=dict(kind='image',id=key,sha256=self.store.blob(raw),mime=mime,
                            url=final,original_url=url,parent_page_url=parent_page,
                            visual_evidence_required=True)
        else:
            raise ValueError('未知资源动作')
        self.state['reads'].append(result)
        return result
