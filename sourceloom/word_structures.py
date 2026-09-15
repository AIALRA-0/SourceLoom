"""Read stored Word bullets and supported cached fields without executing fields."""
import copy
import io
import re
import zipfile
from defusedxml import ElementTree as ET

W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
A='http://schemas.openxmlformats.org/drawingml/2006/main'
R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def attr(node,name):
    return node.get('{'+W+'}'+name) if node is not None else None


def bullet_definition(node,numbering):
    props=node.find('{'+W+'}pPr/{'+W+'}numPr')
    if props is None or numbering is None:return None
    num_id=attr(props.find('{'+W+'}numId'),'val')
    level=attr(props.find('{'+W+'}ilvl'),'val') or '0'
    num=next((n for n in numbering.findall('{'+W+'}num') if attr(n,'numId')==num_id),None)
    if num is None:return None
    abstract_id=attr(num.find('{'+W+'}abstractNumId'),'val')
    abstract=next((n for n in numbering.findall('{'+W+'}abstractNum') if attr(n,'abstractNumId')==abstract_id),None)
    if abstract is None:return None
    definition=next((n for n in abstract.findall('{'+W+'}lvl') if attr(n,'ilvl')==level),None)
    override=next((n for n in num.findall('{'+W+'}lvlOverride') if attr(n,'ilvl')==level),None)
    if override is not None and override.find('{'+W+'}lvl') is not None:definition=override.find('{'+W+'}lvl')
    if definition is None or attr(definition.find('{'+W+'}numFmt'),'val')!='bullet':return None
    marker=attr(definition.find('{'+W+'}lvlText'),'val')
    if not marker or '%' in marker or definition.find('{'+W+'}lvlPicBulletId') is not None:return None
    return dict(num_id=num_id,level=int(level),format='bullet',marker=marker,
                definition_xml=ET.tostring(definition,encoding='unicode'))


def cached_fields(node,has_saved_image):
    fields=[];active=None
    for child in node.iter():
        if child.tag=='{'+W+'}fldChar':
            state=attr(child,'fldCharType')
            if state=='begin':
                if active is not None:return None
                active=dict(instruction='',result_text='',image_ids=[],state='instruction')
            elif state=='separate':
                if active is None or active['state']!='instruction':return None
                active['state']='result'
            elif state=='end':
                if active is None or active['state']!='result':return None
                instruction=active['instruction'].strip()
                if re.fullmatch(r'PAGE(?:\s+\\\*\s+(?:MERGEFORMAT|Arabic))*',instruction,re.I) and re.fullmatch(r'\d+',active['result_text'].strip()):
                    kind='cached_page_number'
                elif re.fullmatch(r'INCLUDEPICTURE\s+"[^"\r\n]+"(?:\s+\\\*\s+MERGEFORMAT(?:INET)?)*',instruction,re.I) and active['image_ids'] and has_saved_image:
                    kind='cached_embedded_picture'
                else:return None
                fields.append({k:v for k,v in active.items() if k!='state'}|{'kind':kind,'evaluated':False})
                active=None
            else:return None
        elif child.tag=='{'+W+'}instrText':
            if active is None or active['state']!='instruction':return None
            active['instruction']+=child.text or ''
        elif active is not None and active['state']=='result':
            if child.tag=='{'+W+'}t':active['result_text']+=child.text or ''
            if child.tag=='{'+A+'}blip' and child.get('{'+R+'}embed'):active['image_ids'].append(child.get('{'+R+'}embed'))
    return fields if active is None and fields else None


def resolve_word_structures(store,source):
    if source.get('word_structure_resolution_version')==1:return source
    originals=[o for o in source.get('originals',[]) if o['name'].lower().endswith('.docx')]
    if not originals:return source
    result=copy.deepcopy(source);resolved=[]
    for original in originals:
        with zipfile.ZipFile(io.BytesIO(store.read_blob(original['sha256']))) as archive:
            numbering=ET.fromstring(archive.read('word/numbering.xml')) if 'word/numbering.xml' in archive.namelist() else None
        for obj in result['objects']:
            if not obj.get('source_part') or not obj['locator'].startswith(original['name']+'/') or not obj.get('raw'):continue
            try:node=ET.fromstring(obj['raw'])
            except ET.ParseError:continue
            bullet=bullet_definition(node,numbering)
            # A cached image must also be registered as a saved child resource.
            images=[o for o in result['objects'] if o.get('parent_id')==obj['id'] and o.get('resource_id') and o['kind']=='image']
            fields=cached_fields(node,bool(images))
            if bullet:obj['word_list']=bullet
            if fields:obj['word_cached_fields']=fields
            for gap in result.get('unknown',[]):
                if gap['object_id']!=obj['id']:continue
                if (bullet and gap['reason']=='原始编号结构已保留，需要核对显示编号及重启规则') or (
                    fields and gap['reason'] in {'文稿包含需要核对的原始结构：fldChar','文稿包含需要核对的原始结构：instrText'}):
                    resolved.append(gap)
    result['unknown']=[g for g in result.get('unknown',[]) if g not in resolved]
    result['resolved_word_structure_gaps']=result.get('resolved_word_structure_gaps',[])+resolved
    result['word_structure_resolution_version']=1
    return result
