"""Complete source content without duplicated parser traces or prior reviews."""

import re

DERIVED={'coordinates','original_extracted_text','visual_audit','visual_classification'}


def objects_view(objects):
    return [{k:v for k,v in obj.items() if k not in DERIVED} for obj in objects]


def inventory_groups(objects,limit=8000,max_objects=None):
    import re
    def container(obj):
        locator=obj.get('locator','')
        matches=list(re.finditer(r'/(?:li|p|td|th|figure|figcaption|pre|blockquote)\[\d+\]',locator))
        return locator[:matches[-1].end()] if matches else ''
    groups=[];current=[];size=0
    for obj in objects:
        n=len(obj.get('text',''))
        together=current and container(current[-1]) and container(current[-1])==container(obj)
        if current and not together and (size+n>limit or max_objects and len(current)>=max_objects):
            groups.append(current);current=[];size=0
        current.append(obj);size+=n
    if current:groups.append(current)
    return groups


def merge_inventories(parts):
    facts=[];assessed=[];unresolved=[]
    for index,part in enumerate(parts,1):
        facts.extend(f|{'id':f'group-{index}-'+f['id']} for f in part['facts'])
        assessed.extend(part['assessed_source_ids']);unresolved.extend(part['unresolved'])
    return dict(facts=facts,assessed_source_ids=assessed,unresolved=unresolved)


def patch_inventory(original,patch):
    import copy
    from .production_contracts import InventoryPatch
    patch=InventoryPatch.model_validate(patch).model_dump()
    current={f['id']:f for f in original['facts']}
    replacements={f['id']:f for f in patch['replacements']}
    additions={f['id']:f for f in patch['additions']}
    removed=set(patch['remove_ids'])
    if (len(replacements)!=len(patch['replacements']) or len(additions)!=len(patch['additions'])
            or len(removed)!=len(patch['remove_ids']) or not set(replacements)<=set(current)
            or not removed<=set(current) or set(additions)&set(current) or removed&set(replacements)):
        raise ValueError('清单局部补丁的新增、修改或删除身份冲突：'+str({
            'unknown_replacements':sorted(set(replacements)-set(current)),
            'existing_additions':sorted(set(additions)&set(current)),
            'unknown_removals':sorted(removed-set(current)),
            'replace_and_remove':sorted(removed&set(replacements)),
            'duplicate_replacements':len(replacements)!=len(patch['replacements']),
            'duplicate_additions':len(additions)!=len(patch['additions']),
            'duplicate_removals':len(removed)!=len(patch['remove_ids'])}))
    return dict(facts=[copy.deepcopy(replacements.get(f['id'],f)) for f in original['facts'] if f['id'] not in removed]+copy.deepcopy(patch['additions']),
                assessed_source_ids=list(original['assessed_source_ids']),unresolved=list(patch['unresolved']))


def classify_inert_markup(source):
    """Preserve complete web script/style elements as inert source metadata."""
    import copy,re
    result=copy.deepcopy(source);converted=set()
    for obj in result['objects']:
        if obj['kind']!='unknown':continue
        raw=obj.get('raw','')
        match=re.fullmatch(r'<(script|style)\b[^>]*>[\s\S]*</\1\s*>',raw,re.I)
        if not match:continue
        obj.update(kind='metadata',original_extracted_text=obj['text'],text=raw,
                   inert_markup=match[1].lower())
        converted.add(obj['id'])
    resolved=[gap for gap in result.get('unknown',[]) if gap['object_id'] in converted and
              re.match(r'^(script|style) 未执行，需要独立解释或安全转换$',gap['reason'])]
    result['unknown']=[g for g in result.get('unknown',[]) if g not in resolved]
    if resolved:result['resolved_markup_gaps']=result.get('resolved_markup_gaps',[])+resolved
    return result


