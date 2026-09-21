"""Conservative structural intake; uncertain regions remain visible obligations."""

import base64
from io import BytesIO
import mimetypes
import posixpath
import re
import stat
from urllib.parse import urljoin, urlsplit, unquote
import zipfile

from bs4 import BeautifulSoup, NavigableString, Tag, Comment
from defusedxml import ElementTree as ET
from markdown_it import MarkdownIt
from pypdf import PdfReader

from .store import digest, identity

MAX_FILE = 25 * 1024 * 1024
MAX_EXPANDED = 100 * 1024 * 1024
MAX_OBJECTS = 5000
SAFE_IMAGE = {"image/png", "image/jpeg", "image/gif", "image/webp"}

SPHINX_TEXT_DIRECTIVES = {
    'module','currentmodule','function','method','classmethod','staticmethod',
    'class','attribute','data','exception','availability','versionchanged',
    'versionadded','deprecated','seealso','impl-detail',
}


def sphinx_directive_blocks(text):
    """Return top-level Sphinx directives that docutils otherwise discards."""
    lines=text.splitlines(keepends=True);result=[];index=0
    pattern=re.compile(r'^\.\.\s+([A-Za-z][\w:-]*)::\s*(.*?)\s*(?:\r?\n)?$')
    while index<len(lines):
        match=pattern.match(lines[index])
        if not match or match[1].casefold() not in SPHINX_TEXT_DIRECTIVES:
            index+=1;continue
        start=index;index+=1
        while index<len(lines) and (not lines[index].strip() or lines[index][:1].isspace()):
            index+=1
        raw=''.join(lines[start:index])
        body_lines=lines[start+1:index]
        indents=[len(line)-len(line.lstrip()) for line in body_lines if line.strip()]
        indent=min(indents) if indents else 0
        body=''.join(line[indent:] if line.strip() else '\n' for line in body_lines).strip()
        result.append(dict(name=match[1].casefold(),argument=match[2].strip(),
                           body=body,raw=raw,line=start+1))
    return result


def safe_member(name):
    if not name or "\\" in name or name.startswith("/") or ":" in name or "\x00" in name:
        raise ValueError("压缩包存在不安全路径")
    parts = name.split("/")
    if any(x in ("..", ".") for x in parts):
        raise ValueError("压缩包存在越界路径")
    return name


def unpack(raw):
    files = {}
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        items = archive.infolist()
        if len(items) > 1000 or sum(x.file_size for x in items) > MAX_EXPANDED:
            raise ValueError("压缩包超过文件数或展开大小限制")
        for info in items:
            safe_member(info.orig_filename)
            name = safe_member(info.filename)
            if info.flag_bits & 1 or stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("不接受加密条目或符号链接")
            if name in files:
                raise ValueError("压缩包包含同名条目")
            if not info.is_dir():
                if info.file_size > MAX_FILE:
                    raise ValueError("包内单文件超过大小限制")
                files[name] = archive.read(info)
    return files


def decode(raw):
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig", errors="strict")


def attach_markdown_fences(objects, text, tokens=None):
    """Bind exact fenced source syntax to matching parsed code objects."""
    tokens=tokens if tokens is not None else MarkdownIt("commonmark", {"html":True}).enable("table").parse(text)
    lines=text.splitlines(keepends=True)
    code_objects=[o for o in objects if o['kind']=='code']
    next_code=0
    attached=0
    for token in tokens:
        if token.type!='fence' or not token.map:
            continue
        info=token.info.strip()
        language=info.split(None,1)[0] if info else ''
        for index in range(next_code,len(code_objects)):
            code=code_objects[index]
            if (code['text'].strip('\n')==token.content.strip('\n') and
                    code.get('language','')==language):
                raw=''.join(lines[token.map[0]:token.map[1]])
                if code.get('fence_raw') and code['fence_raw']!=raw:
                    raise ValueError('同一代码对象的原始围栏记录不一致')
                code['fence_raw']=raw
                code['fence_info']=info
                next_code=index+1
                attached+=1
                break
    return attached


