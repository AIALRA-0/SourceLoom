"""Addressable, immutable resources with explicit reads and recoverable releases."""
import copy
import json
import re
import time
from bs4 import BeautifulSoup
from .store import digest


SEARCH_ROUTE_ORDER = {
    'general': ('tinyfish', 'octen', 'parallel'),
    'academic': ('openalex', 'tinyfish', 'octen', 'parallel'),
}


def _route_value(route, key, default=None):
    if isinstance(route, dict):
        return route.get(key, default)
    return getattr(route, key, default)


def _route_name(route):
    return str(_route_value(route, 'provider_id',
               _route_value(route, 'id', _route_value(route, 'provider', '')))).casefold()


def _route_url(route):
    from urllib.parse import urljoin
    explicit=_route_value(route, 'search_url') or _route_value(route, 'search_endpoint')
    if explicit:return str(explicit)
    base=str(_route_value(route, 'base_url', '') or '').rstrip('/')
    endpoint=str(_route_value(route, 'endpoint', '') or '')
    if not base or endpoint in {'/responses','/chat/completions'}:
        return ''
    return urljoin(base+'/',endpoint.lstrip('/'))


def _search_matches(raw, mime):
    """Normalize common JSON/RSS result shapes without treating snippets as evidence."""
    text=raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else str(raw)
    parsed=None
    if 'json' in (mime or '').casefold() or text.lstrip().startswith(('{','[')):
        try:parsed=json.loads(text)
        except (TypeError,ValueError):parsed=None
    if parsed is not None:
        rows=parsed if isinstance(parsed,list) else None
        if rows is None and isinstance(parsed,dict):
            for key in ('results','items','matches','data','works'):
                value=parsed.get(key)
                if isinstance(value,list):rows=value;break
                if isinstance(value,dict) and isinstance(value.get('results'),list):rows=value['results'];break
        rows=rows or []
        result=[]
        for row in rows:
            if not isinstance(row,dict):continue
            location=row.get('primary_location') if isinstance(row.get('primary_location'),dict) else {}
            source=location.get('source') if isinstance(location.get('source'),dict) else {}
            url=(row.get('url') or row.get('link') or row.get('href') or
                 location.get('landing_page_url') or source.get('homepage_url') or
                 row.get('id') or '')
            if not str(url).startswith(('http://','https://')):continue
            title=row.get('title') or row.get('display_name') or row.get('name') or ''
            excerpts=row.get('excerpts')
            if isinstance(excerpts,list):excerpts=' '.join(str(item) for item in excerpts)
            snippet=row.get('snippet') or row.get('description') or row.get('highlight') or excerpts or ''
            result.append(dict(title=str(title),url=str(url),snippet=str(snippet)))
        return result
    try:
        from defusedxml import ElementTree
        root=ElementTree.fromstring(raw)
    except Exception:
        return []
    result=[]
    for item in root.findall('.//item'):
        target=item.findtext('link','')
        if target.startswith(('http://','https://')):
            result.append(dict(title=item.findtext('title',''),url=target,
                               snippet=item.findtext('description','')))
    return result


def canonical_url(value):
    """Return a stable URL identity without changing the fetched URL."""
    from urllib.parse import urlsplit, urlunsplit
    parsed = urlsplit((value or '').strip())
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        return value.strip()
    host = parsed.hostname.casefold()
    port = parsed.port
    if port and not ((parsed.scheme == 'http' and port == 80) or
                     (parsed.scheme == 'https' and port == 443)):
        host += ':' + str(port)
    path = parsed.path.rstrip('/') or '/'
    return urlunsplit((parsed.scheme.casefold(), host, path, parsed.query, ''))


