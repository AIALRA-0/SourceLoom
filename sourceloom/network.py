"""Bounded HTTPS snapshots with connection-pinned public DNS addresses."""

import http.client
import ipaddress
import socket
import ssl
import certifi
import time
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import PurePosixPath
from bs4 import BeautifulSoup
from markdown_it import MarkdownIt
from urllib.parse import quote, unquote, urljoin, urlsplit

from .ingest import MAX_FILE

DOCUMENT_TYPES={'text/html':'html','text/plain':'txt','text/markdown':'md','application/pdf':'pdf',
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document':'docx'}


def rasterize_svg(image):
    """Convert one self-contained SVG into a bounded PNG.

    The original SVG remains in the source object.  HTML, scripts, external
    references and unbounded page geometry are rejected before CairoSVG sees
    the document.
    """
    from defusedxml import ElementTree
    from defusedxml.common import DefusedXmlException
    from xml.etree.ElementTree import ParseError
    import cairosvg
    if len(image)>2_000_000:
        raise ValueError('SVG 源文件超过安全处理上限')
    try:
        root=ElementTree.fromstring(image)
    except (ParseError, DefusedXmlException) as exc:
        raise ValueError('SVG 原始结构无法安全解析') from exc
    if root.tag.rsplit('}',1)[-1].lower()!='svg':
        raise ValueError('SVG 根元素无效')
    identifiers={element.get('id') for element in root.iter() if element.get('id')}
    def safe_css_urls(value):
        # Local paint servers such as gradients are ordinary SVG structure.
        # Accept only fragment URLs that resolve inside this document; remote
        # URLs and malformed references remain blocked before CairoSVG runs.
        starts=list(re.finditer(r'url\s*\(',value or '',re.I))
        references=list(re.finditer(
            r'''url\(\s*(?P<quote>["']?)#(?P<id>[A-Za-z0-9_][\w:.-]*)(?P=quote)\s*\)''',
            value or '',re.I))
        return (len(starts)==len(references) and
                all(match['id'] in identifiers for match in references))
    for parent in list(root.iter()):
        children=list(parent)
        if not any(child.tag.rsplit('}',1)[-1].lower()=='foreignobject' for child in children):
            continue
        if parent.tag.rsplit('}',1)[-1].lower()!='switch' or not any(
                child.tag.rsplit('}',1)[-1].lower()=='text' for child in children):
            raise ValueError('SVG 包含无法安全替代的 HTML 内容')
        for child in children:
            if child.tag.rsplit('}',1)[-1].lower()=='foreignobject':parent.remove(child)
    if any(
        element.tag.rsplit('}',1)[-1].lower() in {'script','foreignobject','use'} or
        (element.tag.rsplit('}',1)[-1].lower()=='style' and
         (re.search(r'@import\b',element.text or '',re.I) or
          not safe_css_urls(element.text or ''))) or
        any((key.rsplit('}',1)[-1].lower()=='href' and value and
             not (element.tag.rsplit('}',1)[-1].lower()=='image' and
                  value.startswith(('data:image/png;base64,','data:image/jpeg;base64,')))) or
            (re.search(r'url\s*\(',value,re.I) and not safe_css_urls(value))
            for key,value in element.attrib.items())
        for element in root.iter()):
        raise ValueError('SVG 包含不可安全栅格化的外部或脚本内容')
    def length(value):
        match=re.fullmatch(r'\s*(\d+(?:\.\d+)?)(?:px)?\s*',value or '')
        return float(match[1]) if match else None
    width=length(root.get('width'));height=length(root.get('height'))
    viewbox_value=root.get('viewBox') or root.get('viewbox') or ''
    viewbox=[float(part) for part in re.split(r'[\s,]+',viewbox_value.strip()) if part] if viewbox_value else []
    if len(viewbox)==4 and viewbox[2]>0 and viewbox[3]>0:
        width=width or viewbox[2];height=height or viewbox[3]
    width=width or 512;height=height or 512
    if width<=0 or height<=0 or width/height>32 or height/width>32:
        raise ValueError('SVG 页面比例超出安全范围')
    scale=min(1600/max(width,height),max(1,512/max(width,height)))
    try:
        return cairosvg.svg2png(bytestring=ElementTree.tostring(root),
            output_width=max(1,round(width*scale)),output_height=max(1,round(height*scale)))
    except Exception as exc:
        raise ValueError('SVG 无法安全栅格化') from exc


def public_addresses(host):
    result=[]
    for entry in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM):
        address=entry[4][0]
        ip=ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("网页地址解析到非公网目标，已拒绝抓取")
        if address not in result:
            result.append(address)
    if not result:
        raise ValueError("网页域名没有可用地址")
    return result


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self,hostname,address,timeout=12):
        # Use the same maintained CA bundle as the HTTP client dependency
        # instead of the host machine's optional certificate store.  Minimal
        # Windows and VPS images frequently have an incomplete store even when
        # the public site has a valid chain.
        context=ssl.create_default_context(cafile=certifi.where())
        super().__init__(hostname,timeout=timeout,context=context)
        self.address=address

    def connect(self):
        sock=socket.create_connection((self.address,443),timeout=self.timeout)
        self.sock=self._context.wrap_socket(sock,server_hostname=self.host)


