"""Bind a web acquisition manifest to parsed source objects before generation."""

import copy
from collections import defaultdict, deque


def bind_web_material_manifest(source, manifest):
    """Attach every discovered image occurrence to its exact stored object."""
    result=copy.deepcopy(source)
    image_manifest=manifest.get('images',[]) if isinstance(manifest,dict) else (manifest or [])
    rendered_manifest=manifest.get('rendered_objects',[]) if isinstance(manifest,dict) else []
    originals={item['name']:item['sha256'] for item in result.get('originals',[])}
    by_resource=defaultdict(deque)
    for obj in result.get('objects',[]):
        if obj.get('kind')=='image' and obj.get('resource_id'):
            by_resource[obj['resource_id']].append(obj)
    bound=[]
    for raw in image_manifest:
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
    by_capture={obj.get('capture_id'):obj for obj in result.get('objects',[]) if obj.get('capture_id')}
    rendered=[]
    for raw in rendered_manifest:
        item=copy.deepcopy(raw);obj=by_capture.get(item.get('id'))
        # A nested SVG/canvas can be captured by the browser even when the
        # structural parser has already consumed its outer visual container.
        # Keep the independently archived screenshot as its own source object;
        # the visual model will decide its meaning later.  This is preferable
        # to dropping a real rendered object or blocking the entire document.
        preview_id=originals.get(item.get('preview_name',''))
        if not obj and preview_id:
            source_id='rendered-'+str(item.get('id') or len(rendered)+1)
            known_ids={entry['id'] for entry in result.get('objects',[])}
            if source_id in known_ids:
                suffix=2
                while f'{source_id}-{suffix}' in known_ids:suffix+=1
                source_id=f'{source_id}-{suffix}'
            kind=item.get('kind','image')
            obj=dict(id=source_id,kind='image' if kind=='image' else 'media',
                text=item.get('alt') or item.get('text') or '',
                locator='rendered-capture/'+str(item.get('id') or len(rendered)+1),
                raw='',target=item.get('target') or item.get('poster') or '',
                resource_id=preview_id,capture_id=item.get('id',''),
                material_candidate=item.get('scope')=='article',
                source_scope='article_media' if item.get('scope')=='article' else 'rendered_page',
                rendered_box={key:item.get(key) for key in ('x','y','width','height')})
            if kind!='image':obj['media_type']=kind
            result.setdefault('objects',[]).append(obj)
            by_capture[item.get('id')]=obj
        item.update(source_id=obj['id'] if obj else '',ready=bool(obj),
                    resource_id=obj.get('resource_id','') if obj else '')
        rendered.append(item)
    rendered_gaps=[dict(material_id=item['id'],kind=item.get('kind','unknown'),
                        failure='渲染对象没有绑定到原件清单') for item in rendered
                   if item.get('scope')=='article' and not item['ready']]
    result['web_snapshot'].update(rendered_capture=dict(
        attempted=isinstance(manifest,dict) and manifest.get('rendered') is not None,
        completed=bool(manifest.get('rendered')) if isinstance(manifest,dict) else False,
        objects=rendered,gaps=rendered_gaps))
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
    rendered=snapshot.get('rendered_capture')
    if rendered and rendered.get('attempted'):
        if not rendered.get('completed'):
            raise ValueError('动态网页尚未完成浏览器渲染与非文字材料盘点，未开始改写')
        if rendered.get('gaps'):
            labels=', '.join(item['material_id'] for item in rendered['gaps'][:12])
            raise ValueError('动态网页材料尚未完整绑定，未开始改写：'+labels)
