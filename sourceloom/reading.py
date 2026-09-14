"""Reader projections and reversible heading presentation; never modify originals."""
import copy
import re
from markdown_it import MarkdownIt


def presentation(project, numbering=None):
    numbering=numbering or project.get('heading_numbering','preserve')
    if numbering not in {'preserve','numbered','none'}:
        raise ValueError('标题编号选项无效')
    if not project.get('draft'):
        return project
    if numbering in {'preserve','none'} and not project.get('_heading_original_markdown'):
        return project|{'heading_numbering':numbering}
    result=copy.deepcopy(project)
    originals=project.get('_heading_original_markdown') or {b['id']:b['markdown'] for b in project['draft']['blocks']}
    for block in result['draft']['blocks']:block['markdown']=originals[block['id']]
    result['_heading_original_markdown']=originals
    result['heading_numbering']=numbering
    if numbering in {'preserve','none'}:return result
    counters=[]
    levels=[]
    parser=MarkdownIt('commonmark',{'html':True})
    for block in result['draft']['blocks']:
        lines=block['markdown'].splitlines(keepends=True)
        changes=[]
        for token in parser.parse(block['markdown']):
            if token.type!='heading_open' or not token.map:
                continue
            start,end=token.map
            # Code, quoted originals and embedded HTML keep their characters.
            if not re.match(r'^ {0,3}#{1,6}\s',lines[start]):
                continue
            level=int(token.tag[1:])
            while levels and levels[-1]>level:
                levels.pop();counters.pop()
            if levels and levels[-1]==level:counters[-1]+=1
            else:levels.append(level);counters.append(1)
            match=re.match(r'^( {0,3}#{1,6}\s+)(.*?)(\r?\n)?$',lines[start])
            label=match[2]
            # An existing leading number may be a year, quantity, or source label.
            # Keep it verbatim; never guess that it is safe to delete.
            prefix='' if re.match(r'^\d+(?:\.\d+)*[.)]?\s+',label) else '.'.join(map(str,counters))+' '
            changes.append((start,match[1]+prefix+label+(match[3] or '')))
        for start,line in changes:lines[start]=line
        block['markdown']=''.join(lines)
    return result


def reader_summary(project,costs):
    p={k:project.get(k) for k in ('id','title','mode','state','revision','active_job','goal','folder',
        'library_revision','trashed','budget_usd','max_calls','heading_numbering')}
    inv=project.get('inventory')
    p['inventory']=None if not inv else {'originals':inv.get('originals',[]),
        'source_chars':sum(len(o.get('text','')) for o in inv['objects']),
        'visual_count':sum(o['kind'] in {'page','image'} and bool(o.get('resource_id')) for o in inv['objects'])}
    p['draft']=bool(project.get('draft'))
    p['cost_summary']={'known_usd':sum(c['actual'] or 0 for c in costs),
        'subscription_calls':sum(c['actual'] is None and c['body'].get('channel') in {'subscription','router','codex-cli'} for c in costs)}
    return p
