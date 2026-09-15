"""Use the deployed skill's actual composer, checker and minimal committer."""

import copy
import html
import importlib.util
import json
import re
from pathlib import Path
import subprocess
import sys

from .store import Conflict, digest
from .production_contracts import ComposedDraft, FlatDraft, LocalRepair, BindingPatch


def canonical(draft):
    return '\n\n'.join(b['markdown'] for b in draft['blocks']) + '\n'


def available_draft(job):
    """Display received writing even when later processing failed; never publish it."""
    def display_order(draft):
        if not job.get('plan'):
            return draft
        from .pedagogy import arrange_document_info
        try:
            return arrange_document_info(draft,job['plan'])
        except ValueError:
            # Keep an invalid partial response visible for diagnosis; the
            # production audit still rejects its misplaced content.
            return draft
    if job.get('draft',{}).get('blocks'):
        return display_order(job['draft'])
    received=[job.get('results',{}).get('writer-'+u['id']) for u in job.get('plan',{}).get('units',[])]
    received=[r for r in received if r is not None]
    if not received:
        return None
    from .skills import load_bundle
    bundle=load_bundle(job['writing_skill']['root'],job['writing_skill']['package_digest'])
    blocks=[]
    for response in received:
        try:
            blocks.extend(compose(bundle,response,job['inventory']|{'_verified_terminology':job.get('verified_terminology',[])})['blocks'])
        except ValueError:
            # Keep a malformed response in the job, without breaking access to
            # any other already-renderable unit or asserting it is complete.
            continue
    return display_order({'blocks':blocks}) if blocks else None


def protected_objects(inventory):
    result={}
    for o in inventory['objects']:
        kind=o['kind']
        if kind in {'text','heading'}:
            continue
        if kind in {'image','page'} and o.get('resource_id'):
            from .media import image_markup
            text=image_markup(o)
        elif kind=='table' and o.get('raw','').lstrip().startswith('<table'):
            text=o['raw']
        elif kind=='code' and o.get('fence_raw'):
            text=o['fence_raw']
        elif kind=='link':
            label=(o['text'] or o.get('target','')).replace('[','\\[').replace(']','\\]')
            target=o.get('target','')
            original_target=o.get('original_target',target)
            raw_label=original_target if original_target else '（空字符串）'
            title=(' "原始链接目标：'+raw_label.replace('\\','\\\\').replace('"','\\"')+'"'
                   if original_target!=target else '')
            text='['+label+']('+target+title+')'
        elif kind=='metadata' and o.get('inert_markup') in {'script','style'}:
            raw=o['raw'];fence='`'*max(3,1+max((len(m.group()) for m in re.finditer(r'`+',raw)),default=0))
            text=fence+'html\n'+raw+'\n'+fence
        elif kind=='metadata' and o.get('raw','').startswith('<!--'):
            text=o['raw']
        else:
            text=o['text']
        result[o['id']]=text
    return result


def normalize_definition_encoding(response):
    """Unpack an explicitly delimited 3–5-part definition without changing words."""
    result=copy.deepcopy(response)
    for block in result.get('blocks',[]):
        for node in block.get('content',[]):
            parts=node.get('definition')
            if node.get('type')!='term' or not isinstance(parts,list) or len(parts)!=1 or not isinstance(parts[0],str):continue
            sentences=parts[0].split('；')
            if 3<=len(sentences)<=5 and all(s.strip() for s in sentences):
                assert '；'.join(sentences)==parts[0]
                node['definition']=sentences
    return result


