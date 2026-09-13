"""Conservative structural intake; uncertain regions remain visible obligations."""

import base64
from io import BytesIO
import mimetypes
import posixpath
import re
import stat
from urllib.parse import urljoin, urlsplit, unquote
import zipfile

from bs4 import BeautifulSoup, NavigableString, Tag
from defusedxml import ElementTree as ET
from markdown_it import MarkdownIt
from pypdf import PdfReader

from .store import digest, identity

MAX_FILE = 25 * 1024 * 1024
MAX_EXPANDED = 100 * 1024 * 1024
MAX_OBJECTS = 5000
SAFE_IMAGE = {"image/png", "image/jpeg", "image/gif", "image/webp"}


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


def intake(store, uploads, source_url=None):
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
        root = soup.body or soup
        blocks = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "table", "figcaption", "blockquote", "dt", "dd", "summary"}

        def walk(node, locator):
            if isinstance(node, NavigableString):
                if str(node).strip():
                    add("text", str(node), locator)
                return
            if not isinstance(node, Tag):
                return
            if node.name in {"script", "style", "iframe", "object", "embed", "svg", "canvas"}:
                obj = add("unknown", node.get_text() or f"原始 {node.name} 对象", locator, raw=str(node))
                gap(f"{node.name} 未执行，需要独立解释或安全转换", locator, obj["id"])
                return
            if node.name == "img":
                ref = node.get("src", node.get("data-src", ""))
                key = resolve_asset(ref, name)
                obj = add("image", node.get("alt", ""), locator, resource_id=key, target=ref)
                if not key:
                    gap("图片资源尚未取得，原地址已保留", locator, obj["id"])
                return
            if node.name=='math':
                obj=add('formula',str(node),locator,raw=str(node),syntax='mathml')
                gap('MathML 原始结构已保存，教学排版与公式解释需要核对',locator,obj['id'])
                return
            if node.name in blocks:
                text = node.get_text("\n", strip=False)
                kind = "table" if node.name == "table" else "code" if node.name == "pre" else "heading" if re.fullmatch("h[1-6]", node.name) else "text"
                if node.get('role')=='doc-footnote' or re.match(r'^(fn|footnote)[-_:]?\d',str(node.get('id','')),re.I):kind='footnote'
                html_ids=([node['id']] if node.get('id') else [])+[n['id'] for n in node.select('[id]')]
                obj = add(kind, text, locator, raw=str(node),html_ids=html_ids)
                if node.name == "table":
                    obj["cells"] = [[dict(text=c.get_text(), rowspan=c.get("rowspan", "1"), colspan=c.get("colspan", "1")) for c in r.find_all(["td", "th"], recursive=False)] for r in node.find_all("tr")]
                for n, link in enumerate(node.find_all("a")):
                    target = link.get("href", "")
                    add("link", link.get_text(), f"{locator}/a[{n+1}]", target=urljoin(source_url, target) if source_url else target, parent_id=obj["id"])
                for n, pic in enumerate(node.find_all("img")):
                    walk(pic, f"{locator}/img[{n+1}]")
                for n,math in enumerate(node.find_all('math')):
                    walk(math,f'{locator}/math[{n+1}]')
                if node.find(["math", "iframe", "svg"]) or node.find(attrs={"hidden":True}) or node.has_attr("hidden"):
                    gap("混合数学、嵌入或隐藏对象需要补充核对", locator, obj["id"])
                return
            if node.name == "a":
                target = node.get("href", "")
                add("link", node.get_text(), locator, target=urljoin(source_url, target) if source_url else target)
                return
            for n, child in enumerate(node.children):
                walk(child, f"{locator}/{getattr(child, 'name', None) or 'text'}[{n+1}]")

        for n, child in enumerate(root.children):
            walk(child, f"{name}/node[{n+1}]")

    ordered_files=sorted(files.items(),key=lambda pair:mimetypes.guess_type(pair[0])[0] in SAFE_IMAGE)
    for name, raw in ordered_files:
        ext = name.lower().rsplit(".", 1)[-1]
        if ext in {"txt", "md", "markdown", "html", "htm"}:
            text = decode(raw)
            if ext in {"html", "htm", "md", "markdown"}:
                html = MarkdownIt("commonmark", {"html":True}).enable("table").render(text) if ext in {"md", "markdown"} else text
                html_objects(html, name)
                if ext in {"md", "markdown"} and re.search(r"\[\^[^\]]+\]", text):
                    gap("脚注扩展语法以原文保留，需要核对归属", name)
                if ext in {'md','markdown'}:
                    # Code fences are already protected code objects, not math prose.
                    prose=re.sub(r'(?ms)^```.*?^```[^\n]*|^~~~.*?^~~~[^\n]*','',text)
                    for n,m in enumerate(re.finditer(r'\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|(?<![\\$])\$(?!\$)[^\n$]+?(?<!\\)\$',prose)):
                        add('formula',m.group(),f'{name}/math-source[{n+1}]',syntax='tex')
            else:
                for n, part in enumerate(re.split(r"\n\s*\n", text)):
                    if part.strip():
                        add("text", part, f"{name}/paragraph[{n+1}]")
        elif ext == "pdf":
            reader = PdfReader(BytesIO(raw))
            if reader.is_encrypted:
                raise ValueError("加密 PDF 需要先提供可读取副本")
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
            parts = unpack(raw)
            for part, data in parts.items():
                if part.startswith("word/media/"):
                    key = store.blob(data)
                    mime = mimetypes.guess_type(part)[0] or "application/octet-stream"
                    resources.append(dict(id=key, name=part, sha256=key, size=len(data), mime=mime))
                    add("image" if mime in SAFE_IMAGE else "attachment", part, name+"/"+part, resource_id=key)
                if part.startswith("word/") and part.endswith(".xml"):
                    root = ET.fromstring(data)
                    local = lambda tag: tag.rsplit("}", 1)[-1]
                    for n, element in enumerate(root.iter()):
                        tag = local(element.tag)
                        if tag in {"p", "tbl", "oMath", "comment", "ins", "del"}:
                            text = "".join(e.text or "" for e in element.iter() if local(e.tag) in {"t", "delText"})
                            if text:
                                add("table" if tag=="tbl" else "formula" if tag=="oMath" else "text", text, f"{name}/{part}/{tag}[{n}]", raw=ET.tostring(element, encoding="unicode"))
                if part.endswith(".rels"):
                    for rel in ET.fromstring(data):
                        if rel.get("TargetMode") == "External":
                            add("link", rel.get("Id", "关系"), name+"/"+part, target=rel.get("Target", ""))
            gap("DOCX 部件已清点，修订、嵌套表格、批注归属和媒体位置需独立核对", name)
        elif mimetypes.guess_type(name)[0] in SAFE_IMAGE:
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