def fetch(url,allowed_types=None,timeout=12):
    allowed_types=allowed_types or set(DOCUMENT_TYPES)
    deadline=time.monotonic()+timeout
    for _ in range(4):
        parsed=urlsplit(url)
        if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None,443):
            raise ValueError("仅抓取不含凭据、使用标准端口的 HTTPS 网页")
        addresses=public_addresses(parsed.hostname)
        remaining=deadline-time.monotonic()
        if remaining<=0:raise ValueError('网页获取已达到等待上限')
        # GET is read-only. Try a second validated address after a connection
        # failure, within the same total deadline; never retry a rejected URL.
        candidates=addresses[:2]
        for index,address in enumerate(candidates):
            remaining=deadline-time.monotonic()
            if remaining<=0:raise ValueError('网页获取已达到等待上限')
            cx=PinnedHTTPS(parsed.hostname,address,min(4,remaining) if index+1<len(candidates) else remaining)
            try:
                # HTML permits literal spaces and other Unicode characters in
                # discovered href/src values, while the HTTP request target
                # requires their percent-encoded wire form.  Preserve existing
                # escapes and URL separators instead of rejecting a valid asset.
                path=quote(parsed.path or "/",safe="/%:@-._~!$&'()*+,;=")
                query=quote(parsed.query,safe="=&?/:;+,%@-._~!$'()*")
                cx.request("GET",path+("?"+query if query else ""),headers={"User-Agent":"SourceLoom/0.1 (+bounded document snapshot)","Accept-Encoding":"identity"})
                r=cx.getresponse()
                if r.status in (301,302,303,307,308):
                    url=urljoin(url,r.getheader("Location", ""))
                    break
                if r.status!=200:
                    raise ValueError(f"网页返回 {r.status}，未取得完整材料")
                mime=r.getheader("Content-Type", "").split(";",1)[0]
                if mime not in allowed_types:
                    raise ValueError("网页类型不在当前接入范围")
                raw=r.read(MAX_FILE+1)
                if len(raw)>MAX_FILE:
                    raise ValueError("网页超过 25 MB 上限")
                return raw,mime,url
            except (OSError,http.client.HTTPException) as exc:
                if index+1==len(candidates):raise ValueError('网页连接暂时失败，地址已保留，可以重试或上传原始文件') from exc
            finally:
                cx.close()
    raise ValueError("网页重定向超过三次，已停止")


