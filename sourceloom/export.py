"""ReadWeave-native candidate archives retain original bytes and honest status."""

import copy
import html
import json
from io import BytesIO
import zipfile
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt
from .checks import inspect_draft, release_issues
from .store import Conflict, digest

SAFE_TAGS = {"p","br","strong","em","b","i","u","s","sub","sup","table","thead","tbody","tfoot","tr","td","th","caption","pre","code","blockquote","ul","ol","li","h2","h3","h4","h5","h6","span","a","img","figure","figcaption","hr"}


def safe_html(raw):
    soup = BeautifulSoup(raw, "html.parser")
    for node in list(soup.find_all(True)):
        if node.name in {"script","style","iframe","svg","object","embed"}:
            node.decompose()
            continue
        if node.name is None:
            continue
        if node.name == "h1":
            node.name = "h2"
        if node.name not in SAFE_TAGS:
            node.unwrap()
            continue
        attrs = {}
        for key,value in node.attrs.items():
            if key in {"rowspan","colspan"} and str(value).isdigit() and 1 <= int(value) <= 100:
                attrs[key] = value
            elif key in {"alt","title"}:
                attrs[key] = str(value)
            elif key == "href" and (urlsplit(str(value)).scheme in {"http","https","mailto"} or str(value).startswith("#")):
                attrs[key] = str(value)
            elif key == "src" and str(value).startswith("assets/"):
                attrs[key] = str(value)
        node.attrs = attrs
    return str(soup)


def render(project, asset_url=lambda key:"assets/"+key):
    inventory = project["inventory"]
    src = {o["id"]:o for o in inventory["objects"]}
    anchor_map={}
    scope=lambda o:o.get('source_scope',o['locator'].split('/')[0])
    for o in inventory['objects']:
        for old_id in o.get('html_ids',[]):anchor_map.setdefault((scope(o),old_id),'loom-source-'+o['id'])
    md = MarkdownIt("commonmark", {"html":False}).enable("table")
    parts = []
    inserted={}
    if not project.get("draft"):
        return "<p>尚未生成候选</p>"
    for b in project["draft"]["blocks"]:
        parts.append(f'<section data-readweave-anchor-id="{html.escape(b["id"],quote=True)}">')
        parts.append(md.render(b["markdown"]))
        if b['kind']=='source' and b['object_ids']:
            parts.append('<blockquote>')
        for sid in b["object_ids"]:
            o = src.get(sid)
            if not o:
                parts.append("<p>原对象缺失</p>")
                continue
            inserted[sid]=inserted.get(sid,0)+1
            suffix='' if inserted[sid]==1 else '-repeat-'+str(inserted[sid])
            parts.append('<div id="loom-source-'+html.escape(sid,quote=True)+suffix+'">')
            if o["kind"] in {"image","page"}:
                if o.get("resource_id"):
                    parts.append(f'<figure class="image"><img src="{html.escape(asset_url(o["resource_id"]),quote=True)}" alt="{html.escape(o["text"][:160] or o["locator"],quote=True)}"></figure>')
                else:
                    parts.append("<p>图片资源尚未取得</p>")
            elif o["kind"]=="table" and o.get("raw", "").lstrip().startswith("<table"):
                parts.append(safe_html(o["raw"]))
            elif o["kind"]=="link":
                target = o.get("target", "")
                if target.startswith('#') and (scope(o),target[1:]) in anchor_map:target='#'+anchor_map[(scope(o),target[1:])]
                if urlsplit(target).scheme in {"http","https","mailto"} or target.startswith("#"):
                    parts.append(f'<p><a href="{html.escape(target,quote=True)}">{html.escape(o["text"] or target)}</a></p>')
                else:
                    parts.append(f'<p>{html.escape(o["text"])} — 原链接：{html.escape(target)}</p>')
            elif o["kind"] in {"code","formula","table","unknown"}:
                parts.append("<pre><code>"+html.escape(o["text"])+"</code></pre>")
            elif o["kind"] in {"text","heading","footnote"}:
                # A source reference is a real protected insertion, not metadata-only coverage.
                parts.append(safe_html(o["raw"]) if o.get("raw") else "<p>"+html.escape(o["text"]).replace("\n","<br>")+"</p>")
            elif o["kind"]=="attachment" and o.get("resource_id"):
                parts.append(f'<p><a href="{html.escape(asset_url(o["resource_id"]),quote=True)}">{html.escape(o["text"])}</a></p>')
            parts.append('</div>')
        if b['kind']=='source' and b['object_ids']:
            parts.append('</blockquote>')
        parts.append("</section>")
    result='\n'.join(parts)
    if anchor_map:
        doc=BeautifulSoup(result,'html.parser')
        wrappers={'loom-source-'+o['id']:o for o in inventory['objects']}
        for a in doc.select('a[href]'):
            wrapper=next((n for n in a.parents if n.get('id','').split('-repeat-')[0] in wrappers),None)
            if wrapper is None:continue
            obj=wrappers[wrapper['id'].split('-repeat-')[0]]
            source_url=(obj.get('source_url') or inventory.get('source_url') or '').split('#')[0]
            target=a['href']
            old=target[1:] if target.startswith('#') else target[len(source_url)+1:] if source_url and target.startswith(source_url+'#') else None
            if (scope(obj),old) in anchor_map:a['href']='#'+anchor_map[(scope(obj),old)]
        result=str(doc)
    return result


