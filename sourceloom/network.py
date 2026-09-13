"""Bounded HTTPS snapshots with connection-pinned public DNS addresses."""

import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urljoin, urlsplit

from .ingest import MAX_FILE


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
    def __init__(self,hostname,address):
        super().__init__(hostname,timeout=12,context=ssl.create_default_context())
        self.address=address

    def connect(self):
        sock=socket.create_connection((self.address,443),timeout=self.timeout)
        self.sock=self._context.wrap_socket(sock,server_hostname=self.host)


def fetch(url):
    for _ in range(4):
        parsed=urlsplit(url)
        if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None,443):
            raise ValueError("仅抓取不含凭据、使用标准端口的 HTTPS 网页")
        addresses=public_addresses(parsed.hostname)
        cx=PinnedHTTPS(parsed.hostname,addresses[0])
        try:
            cx.request("GET",(parsed.path or "/")+("?"+parsed.query if parsed.query else ""),headers={"User-Agent":"SourceLoom/0.1 (+bounded document snapshot)","Accept-Encoding":"identity"})
            r=cx.getresponse()
            if r.status in (301,302,303,307,308):
                url=urljoin(url,r.getheader("Location", ""))
                continue
            if r.status!=200:
                raise ValueError(f"网页返回 {r.status}，未取得完整材料")
            mime=r.getheader("Content-Type", "").split(";",1)[0]
            if mime not in {"text/html","text/plain","application/pdf"}:
                raise ValueError("网页类型不在当前接入范围")
            raw=r.read(MAX_FILE+1)
            if len(raw)>MAX_FILE:
                raise ValueError("网页超过 25 MB 上限")
            return raw,mime,url
        finally:
            cx.close()
    raise ValueError("网页重定向超过三次，已停止")
