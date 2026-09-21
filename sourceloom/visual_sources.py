"""Observable transparent resources and detailed views of unchanged PDF pages."""
import copy
import json
import math
import re
from io import BytesIO

from PIL import Image
from .store import digest


def decorative_resource(obj):
    if obj.get('kind')=='text' and not obj.get('text','').strip():return True
    if obj.get('source_scope') in {'site_chrome','source_metadata'}:return True
    card=obj.get('visual_card') or {}
    if (obj.get('kind') in {'image','page'} and card.get('role')=='decorative'
            and not card.get('source_text','').strip()):
        return True
    if (obj.get('kind')=='link' and not obj.get('text','').strip()
            and '/figure[' in obj.get('locator','')
            and (re.search(r'\.(?:avif|gif|jpe?g|png|svg|webp)(?:[?#]|$)',obj.get('target',''),re.I)
                 or 'substackcdn.com/image/' in obj.get('target','').casefold())):
        return True
    evidence=obj.get('visual_classification') or {}
    if evidence.get('method')=='source_dom_heading_home_link' and evidence.get('source_role')=='site_branding':
        return True
    return (evidence.get('method')=='all_pixels_alpha_zero'
            and evidence.get('source_role')=='explicit_empty_alt'
            and not obj.get('original_extracted_text',obj.get('text','')).strip())


def classify_transparent(store,source):
    result=copy.deepcopy(source);resolved=[];checked={}
    for obj in result['objects']:
        if obj['kind']!='image' or not obj.get('resource_id'):continue
        key=obj['resource_id']
        if key not in checked:
            with Image.open(BytesIO(store.read_blob(key))) as image:
                blank=getattr(image,'n_frames',1)==1 and image.convert('RGBA').getchannel('A').getextrema()==(0,0)
                checked[key]={'method':'all_pixels_alpha_zero','resource_id':key,'size':list(image.size)} if blank else None
        if not checked[key]:continue
        obj['visual_classification']=dict(checked[key])
        obj.setdefault('original_extracted_text',obj.get('text',''))
        # Alpha proves invisibility, not decorative intent. Require source markup
        # explicitly declaring empty alt, without another meaningful label.
        from bs4 import BeautifulSoup
        images=BeautifulSoup(obj.get('raw',''),'html.parser').find_all('img')
        if not images:
            for original in source.get('originals',[]):
                if not original['name'].lower().endswith(('.html','.htm')):continue
                doc=BeautifulSoup(store.read_blob(original['sha256']),'html.parser')
                images.extend(i for i in doc.find_all('img') if i.get('src')==obj.get('target'))
        if images and all(i.has_attr('alt') and not i.get('alt','').strip()
                          and not i.get('title') and not i.get('aria-label')
                          and not i.get('aria-labelledby') for i in images) and not obj['original_extracted_text'].strip():
            obj['visual_classification']['source_role']='explicit_empty_alt'
        resolved.append(obj['id'])
    if resolved:
        result['resolved_visual_gaps']=result.get('resolved_visual_gaps',[])+[g for g in result.get('unknown',[]) if g['object_id'] in resolved]
        result['unknown']=[g for g in result.get('unknown',[]) if g['object_id'] not in resolved]
    return result


def image_resources(store,pages,source,detail_ids=()):
    resources=[];cache=store.root/'visual-details';cache.mkdir(exist_ok=True)
    for obj in pages:
        key=obj['resource_id'];match=re.fullmatch(r'(.+)/page\[(\d+)\]',obj.get('locator',''))
        original=next((o for o in source.get('originals',[]) if match and o['name']==match[1] and o['name'].lower().endswith('.pdf')),None)
        if original:
            record=cache/(digest([original['sha256'],int(match[2]),'180dpi-6mp'])+'.json')
            if record.exists():key=json.loads(record.read_text())['sha256']
            else:
                import pypdfium2 as pdfium
                pdf=pdfium.PdfDocument(store.read_blob(original['sha256']))
                try:
                    page=pdf[int(match[2])-1];width,height=page.get_size()
                    scale=min(2.5,math.sqrt(6000000/(width*height)))
                    bitmap=page.render(scale=scale)
                    try:
                        buf=BytesIO();bitmap.to_pil().save(buf,'PNG');key=store.blob(buf.getvalue())
                    finally:bitmap.close();page.close()
                finally:pdf.close()
                record.write_text(json.dumps({'sha256':key,'original':original['sha256'],'page':int(match[2]),'scale':scale}))
        resources.append({'source_id':obj['id'],'sha256':key})
        if obj['id'] in detail_ids:
            # Detail views are additional evidence; retain the complete original
            # and label coordinates so overlap is never mistaken for new content.
            with Image.open(BytesIO(store.read_blob(key))) as picture:
                width,height=picture.size
                if width>=800 and height>=800:
                    for label,box in [('top-left',(0,0,int(width*.6),int(height*.6))),
                                      ('top-right',(int(width*.4),0,width,int(height*.6))),
                                      ('bottom-left',(0,int(height*.4),int(width*.6),height)),
                                      ('bottom-right',(int(width*.4),int(height*.4),width,height))]:
                        buf=BytesIO();picture.crop(box).save(buf,'PNG')
                        resources.append({'source_id':obj['id'],'sha256':store.blob(buf.getvalue()),
                            'view':label,'pixel_bounds':list(box),'original_size':[width,height],
                            'derived_from':key,'note':'Overlapping detail of the same original, not additional source content'})
    return resources