def classify_web_chrome(store,source):
    """Archive linked site branding in the page title, including missing assets.

    The original HTML and any fetched image remain intact; this classification
    only excludes a proven header logo from article rewriting and OCR.
    """
    import copy
    from urllib.parse import urljoin,urlsplit
    from bs4 import BeautifulSoup
    result=copy.deepcopy(source)
    page_url=result.get('source_url','')
    if not page_url:return result
    parsed=urlsplit(page_url)
    pages=[];chrome_prefixes=[];metadata_prefixes=[];content_roots=[];control_icon_prefixes=[];profile_images=set()
    def dom_locator(name,root,element):
        parts=[];child=element
        while child is not root and child.parent is not None:
            parent=child.parent
            index=next(i for i,item in enumerate(parent.children,1) if item is child)
            parts.append(('node' if parent is root else child.name)+f'[{index}]')
            child=parent
        return name+'/'+('/'.join(reversed(parts))) if child is root else None
    for original in result.get('originals',[]):
        if original['name'].lower().endswith(('.html','.htm')):
            page=BeautifulSoup(store.read_blob(original['sha256']),'html.parser')
            pages.append(page)
            root=page.body or page
            # An inline SVG nested in an actual button/summary is interface chrome.
            # Classify only the icon itself: article diagrams and the control's
            # visible label stay available to the normal source rules.
            for control in root.find_all(['button','summary']):
                for icon in control.find_all('svg'):
                    prefix=dom_locator(original['name'],root,icon)
                    if prefix:control_icon_prefixes.append(prefix)
            main=(root.find('main') or root.find(id='main') or root.find(attrs={'role':'main'}))
            has_main=bool(main)
            if main:
                articles=[item for item in main.find_all('article')
                          if item.find('h1') and len(item.get_text(' ',strip=True))>100]
                content=articles[0] if len(articles)==1 else main
                prefix=dom_locator(original['name'],root,content)
                if prefix:content_roots.append((original['name'],prefix))
                for image in content.find_all('img'):
                    if image.find_parent('figure') or not re.search(r'\bavatar\b',image.get('alt',''),re.I):continue
                    located=dom_locator(original['name'],root,image)
                    if located:profile_images.add(located)
                for element in content.find_all(True):
                    classes=' '.join(element.get('class',[])).lower()
                    if (element.name=='header' and 'in-resource' in element.get('class',[])
                            and element.find('h1')):
                        for paragraph in element.find_all('p',recursive=False):
                            anchor=paragraph.find('a',href=True)
                            if not anchor or not paragraph.get_text(' ',strip=True).lower().startswith('in '):
                                continue
                            target=urlsplit(urljoin(page_url,anchor['href']))
                            if (target.hostname==parsed.hostname and
                                    parsed.path.startswith(target.path.rstrip('/')+'/')):
                                prefix=dom_locator(original['name'],root,paragraph)
                                if prefix:chrome_prefixes.append(prefix)
                    support_panel=(element.name=='aside' and
                        re.match(r'(?i)^(?:related .+ resources|help improve this page)\b',
                                 element.get_text(' ',strip=True)))
                    if element.name=='nav' or support_panel or any(label in classes
                        for label in ('secondary-navigation','breadcrumb','site-navigation')):
                        prefix=dom_locator(original['name'],root,element)
                        if prefix:chrome_prefixes.append(prefix)
            for index,child in enumerate(root.children,1):
                if getattr(child,'name',None)=='address':
                    metadata_prefixes.append(f"{original['name']}/node[{index}]")
                if (getattr(child,'name',None) and child.get('id')=='banner' and
                        child.find(['h1','h2','h3'],string=re.compile(r'^\s*Site navigation\s*$',re.I))):
                    chrome_prefixes.append(f"{original['name']}/node[{index}]")
            if has_main:
                for index,child in enumerate(root.children,1):
                    if not getattr(child,'name',None):continue
                    labels={str(child.get('id','')).lower(),*(str(x).lower() for x in child.get('class',[]))}
                    if child.name in {'nav','header','footer'} or labels&{
                        'banner','menu','navbar','searchbox','copyright','site-header','site-footer'}:
                        chrome_prefixes.append(f"{original['name']}/node[{index}]")
    resolved=[]
    for original in result.get('originals',[]):
        if not original['name'].lower().endswith(('.md','.markdown')):continue
        body=store.read_blob(original['sha256']).decode('utf-8',errors='replace')
        first_line=next((line for line in body.splitlines() if line.strip()),'')
        badge_parents=set()
        if re.match(r'^#{1,6}\s',first_line):
            badge_context=first_line
            paragraphs=body.split('\n\n',2)
            if len(paragraphs)>1:
                from markdown_it import MarkdownIt
                tokens=MarkdownIt('commonmark').parse(paragraphs[1])
                children=[child for token in tokens for child in token.children or []]
                images=[child.attrGet('src') or '' for child in children if child.type=='image']
                if (images and all(re.search(r'/badge(?:\.svg)?(?:\?|$)',url,re.I)
                                   or 'img.shields.io/' in url for url in images)
                        and all(child.type in {'image','link_open','link_close','softbreak'}
                                or child.type=='text' and not child.content.strip()
                                for child in children)):
                    badge_context+='\n'+paragraphs[1]
            for obj in result['objects']:
                target=obj.get('target','')
                if (obj['kind']!='image' or not target or target not in badge_context
                        or not (re.search(r'/badge(?:\.svg)?(?:\?|$)',target,re.I)
                                or 'img.shields.io/' in target)):
                    continue
                obj['visual_classification']=dict(method='markdown_heading_status_badge',
                    source_role='source_metadata',resource_available=bool(obj.get('resource_id')))
                obj['source_scope']='source_metadata'
                badge_parents.add(obj.get('locator','').rsplit('/',1)[0])
                resolved.append(obj['id'])
            for obj in result['objects']:
                if (obj['kind']=='link' and not obj.get('text','').strip()
                        and obj.get('target','') in badge_context
                        and obj.get('locator','').rsplit('/',1)[0] in badge_parents):
                    obj['source_scope']='source_metadata'
        first=body.split('\n\n',1)[0]
        wrapper=BeautifulSoup(first,'html.parser').find('div')
        if not wrapper or str(wrapper.get('align','')).lower()!='center':continue
        for image in wrapper.find_all('img'):
            if not image.has_attr('alt') or image.get('alt','').strip():continue
            target=urljoin(page_url,image.get('src',''))
            for obj in result['objects']:
                if obj['kind']=='image' and obj.get('target')==target and obj.get('locator','').startswith(original['name']+'/node[1]'):
                    obj['visual_classification']=dict(method='markdown_centered_empty_alt_branding',
                        source_role='site_branding',resource_available=bool(obj.get('resource_id')))
                    obj['source_scope']='site_chrome'
                    resolved.append(obj['id'])
    for obj in result['objects']:
        if obj.get('locator') in profile_images and obj.get('kind')=='image':
            obj['visual_classification']=dict(method='source_dom_profile_avatar',
                source_role='source_metadata',resource_available=bool(obj.get('resource_id')))
            obj['source_scope']='source_metadata';resolved.append(obj['id'])
        if (obj.get('locator','') in control_icon_prefixes and obj.get('kind') in {'unknown','image'}
                and re.match(r'^\s*<svg\b',obj.get('raw',''),re.I)):
            obj['visual_classification']=dict(method='source_dom_interactive_control_icon',
                source_role='site_chrome',resource_available=True)
            obj['source_scope']='site_chrome'
            resolved.append(obj['id'])
        for name,prefix in content_roots:
            locator=obj.get('locator','')
            if locator.startswith(name+'/node[') and not (locator==prefix or locator.startswith(prefix+'/')):
                obj['source_scope']='site_chrome'
        if any(obj.get('locator','').startswith(prefix+'/') or obj.get('locator')==prefix
               for prefix in metadata_prefixes):
            obj['source_scope']='source_metadata'
        if any(obj.get('locator','').startswith(prefix+'/') or obj.get('locator')==prefix
               for prefix in chrome_prefixes):
            obj['source_scope']='site_chrome'
        if obj['kind']!='image' or obj.get('visual_classification') or not obj.get('target'):continue
        for page in pages:
            for image in page.find_all('img'):
                if urljoin(page_url,image.get('src',''))!=obj['target']:continue
                anchor=image.find_parent('a',href=True)
                heading=image.find_parent(['h1','header'])
                if not anchor or not heading:continue
                destination=urljoin(page_url,anchor['href'])
                linked=urlsplit(destination)
                if linked.hostname!=parsed.hostname or not parsed.path.startswith(linked.path.rstrip('/')+'/'):
                    continue
                obj['visual_classification']=dict(method='source_dom_heading_home_link',
                    source_role='site_branding',destination=destination,
                    resource_available=bool(obj.get('resource_id')))
                obj['source_scope']='site_chrome'
                resolved.append(obj['id'])
                break
            if obj.get('visual_classification'):break
    if resolved:
        result['resolved_chrome_gaps']=result.get('resolved_chrome_gaps',[])+[
            gap for gap in result.get('unknown',[]) if gap['object_id'] in resolved]
        result['unknown']=[gap for gap in result.get('unknown',[]) if gap['object_id'] not in resolved]
    return result


def classify_layout_tables(source):
    """Unwrap an unambiguous one-row web layout, never a data table.

    The original HTML bytes stay in raw and the original file; extracted images
    and links keep their own source identities and normal preservation rules.
    """
    import copy
    from bs4 import BeautifulSoup
    result=copy.deepcopy(source)
    for obj in result['objects']:
        if obj['kind']!='table' or not obj.get('raw','').lstrip().startswith('<table'):continue
        table=BeautifulSoup(obj['raw'],'html.parser').find('table')
        if not table or table.find(['th','caption']) or len(table.find_all('tr'))!=1:continue
        cells=table.find_all('td')
        content=[c for c in cells if c.get_text(strip=True)]
        if len(content)!=1 or not content[0].find(['p','ul','ol']):continue
        if not any(c.find('img') for c in cells if c is not content[0]):continue
        obj.update(kind='text',original_kind='table',layout_classification='single-row-one-text-cell-with-image-layout')
    return result
