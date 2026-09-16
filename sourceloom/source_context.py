"""Complete source content without duplicated parser traces or prior reviews."""

DERIVED={'coordinates','original_extracted_text','visual_audit','visual_classification'}


def objects_view(objects):
    return [{k:v for k,v in obj.items() if k not in DERIVED} for obj in objects]


def inventory_groups(objects,limit=8000):
    groups=[];current=[];size=0
    for obj in objects:
        n=len(obj.get('text',''))
        if current and size+n>limit:groups.append(current);current=[];size=0
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
