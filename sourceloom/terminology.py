"""Small primary-source glossary snapshots, kept separate from original assertions."""
import json
import re
import http.client
import time

CATALOG=[
    {'triggers':['HTML','html'],'abbr':'HTML','zh':'超文本标记语言','en':'HyperText Markup Language',
     'url':'https://www.w3.org/TR/html401/intro/intro.html',
     'fallback_urls':['https://www.w3.org/MarkUp/html-spec/html-spec_1.html'],
     'note':'仅用官方历史规范确认名称与基本用途，不据此把旧版语言规则当成当前规范；文件名及原始代码中的小写字符保持原样'},
    {'triggers':['CVE','CVEs'],'abbr':'CVE','zh':'通用漏洞披露','en':'Common Vulnerabilities and Exposures',
     'url':'https://www.cve.org/ResourcesSupport/FAQs','fallback_urls':['https://www.cve.org/'],
     'reference_urls':['https://raw.githubusercontent.com/CVEProject/cve-website/dev/src/assets/data/faqs.json'],
     'note':'英文名称由官方资料查证；本条名称证据不能代替用途或机制证据，也不能据此推断原项目的报告、修复或发布政策'},
    {'triggers':['IETF'],'abbr':'IETF','zh':'互联网工程任务组','en':'Internet Engineering Task Force',
     'url':'https://www.ietf.org/about/','fallback_urls':['https://www.rfc-editor.org/rfc/rfc2026.txt'],
     'note':'互联网标准组织；不能声称原文给了全称，名称由官方资料补充查证'},
    {'triggers':['ECMA','Ecma','ECMAScript'],'abbr':'ECMA','zh':'欧洲计算机制造商协会','en':'European Computer Manufacturers Association',
     'url':'https://ecma-international.org/history/','note':'这是 1994 年以前的历史名称；现称 Ecma International，ECMAScript 是完整专名，不能改写或拆开其拼写'},
    {'triggers':['ECMAScript'],'abbr':'','zh':'ECMAScript 语言规范','en':'ECMAScript Language Specification',
     'url':'https://tc39.es/ecma262/','note':'保留 ECMAScript 的官方拼写，解释其与标准组织的关系；Script 不作为独立缩写生造全称'},
]

def verified_terms(store,source,fetcher=None):
    from .network import fetch
    from bs4 import BeautifulSoup
    from .store import digest
    fetcher=fetcher or fetch
    text='\n'.join(o.get('text','') for o in source['objects'])
    result=[]
    directory=store.root/'terminology';directory.mkdir(exist_ok=True)
    for entry in CATALOG:
        if not any(re.search(r'(?<![A-Za-z])'+re.escape(t)+r'(?![A-Za-z])',text) for t in entry['triggers']):continue
        path=directory/(digest(entry['url'].encode())+'.json')
        snapshot=None
        if path.exists():
            try:snapshot=json.loads(path.read_text(encoding='utf-8'))
            except (ValueError,OSError):pass
        if snapshot and entry['en'].casefold() not in snapshot.get('quote','').casefold():snapshot=None
        if not snapshot:
            for target in [entry['url'],*entry.get('fallback_urls',[])]:
                try:
                    raw,mime,url=fetcher(target,timeout=12)
                except (ValueError,OSError,TimeoutError,http.client.HTTPException):continue
                # The language specification is very large; only the fetched evidence text is sent.
                content=' '.join(BeautifulSoup(raw,'html.parser').get_text(' ',strip=True).split())
                matches=list(re.finditer(re.escape(entry['en']),content,re.I))
                if not matches:continue
                match=next((m for m in matches if entry['abbr'] and entry['abbr'] in content[max(0,m.start()-80):m.end()+160]),matches[0])
                snapshot={'url':url,'quote':content[max(0,match.start()-80):match.end()+160],
                          'snapshot_blob':store.blob(raw)}
                path.write_text(json.dumps(snapshot,ensure_ascii=False),encoding='utf-8')
                break
        if snapshot:result.append(entry|snapshot)
        else:
            result.append(entry|{'status':'lookup_failed','quote':'','note':'官方资料获取失败，不能伪造查证完成；需要后续查证'})
    return result