def attach_original_pages(response, inventory, unit_id, prior_draft):
    """Place retained page images once after their assigned facts have prose bindings."""
    result=copy.deepcopy(response)
    literals=protected_objects(inventory)
    from .media import source_literals
    objects={o['id']:o for o in inventory['objects']}
    covered={fid for b in result['blocks'] for fid in b.get('obligation_ids',[])}
    def placed(nodes):
        return {n['id'] for n in nodes if n['type']=='source'}
    present=set().union(*(placed(b['content']) for b in result['blocks']))
    earlier={sid for sid,text in literals.items() if any(sid in b.get('embedded_object_ids',[]) and
        sid in b['object_ids'] and any(v in b['markdown'] for v in source_literals(objects[sid],text)) for b in prior_draft.get('blocks',[]))}
    for obj in inventory['objects']:
        sid=obj['id']
        if obj['kind']!='page' or not obj.get('resource_id') or sid in present|earlier:continue
        facts={o['id'] for o in inventory['obligations'] if o['object_id']==sid}
        if not facts or not facts<=covered:continue
        bid=unit_id+'-original-page-'+sid
        if any(b['id']==bid for b in result['blocks']):raise ValueError('原页附件段落身份重复')
        result['blocks'].append(dict(id=bid,unit_id=unit_id,kind='object',obligation_ids=[],
            object_ids=[sid],evidence=[{'source_id':sid,'quote':obj['text']}],content=[
                dict(type='source',id=sid,node_id=bid+'-image',parent_id='',layout='centered_image')]))
    return result


def invalid_inline_references(response,inventory):
    links={o['id'] for o in inventory['objects'] if o['kind']=='link'}
    return [dict(block_id=b['id'],node_id=n['node_id'],old_text=n['text'])
        for b in response['blocks'] for n in b['content']
        if n['type'] in {'paragraph','list_item'} and any(
            sid not in links for sid in re.findall(r'\{\{source:([^{}]+)\}\}',n['text']))]


def apply_reference_patch(response,patch,inventory):
    from .production_contracts import ReferenceTextPatch
    edits=ReferenceTextPatch.model_validate(patch).model_dump()['edits']
    targets={(x['block_id'],x['node_id']):x['old_text'] for x in invalid_inline_references(response,inventory)}
    keys=[(x['block_id'],x['node_id']) for x in edits]
    if len(keys)!=len(set(keys)) or set(keys)!=set(targets):raise ValueError('引用修复必须准确覆盖已定位的错误节点')
    result=copy.deepcopy(response)
    nodes={(b['id'],n['node_id']):n for b in result['blocks'] for n in b['content']}
    for edit in edits:
        key=(edit['block_id'],edit['node_id'])
        if edit['old_text']!=targets[key] or not edit['new_text'].strip():raise ValueError('引用修复与旧句不一致或清空了内容')
        # Keep the exact surrounding words; only the placeholder's own local
        # reference span may change. Preserve all other text byte for byte.
        spans=re.split(r'\{\{source:[^{}]+\}\}',edit['old_text'])
        removed=edit['old_text']
        links={o['id'] for o in inventory['objects'] if o['kind']=='link'}
        for match in re.finditer(r'\{\{source:([^{}]+)\}\}',edit['old_text']):
            if match[1] in links:continue
            token=match[0]
            if '（'+token+'）' in removed:removed=removed.replace('（'+token+'）','')
            elif '('+token+')' in removed:removed=removed.replace('('+token+')','')
            else:removed=removed.replace(token,'')
        if edit['new_text']!=removed and not re.fullmatch(r'.+?'.join(re.escape(span) for span in spans),edit['new_text'],re.S):
            raise ValueError('引用修复改变了占位符之外的原句')
        nodes[key]['text']=edit['new_text']
    if invalid_inline_references(result,inventory):raise ValueError('引用修复仍包含无效原对象引用')
    return result


