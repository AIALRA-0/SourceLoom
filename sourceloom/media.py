"""Readable resource projections; original objects and saved drafts remain intact."""
import copy
import html
import re


def document_info_label(block):
    """Recognize an explicitly named metadata section, never infer from language.

    Writers can label bibliographic sections as explanation blocks. Only a
    single, explicit metadata heading earns a reversible disclosure; a mixed
    section or ordinary English prose stays visible.
    """
    labels={'文章标题、作者与机构信息','作者与机构信息','作者及机构信息',
            '出版信息','文章元数据','文献元数据','Author Affiliations',
            'Publication Information','Article Metadata'}
    first=block['markdown'].lstrip().split('\n',1)[0]
    if not first.startswith('#') or first.lstrip('#').strip() not in labels:return None
    from markdown_it import MarkdownIt
    tokens=MarkdownIt('commonmark',{'html':True}).parse(block['markdown'])
    headings=[tokens[i+1].content for i,t in enumerate(tokens)
              if t.type=='heading_open' and i+1<len(tokens)]
    if len(headings)==1 and headings[0] in labels:return headings[0]
    return None


def image_markup(obj, legacy=False):
    description=obj.get('text') or obj['locator']
    if not legacy:
        if obj['kind']=='page':description='原件页面 · '+obj['locator']
        else:description=' '.join(description.split())[:180]
    return '<img src="assets/'+obj['resource_id']+'" alt="'+html.escape(description,quote=True)+'">'


def source_literals(obj,current):
    if obj['kind'] in {'page','image'} and obj.get('resource_id'):
        return list(dict.fromkeys([current,image_markup(obj,legacy=True)]))
    return [current]


def layout_reference(obj):
    """Fold prose layout tables, never ordinary data tables; keep all bytes."""
    if obj.get('kind')!='table' or len(obj.get('text',''))<600:return False
    from bs4 import BeautifulSoup
    doc=BeautifulSoup(obj.get('raw',''),'html.parser')
    return not doc.find('th') and len(doc.find_all('tr'))<=2 and len(doc.find_all('p'))>=2 and bool(doc.find('img'))


def reading_draft(project):
    """Add reading wrappers without changing stored words or source bytes."""
    result=copy.deepcopy(project['draft'])
    if not result:return result
    objects={o['id']:o for o in (project.get('inventory') or {}).get('objects',[])}
    for block in result['blocks']:
        for sid in block.get('embedded_object_ids',[]):
            obj=objects.get(sid)
            if obj and obj['kind']=='code' and not obj.get('fence_raw'):
                raw=obj.get('text','')
                if raw and raw in block['markdown']:
                    fence='`'*max(3,1+max((len(m.group()) for m in re.finditer(r'`+',raw)),default=0))
                    block['markdown']=block['markdown'].replace(raw,
                        fence+'text\n'+raw+'\n'+fence,1)
            if obj and (layout_reference(obj) or obj.get('kind')=='table' and '<img' in obj.get('raw','').lower()):
                raw=obj['raw'];replacement='<details><summary>查看原文版式与配图</summary>\n\n'+raw+'\n\n</details>'
                if replacement not in block['markdown']:block['markdown']=block['markdown'].replace(raw,replacement)
            if not obj or obj['kind'] not in {'image','page'} or not obj.get('resource_id'):continue
            current=image_markup(obj)
            old=image_markup(obj,legacy=True)
            block['markdown']=block['markdown'].replace(old,current)
            label=html.escape(('原件页面' if obj['kind']=='page' else '材料配图')+' · '+obj['locator'])
            replacement='<details><summary>'+label+'</summary>\n\n'+current+'\n\n</details>'
            # Existing page wrappers retain their labels without nested controls.
            from bs4 import BeautifulSoup
            existing=BeautifulSoup(block['markdown'],'html.parser')
            wrapped=any(i.find_parent('details') for i in existing.select('img') if i.get('src')=='assets/'+obj['resource_id'])
            if not wrapped:block['markdown']=block['markdown'].replace(current,replacement)
        # Long literal quotations remain accessible without dominating the rewrite.
        if block.get('kind')=='source' and len(block['markdown'])>600 and block['markdown'].lstrip().startswith('>'):
            block['markdown']='<details><summary>查看这段原文</summary>\n\n'+block['markdown']+'\n\n</details>'
        label='查看材料附记' if block.get('kind')=='document_info' else document_info_label(block)
        if label and not block['markdown'].startswith('<details>'):
            block['markdown']='<details><summary>'+html.escape(label)+'</summary>\n\n'+block['markdown']+'\n\n</details>'
    return result


def fold_media(raw):
    """Add presentation controls without modifying image or table contents."""
    from bs4 import BeautifulSoup
    doc=BeautifulSoup(raw,'html.parser')
    for table in doc.select('table'):
        if table.find_parent('table') or table.find_parent(class_='table-scroll'):continue
        wrapper=doc.new_tag('div');wrapper['class']='table-scroll'
        table.wrap(wrapper)
    for image in doc.select('img'):
        if 'source-spacer' in image.get('class',[]):continue
        parent=image.find_parent('details')
        if parent is None:
            target=image.find_parent('table') or image.find_parent('figure') or image.find_parent('a') or image
            paragraph=image.find_parent('p')
            if paragraph is not None and (target is image or paragraph in target.parents):
                if not paragraph.get_text(strip=True):target=paragraph
                else:
                    # A disclosure cannot live inside <p>: browsers otherwise
                    # split it and manufacture an empty trailing paragraph.
                    paragraph.name='div'
                    paragraph['class']=[*paragraph.get('class',[]),'media-paragraph']
            parent=doc.new_tag('details');label=doc.new_tag('summary');label.string='材料配图'
            target.wrap(parent);parent.insert(0,label)
        # Open all ancestor disclosures together, including layout references.
        for region in [parent,*parent.find_parents('details')]:
            region['data-media']='true';region.attrs.pop('open',None)
    return str(doc)
