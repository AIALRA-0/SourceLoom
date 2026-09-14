"""Small primary-source glossary snapshots, kept separate from original assertions."""
import json
import re
import http.client

CATALOG=[
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
