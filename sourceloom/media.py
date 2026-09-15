"""Readable resource projections; original objects and saved drafts remain intact."""
import copy
import html
import re


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
    """Change generated image wrappers only, never stored words or source bytes."""
    result=copy.deepcopy(project['draft'])
    if not result:return result
    objects={o['id']:o for o in (project.get('inventory') or {}).get('objects',[])}
    for block in result['blocks']:
        for sid in block.get('embedded_object_ids',[]):
            obj=objects.get(sid)
            if obj and layout_reference(obj):
                raw=obj['raw'];replacement='<details><summary>查看原文版式与配图</summary>\n\n'+raw+'\n\n</details>'
                if replacement not in block['markdown']:block['markdown']=block['markdown'].replace(raw,replacement)
            if not obj or obj['kind'] not in {'image','page'} or not obj.get('resource_id'):continue
            current=image_markup(obj)
            old=image_markup(obj,legacy=True)
            block['markdown']=block['markdown'].replace(old,current)
            if obj['kind']=='page':
                # Complete page facsimiles are optional reference, not article prose.
                label=html.escape('查看原件页面 · '+obj['locator'])
                replacement='<details><summary>'+label+'</summary>\n\n'+current+'\n\n</details>'
                if replacement not in block['markdown']:
                    block['markdown']=block['markdown'].replace(current,replacement)
        # Long literal quotations remain accessible without dominating the rewrite.
        if block.get('kind')=='source' and len(block['markdown'])>600 and block['markdown'].lstrip().startswith('>'):
            block['markdown']='<details><summary>查看这段原文</summary>\n\n'+block['markdown']+'\n\n</details>'
        if block.get('kind')=='document_info' and not block['markdown'].startswith('<details>'):
            block['markdown']='<details><summary>查看材料附记</summary>\n\n'+block['markdown']+'\n\n</details>'
    return result
