"""Read Word document stories in order and retain unsupported constructs explicitly."""

import html
import mimetypes
import posixpath
from defusedxml import ElementTree as ET

W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
A='http://schemas.openxmlformats.org/drawingml/2006/main'


def read_word(parts,name,store,resources,add,gap,safe_images):
    local=lambda tag:tag.rsplit('}',1)[-1]
    attr=lambda node,key,default=None:node.get('{'+W+'}'+key,default)
    media={}
    emitted=set()
    for path,raw in parts.items():
        if path.startswith('word/media/'):
            key=store.blob(raw);mime=mimetypes.guess_type(path)[0] or 'application/octet-stream'
            media[path]=(key,mime)
            resources.append(dict(id=key,name=path,sha256=key,size=len(raw),mime=mime))
    stories=['word/document.xml']+sorted(p for p in parts if
        p.startswith('word/') and p.endswith('.xml') and
        (p.split('/')[-1].startswith(('header','footer')) or
         p in {'word/footnotes.xml','word/endnotes.xml','word/comments.xml'}))
    if stories[0] not in parts:
        raise ValueError('文稿缺少正文部件')

    def text_of(node):
        chunks=[]
        for child in node.iter():
            tag=local(child.tag)
            if tag in {'t','delText'}:chunks.append(child.text or '')
            elif tag=='tab':chunks.append('\t')
            elif tag in {'br','cr'}:chunks.append('\n')
            elif tag=='noBreakHyphen':chunks.append('\u2011')
            elif tag=='softHyphen':chunks.append('\u00ad')
        return ''.join(chunks)

    for part in stories:
        root=ET.fromstring(parts[part]);base=posixpath.dirname(part)
        relpath=base+'/_rels/'+posixpath.basename(part)+'.rels'
        rels={r.get('Id'):r for r in ET.fromstring(parts[relpath])} if relpath in parts else {}

        def related(node,obj,location):
            for index,child in enumerate(node.iter()):
                tag=local(child.tag);loc=location+'/'+tag+'['+str(index)+']'
                if tag=='hyperlink':
                    relation=rels.get(child.get('{'+R+'}id'))
                    target=relation.get('Target') if relation is not None else None
                    anchor=attr(child,'anchor')
                    if target or anchor:
                        add('link',text_of(child),loc,target=target or '#'+anchor,
                            original_target=target or '#'+anchor,parent_id=obj['id'])
                    else:gap('文稿链接没有可解析的目标',loc,obj['id'])
                elif tag=='blip':
                    rid=child.get('{'+R+'}embed') or child.get('{'+R+'}link');rel=rels.get(rid)
                    target=rel.get('Target','') if rel is not None else ''
                    path=posixpath.normpath(posixpath.join(base,target))
                    if path not in media:
                        gap('文稿图片关系未指向已保存的媒体',loc,obj['id']);continue
                    key,mime=media[path];emitted.add(path)
                    pic=add('image' if mime in safe_images else 'attachment',path,loc,
                        resource_id=key,parent_id=obj['id'],source_part=part)
                    if mime not in safe_images:gap('此文稿媒体尚无可靠显示方式',loc,pic['id'])
                elif tag in {'footnoteReference','endnoteReference','commentReference'}:
                    add('metadata',tag+':'+str(attr(child,'id','')),loc,parent_id=obj['id'],
                        reference_kind=tag,reference_id=attr(child,'id',''),source_part=part)
                elif tag in {'oMath','oMathPara'}:
                    if tag=='oMath':
                        formula=add('formula',ET.tostring(child,encoding='unicode'),loc,syntax='omml',parent_id=obj['id'])
                        gap('原始文稿公式已保留，尚未完成可视排版核对',loc,formula['id'])
                elif tag in {'ins','del','moveFrom','moveTo','fldChar','instrText','sym','object','altChunk','pict','vanish','anchor'}:
                    gap('文稿包含需要核对的原始结构：'+tag,loc,obj['id'])

        def paragraph(node,location,context=None):
            text=text_of(node)
            props=node.find('{'+W+'}pPr');style=props.find('{'+W+'}pStyle') if props is not None else None
            style_name=attr(style,'val','') if style is not None else ''
            kind='heading' if style_name.lower().startswith(('heading','title')) else 'footnote' if context and context.get('kind') in {'footnote','endnote'} else 'text'
            if not text and not any(local(c.tag) in {'blip','oMath','pict','object'} for c in node.iter()):return
            obj=add(kind,text,location,raw=ET.tostring(node,encoding='unicode'),source_part=part,
                    story=context or {},paragraph_style=style_name,
                    html_ids=[attr(b,'name','') for b in node.iter('{'+W+'}bookmarkStart') if attr(b,'name','')])
            if props is not None and props.find('{'+W+'}numPr') is not None:
                gap('原始编号结构已保留，需要核对显示编号及重启规则',location,obj['id'])
            related(node,obj,location)

        def table(node,location,context=None):
            rows=[];cells=[]
            for row in node.findall('{'+W+'}tr'):
                raw_cells=[];row_cells=[]
                for cell in row.findall('{'+W+'}tc'):
                    props=cell.find('{'+W+'}tcPr');span=props.find('{'+W+'}gridSpan') if props is not None else None
                    colspan=attr(span,'val','1') if span is not None else '1'
                    paragraphs=[text_of(p) for p in cell.findall('{'+W+'}p')]
                    text='\n'.join(paragraphs)
                    raw_cells.append('<td colspan="'+html.escape(colspan,quote=True)+'">'+''.join('<p>'+html.escape(p)+'</p>' for p in paragraphs)+'</td>')
                    row_cells.append(dict(text=text,rowspan='1',colspan=colspan))
                rows.append('<tr>'+''.join(raw_cells)+'</tr>');cells.append(row_cells)
            obj=add('table','\n'.join('\t'.join(c['text'] for c in row) for row in cells),location,
                    raw='<table>'+''.join(rows)+'</table>',source_xml=ET.tostring(node,encoding='unicode'),
                    cells=cells,source_part=part,story=context or {})
            related(node,obj,location)
            if node.find('.//{'+W+'}vMerge') is not None:
                gap('原表包含纵向合并，显示跨度需要核对',location,obj['id'])
            if any(c is not node for c in node.iter('{'+W+'}tbl')):
                gap('原表包含嵌套表格，已保留完整结构但显示关系尚未核对',location,obj['id'])

        def walk(node,location,context=None):
            tag=local(node.tag)
            if tag=='p':paragraph(node,location,context);return
            if tag=='tbl':table(node,location,context);return
            if tag in {'footnote','endnote','comment'}:
                context={'kind':tag,'id':attr(node,'id',''),'author':attr(node,'author',''),'date':attr(node,'date','')}
                if attr(node,'type') in {'separator','continuationSeparator'}:
                    add('metadata',ET.tostring(node,encoding='unicode'),location,source_part=part,story=context);return
            if tag in {'altChunk','object','pict'}:
                obj=add('unknown',ET.tostring(node,encoding='unicode'),location,source_part=part)
                gap('原始嵌入部件需要独立转换',location,obj['id']);return
            for index,child in enumerate(node):walk(child,location+'/'+local(child.tag)+'['+str(index)+']',context)
        walk(root,name+'/'+part)
    for path,(key,mime) in media.items():
        if path not in emitted:
            obj=add('attachment',path,name+'/'+path,resource_id=key)
            gap('媒体已保存，但正文中的归属尚未确定',name+'/'+path,obj['id'])
    for path,raw in parts.items():
        if path.startswith('docProps/') and path.endswith('.xml'):
            root=ET.fromstring(raw)
            add('metadata','\n'.join(local(n.tag)+': '+n.text for n in root.iter() if n.text and n.text.strip()),
                name+'/'+path,raw=raw.decode('utf-8-sig'),syntax='xml')
        elif path.startswith(('word/charts/','word/diagrams/','word/embeddings/')):
            key=store.blob(raw);mime=mimetypes.guess_type(path)[0] or 'application/octet-stream'
            resources.append(dict(id=key,name=path,sha256=key,size=len(raw),mime=mime))
            obj=add('attachment',path,name+'/'+path,resource_id=key)
            gap('原始图形或嵌入部件已保存，内部内容仍需独立解释',name+'/'+path,obj['id'])