def expand_response(response, heading_depths=None):
    """Resolve parent IDs once before initial rendering; preserve every text field."""
    if response.get('encoding')!='flat_nodes_v1':
        return response
    raw=FlatDraft.model_validate(normalize_definition_encoding(response)).model_dump(exclude_none=True)
    blocks=[]
    active_sections={}
    active_members={}
    section_paths={}
    active_paths={}
    for block in raw['blocks']:
        nodes=[]
        for node in block['content']:
            if node['type']=='paragraph' and any(c in node['text'] for c in '\r\n'):
                lines=[line for line in node['text'].splitlines() if line.strip()]
                for index,line in enumerate(lines):
                    nodes.append(node|dict(text=line,node_id=node['node_id'] if index==0 else node['node_id']+'-line-'+str(index)))
            else:
                nodes.append(node)
        normalized=[]
        for node in nodes:
            marker=re.match(r'^(?:([-+*])|(1)[.)])\s+(.+)$',node.get('text','')) if node['type']=='paragraph' else None
            if marker:
                normalized.append(dict(type='list',node_id=node['node_id'],parent_id=node['parent_id'],ordered=bool(marker[2])))
                normalized.append(dict(type='list_item',node_id=node['node_id']+'-item',parent_id=node['node_id'],text=marker[3]))
            else:
                normalized.append(node)
        nodes=normalized
        local_ids={n['node_id'] for n in nodes}
        orphaned_lists={n['parent_id'] for n in nodes if n['type']=='list_item' and
                        n['parent_id'].endswith('-list') and n['parent_id'] not in local_ids}
        # The model can omit an otherwise referenced list wrapper when returning
        # a continuation block. Restore only that structural container. The item
        # text stays byte-for-byte intact and later content review still applies.
        nodes=[{'type':'list','node_id':key,'parent_id':'','ordered':False} for key in sorted(orphaned_lists)]+nodes
        declared_parents={n['parent_id'] for n in nodes}
        empty_heading=None
        for index,node in enumerate(nodes):
            if node['parent_id']=='':
                if node['type']=='section':
                    empty_heading=node['node_id'] if node['node_id'] not in declared_parents else None
                elif empty_heading:
                    # Root siblings after a bare heading are its content in
                    # document order; bind them without changing any words.
                    nodes[index]=node|{'parent_id':empty_heading}
        local_ids={n['node_id'] for n in nodes}
        external={n['parent_id'] for n in nodes if n['parent_id'] and n['parent_id'] not in local_ids}
        original_parents={n['node_id']:n['parent_id'] for n in nodes}
        # The active preceding heading is already above this block in canonical
        # Markdown. A newer heading supersedes it, preventing stale references.
        ancestors=set(active_paths.get(block['unit_id'],[]))
        if external and external<=active_members.get(block['unit_id'],set())|ancestors:
            depths={len(section_paths[(block['unit_id'],sid)]) for sid in external if sid in ancestors}
            if len(depths)>1:
                raise ValueError('跨段落节点引用不同层级，需明确各段的结构')
            if heading_depths is not None and depths:
                heading_depths[block['id']]=next(iter(depths))
            nodes=[n|{'parent_id':''} if n['parent_id'] in external else n for n in nodes]
        # The skill's term renderer itself emits one list item. Empty unordered
        # wrappers around terms must not create a second bullet or lose a term.
        remove=set();promote={}
        for n in nodes:
            if n['type']!='list' or n.get('ordered'):continue
            items=[x for x in nodes if x['parent_id']==n['node_id']]
            if not items:continue
            terms=[]
            for item in items:
                children=[x for x in nodes if x['parent_id']==item['node_id']]
                if item['type']!='list_item' or item.get('text') or len(children)!=1 or children[0]['type']!='term':break
                if any(x['parent_id']==children[0]['node_id'] for x in nodes):break
                terms.append(children[0])
            term_ids=[x['node_id'] for x in terms]
            if len(terms)==len(items) and [x['node_id'] for x in nodes if x['node_id'] in term_ids]==term_ids:
                remove.update([n['node_id'],*(x['node_id'] for x in items)])
                promote.update({x['node_id']:n['parent_id'] for x in terms})
        nodes=[n|{'parent_id':promote[n['node_id']]} if n['node_id'] in promote else n for n in nodes if n['node_id'] not in remove]
        while True:
            parents={n['parent_id'] for n in nodes}
            empty={n['node_id'] for n in nodes if n['node_id'] not in parents and
                   (n['type']=='list' or n['type']=='list_item' and n['text']=='')}
            if not empty:break
            nodes=[n for n in nodes if n['node_id'] not in empty]
        by_id={n['node_id']:n for n in nodes}
        if len(by_id)!=len(nodes) or '' in by_id:
            raise ValueError('排版节点身份为空或重复')
        if any(n['parent_id'] and n['parent_id'] not in by_id for n in nodes):
            raise ValueError('排版节点引用不存在的父节点')
        children={key:[] for key in ['',*by_id]}
        for n in nodes:
            children[n['parent_id']].append(n)
        visited=set()
        rendered_sections=[]
        def build(node):
            key=node['node_id']
            if key in visited:
                raise ValueError('排版节点存在循环引用')
            visited.add(key)
            out={k:v for k,v in node.items() if k not in {'node_id','parent_id'}}
            descendants=children[key]
            if out['type']=='section':
                rendered_sections.append(key)
                if any(n['type']=='list_item' for n in descendants):
                    raise ValueError('列表项必须归属于列表')
                out['blocks']=[build(n) for n in descendants]
            elif out['type']=='list':
                if any(n['type']!='list_item' for n in descendants):
                    raise ValueError('列表只能直接包含列表项')
                out['items']=[build(n) for n in descendants]
            elif out['type']=='list_item':
                out.pop('type')
                nested_lists=[n for n in descendants if n['type']=='list']
                source_nodes=[n for n in descendants if n['type']=='source']
                if len(nested_lists)>1 or len(nested_lists)+len(source_nodes)!=len(descendants):
                    raise ValueError('列表项下级只能包含一个嵌套列表与原对象')
                if nested_lists and source_nodes:
                    raise ValueError('原对象与嵌套列表混排的顺序尚未支持')
                if nested_lists:
                    nested=build(nested_lists[0])
                    out['children']=nested['items']
                    out['children_ordered']=nested['ordered']
                if source_nodes:
                    out['source_children']=[build(n) for n in source_nodes]
            elif descendants:
                raise ValueError('正文叶节点不能包含下级排版节点')
            return out
        if any(n['type']=='list_item' for n in children['']):
            raise ValueError('根节点不能是无归属的列表项')
        content=[build(n) for n in children['']]
        if visited!=set(by_id):
            raise ValueError('有排版节点无法从根节点到达')
        blocks.append({k:v for k,v in block.items() if k!='content'}|dict(content=content))
        if rendered_sections:
            for section_id in rendered_sections:
                parent=original_parents[section_id]
                path=section_paths.get((block['unit_id'],parent),[])
                section_paths[(block['unit_id'],section_id)]=path+[section_id]
            section=rendered_sections[-1]
            active_paths[block['unit_id']]=section_paths[(block['unit_id'],section)]
            active_sections[block['unit_id']]=section
            members={section};pending=[section]
            while pending:
                for child in children[pending.pop()]:
                    members.add(child['node_id']);pending.append(child['node_id'])
            active_members[block['unit_id']]=members
        elif block['unit_id'] in active_sections and block['kind']!='document_info':
            # Continuation objects sometimes name the preceding leaf rather
            # than its heading. They remain siblings under that active heading;
            # a superseded heading or another unit never qualifies.
            active_members[block['unit_id']].update(by_id)
    return {'blocks':blocks}


