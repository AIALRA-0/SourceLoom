"""Bind a web acquisition manifest to parsed source objects before generation."""

import copy
from collections import defaultdict, deque


def bind_web_material_manifest(source, manifest):
    """Attach every discovered image occurrence to its exact stored object."""
    result=copy.deepcopy(source)
    originals={item['name']:item['sha256'] for item in result.get('originals',[])}
    by_resource=defaultdict(deque)
    for obj in result.get('objects',[]):
        if obj.get('kind')=='image' and obj.get('resource_id'):
            by_resource[obj['resource_id']].append(obj)
    bound=[]
    for raw in manifest or []:
        item=copy.deepcopy(raw);resource_id=originals.get(item.get('asset_name',''))
        candidates=by_resource.get(resource_id,deque())
        obj=candidates.popleft() if candidates else None
        item.update(resource_id=resource_id or '',source_id=obj['id'] if obj else '',
                    ready=bool(item.get('fetched') and resource_id and obj))
        if obj:
            obj['material_id']=item['id']
            obj['material_scope']=item.get('scope','')
            if item.get('scope')=='article' and item.get('figure'):
                obj['material_candidate']=True
                obj['source_scope']='article_media'
        bound.append(item)
    required=[item for item in bound if item.get('scope')=='article' and item.get('figure')]
    gaps=[dict(material_id=item['id'],selected_url=item.get('selected_url',''),
               failure=item.get('failure') or '图片没有绑定到正文对象')
          for item in required if not item['ready']]
    result['web_snapshot']=dict(result.get('web_snapshot') or {},material_manifest=bound,
        material_summary=dict(discovered=len(bound),article_figures=len(required),
                              ready_article_figures=len(required)-len(gaps),gaps=gaps))
    return result


def require_complete_web_materials(source):
    """Refuse semantic generation when a discovered article figure is absent."""
    snapshot=source.get('web_snapshot') or {}
    manifest=snapshot.get('material_manifest')
    if manifest is None:return
    required=[item for item in manifest if item.get('scope')=='article' and item.get('figure')]
    gaps=[item for item in required if not item.get('ready')]
    if gaps:
        labels=', '.join(item.get('id','unknown') for item in gaps[:12])
        raise ValueError('网页正文材料尚未完整取得，未开始改写：'+labels)
    object_ids={obj['id'] for obj in source.get('objects',[])}
    if any(not item.get('source_id') or item['source_id'] not in object_ids for item in required):
        raise ValueError('网页正文图片与材料位置未完整对应，未开始改写')