def api_request(url, method="GET", headers=None, json_body=None, timeout=15, max_bytes=2_000_000):
    """Call one configured HTTPS API while pinning the validated public address.

    API redirects are rejected so authorization headers can never cross hosts.
    Response bodies and upstream error details are bounded and are not echoed.
    """
    parsed=urlsplit(url)
    if (parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None,443)):
        raise ValueError("API 地址必须是不含凭据、使用标准端口的 HTTPS 地址")
    method=str(method).upper()
    if method not in {"GET","POST"}:
        raise ValueError("当前 API 调用只允许 GET 或 POST")
    raw_body=(json.dumps(json_body,ensure_ascii=False,separators=(",", ":")).encode("utf-8")
              if json_body is not None else None)
    safe_headers={"User-Agent":"SourceLoom/0.1 (+bounded configured API)",
                  "Accept":"application/json","Accept-Encoding":"identity"}
    safe_headers.update({str(key):str(value) for key,value in (headers or {}).items() if value is not None})
    if raw_body is not None:safe_headers["Content-Type"]="application/json"
    deadline=time.monotonic()+timeout
    addresses=public_addresses(parsed.hostname)
    for index,address in enumerate(addresses[:2]):
        remaining=deadline-time.monotonic()
        if remaining<=0:raise ValueError("API 调用已达到等待上限")
        cx=PinnedHTTPS(parsed.hostname,address,min(5,remaining) if index+1<len(addresses[:2]) else remaining)
        try:
            cx.request(method,(parsed.path or "/")+("?"+parsed.query if parsed.query else ""),
                       body=raw_body,headers=safe_headers)
            response=cx.getresponse()
            if response.status in (301,302,303,307,308):
                raise ValueError("API 返回重定向，已拒绝携带凭据继续请求")
            if not 200<=response.status<300:
                raise ValueError(f"API 返回 {response.status}")
            mime=response.getheader("Content-Type","").split(";",1)[0].casefold()
            if mime not in {"application/json","text/json","text/plain"}:
                raise ValueError("API 没有返回 JSON")
            raw=response.read(max_bytes+1)
            if len(raw)>max_bytes:raise ValueError("API 响应超过大小上限")
            return raw,mime,url
        except (OSError,http.client.HTTPException) as exc:
            if index+1==len(addresses[:2]):
                raise ValueError("API 连接暂时失败") from exc
        finally:
            cx.close()
    raise ValueError("API 没有可用地址")


def _srcset_urls(value):
    """Return srcset URLs from largest advertised candidate to smallest."""
    candidates=[]
    for item in (value or '').split(','):
        parts=item.strip().split()
        if not parts:continue
        score=0.0
        if len(parts)>1:
            descriptor=parts[-1].lower()
            try:
                score=float(descriptor[:-1]) if descriptor.endswith(('w','x')) else 0.0
            except ValueError:score=0.0
        candidates.append((score,parts[0]))
    return [item[1] for item in sorted(candidates,reverse=True)]


def _looks_like_image_url(value):
    try:parsed=urlsplit(value or '')
    except ValueError:return False
    decoded=unquote(parsed.path).lower()
    return bool(re.search(r'\.(?:avif|gif|jpe?g|png|svg|webp)(?:$|[?#])',decoded)
                or '/image/' in parsed.path.lower())


def _safe_urljoin(base,ref):
    try:return urljoin(base,ref)
    except ValueError:return ref


def _html_image_groups(raw,base):
    """Discover one ordered download group for every HTML image occurrence.

    A group keeps every URL spelling used by picture/srcset/link wrappers, while
    downloading only the best available representation.  Article figures come
    before site chrome so a page with many icons cannot starve its actual media.
    """
    soup=BeautifulSoup(raw,'html.parser')
    document_base=base
    if soup.find('base',href=True):document_base=_safe_urljoin(base,soup.find('base',href=True)['href'])
    article=soup.find('article') or soup.find('main')
    ordered=[]
    seen_nodes=set()
    pools=[]
    if article:pools.append(('article',article.find_all('img')))
    pools.append(('page',soup.find_all('img')))
    for scope,images in pools:
        for image in images:
            if id(image) in seen_nodes:continue
            seen_nodes.add(id(image))
            refs=[]
            figure=image.find_parent('figure')
            anchor=image.find_parent('a',href=True)
            if figure and anchor and _looks_like_image_url(_safe_urljoin(document_base,anchor.get('href',''))):
                refs.append(anchor.get('href',''))
            for source in (image.find_parent('picture') or image).find_all('source') if image.find_parent('picture') else []:
                refs.extend(_srcset_urls(source.get('srcset','')))
            refs.extend(_srcset_urls(image.get('srcset','')))
            for key in ('data-original','data-src','data-lazy-src','src'):
                if image.get(key):refs.append(image.get(key))
            urls=[]
            for ref in refs:
                if not ref or ref.startswith('data:'):continue
                target=_safe_urljoin(document_base,ref)
                if target not in urls:urls.append(target)
            if not urls:continue
            caption=figure.find('figcaption').get_text(' ',strip=True) if figure and figure.find('figcaption') else ''
            ordered.append(dict(id=f'web-image-{len(ordered)+1:04d}',scope='article' if article and article in image.parents else scope,
                figure=bool(figure),alt=image.get('alt',''),caption=caption,urls=urls,
                selected_url=urls[0],fetched=False,asset_name='',failure=''))
    return ordered


