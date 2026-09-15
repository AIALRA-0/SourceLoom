"""Observable transparent resources and detailed views of unchanged PDF pages."""
import copy
import json
import math
import re
from io import BytesIO

from PIL import Image
from .store import digest


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
        obj['visual_classification']=checked[key]
        obj.setdefault('original_extracted_text',obj.get('text',''))
        obj['text']=obj.get('text') or '透明占位图，无可见文字或图形'
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