def validate_layout_repair(original, candidate):
    """Location-only repair cannot become an unreviewed prose rewrite."""
    old=FlatDraft.model_validate(normalize_definition_encoding(original)).model_dump(exclude_none=True)
    new=FlatDraft.model_validate(normalize_definition_encoding(candidate)).model_dump(exclude_none=True)
    def signature(block):
        nodes=[{k:v for k,v in n.items() if k not in {'node_id','parent_id'}} for n in block['content']]
        return {k:v for k,v in block.items() if k!='content'},nodes
    if len(old['blocks'])!=len(new['blocks']):raise ValueError('结构修复不能增删正文块')
    for before,after in zip(old['blocks'],new['blocks']):
        metadata,nodes=signature(before);updated,changed=signature(after)
        if metadata!=updated:raise ValueError('结构修复不能改变正文身份与来源')
        i=0
        for node in changed:
            if i<len(nodes) and node==nodes[i]:i+=1
            elif node.get('type')!='list' and node!={'type':'list_item','text':''}:
                raise ValueError('结构修复不能重写、移动或新增内容')
        if i!=len(nodes):raise ValueError('结构修复丢失原有内容')
    return candidate


def validate_binding_repair(original, candidate, inventory=None):
    old=FlatDraft.model_validate(original).model_dump(exclude_none=True)
    candidate=copy.deepcopy(candidate)
    texts={o['id']:o['text'] for o in (inventory or {}).get('objects',[])}
    for block in candidate.get('blocks',[]):
        for evidence in block.get('evidence',[]):
            sid=evidence.get('quote_source_id')
            if sid is not None:
                if sid!=evidence.get('source_id') or sid not in texts or ('quote' in evidence and evidence['quote']!=texts[sid]):
                    raise ValueError('来源引用别名与原文不一致')
                evidence['quote']=texts[sid]
                evidence.pop('quote_source_id')
    new=FlatDraft.model_validate(candidate).model_dump(exclude_none=True)
    fields={'obligation_ids','object_ids','evidence'}
    unchanged=lambda body:[{k:v for k,v in b.items() if k not in fields} for b in body['blocks']]
    if unchanged(old)!=unchanged(new):
        raise ValueError('来源关系修复不能改写正文、对象或段落身份')
    return candidate