def intake(store, uploads, source_url=None, asset_aliases=None):
    files = {}
    originals = []
    for name, raw in uploads:
        name = safe_member(name)
        if len(raw) > MAX_FILE:
            raise ValueError("单文件上限为 25 MB")
        if name in files:
            raise ValueError("同一次接入不能有同名文件")
        key = store.blob(raw)
        originals.append(dict(name=name, sha256=key, size=len(raw)))
        if name.lower().endswith(".zip"):
            for child, content in unpack(raw).items():
                if child in files:
                    raise ValueError("资源包与上传文件同名")
                files[child] = content
        else:
            files[name] = raw
    if sum(len(raw) for raw in files.values()) > MAX_EXPANDED:
        raise ValueError("本次材料超过展开总量限制")
    objects, resources, unknown = [], [], []
    resource_index = {}
    document_base=source_url
    for name, raw in files.items():
        key = store.blob(raw)
        resource_index[name] = key
        resources.append(dict(id=key, name=name, sha256=key, size=len(raw), mime=mimetypes.guess_type(name)[0] or "application/octet-stream"))

    def add(kind, text, locator, **extra):
        if len(objects) >= MAX_OBJECTS:
            raise ValueError("材料结构超过 5000 项，需拆分指定范围")
        obj = dict(id=f"src-{len(objects)+1:05d}", kind=kind, text=text, locator=locator, **extra)
        objects.append(obj)
        return obj

    def gap(reason, locator, object_id=""):
        unknown.append(dict(id=f"gap-{len(unknown)+1}", reason=reason, locator=locator, object_id=object_id))

    def resolve_asset(ref, name):
        linked=(asset_aliases or {}).get(urljoin(document_base or '',ref))
        if linked in resource_index:
            return resource_index[linked]
        parsed = urlsplit(ref)
        if parsed.scheme == "data":
            try:
                head, data = ref.split(",", 1)
                mime = head[5:].split(";")[0]
                if mime not in SAFE_IMAGE or ";base64" not in head or len(data) > MAX_FILE * 2:
                    raise ValueError()
                content = base64.b64decode(data, validate=True)
                key = store.blob(content)
                resources.append(dict(id=key, name=f"inline-{key[:12]}", sha256=key, size=len(content), mime=mime))
                return key
            except (ValueError, TypeError):
                return None
        if parsed.scheme or parsed.netloc:
            return None
        path = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(parsed.path)))
        return resource_index.get(path)

    def html_objects(html, name):
        soup = BeautifulSoup(html, "html.parser")
        nonlocal document_base
        if document_base and soup.find('base',href=True):
            document_base=urljoin(document_base,soup.find('base',href=True)['href'])
        root = soup.body or soup
        blocks = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "table", "figcaption", "blockquote", "dt", "dd", "summary"}

        def walk(node, locator):
            if isinstance(node, Comment):
                add('metadata',str(node),locator,raw='<!--'+str(node)+'-->')
                return
            if isinstance(node, NavigableString):
                if str(node).strip():
                    add("text", str(node), locator)
                return
            if not isinstance(node, Tag):
                return
            if node.name == 'svg':
                raw=str(node)
                try:
                    from .network import rasterize_svg
                    image=rasterize_svg(raw.encode('utf-8'))
                    key=store.blob(image)
                    resources.append(dict(id=key,name=f'inline-svg-{key[:12]}.png',sha256=key,
                                          size=len(image),mime='image/png'))
                    label=node.get('aria-label') or (node.find('title').get_text(' ',strip=True)
                        if node.find('title') else '')
                    add('image',label,locator,resource_id=key,target='',raw=raw,
                        source_format='inline-svg')
                except (ValueError,TypeError):
                    obj=add('unknown',node.get_text() or '原始 svg 对象',locator,raw=raw)
                    gap('svg 未执行，需要独立解释或安全转换',locator,obj['id'])
                return
            if node.name in {"script", "style", "iframe", "object", "embed", "canvas"}:
                obj = add("unknown", node.get_text() or f"原始 {node.name} 对象", locator, raw=str(node))
                gap(f"{node.name} 未执行，需要独立解释或安全转换", locator, obj["id"])
                return
            if node.name == "img":
                ref = node.get("src", node.get("data-src", ""))
                key = resolve_asset(ref, name)
                obj = add("image", node.get("alt", ""), locator, resource_id=key, target=ref,
                          raw=str(node))
                if not key:
                    gap("图片资源尚未取得，原地址已保留", locator, obj["id"])
                return
            if node.name=='math':
                obj=add('formula',str(node),locator,raw=str(node),syntax='mathml')
                gap('MathML 原始结构已保存，教学排版与公式解释需要核对',locator,obj['id'])
                return
            if node.name in blocks:
                text = node.get_text(strip=False)
                kind = "table" if node.name == "table" else "code" if node.name == "pre" else "heading" if re.fullmatch("h[1-6]", node.name) else "text"
                if node.get('role')=='doc-footnote' or re.match(r'^(fn|footnote)[-_:]?\d',str(node.get('id','')),re.I):kind='footnote'
                html_ids=([node['id']] if node.get('id') else [])+[n['id'] for n in node.select('[id]')]
                obj = add(kind, text, locator, raw=str(node),html_ids=html_ids)
                if kind=='code':
                    code=node.find('code')
                    classes=(code or node).get('class',[])
                    obj['language']=next((c[9:] for c in classes if c.startswith('language-')),'')
                if node.name == "table":
                    obj["cells"] = [[dict(text=c.get_text(), rowspan=c.get("rowspan", "1"), colspan=c.get("colspan", "1")) for c in r.find_all(["td", "th"], recursive=False)] for r in node.find_all("tr")]
                for n, link in enumerate(node.find_all("a")):
                    target = link.get("href", "")
                    add("link", link.get_text(), f"{locator}/a[{n+1}]", original_target=target,target=urljoin(document_base, target) if document_base else target, parent_id=obj["id"])
                for n, pic in enumerate(node.find_all("img")):
                    walk(pic, f"{locator}/img[{n+1}]")
                for n,math in enumerate(node.find_all('math')):
                    walk(math,f'{locator}/math[{n+1}]')
                for n,svg in enumerate(node.find_all('svg')):
                    walk(svg,f'{locator}/svg[{n+1}]')
                if kind not in {'code','table'}:
                    for n,pre in enumerate(node.find_all('pre')):
                        before=len(objects)
                        walk(pre,f'{locator}/pre[{n+1}]')
                        if len(objects)>before:
                            objects[before]['parent_id']=obj['id']
                if node.find(["math", "iframe"]) or node.find(attrs={"hidden":True}) or node.has_attr("hidden"):
                    gap("混合数学、嵌入或隐藏对象需要补充核对", locator, obj["id"])
                return
            if node.name == "a":
                target = node.get("href", "")
                add("link", node.get_text(), locator, original_target=target,target=urljoin(document_base, target) if document_base else target)
                return
            for n, child in enumerate(node.children):
                walk(child, f"{locator}/{getattr(child, 'name', None) or 'text'}[{n+1}]")

        for n, child in enumerate(root.children):
            walk(child, f"{name}/node[{n+1}]")

    ordered_files=sorted(files.items(),key=lambda pair:mimetypes.guess_type(pair[0])[0] in SAFE_IMAGE)
    for name, raw in ordered_files:
        ext = name.lower().rsplit(".", 1)[-1]
        if ext in {"txt", "md", "markdown", "rst", "rest", "html", "htm"}:
            text = decode(raw)
            document_base=source_url
            if ext in {'md','markdown'}:
                front=re.match(r'\A---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)',text)
                if front and re.search(r'(?m)^[A-Za-z][\w-]*:',front[1]):
                    add('metadata',front[0],name+'/frontmatter',raw=front[0],syntax='yaml')
                    text=text[front.end():]
                    # MDN content explicitly identifies its published path in slug.
                    # Keep the retrieval URL and literal href separately as provenance.
                    slug=re.search(r'(?m)^slug:\s*["\']?([^\r\n"\']+)',front[1])
                    if slug and source_url and source_url.startswith('https://raw.githubusercontent.com/mdn/content/'):
                        document_base='https://developer.mozilla.org/en-US/docs/'+slug[1].strip()
            if ext in {"html", "htm", "md", "markdown", "rst", "rest"}:
                markdown = MarkdownIt("commonmark", {"html":True}).enable("table") if ext in {"md", "markdown"} else None
                tokens=markdown.parse(text) if markdown else []
                if ext in {'rst','rest'}:
                    from docutils.core import publish_parts
                    html=publish_parts(text,writer_name='html5',settings_overrides={
                        'doctitle_xform':False,'file_insertion_enabled':False,'raw_enabled':False,
                        'report_level':5,'halt_level':6})['body']
                else:
                    html = markdown.render(text) if markdown else text
                first_object=len(objects)
                html_objects(html, name)
                if ext in {'rst','rest'}:
                    represented='\n'.join(obj.get('text','') for obj in objects[first_object:])
                    for directive in sphinx_directive_blocks(text):
                        signature=' '.join(part for part in
                            (directive['name'],directive['argument']) if part)
                        # A known Sphinx directive may be absent from docutils'
                        # HTML entirely. Preserve its signature, options and
                        # indented body as one source obligation without
                        # duplicating a directive a configured parser retained.
                        if (directive['argument'] and directive['argument'] in represented
                                and directive['body'] and directive['body'] in represented):
                            continue
                        rendered='\n'.join(part for part in (signature,directive['body']) if part)
                        if not rendered:continue
                        kind=('metadata' if directive['name'] in
                              {'module','currentmodule','availability','versionchanged',
                               'versionadded','deprecated'} else 'text')
                        add(kind,rendered,f"{name}/directive[{directive['line']}]",
                            raw=directive['raw'],directive=directive['name'])
                if markdown:
                    attach_markdown_fences(objects[first_object:],text,tokens)
                if ext in {"md", "markdown"} and re.search(r"\[\^[^\]]+\]", text):
                    gap("脚注扩展语法以原文保留，需要核对归属", name)
                if ext in {'md','markdown'}:
                    # Code fences are already protected code objects, not math prose.
                    prose=re.sub(r'(?ms)^```.*?^```[^\n]*|^~~~.*?^~~~[^\n]*','',text)
                    for n,m in enumerate(re.finditer(r'\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|(?<![\\$])\$(?!\$)[^\n$]+?(?<!\\)\$',prose)):
                        add('formula',m.group(),f'{name}/math-source[{n+1}]',syntax='tex')
            else:
                rfc_layout=bool(re.search(r'(?m)^Request for Comments:\s*\d+\b',text[:1000]))
                for n, part in enumerate(re.split(r"\n\s*\n", text)):
                    if part.strip():
                        lines=[line for line in part.splitlines() if line.strip()]
                        first_header=(len(lines)<=6 and lines[0].startswith('Network Working Group')
                                      and any(re.match(r'^Request for Comments:\s*\d+\b',line) for line in lines))
                        page_footer=(len(lines)==1 and bool(re.search(r'\[Page\s+\d+\]\s*$',lines[0])))
                        running_header=(len(lines)==1 and bool(re.match(
                            r'^RFC\s+\d+\s{2,}.+\s{2,}(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}\s*$',
                            lines[0])))
                        kind='metadata' if rfc_layout and (first_header or page_footer or running_header) else 'text'
                        add(kind, part, f"{name}/paragraph[{n+1}]")
        elif ext == "pdf":
            reader = PdfReader(BytesIO(raw))
            if reader.is_encrypted and not reader.decrypt(""):
                raise ValueError("这份 PDF 需要打开密码，请提供可读取副本")
            if len(reader.pages) > 50:
                raise ValueError("当前单份 PDF 上限为 50 页")
            import pypdfium2 as pdfium
            pdf = pdfium.PdfDocument(raw)
            try:
                for n, page in enumerate(reader.pages):
                    locator = f"{name}/page[{n+1}]"
                    rendered = pdf[n].render(scale=1.3).to_pil()
                    buf = BytesIO()
                    rendered.save(buf, "PNG")
                    key = store.blob(buf.getvalue())
                    resources.append(dict(id=key, name=f"page-{n+1}.png", sha256=key, size=len(buf.getvalue()), mime="image/png"))
                    coords = []
                    def visit(t, cm, tm, font, size):
                        if t.strip():
                            coords.append(dict(text=t,x=tm[4],y=tm[5],size=size))
                    extracted = page.extract_text(visitor_text=visit) or ""
                    obj = add("page", extracted, locator, resource_id=key, coordinates=coords)
                    gap("PDF 页面的阅读顺序、图表、公式和识别完整性需独立核对", locator, obj["id"])
                    for a in page.get("/Annots", []):
                        a = a.get_object()
                        target = a.get("/A", {}).get("/URI")
                        if target:
                            add("link", str(a.get("/Contents", target)), locator+"/annotation", target=str(target))
            finally:
                pdf.close()
        elif ext == "docx":
            from .word_intake import read_word
            parts = unpack(raw)
            read_word(parts,name,store,resources,add,gap,SAFE_IMAGE)
        elif mimetypes.guess_type(name)[0] in SAFE_IMAGE:
            if name.startswith('web-assets/'):
                # A fetched page asset belongs to its HTML/Markdown occurrence.
                # Keep its bytes in originals/resources without making a second
                # detached article image when the source uses srcset or a proxy.
                continue
            if not any(o.get('resource_id')==resource_index[name] for o in objects):
                add("image", name, name, resource_id=resource_index[name])
        else:
            obj = add("attachment", name, name, resource_id=resource_index[name])
            gap("此文件已保存，尚无完整内容解析器", name, obj["id"])
    obligations = [dict(id=f"obl-{n+1:05d}",object_id=o["id"],statement=o["text"] or f"保留 {o['kind']} 的原始内容及出现位置",
                        conditions=[],quantities=re.findall(r"\d+(?:\.\d+)?%?",o["text"]),negations=[],status="unreviewed") for n,o in enumerate(objects)]
    return dict(id=identity(), version=1, originals=originals, resources=resources, objects=objects,
                obligations=obligations, unknown=unknown, frozen=False, inventory_review=None,
                source_url=source_url, digest=None, extraction_claim="结构候选，语义清点尚待独立核对")