def fetch_uploaded_assets(uploads):
    """Collect absolute image references in uploaded Markdown/HTML before parsing.

    Relative references are already resolved against companion uploaded files by
    ``ingest.intake``. Failed remote fetches remain explicit source gaps; the
    original URL and alt text are never replaced or inferred.
    """
    groups=[]
    for name,raw in uploads:
        suffix=PurePosixPath(name).suffix.lower()
        if suffix in {'.md','.markdown'}:
            text=raw.decode('utf-8',errors='replace')
            tokens=MarkdownIt('commonmark').parse(text)
            groups.extend([child.attrGet('src') or ''] for token in tokens
                          for child in token.children or [] if child.type=='image')
            groups.extend(group['urls'] for group in _html_image_groups(text,''))
        elif suffix in {'.html','.htm'}:
            groups.extend(group['urls'] for group in _html_image_groups(raw,''))
    result=list(uploads);aliases={};failures=[];total=sum(len(raw) for _,raw in uploads)
    deadline=time.monotonic()+60
    types={'image/png':'png','image/jpeg':'jpg','image/gif':'gif','image/webp':'webp','image/svg+xml':'png'}
    known={}
    for refs in groups[:64]:
        targets=[]
        for ref in refs:
            try:
                if urlsplit(ref).scheme=='https' and ref not in targets:targets.append(ref)
            except ValueError as exc:
                failures.append(dict(target=ref,reason=type(exc).__name__))
        last_error=None
        for target in targets:
            if target in known:
                name=known[target]
                for alias in targets:aliases[alias]=name
                break
            try:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise ValueError('上传材料图片获取已达到等待上限')
                image,kind,resolved=fetch(target,set(types),min(12,remaining))
                if kind=='image/svg+xml':image=rasterize_svg(image);kind='image/png'
                if total+len(image)>MAX_FILE*4:raise ValueError('材料与资源超过 100 MB 总量')
                name='uploaded-assets/'+hashlib.sha256(target.encode()).hexdigest()+'.'+types[kind]
                result.append((name,image));total+=len(image)
                for alias in targets:aliases[alias]=name;known[alias]=name
                aliases[resolved]=name;known[resolved]=name
                break
            except (ValueError,OSError,http.client.HTTPException) as exc:
                last_error=exc
        else:
            if last_error is not None:
                failures.append(dict(target=targets[0],reason=type(last_error).__name__))
    return result,aliases,failures


def _web_document_text(raw):
    """Return conservative visible-text evidence for comparing two HTML pages."""
    soup=BeautifulSoup(raw,'html.parser')
    title=soup.title.get_text(' ',strip=True) if soup.title else ''
    for node in soup(['script','style','noscript','template','svg']):node.decompose()
    candidates=soup.find_all(['article','main'])
    text=max((node.get_text(' ',strip=True) for node in candidates),key=len,default='')
    if len(text)<160:
        body=soup.body or soup
        text=body.get_text(' ',strip=True)
    text=re.sub(r'\s+',' ',text).strip().lower()
    tokens=re.findall(r"[a-z0-9]{2,}|[^\W\d_]{2,}|\d+",text,re.I)
    return title.strip().lower(),text,tokens