def export_zip(store, project, release=False):
    findings = inspect_draft(project["inventory"], project["draft"], project.get("plan"))
    if findings:
        raise Conflict("候选仍有结构或来源错误，先修复再导出")
    if release and release_issues(project):
        raise Conflict("当前只能导出带缺口说明的候选，尚不能标为已接受交付")
    root_id = "loom"+project["id"][:12]
    file_map = {}
    attachments = []
    def attach(raw, title, mime, role, key):
        name = "asset-"+key
        if mime.startswith("image/"):
            name += {"image/png":".png","image/jpeg":".jpg","image/webp":".webp","image/gif":".gif"}.get(mime,".bin")
        if name not in file_map:
            file_map[name] = raw
            attachments.append(dict(attachmentId="att"+key[:12],title=title,role=role,mime=mime,dataFileName=name,position=len(attachments)))
        return name
    resource_names = {}
    for r in project["inventory"]["resources"]:
        raw = store.read_blob(r["sha256"])
        resource_names[r["id"]] = attach(raw,r["name"],r["mime"],"image" if r["mime"].startswith("image/") else "file",r["id"])
    for original in project["inventory"]["originals"]:
        raw = store.read_blob(original["sha256"])
        attach(raw,original["name"],"application/octet-stream","file",original["sha256"])
    content = render(project,lambda key:resource_names[key])
    status = "已接受候选" if release else "待核对候选"
    banner = f'<p><strong>{status}</strong> · 原件保存、结构检查、语义审核和用户接受分别记录，详见附带审计文件</p>'
    file_map["material.html"] = (banner+content).encode()
    audit = dict(schema="sourceloom/1",revision=project["revision"],inventory=project["inventory"],plan=project["plan"],
                 draft=project["draft"],review=project["review"],accepted_revision=project["accepted_revision"],
                 release_issues=release_issues(project),readweave_roundtrip="not_verified")
    audit['previous_source_versions']=store.source_versions(project['id'])
    audit_raw = json.dumps(audit,ensure_ascii=False,indent=2).encode()
    attach(audit_raw,"sourceloom-audit.json","application/json","file",digest(audit_raw))
    meta = dict(formatVersion=2,appVersion="0.1.0",files=[dict(noteId=root_id,title=project["title"]+" · "+status,
         type="text",mime="text/html",format="html",dataFileName="material.html",attachments=attachments,children=[],
         attributes=[dict(type="label",name="sourceloomCandidate",value=project["id"]+":"+str(project["revision"]),isInheritable=False)])])
    output = BytesIO()
    with zipfile.ZipFile(output,"w",zipfile.ZIP_DEFLATED) as z:
        # Native importer discovers metadata before resolving body and attachments.
        z.writestr("!!!meta.json",json.dumps(meta,ensure_ascii=False))
        for name,raw in file_map.items():
            z.writestr(name,raw)
    return output.getvalue()
