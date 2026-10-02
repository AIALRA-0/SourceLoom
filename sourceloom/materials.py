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
    if isinstance(manifest,dict) and isinstance(manifest.get('source_continuity'),dict):
        result['web_snapshot']['source_continuity']=copy.deepcopy(manifest['source_continuity'])
    by_capture={obj.get('capture_id'):obj for obj in result.get('objects',[]) if obj.get('capture_id')}
    rendered=[]
    link_target_discrepancies=[]
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
        if obj and obj.get('kind')=='link':
            # Keep the browser-resolved destination beside the source href and
            # parser-resolved target. Hydration may rewrite links after the
            # initial response; binding by capture ID preserves that fact
            # without silently replacing either source representation.
            rendered_target=item.get('target','')
            parser_target=obj.get('target','')
            obj['rendered_target']=rendered_target
            try:
                from .active_resources import canonical_url
                same_destination=(canonical_url(parser_target)==
                                 canonical_url(rendered_target))
            except (AttributeError,TypeError,ValueError):
                same_destination=parser_target==rendered_target
            item['target_comparison']='canonical_match' if same_destination else 'canonical_mismatch'
            if same_destination:
                obj.pop('rendered_target_discrepancy',None)
            else:
                discrepancy=dict(source_id=obj['id'],capture_id=item.get('id',''),
                    original_target=obj.get('original_target',parser_target),
                    parser_target=parser_target,rendered_target=rendered_target,
                    reason='rendered_link_destination_differs_from_parser_target')
                obj['rendered_target_discrepancy']=discrepancy.copy()
                item['target_discrepancy']=discrepancy.copy()
                link_target_discrepancies.append(discrepancy)
        rendered.append(item)
    rendered_gaps=[dict(material_id=item['id'],kind=item.get('kind','unknown'),
                        failure='渲染对象没有绑定到原件清单') for item in rendered
                   if item.get('scope')=='article' and not item['ready']]
    result['web_snapshot'].update(rendered_capture=dict(
        attempted=isinstance(manifest,dict) and manifest.get('rendered') is not None,
        completed=bool(manifest.get('rendered')) if isinstance(manifest,dict) else False,
        objects=rendered,gaps=rendered_gaps))
    result['web_snapshot']['link_target_discrepancies']=link_target_discrepancies
    repair_nested_rendered_bindings(result)
    return result


def repair_nested_rendered_bindings(source):
    """Bind separately reported SVG/canvas descendants to saved parent pixels."""
    rendered=(source.get('web_snapshot') or {}).get('rendered_capture')
    if not isinstance(rendered,dict):return 0
    objects=rendered.get('objects') or []
    captured=[item for item in objects if item.get('ready') and item.get('resource_id')
              and item.get('kind') in {'image','canvas','video','media'}]
    repaired=0
    for item in objects:
        if item.get('ready') or item.get('scope')!='article':continue
        try:
            left,top=float(item['x']),float(item['y'])
            right,bottom=left+float(item['width']),top+float(item['height'])
        except (KeyError,TypeError,ValueError):continue
        def contains(candidate):
            try:
                x,y=float(candidate['x']),float(candidate['y'])
                return (x<=left and y<=top and x+float(candidate['width'])>=right
                        and y+float(candidate['height'])>=bottom)
            except (KeyError,TypeError,ValueError):return False
        candidates={candidate.get('id'):candidate for candidate in captured
                    if candidate.get('scope')=='article' and contains(candidate)}
        ancestry=item.get('parent_capture_ids')
        if isinstance(ancestry,list):
            # New captures carry true DOM ancestry in nearest-first order.
            # Geometry is still checked so stale or malformed ancestry cannot
            # assign a descendant to pixels that do not cover it.
            parent=next((candidates[capture_id] for capture_id in ancestry
                         if capture_id in candidates),None)
            if parent is None and candidates:
                item['binding_failure']='declared_ancestors_do_not_contain_descendant'
                item['candidate_parent_captures']=list(candidates)
        else:
            # Older manifests have only rectangles. Keep the established
            # single-parent behavior; overlapping candidates are ambiguous and
            # must remain visible as a gap instead of silently swapping IDs.
            parent=next(iter(candidates.values())) if len(candidates)==1 else None
            if len(candidates)>1:
                item['binding_failure']='ambiguous_parent_capture_without_dom_ancestry'
                item['candidate_parent_captures']=list(candidates)
        if parent:
            item.update(source_id=parent['source_id'],resource_id=parent['resource_id'],
                        ready=True,covered_by_capture=parent['id'])
            item.pop('binding_failure',None)
            item.pop('candidate_parent_captures',None)
            repaired+=1
    rendered['gaps']=[dict(material_id=item['id'],kind=item.get('kind','unknown'),
                            failure=item.get('binding_failure') or '渲染对象没有绑定到原件清单',
                            **({'candidate_parent_captures':item['candidate_parent_captures']}
                               if item.get('candidate_parent_captures') else {})
                            ) for item in objects
                       if item.get('scope')=='article' and not item.get('ready')]
    return repaired


def require_complete_web_materials(source):
    """Refuse semantic generation when a discovered article figure is absent."""
    repair_nested_rendered_bindings(source)
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