class Resources:
    def __init__(self, store, source, state=None, search_routes=None, search_order=None):
        self.store = store
        if isinstance(search_routes, dict):
            self.search_routes=[dict(value, provider_id=key) if isinstance(value,dict)
                                else value for key,value in search_routes.items()]
        else:
            self.search_routes = list(search_routes or [])
        requested=tuple(str(name).casefold() for name in (search_order or SEARCH_ROUTE_ORDER['general'])
                        if str(name).casefold() in {'tinyfish','octen','parallel','openalex'})
        general=tuple(name for name in requested if name!='openalex') or SEARCH_ROUTE_ORDER['general']
        self.search_route_order={'general':general,'academic':('openalex',)+general}
        self.state = copy.deepcopy(state or {'entries': {}, 'opened': {}, 'reads': [], 'spans': {},
                                             'url_index': {}, 'search_index': {}})
        self.state.setdefault('url_index', {})
        self.state.setdefault('search_index', {})
        for key, entry in self.state['entries'].items():
            if entry.get('kind') == 'external':
                address = entry.get('original_url') or entry.get('locator')
                if address:
                    self.state['url_index'].setdefault(canonical_url(address), key)
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
        if metadata.get('kind') == 'external':
            address = metadata.get('original_url') or metadata.get('locator')
            if address:
                self.state['url_index'][canonical_url(address)] = key
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
            from urllib.parse import urlencode
            from .network import api_request, fetch
            query=action.get('query','').strip()
            if not query:raise ValueError('搜索词不能为空')
            profile=action.get('search_profile','general')
            query_key=profile+':'+ ' '.join(query.casefold().split())
            prior=self.state['search_index'].get(query_key)
            if prior:
                result=copy.deepcopy(prior)
                result['reused_search']=True
                self.state['reads'].append(result)
                return result
            configured={_route_name(route):route for route in self.search_routes if _route_name(route)}
            attempts=[];matches=[];provider='';route_url='';raw=b'';mime=''
            for route_name in self.search_route_order.get(profile, self.search_route_order['general']):
                route=configured.get(route_name)
                if not route or not bool(_route_value(route,'enabled',True)):continue
                endpoint=_route_url(route)
                if not endpoint:continue
                api_key=str(_route_value(route,'api_key','') or '')
                if route_name in {'tinyfish','octen','parallel'} and not api_key:
                    attempts.append(dict(provider=route_name,status='unavailable',reason='missing server-side credential'))
                    continue
                try:
                    parameters=_route_value(route,'model_parameters',{}) or {}
                    if route_name=='tinyfish':
                        url=endpoint+('?' if '?' not in endpoint else '&')+urlencode({'query':query})
                        raw,mime,_=api_request(url,headers={'X-API-Key':api_key},timeout=15)
                    elif route_name=='openalex':
                        values={'search':query,'per-page':int(parameters.get('perPage',3) or 3)}
                        if api_key:values['api_key']=api_key
                        url=endpoint+('?' if '?' not in endpoint else '&')+urlencode(values)
                        raw,mime,_=api_request(url,timeout=15)
                    elif route_name=='octen':
                        raw,mime,_=api_request(endpoint,method='POST',headers={'x-api-key':api_key},
                            json_body={'query':query,'count':int(parameters.get('count',8) or 8)},timeout=15)
                    else:
                        raw,mime,_=api_request(endpoint,method='POST',headers={'x-api-key':api_key},
                            json_body={'objective':query,'search_queries':[query],
                                'mode':str(parameters.get('mode','turbo')),
                                'advanced_settings':{'max_results':int(parameters.get('maxResults',8) or 8),
                                    'excerpt_settings':{'max_chars_per_result':2000}}},timeout=15)
                    matches=_search_matches(raw,mime)
                    attempts.append(dict(provider=route_name,status='matched' if matches else 'empty'))
                    if matches:
                        provider=route_name;route_url=endpoint;break
                except (ValueError,OSError,TimeoutError) as error:
                    attempts.append(dict(provider=route_name,status='unavailable',reason=str(error)[:240]))
            if not matches:
                # Compatibility fallback for deployments that have not yet
                # supplied the ReadWeave search registry.
                url='https://www.bing.com/search?'+urlencode({'format':'rss','q':query})
                raw,_,_=fetch(url,{'text/xml','application/xml','application/rss+xml'},timeout=15)
                matches=_search_matches(raw,'application/rss+xml')
                provider='bing_rss';route_url='https://www.bing.com/search'
                attempts.append(dict(provider=provider,status='matched' if matches else 'empty'))
            result=dict(kind=kind,query=query,search_profile=profile,matches=matches,
                search_provider=provider,search_route=route_url,attempts=attempts,
                snapshot_blob=self.store.blob(raw),
                evidence_status='discovery_only; open the primary page before citing its claims')
            self.state['search_index'][query_key]=copy.deepcopy(result)
        elif kind == 'page':
            if not allow_external:
                raise ValueError('当前任务禁止外部查证')
            from .network import fetch
            from urllib.parse import urlsplit,urlunsplit
            url = action['url']
            key = 'external-' + digest(url.encode())[:20]
            canonical=canonical_url(url)
            indexed=self.state['url_index'].get(canonical)
            if indexed and indexed in self.state['entries']:
                entry=self.state['entries'][indexed]
                length=entry['chars']
                result=self.read(indexed, 0, min(length, 6000))
                result['complete']=length <= 6000
                result['image_refs']=entry.get('image_refs',[])
                result['reused_external']=True
                return result
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
                    original_path=urlsplit(url).path.rstrip('/').lower()
                    final_path=urlsplit(final).path.rstrip('/').lower()
                    access_segments={'login','signin','sign-in','auth','authenticate'}
                    redirected_to_access_gate=(original_path!=final_path and
                        any(part in access_segments for part in final_path.split('/')))
                    if redirected_to_access_gate:
                        result=dict(kind='page',url=url,status='unavailable',
                            reason='目标页重定向到登录或访问验证页面，未返回链接所指正文',
                            resolved_url=final,snapshot_blob=self.store.blob(raw),fetched_at=time.time())
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