def retrieve_background(store,prepared,fetcher=None,verified=()):
    """Fetch proposed references once; fetched text is evidence, not authority."""
    from .network import fetch
    from .store import digest
    from bs4 import BeautifulSoup
    fetcher=fetcher or fetch
    directory=store.root/'terminology';directory.mkdir(exist_ok=True)
    requests={}
    for term in prepared['terms']:
        registered=[url for item in verified
                    if (term.get('en') and term['en'].casefold()==item['en'].casefold())
                    or (term.get('abbr') and term['abbr']==item.get('abbr'))
                    for url in item.get('reference_urls',[])]
        for url in dict.fromkeys([*registered,*term.get('reference_urls',[])]):
            requests.setdefault(url,[]).append(term)
    result=[];deadline=time.monotonic()+60
    for index,(url,terms) in enumerate(requests.items()):
        entry={'url':url,'terms':[t['zh'] for t in terms],
               'scope':'Retrieved outside background, not an original author claim or automatic confirmation of the proposed definition'}
        if index>=6 or time.monotonic()>=deadline:
            result.append(entry|{'status':'not_retrieved','reason':'本单元参考资料获取范围已达到，未假装查证完成'});continue
        path=directory/(digest(('background-v1:'+url).encode())+'.json')
        saved=None
        if path.exists():
            try:
                candidate=json.loads(path.read_text(encoding='utf8'))
                if time.time()-candidate.get('fetched_at',0)<86400:saved=candidate
            except (OSError,ValueError):pass
        if saved is None:
            try:
                raw,mime,resolved=fetcher(url,allowed_types={'text/html','text/plain','text/markdown','application/json'},timeout=min(12,max(.01,deadline-time.monotonic())))
                text=raw.decode('utf8',errors='replace')
                # JSON reference documents retain their actual strings and order.
                if mime=='application/json' or text.lstrip().startswith(('[','{')):
                    try:
                        parsed=json.loads(text)
                        def strings(value):
                            if isinstance(value,str):yield value
                            elif isinstance(value,list):
                                for item in value:yield from strings(item)
                            elif isinstance(value,dict):
                                for item in value.values():yield from strings(item)
                        text='\n'.join(strings(parsed))
                    except ValueError:pass
                doc=BeautifulSoup(text,'html.parser')
                for node in doc.select('script,style,nav,header,footer,noscript'):node.decompose()
                content=' '.join(doc.get_text(' ',strip=True).split())
                saved={'resolved_url':resolved,'text':content,'snapshot_blob':store.blob(raw),'fetched_at':time.time()}
                path.write_text(json.dumps(saved,ensure_ascii=False),encoding='utf8')
            except (ValueError,OSError,TimeoutError,http.client.HTTPException):
                result.append(entry|{'status':'lookup_failed','reason':'未取得可核对的参考正文，不能按地址猜测内容'});continue
        content=saved['text']
        if not content:
            result.append(entry|{'status':'empty_reference','snapshot_blob':saved['snapshot_blob']});continue
        spans=[]
        for term in terms:
            needle=term.get('en') or term.get('abbr') or term['zh']
            found=re.search(re.escape(needle),content,re.I)
            start=max(0,found.start()-200) if found else 0
            span=(start,min(len(content),start+3000))
            if not any(a<=span[0] and span[1]<=b for a,b in spans):spans.append(span)
        excerpts=[content] if len(content)<=6000 else [content[a:b] for a,b in spans[:3]]
        result.append(entry|{k:v for k,v in saved.items() if k!='text'}|{'status':'retrieved','excerpts':excerpts,
            'excerpted':len(content)>sum(len(x) for x in excerpts),
            'instruction':'Judge only claims supported by these actual excerpts, checking publisher and scope; an outside reference cannot add policy to the original. The full raw reference snapshot is retained.'})
    return result
