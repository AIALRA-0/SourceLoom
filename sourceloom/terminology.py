"""Small primary-source glossary snapshots, kept separate from original assertions."""
import json
import re
import http.client
import time

CATALOG=[
    {'triggers':['NASA'],'abbr':'NASA','zh':'美国国家航空航天局',
     'en':'National Aeronautics and Space Administration',
     'url':'https://www.nasa.gov/image-article/nasas-origins/',
     'note':'NASA 官方页面直接并列全称与缩写；这里只核实名称，卫星和研究事实仍以当前原件为准'},
    {'triggers':['W3C'],'abbr':'W3C','zh':'万维网联盟','en':'World Wide Web Consortium',
     'url':'https://www.w3.org/about/',
     'note':'W3C 官方组织名称；仅查证缩写，不据此改变原文所述页面的规范地位'},
    {'triggers':['LZW'],'abbr':'LZW','zh':'伦佩尔—齐夫—韦尔奇编码','en':'Lempel-Ziv-Welch',
     'url':'https://datatracker.ietf.org/doc/html/rfc7230#section-4.2.1',
     'note':'IETF RFC 7230 将 Lempel-Ziv-Welch 与 LZW 明确并列；这里只确认编码名称，不把 HTTP 的 compress 用途误归给当前 GIF 原件，也不额外添加 Algorithm 到英文专名'},
    {'triggers':['TIFF'],'abbr':'TIFF','zh':'标签图像文件格式','en':'Tagged Image File Format',
     'url':'https://www.loc.gov/preservation/digital/formats/fdd/fdd000022.shtml',
     'note':'美国国会图书馆的格式记录列出 TIFF 与 Tagged Image File Format 的对应；该记录也指出历史上存在 Tag Image File Format 的另一种展开，因此这里只采用记录中的名称，不据此推断当前图片的编码细节'},
    {'triggers':['assistive technologies','assistive technology'],'abbr':'','zh':'辅助技术',
     'en':'assistive technology',
     'url':'https://www.w3.org/WAI/WCAG22/Understanding/page-titled.html',
     'note':'W3C 当前页面的 Key Terms 给出单数名称及定义；复数原文仍按原件保留，不把名称证据当成改写其他事实的依据'},
    {'triggers':['QA Tips','QA'],'abbr':'QA','zh':'质量保证','en':'Quality Assurance',
     'url':'https://www.w3.org/QA/glossary',
     'note':'W3C 质量保证术语表中的名称对应；QA Tips 是栏目名，不能把整个栏目说成质量保证规范'},
    {'triggers':['FAQ'],'abbr':'FAQ','zh':'常见问题','en':'Frequently Asked Questions',
     'url':'https://www.w3.org/WAI/standards-guidelines/wcag/faq/',
     'note':'W3C 页面用于确认通用缩写，不说明日期格式目标页的全部内容'},
    {'triggers':['Q&A'],'abbr':'Q&A','zh':'问答','en':'Questions and Answers',
     'url':'https://www.w3.org/WAI/teach-advocate/accessibility-training/workshop-outline/',
     'note':'W3C 页面用于确认通用缩写，不等于当前链接目标页的正式标题'},
    {'triggers':['NP'],'abbr':'P','zh':'多项式时间','en':'Polynomial Time',
     'url':'https://courses.cs.washington.edu/courses/cse417/25au/readings/pnp.html',
     'note':'计算复杂性中的判定问题类名称，不能将 easy to find 这种直觉解释当成英文全称'},
    {'triggers':['NP'],'abbr':'NP','zh':'非确定性多项式时间','en':'Nondeterministic Polynomial Time',
     'url':'https://courses.cs.washington.edu/courses/cse417/25au/readings/pnp.html',
     'note':'这是计算复杂性中的名称，easy to check 仅是直觉说明，不能当成名称展开；不是 Not Polynomial'},
    {'triggers':['HTML','html'],'abbr':'HTML','zh':'超文本标记语言','en':'HyperText Markup Language',
     'url':'https://www.w3.org/TR/html401/intro/intro.html',
     'fallback_urls':['https://www.w3.org/MarkUp/html-spec/html-spec_1.html'],
     'note':'仅用官方历史规范确认名称与基本用途，不据此把旧版语言规则当成当前规范；文件名及原始代码中的小写字符保持原样'},
    {'triggers':['class','Class'],'abbr':'','zh':'类属性','en':'class attribute',
     'url':'https://www.w3.org/TR/html4/struct/global.html',
     'note':'W3C HTML 规范确认属性名称；这个名称证据本身不增加原文没有的使用规则'},
    {'triggers':['CSS'],'abbr':'CSS','zh':'层叠样式表','en':'Cascading Style Sheets',
     'url':'https://www.w3.org/Style/CSS/',
     'note':'W3C 官方 CSS 介绍确认英文全称与缩写；原文关于类命名的具体建议仍以原件为准'},
    {'triggers':[':link',':visited'],'abbr':'','zh':'链接伪类','en':'Link Pseudo-Classes',
     'url':'https://www.w3.org/TR/CSS22/selector.html',
     'note':'W3C 选择器规范确认 :link 与 :visited 是链接伪类；不能据此改写当前材料对颜色的原有主张'},
    {'triggers':['XML','xml'],'abbr':'XML','zh':'可扩展标记语言','en':'Extensible Markup Language',
     'url':'https://www.w3.org/TR/xml/',
     'note':'仅确认 W3C 规范中的名称；原件若讨论的是转义或安全处理，不能据此添加对 XML 安全性的保证'},
    {'triggers':['SGML'],'abbr':'SGML','zh':'标准通用标记语言','en':'Standard Generalized Markup Language',
     'url':'https://www.w3.org/MarkUp/html3/HTMLandSGML',
     'note':'W3C 历史 HTML 资料核实 SGML 展开及两者关系；不由名称证据推断现代 HTML 解析的细节'},
    {'triggers':['ISO'],'abbr':'ISO','zh':'国际标准化组织','en':'International Organization for Standardization',
     'url':'https://committee.iso.org/about.html',
     'note':'ISO 官方说明 ISO 是跨语言通用的短名称，并非英文首字母缩写；解释 ISO 8601 时保留标准编号，不将组织名称当作标准标题'},
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
    {'triggers':['XSS'],'abbr':'XSS','zh':'跨站脚本攻击','en':'Cross-Site Scripting',
     'url':'https://community.owasp.org/attacks/xss/',
     'note':'OWASP 的名称证据只支持术语展开；原文关于自动转义的具体主张仍须以原件为准'},
    {'triggers':['I18N','i18n'],'abbr':'I18N','zh':'国际化','en':'Internationalization',
     'url':'https://www.w3.org/International/i18n-drafts/nav/about',
     'note':'W3C 说明 i18n 是 Internationalization 的缩写；保留原件对 Jinja 功能的表述'},
    {'triggers':['L10N','l10n'],'abbr':'L10N','zh':'本地化','en':'Localization',
     'url':'https://www.w3.org/TR/i18n-glossary/',
     'note':'W3C 国际化术语表同时说明 Localization 和 l10n 的对应；这里只核实名称和缩写，当前项目的具体功能仍以原件为准'},
    {'triggers':['PRs','PR'],'abbr':'PR','zh':'拉取请求','en':'Pull Request',
     'url':'https://docs.github.com/en/get-started/learning-about-github/github-glossary',
     'note':'GitHub 的术语名称证据不能推断原项目接受请求的条件或流程'},
    {'triggers':['just-in-time'],'abbr':'','zh':'即时编译','en':'Just-in-Time Compilation',
     'url':'https://blog.llvm.org/posts/2020-11-30-interactive-cpp-with-cling/',
     'note':'LLVM 文档只确认该技术名称；Jinja 是否采用及如何缓存仍以原件为准'},
    {'triggers':['ahead-of-time'],'abbr':'','zh':'提前编译','en':'Ahead-of-Time Compilation',
     'url':'https://vmkit.llvm.org/',
     'note':'LLVM 文档只确认该技术名称；Jinja 的具体表述仍以原件为准'},
    {'triggers':['cryptographically signed'],'abbr':'','zh':'密码学签名','en':'Cryptographic Signature',
     'url':'https://openpgp.dev/book/glossary.html',
     'note':'此处只确认英文通用名称存在；OpenPGP 的非对称签名机制不能移植到 ItsDangerous，后者的具体机制须另查其官方资料'},
    {'triggers':['serialized','serialization'],'abbr':'','zh':'序列化','en':'Serialization',
     'url':'https://developer.mozilla.org/en-US/docs/Glossary/Serialization',
     'note':'只确认通用术语名称与基础含义；原件所称可自定义序列化方式仍以当前项目原件为准'},
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
        path=directory/(digest((entry['url']+'|'+entry['en']).encode())+'.json')
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
