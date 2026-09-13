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
from .production_contracts import ComposedDraft, FlatDraft, LocalRepair


def canonical(draft):
    return '\n\n'.join(b['markdown'] for b in draft['blocks']) + '\n'


def available_draft(job):
    """Display received writing even when later processing failed; never publish it."""
    if job.get('draft',{}).get('blocks'):
        return job['draft']
    received=[job.get('results',{}).get('writer-'+u['id']) for u in job.get('plan',{}).get('units',[])]
    received=[r for r in received if r is not None]
    if not received:
        return None
    from .skills import load_bundle
    bundle=load_bundle(job['writing_skill']['root'],job['writing_skill']['package_digest'])
    blocks=[]
    for response in received:
        blocks.extend(compose(bundle,response,job['inventory'])['blocks'])
    return {'blocks':blocks} if blocks else None


def protected_objects(inventory):
    result={}
    for o in inventory['objects']:
        kind=o['kind']
        if kind in {'text','heading'}:
            continue
        if kind in {'image','page'} and o.get('resource_id'):
            text='<img src="assets/'+o['resource_id']+'" alt="'+html.escape(o['text'] or o['locator'],quote=True)+'">'
        elif kind=='table' and o.get('raw','').lstrip().startswith('<table'):
            text=o['raw']
        elif kind=='link':
            text='['+(o['text'] or o.get('target','')).replace('[','\\[').replace(']','\\]')+']('+o.get('target','')+')'
        elif kind=='metadata' and o.get('raw','').startswith('<!--'):
            text=o['raw']
        else:
            text=o['text']
        result[o['id']]=text
    return result


def expand_response(response):
    """Resolve parent IDs once before initial rendering; preserve every text field."""
    if response.get('encoding')!='flat_nodes_v1':
        return response
    raw=FlatDraft.model_validate(response).model_dump(exclude_none=True)
    blocks=[]
    for block in raw['blocks']:
        nodes=[]
        for node in block['content']:
            if node['type']=='paragraph' and any(c in node['text'] for c in '\r\n'):
                lines=[line for line in node['text'].splitlines() if line.strip()]
                for index,line in enumerate(lines):
                    nodes.append(node|dict(text=line,node_id=node['node_id'] if index==0 else node['node_id']+'-line-'+str(index)))
            else:
                nodes.append(node)
        by_id={n['node_id']:n for n in nodes}
        if len(by_id)!=len(nodes) or '' in by_id:
            raise ValueError('排版节点身份为空或重复')
        if any(n['parent_id'] and n['parent_id'] not in by_id for n in nodes):
            raise ValueError('排版节点引用不存在的父节点')
        children={key:[] for key in ['',*by_id]}
        for n in nodes:
            children[n['parent_id']].append(n)
        visited=set()
        def build(node):
            key=node['node_id']
            if key in visited:
                raise ValueError('排版节点存在循环引用')
            visited.add(key)
            out={k:v for k,v in node.items() if k not in {'node_id','parent_id'}}
            descendants=children[key]
            if out['type']=='section':
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
    return {'blocks':blocks}


def compose(bundle, response, inventory):
    body=ComposedDraft.model_validate(expand_response(response)).model_dump(exclude_none=True)
    module_path=Path(bundle['root'])/'runtime/composition.py'
    spec=importlib.util.spec_from_file_location('sourceloom_frozen_composer',module_path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sources=protected_objects(inventory)
    source_text='\n'.join(o['text'] for o in inventory['objects'])
    normalized_source=re.sub(r'\s+',' ',source_text).casefold()
    lowered_sources=set()
    def format_terms(nodes):
        for node in nodes:
            if node['type']=='section':
                format_terms(node['blocks'])
            if node['type']=='list':
                def optional_children(items):
                    for item in items:
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
            if en.casefold() not in normalized_source:
                text=(abbr+' ' if abbr else '')+node['zh']+'：'+'；'.join(node['definition'])
                node.clear()
                node.update(type='list',items=[{'text':text}])
            else:
                node['en']=en
    blocks=[]
    def placed_sources(nodes):
        for node in nodes:
            if node['type']=='source':
                yield node['id']
            elif node['type']=='section':
                yield from placed_sources(node['blocks'])
    for b in body['blocks']:
        lowered_sources.clear()
        format_terms(b['content'])
        placed=list(placed_sources(b['content']))
        selected={sid:sources[sid] for sid in placed if sid in sources}
        if set(selected)!=set(placed):
            raise ValueError('普通正文不能冒充逐字原对象，或对象身份不存在')
        text=module.render_document({'blocks':b['content']},sources=selected)
        # Placement is established by actual source nodes, never by a second model
        # list that can disagree with its own authored layout. Raw result is retained.
        embedded=list(dict.fromkeys([*selected,*sorted(lowered_sources)]))
        blocks.append({k:v for k,v in b.items() if k!='content'}|dict(markdown=text,object_ids=embedded,embedded_object_ids=embedded))
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
