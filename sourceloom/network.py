"""Bounded HTTPS snapshots with connection-pinned public DNS addresses."""

import http.client
import ipaddress
import socket
import ssl
import time
import hashlib
import json
import re
from pathlib import PurePosixPath
from bs4 import BeautifulSoup
from markdown_it import MarkdownIt
from urllib.parse import urljoin, urlsplit

from .ingest import MAX_FILE

DOCUMENT_TYPES={'text/html':'html','text/plain':'txt','text/markdown':'md','application/pdf':'pdf',
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document':'docx'}


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
        super().__init__(hostname,timeout=timeout,context=ssl.create_default_context())
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
                cx.request("GET",(parsed.path or "/")+("?"+parsed.query if parsed.query else ""),headers={"User-Agent":"SourceLoom/0.1 (+bounded document snapshot)","Accept-Encoding":"identity"})
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


def fetch_bundle(url):
    """Snapshot original HTML/Markdown and bounded referenced images."""
    deadline=time.monotonic()+60
    raw,mime,final=fetch(url)
    suffix=DOCUMENT_TYPES[mime]
    if mime=='text/plain':
        source_suffix=PurePosixPath(urlsplit(final).path).suffix.lower()
        if source_suffix in {'.md','.markdown'}:suffix='md'
        elif source_suffix in {'.rst','.rest'}:suffix='rst'
    uploads=[('snapshot.'+suffix,raw)];aliases={};failures=[]
    targets=[]
    if mime=='text/html':
        soup=BeautifulSoup(raw,'html.parser')
        base=final
        if soup.find('base',href=True):base=urljoin(final,soup.find('base',href=True)['href'])
        references=(image.get('src') or image.get('data-src') or '' for image in soup.find_all('img'))
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
    else:return uploads,final,aliases,failures
    for ref in references:
        if ref and not ref.startswith('data:'):
            target=urljoin(base,ref)
            if target not in targets:targets.append(target)
    total=len(raw)
    types={'image/png':'png','image/jpeg':'jpg','image/gif':'gif','image/webp':'webp',
           'image/svg+xml':'png'}
    for index,target in enumerate(targets):
        if index>=24 or time.monotonic()>=deadline:
            failures.append(dict(target=target,reason='网页图片数量或 60 秒获取时限已达到，原地址保留'))
            continue
        try:
            image,kind,resolved=fetch(target,set(types),min(12,deadline-time.monotonic()))
            if kind=='image/svg+xml':
                from defusedxml import ElementTree
                import cairosvg
                if len(image)>2_000_000:
                    raise ValueError('SVG 源文件超过安全处理上限')
                root=ElementTree.fromstring(image)
                if root.tag.rsplit('}',1)[-1].lower()!='svg' or any(
                    element.tag.rsplit('}',1)[-1].lower() in {'script','foreignobject','use'} or
                    (element.tag.rsplit('}',1)[-1].lower()=='style' and
                     re.search(r'url\s*\(|@import\b',element.text or '',re.I)) or
                    any((key.rsplit('}',1)[-1].lower()=='href' and value and
                         not (element.tag.rsplit('}',1)[-1].lower()=='image' and
                              value.startswith(('data:image/png;base64,','data:image/jpeg;base64,')))) or
                        (key.rsplit('}',1)[-1].lower()=='style' and 'url(' in value.lower())
                        for key,value in element.attrib.items())
                    for element in root.iter()):
                    raise ValueError('SVG 包含不可安全栅格化的外部或脚本内容')
                def length(value):
                    match=re.fullmatch(r'\s*(\d+(?:\.\d+)?)(?:px)?\s*',value or '')
                    return float(match[1]) if match else None
                width=length(root.get('width'));height=length(root.get('height'))
                viewbox=[float(part) for part in re.split(r'[\s,]+',root.get('viewBox','').strip())
                         if part] if root.get('viewBox') else []
                if len(viewbox)==4 and viewbox[2]>0 and viewbox[3]>0:
                    width=width or viewbox[2];height=height or viewbox[3]
                width=width or 512;height=height or 512
                if width<=0 or height<=0 or width/height>32 or height/width>32:
                    raise ValueError('SVG 页面比例超出安全范围')
                scale=min(1600/max(width,height),max(1,512/max(width,height)))
                try:
                    image=cairosvg.svg2png(bytestring=image,
                        output_width=max(1,round(width*scale)),output_height=max(1,round(height*scale)))
                except Exception as exc:
                    raise ValueError('SVG 无法安全栅格化') from exc
            if total+len(image)>MAX_FILE*4:raise ValueError('网页与资源超过 100 MB 总量')
            name='web-assets/'+hashlib.sha256(target.encode()).hexdigest()+'.'+types[kind]
            uploads.append((name,image));aliases[target]=name;total+=len(image)
        except (ValueError,OSError,http.client.HTTPException) as exc:
            failures.append(dict(target=target,reason=type(exc).__name__))
    return uploads,final,aliases,failures