def _substantive_original_html(raw, text, tokens):
    """Require article structure before treating a failed render as dispensable."""
    if len(text)<300 or len(set(tokens))<24:
        return False
    soup=BeautifulSoup(raw,'html.parser')
    for node in soup(['script','style','noscript','template']):node.decompose()
    article=soup.find(['main','article'])
    if article and len(article.get_text(' ',strip=True))>=300:
        return True
    paragraphs=[node.get_text(' ',strip=True) for node in soup.find_all('p')]
    return len([item for item in paragraphs if len(item)>=80])>=2 and sum(map(len,paragraphs))>=300


def _rendered_continuity(original,rendered,original_url,rendered_url):
    """Reject obvious error pages and materially unrelated browser documents."""
    title,source_text,source_tokens=_web_document_text(original)
    rendered_title,rendered_text,rendered_tokens=_web_document_text(rendered)
    source_words=set(source_tokens);rendered_words=set(rendered_tokens)
    overlap=(len(source_words & rendered_words)/len(source_words)) if source_words else 1.0
    error_text=rendered_text[:5000]
    error_patterns=(
        r'\b403\s+error\b',r'\b404\s+not found\b',r'\b502\s+bad gateway\b',
        r'\b503\s+service unavailable\b',r'\baccess denied\b',
        r'\brequest could not be satisfied\b',r'\bthis site can.t be reached\b',
    )
    cloudfront_error=('cloudfront' in error_text and
                      any(phrase in error_text for phrase in
                          ('access denied','request could not be satisfied','403 error','generated by cloudfront')))
    source_substantive=_substantive_original_html(original,source_text,source_tokens)
    has_error_phrase=cloudfront_error or any(re.search(pattern,error_text,re.I) for pattern in error_patterns)
    error_title=bool(re.search(r'^(?:403\s+error|404\s+not found|502\s+bad gateway|503\s+service unavailable|access denied)$',
                               rendered_title,re.I))
    explicit_error=((not source_substantive and (error_title or cloudfront_error))
                    or (has_error_phrase and overlap<0.30))
    if explicit_error:
        reason='rendered_error_page'
    elif source_substantive and len(rendered_text)<max(160,len(source_text)*0.35) and overlap<0.45:
        reason='rendered_document_lost_source_content'
    elif source_substantive and len(source_words)>=50 and len(rendered_words)>=10 and overlap<0.12:
        reason='rendered_document_content_mismatch'
    elif source_substantive and title and rendered_title and title!=rendered_title and overlap<0.2:
        reason='rendered_document_title_mismatch'
    else:
        reason='source_content_continuity'
    accepted=reason=='source_content_continuity'
    return dict(accepted=accepted,reason=reason,
        original_url=original_url,rendered_url=rendered_url,
        original_title=title,rendered_title=rendered_title,
        original_substantive=source_substantive,
        original_text_chars=len(source_text),rendered_text_chars=len(rendered_text),
        shared_source_word_ratio=round(overlap,4))


