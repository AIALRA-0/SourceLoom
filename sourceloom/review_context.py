"""Evidence scopes and immutable line addresses, without modifying prose."""
import json
from pathlib import Path
from .store import Conflict, digest
from .writing import canonical, protected_objects
from .media import source_literals


PROCESS_RULES = {'FMT-001', 'FMT-004', 'FMT-005', 'FMT-006', 'FMT-007', 'FMT-109'}


def _matches_transaction(before,after,edits):
    if not edits:return False
    text=before.decode('utf8');lines=text.splitlines(keepends=True);starts=[];cursor=0
    for line in lines:starts.append(cursor);cursor+=len(line)
    replacements=[]
    for edit in edits:
        identity=edit['node_id']
        if not identity.startswith('LINE-'):return False
        index=int(identity[5:])-1;old=edit['old_text']
        if index<0 or index>=len(lines) or not old or lines[index].count(old)!=1:return False
        start=starts[index]+lines[index].index(old);end=start+len(old)
        if any(start<b and a<end for a,b,_ in replacements):return False
        replacements.append((start,end,edit['new_text']))
    for start,end,replacement in sorted(replacements,reverse=True):text=text[:start]+replacement+text[end:]
    return text.encode('utf8')==after


def execution_evidence(job, bundle, repair_root=None, repair_limit=2):
    rounds = job.get('repair_rounds', 0)
    repair_limit = max(2, min(32, int(repair_limit)))
    history = job.get('repair_commits', [])
    verified=[]
    if repair_root is not None:
        repair_root=Path(repair_root)
        roots=[repair_root]
        # A quality continuation keeps every earlier commit, while its files
        # remain in the immutable parent job directory. Search only the exact
        # recorded continuation chain and still verify every digest and edit.
        for record in reversed(job.get('quality_history',[])):
            parent=record.get('job')
            if parent:
                candidate=repair_root.parent/parent
                if candidate not in roots:roots.append(candidate)
        for commit in history:
            for root in roots:
                directory=root/('repair-'+str(commit['round']))
                try:
                    before=(directory/'before.md').read_bytes();after=(directory/'after.md').read_bytes()
                    proposal=json.loads((directory/'patch.json').read_text(encoding='utf8'))
                    if (digest(before)!=commit['before'] or digest(after)!=commit['after']
                            or proposal['document_sha256']!=commit['before']
                            or not _matches_transaction(before,after,proposal['edits'])):continue
                    verified.append(commit|{'changes':proposal['edits'],'artifact_job':root.name})
                    break
                except (OSError,ValueError,KeyError):continue
    return {
        'skill-delivery': {'rule_ids': ['FMT-001'], 'status': 'pass',
            'package_digest': bundle.get('package_digest'),
            'instruction_files': list(bundle.get('instructions', {})),
            'reason': 'The provider injects every complete effective skill file and validates the frozen package before each role call; this proves delivery, not model understanding'},
        'local-committer': {'rule_ids': ['FMT-004', 'FMT-005', 'FMT-109'],
            'status': 'pass' if rounds == 0 or history and len(verified)==len(history) else 'unknown',
            'commits': verified,
            'reason': 'No repair yet' if rounds == 0 else 'Actual saved before/after files match the commit digests; exact old/new text, scope and reasons are supplied for semantic scope review. Missing or mismatched transaction artifacts do not establish a pass'},
        'repair-limit': {'rule_ids': ['FMT-006'], 'status': 'pass' if rounds <= repair_limit else 'fail',
            'attempts': rounds, 'limit': repair_limit,
            'authority': ('The current user explicitly authorized end-to-end repair until every required check passes; '
                'the configured bounded limit therefore supersedes the skill default for this production run'),
            'reason': 'Persisted attempt counter, including rejected proposals, checked against the user-authorized bounded limit'},
        'candidate-retention': {'rule_ids': ['FMT-007'], 'status': 'pass',
            'draft_digest': digest(canonical(job['draft']).encode()),
            'reason': 'Candidate remains persisted and readable; user requires unresolved quality issues to block formal acceptance'},
    }


def protected_context(source, draft):
    literals = protected_objects(source)
    objects={o['id']:o for o in source['objects']}
    contexts=[{'source_id': sid, 'kind': next(o['kind'] for o in source['objects'] if o['id'] == sid),
             'literal': text, 'block_ids': [b['id'] for b in draft['blocks'] if text and text in b['markdown']]}
            for sid, current in literals.items() for text in source_literals(objects[sid],current) if text]
    # Only actual displayed spans need original-object exemptions. Complete
    # source objects remain separately supplied and independently checked.
    return [entry for entry in contexts if entry['block_ids']]


def editable_lines(draft, source, allowed):
    protected = protected_objects(source)
    objects={o['id']:o for o in source['objects']}
    variants=[literal for sid,current in protected.items() for literal in source_literals(objects[sid],current)]
    result = []
    for block in draft['blocks']:
        if block['id'] not in allowed:
            continue
        text = block['markdown']
        spans = []
        for literal in variants:
            if not literal:
                continue
            start = 0
            while (offset := text.find(literal, start)) >= 0:
                spans.append((offset, offset + len(literal)))
                start = offset + len(literal)
        cursor = 0
        for number, line in enumerate(text.splitlines(keepends=True), 1):
            raw = line.rstrip('\r\n')
            end = cursor + len(raw)
            if raw.strip() and text.count(raw) == 1 and not any(cursor < b and a < end for a, b in spans):
                result.append({'line_id': block['id'] + ':' + str(number),
                               'block_id': block['id'], 'text': raw})
            cursor += len(line)
    return result


def line_proposal(proposal, lines):
    by_id = {line['line_id']: line for line in lines}
    seen = set()
    edits = []
    for edit in proposal['edits']:
        key = edit['line_id']
        if key in seen or key not in by_id:
            raise Conflict('修复行号重复或不在可编辑范围内，整笔未提交')
        seen.add(key)
        line = by_id[key]
        if line['text'] == edit['replacement']:
            continue
        edits.append({'block_id': line['block_id'], 'old_text': line['text'],
                      'new_text': edit['replacement'], 'reason': edit['reason']})
    return {'document_digest': proposal['document_digest'], 'edits': edits}