def append_unit_completion(original, supplement, unit_id):
    before=FlatDraft.model_validate(normalize_definition_encoding(original)).model_dump(exclude_none=True)
    extra=FlatDraft.model_validate(normalize_definition_encoding(supplement)).model_dump(exclude_none=True)
    ids={b['id'] for b in before['blocks']}
    for block in extra['blocks']:
        if block['unit_id']!=unit_id or block['id'] in ids:
            raise ValueError('补充内容不能覆盖旧段落或改写其他教学单元')
        ids.add(block['id'])
    return before|{'blocks':before['blocks']+extra['blocks']}


def apply_binding_patch(original,patch,inventory):
    patch=BindingPatch.model_validate(patch).model_dump()
    result=copy.deepcopy(original)
    blocks={b['id']:b for b in result['blocks']}
    sources={o['id']:o['text'] for o in inventory['objects']}
    facts={f['id'] for f in inventory['obligations']}
    seen=set()
    for assignment in patch['assignments']:
        bid=assignment['block_id']
        if bid not in blocks or bid in seen:raise ValueError('来源补丁段落不存在或重复')
        seen.add(bid)
        if not set(assignment['source_ids'])<=sources.keys() or not set(assignment['obligation_ids'])<=facts:
            raise ValueError('来源补丁引用未知原对象或事实')
        blocks[bid]['obligation_ids']=list(dict.fromkeys(assignment['obligation_ids']))
        blocks[bid]['evidence']=[{'source_id':sid,'quote':sources[sid]} for sid in dict.fromkeys(assignment['source_ids'])]
    return result