def fetch_bundle(url,include_manifest=False,rendered=False):
    """Snapshot original HTML/Markdown and bounded referenced images."""
    deadline=time.monotonic()+60
    raw,mime,final=fetch(url)
    original_raw=raw
    original_sha256=hashlib.sha256(raw).hexdigest()
    suffix=DOCUMENT_TYPES[mime]
    if mime=='text/plain':
        source_suffix=PurePosixPath(urlsplit(final).path).suffix.lower()
        if source_suffix in {'.md','.markdown'}:suffix='md'
        elif source_suffix in {'.rst','.rest'}:suffix='rst'
    uploads=[('snapshot.'+suffix,raw)];aliases={};failures=[];rendered_manifest=[];rendered_success=False
    rendered_outcome=None
    continuity=dict(status='not_attempted',basis='original_response',
                    original_response_sha256=original_sha256,
                    snapshot_name='snapshot.'+suffix,snapshot_sha256=original_sha256,
                    rendered_candidate_sha256='',rendered_candidate_url='',reason='browser_capture_not_requested')
    targets=[];image_groups=[]
    if mime=='text/html':
        if rendered:
            last_capture_error=None
            candidate_continuity=None
            for _capture_attempt in range(3):
                try:
                    from .web_capture import capture
                    rendered_html,rendered_final,rendered_manifest,capture_assets=capture(final)
                    candidate_continuity=_rendered_continuity(original_raw,rendered_html,final,rendered_final)
                    candidate_sha256=hashlib.sha256(rendered_html).hexdigest()
                    continuity=dict(status='continuous' if candidate_continuity['accepted'] else 'rejected',
                        basis='browser_render' if candidate_continuity['accepted'] else 'original_response',
                        original_response_sha256=original_sha256,
                        snapshot_name='snapshot.html',
                        snapshot_sha256=candidate_sha256 if candidate_continuity['accepted'] else original_sha256,
                        rendered_candidate_sha256=candidate_sha256,
                        rendered_candidate_url=rendered_final,
                        reason=candidate_continuity['reason'],
                        comparison={key:value for key,value in candidate_continuity.items()
                                    if key not in {'accepted','original_url','rendered_url'}})
                    if not candidate_continuity['accepted']:
                        rendered_manifest=[]
                        uploads=[('web-original.bin',original_raw),('snapshot.html',original_raw)]
                        if candidate_continuity.get('original_substantive'):
                            # The static response is complete enough to remain
                            # the source while browser-only additions are recorded
                            # as a rejected provenance candidate.
                            rendered_outcome=None
                            failures.append(dict(target=rendered_final,
                                reason='rendered_snapshot_rejected:'+candidate_continuity['reason'],
                                scope='rendered_page'))
                        else:
                            # A thin JavaScript shell cannot establish article
                            # completeness. Keep its bytes, but retain an explicit
                            # unresolved render gap so generation remains blocked.
                            continuity['status']='unresolved'
                            rendered_outcome=False
                            failures.append(dict(target=rendered_final,
                                reason='rendered_snapshot_unresolved:'+candidate_continuity['reason'],
                                scope='rendered_page'))
                        raw=original_raw
                    else:
                        rendered_success=True
                        rendered_outcome=True
                        uploads=[('web-original.bin',original_raw),('snapshot.html',rendered_html),*capture_assets]
                        raw=rendered_html;final=rendered_final
                        for item in rendered_manifest:
                            if item.get('preview_name'):
                                aliases['capture://'+item['id']]=item['preview_name']
                    break
                except Exception as exc:
                    last_capture_error=exc
            if not rendered_success:
                if candidate_continuity is None:
                    # Keep the original response as the parse target. A failed
                    # browser launch cannot invalidate already fetched static
                    # content or its ordinary image references.
                    reason='rendered_capture_failed:'+type(last_capture_error).__name__
                    failures.append(dict(target=final,reason=reason,scope='rendered_page'))
                    continuity=dict(status='capture_failed',basis='original_response',
                        original_response_sha256=original_sha256,snapshot_name='snapshot.html',
                        snapshot_sha256=original_sha256,rendered_candidate_sha256='',
                        rendered_candidate_url='',reason=reason)
                    _,source_text,source_tokens=_web_document_text(original_raw)
                    source_substantive=_substantive_original_html(original_raw,source_text,source_tokens)
                    if not source_substantive:rendered_outcome=False
        soup=BeautifulSoup(raw,'html.parser')
        base=final
        if soup.find('base',href=True):base=urljoin(final,soup.find('base',href=True)['href'])
        image_groups=_html_image_groups(raw,final)
        references=[]
    elif suffix=='md':
        tokens=MarkdownIt('commonmark').parse(raw.decode('utf-8',errors='replace'))
        markdown_refs=[child.attrGet('src') or '' for token in tokens for child in token.children or []
                       if child.type=='image']
        # CommonMark leaves literal <img> elements as HTML tokens. The intake
        # parser still records them as images, so fetch their bytes here too.
        html_refs=[image.get('src') or image.get('data-src') or ''
                   for image in BeautifulSoup(raw,'html.parser').find_all('img')]
        references=markdown_refs+html_refs
        base=final
    else:
        result=(uploads,final,aliases,failures)
        manifest={'images':[],'rendered_objects':[],'rendered':None}
        return result+(manifest,) if include_manifest else result
    for ref in references:
        if ref and not ref.startswith('data:'):
            target=_safe_urljoin(base,ref)
            if target not in targets:targets.append(target)
    total=len(raw)
    types={'image/png':'png','image/jpeg':'jpg','image/gif':'gif','image/webp':'webp',
           'image/svg+xml':'png'}
    if image_groups:
        # Download occurrences concurrently, but preserve DOM order in the
        # manifest and output bundle.  Each occurrence tries its full-size link,
        # srcset and img fallback URLs before becoming an explicit gap.
        deadline=time.monotonic()+180
        def get_group(index,group):
            last=None
            for target in group['urls']:
                try:
                    remaining=deadline-time.monotonic()
                    if remaining<=0:raise ValueError('网页图片获取已达到等待上限')
                    image,kind,resolved=fetch(target,set(types),min(18,remaining))
                    if kind=='image/svg+xml':image=rasterize_svg(image);kind='image/png'
                    return index,target,image,kind,resolved,None
                except (ValueError,OSError,http.client.HTTPException) as exc:last=exc
            return index,group['selected_url'],None,None,None,last
        completed={};signatures={}
        for index,group in enumerate(image_groups):signatures.setdefault(tuple(group['urls']),[]).append(index)
        # Four in-flight files keep the worst-case image buffer near the same
        # 100 MB ceiling as the persisted bundle on the production VPS
        with ThreadPoolExecutor(max_workers=min(4,max(1,len(image_groups)))) as pool:
            futures={pool.submit(get_group,index,image_groups[index]):indexes
                     for indexes in signatures.values() for index in indexes[:1]}
            for future in as_completed(futures):
                result=future.result()
                for index in futures[future]:completed[index]=(index,*result[1:])
        stored_by_digest={}
        for index,group in enumerate(image_groups):
            _,target,image,kind,resolved,error=completed[index]
            if error is not None:
                group['failure']=type(error).__name__
                failures.append(dict(target=target,reason=type(error).__name__,material_id=group['id'],scope=group['scope']))
                continue
            if total+len(image)>MAX_FILE*4:
                group['failure']='网页与资源超过 100 MB 总量'
                failures.append(dict(target=target,reason=group['failure'],material_id=group['id'],scope=group['scope']))
                continue
            sha=hashlib.sha256(image).hexdigest()
            name=stored_by_digest.get(sha)
            if not name:
                name='web-assets/'+hashlib.sha256(target.encode()).hexdigest()+'.'+types[kind]
                uploads.append((name,image));stored_by_digest[sha]=name;total+=len(image)
            for alias in group['urls']:aliases[alias]=name
            aliases[resolved]=name
            group.update(selected_url=target,fetched=True,asset_name=name)
    else:
        for target in targets:
            if time.monotonic()>=deadline:
                failures.append(dict(target=target,reason='网页图片获取已达到等待上限，原地址保留'))
                continue
            try:
                image,kind,resolved=fetch(target,set(types),min(12,deadline-time.monotonic()))
                if kind=='image/svg+xml':image=rasterize_svg(image);kind='image/png'
                if total+len(image)>MAX_FILE*4:raise ValueError('网页与资源超过 100 MB 总量')
                name='web-assets/'+hashlib.sha256(target.encode()).hexdigest()+'.'+types[kind]
                uploads.append((name,image));aliases[target]=name;aliases[resolved]=name;total+=len(image)
            except (ValueError,OSError,http.client.HTTPException) as exc:
                failures.append(dict(target=target,reason=type(exc).__name__))
    continuity['snapshot_sha256']=hashlib.sha256(raw).hexdigest()
    manifest={'images':image_groups,'rendered_objects':rendered_manifest,
              # Browser-rendering completeness applies only to HTML.  A PDF,
              # DOCX, TXT, RST, or Markdown snapshot is already the fetched
              # source document and must not acquire a false render failure.
              'rendered':rendered_outcome if rendered and mime=='text/html' else None}
    if mime=='text/html':manifest['source_continuity']=continuity
    result=(uploads,final,aliases,failures)
    return result+((manifest if rendered else image_groups),) if include_manifest else result
