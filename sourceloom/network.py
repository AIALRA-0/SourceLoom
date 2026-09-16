"""Bounded HTTPS snapshots with connection-pinned public DNS addresses."""

import http.client
import ipaddress
import socket
import ssl
import time
import hashlib
from pathlib import PurePosixPath
from bs4 import BeautifulSoup
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


def fetch_bundle(url):
    """Snapshot original HTML and bounded raster assets without rewriting it."""
    deadline=time.monotonic()+60
    raw,mime,final=fetch(url)
    suffix=DOCUMENT_TYPES[mime]
    if mime=='text/plain' and PurePosixPath(urlsplit(final).path).suffix.lower() in {'.md','.markdown'}:suffix='md'
    uploads=[('snapshot.'+suffix,raw)];aliases={};failures=[]
    if mime!='text/html':return uploads,final,aliases,failures
    soup=BeautifulSoup(raw,'html.parser')
    base=final
    if soup.find('base',href=True):base=urljoin(final,soup.find('base',href=True)['href'])
    targets=[]
    for image in soup.find_all('img'):
        ref=image.get('src') or image.get('data-src') or ''
        if ref and not ref.startswith('data:'):
            target=urljoin(base,ref)
            if target not in targets:targets.append(target)
    total=len(raw)
    types={'image/png':'png','image/jpeg':'jpg','image/gif':'gif','image/webp':'webp'}
    for index,target in enumerate(targets):
        if index>=24 or time.monotonic()>=deadline:
            failures.append(dict(target=target,reason='网页图片数量或 60 秒获取时限已达到，原地址保留'))
            continue
        try:
            image,kind,resolved=fetch(target,set(types),min(12,deadline-time.monotonic()))
            if total+len(image)>MAX_FILE*4:raise ValueError('网页与资源超过 100 MB 总量')
            name='web-assets/'+hashlib.sha256(target.encode()).hexdigest()+'.'+types[kind]
            uploads.append((name,image));aliases[target]=name;total+=len(image)
        except (ValueError,OSError,http.client.HTTPException) as exc:
            failures.append(dict(target=target,reason=type(exc).__name__))
    return uploads,final,aliases,failures