def compose(bundle, response, inventory):
    heading_depths={}
    body=ComposedDraft.model_validate(expand_response(response,heading_depths)).model_dump(exclude_none=True)
    module_path=Path(bundle['root'])/'runtime/composition.py'
    spec=importlib.util.spec_from_file_location('sourceloom_frozen_composer',module_path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sources=protected_objects(inventory)
    protected_ids=set(sources)
    original_text={o['id']:o['text'] for o in inventory['objects']}
    original_raw={o['id']:o.get('raw','') for o in inventory['objects']}
    fact_sources={f['id']:f['object_id'] for f in inventory.get('obligations',[])}
    source_text='\n'.join(o['text'] for o in inventory['objects'])
    normalized_source=re.sub(r'\s+',' ',source_text).casefold()
    verified_names={item['en'].casefold():item['en'] for item in inventory.get('_verified_terminology',[])
                    if item.get('quote') and item.get('url') and item['en'].casefold() in item['quote'].casefold()}
    generic_names={'jargon','anonymous function','named function','nested function','recursive function',
                   'function declaration','function expression','arrow function','first-class function',
                   'immediately invoked function expression','code snippet','return value','argument',
                   'variable','function','object','parameter','call stack'}
    lowered_sources=set()
    inline_link_ids={o['id'] for o in inventory['objects'] if o['kind']=='link'}
    def inline_links(text):
        def insert(match):
            sid=match[1]
            if sid not in inline_link_ids or '\n' in sources[sid] or '\r' in sources[sid]:
                raise ValueError('行内原件引用必须指向现有的单行链接')
            lowered_sources.add(sid)
            return sources[sid]
        return re.sub(r'\{\{source:([^{}]+)\}\}',insert,text)
    def format_terms(nodes):
        for node in nodes:
            if node['type']=='paragraph':
                node['text']=inline_links(node['text'])
            if node['type']=='section':
                format_terms(node['blocks'])
            if node['type']=='list':
                def optional_children(items):
                    for item in items:
                        item['text']=inline_links(item['text'])
                        literals=item.pop('source_children',[])
                        if literals:
                            children=[]
                            for source_node in literals:
                                sid=source_node['id']
                                if sid not in sources:
                                    raise ValueError('列表中的原对象身份不存在')
                                literal=module.render_document({'blocks':[source_node]},sources={sid:sources[sid]}).rstrip('\n')
                                if '\n' in literal or '\r' in literal:
                                    raise ValueError('列表中的多行原对象尚不能无损排版')
                                children.append({'text':literal})
                                lowered_sources.add(sid)
                            if not item['text'] and len(children)==1:
                                item['text']=children[0]['text']
                            else:
                                item['children']=children
                        if not item.get('children'):
                            item.pop('children',None)
                            item.pop('children_ordered',None)
                        else:
                            optional_children(item['children'])
                optional_children(node['items'])
            if node['type']!='term':
                continue
            # English labels require evidence; omission when unconfirmed is an explicit
            # skill rule. This happens once before the first canonical draft is formed.
            en=re.sub(r'\s+',' ',node['en']).strip()
            abbr=node.get('abbr','')
            # The wire schema represents an absent abbreviation as an empty string;
            # the skill composer represents it by omitting the optional field.
            if not abbr:
                node.pop('abbr',None)
            if abbr:
                en=re.sub(r'\s*[,，;；（(]\s*'+re.escape(abbr)+r'\s*[)）]?\s*$','',en)
            if not en or (en.casefold() not in normalized_source and en.casefold() not in verified_names):
                text=(abbr+' ' if abbr else '')+node['zh']+'：'+'；'.join(node['definition'])
                node.clear()
                node.update(type='list',items=[{'text':text}])
            else:
                node['en']=verified_names.get(en.casefold(),en.title() if en.casefold() in generic_names else en)
    blocks=[]
    def placed_sources(nodes):
        for node in nodes:
            if node['type']=='source':
                yield node['id']
            elif node['type']=='section':
                yield from placed_sources(node['blocks'])
    def quoted_text(nodes):
        for node in nodes:
            if node['type']=='source' and node['id'] in original_text and node['id'] not in protected_ids:
                # Exact source text may be explicitly quoted. This does not
                # turn authored prose into an exempt protected source.
                node['presentation']='quote'
                for field in ('language','layout','caption'):node.pop(field,None)
                sources.setdefault(node['id'],original_text[node['id']])
            elif node['type']=='section':
                quoted_text(node['blocks'])
    fenced_codes={o['id'] for o in inventory['objects'] if o['kind']=='code' and o.get('fence_raw')}
    def restore_fence_syntax(nodes):
        for node in nodes:
            if node['type']=='source' and node.get('presentation','raw')!='code':
                # A wire-format language hint has no meaning for raw/quoted
                # objects. It must not invalidate or modify their literal bytes.
                node.pop('language',None)
            if node['type']=='source' and node['id'] in fenced_codes:
                node['presentation']='raw'
                for field in ('language','layout','caption'):node.pop(field,None)
            elif node['type']=='section':
                restore_fence_syntax(node['blocks'])
    for b in body['blocks']:
        lowered_sources.clear()
        quoted_text(b['content'])
        restore_fence_syntax(b['content'])
        format_terms(b['content'])
        placed=list(placed_sources(b['content']))
        selected={sid:sources[sid] for sid in placed if sid in sources}
        if set(selected)!=set(placed):
            raise ValueError('普通正文不能冒充逐字原对象，或对象身份不存在')
        depth=heading_depths.get(b['id'],0)
        if depth>4:raise ValueError('跨段落标题超过可用层级')
        if len(b['content'])==1 and b['content'][0]['type']=='section' and not b['content'][0]['blocks']:
            # The full skill validates heading text, while this adapter preserves
            # a heading-only source anchor whose body is in subsequent blocks.
            # Do not invent child prose or hide a block marker in a paragraph.
            heading=module._text(b['content'][0]['heading'],'section.heading')
            text='#'*(2+depth)+' '+heading+'\n'
        else:
            content=b['content']
            for _ in range(depth):
                content=[{'type':'section','heading':'SourceLoom internal context','blocks':content}]
            text=module.render_document({'blocks':content},sources=selected)
            if depth:
                prefix='\n\n'.join('#'*(2+i)+' SourceLoom internal context' for i in range(depth))+'\n\n'
                if not text.startswith(prefix):raise ValueError('写作技能的标题上下文格式已变化')
                text=text[len(prefix):]
        # canonical() already separates blocks. Remove only the renderer's final
        # newline when no protected literal would lose bytes. Historical drafts
        # and their digests remain untouched.
        if text.endswith('\n') and all(literal in text[:-1] for literal in selected.values()):
            text=text[:-1]
        # Placement is established by actual source nodes, never by a second model
        # list that can disagree with its own authored layout. Raw result is retained.
        embedded=list(dict.fromkeys([*selected,*sorted(lowered_sources)]))
        # Exact embedded objects already have a physical source location. Bind
        # their ledger entries here, rather than asking a writer to duplicate
        # code/links just because it forgot an ID. Semantic review remains due.
        obligations=list(dict.fromkeys([*b['obligation_ids'],
            *(fid for fid,sid in fact_sources.items() if sid in protected_ids and sid in embedded
              and sources[sid] in text)]))
        evidence=list(b['evidence'])
        evidence=[e|dict(quote=original_text[e['source_id']])
                  if e['source_id'] in original_text and e['quote'] and
                     e['quote'] in (sources.get(e['source_id']),original_raw.get(e['source_id'])) else e
                  for e in evidence]
        for fid in obligations:
            sid=fact_sources.get(fid)
            if sid in original_text and sid not in {e['source_id'] for e in evidence}:
                evidence.append(dict(source_id=sid,quote=original_text[sid]))
        # A rewritten heading is related to an original heading, not a literal
        # embedded copy. Keep that explicit relationship only at a real heading.
        heading_ids={o['id'] for o in inventory['objects'] if o['kind']=='heading'}
        headings=[sid for sid in [*b['object_ids'],*(e['source_id'] for e in evidence)] if sid in heading_ids] if any(n['type']=='section' for n in b['content']) else []
        blocks.append({k:v for k,v in b.items() if k!='content'}|dict(markdown=text,obligation_ids=obligations,object_ids=list(dict.fromkeys(embedded+headings)),embedded_object_ids=embedded,evidence=evidence))
    return {'blocks':blocks}


def scan(bundle, draft, work):
    work=Path(work)
    work.mkdir(parents=True,exist_ok=True)
    text=canonical(draft)
    key=digest(text.encode())
    source=work/(key+'.md')
    report=work/(key+'.review.json')
    source.write_bytes(text.encode())
    run=subprocess.run([sys.executable,'-X','utf8',str(Path(bundle['root'])/'scripts/review_writing.py'),
                        '--input',str(source),'--report',str(report)],capture_output=True,timeout=30)
    if run.returncode or not report.is_file():
        raise ValueError('完整技能的实际检查器执行失败，未标为通过')
    result=json.loads(report.read_text(encoding='utf-8'))
    result['canonical_digest']=key
    for category in ('findings','candidates'):
        for n,item in enumerate(result['format'][category]):
            item['id']=category+'-'+str(n+1)
    return result


def repair(bundle, draft, proposal, allowed, work):
    """Validate block scope, then invoke the skill's own protection-aware committer."""
    proposal=LocalRepair.model_validate(proposal).model_dump()
    original=canonical(draft)
    if proposal['document_digest']!=digest(original.encode()):
        raise Conflict('局部补丁正文摘要过期')
    blocks={b['id']:b for b in draft['blocks']}
    starts={}
    cursor=0
    for b in draft['blocks']:
        starts[b['id']]=cursor
        cursor+=len(b['markdown'])+2
    edits=[]
    ranges=[]
    for e in proposal['edits']:
        if e['old_text']==e['new_text']:
            raise Conflict('补丁没有实际修改，整笔未提交')
        if e['block_id'] not in allowed or e['block_id'] not in blocks:
            raise Conflict('修复超出已定位问题的段落')
        before=blocks[e['block_id']]['markdown']
        if '\n' in e['old_text'] or before.count(e['old_text'])!=1:
            raise Conflict('每项补丁必须精确命中一行中的唯一局部文本')
        offset=starts[e['block_id']]+before.index(e['old_text'])
        span=(offset,offset+len(e['old_text']))
        if any(span[0]<end and start<span[1] for start,end in ranges):
            raise Conflict('局部补丁相互重叠，整笔未提交')
        ranges.append(span)
        edits.append(dict(node_id=f'LINE-{original[:offset].count(chr(10))+1:04d}',
                          old_text=e['old_text'],new_text=e['new_text'],scope='sentence',reason=e['reason']))
    work=Path(work)
    work.mkdir(parents=True,exist_ok=True)
    source=work/'before.md';transaction=work/'patch.json';output=work/'after.md';report=work/'report.json'
    source.write_bytes(original.encode())
    transaction.write_text(json.dumps({'document_sha256':proposal['document_digest'],'edits':edits},ensure_ascii=False),encoding='utf-8')
    run=subprocess.run([sys.executable,'-X','utf8',str(Path(bundle['root'])/'scripts/review_writing.py'),
                        '--input',str(source),'--edits',str(transaction),'--output',str(output),
                        '--report',str(report)],capture_output=True,timeout=30)
    if run.returncode or not output.is_file():
        raise Conflict('技能的精确提交器拒绝补丁，原稿保持完整')
    expected=copy.deepcopy(draft)
    for b in expected['blocks']:
        local=[e for e in proposal['edits'] if e['block_id']==b['id']]
        for e in sorted(local,key=lambda e:b['markdown'].index(e['old_text']),reverse=True):
            b['markdown']=b['markdown'].replace(e['old_text'],e['new_text'],1)
    if canonical(expected).encode()!=output.read_bytes():
        raise Conflict('精确提交结果与指定局部修改不一致，未写回')
    return expected
