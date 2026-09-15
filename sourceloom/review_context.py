"""Evidence scopes and immutable line addresses, without modifying prose."""
from .store import Conflict, digest
from .writing import canonical, protected_objects
from .media import source_literals


PROCESS_RULES = {'FMT-001', 'FMT-004', 'FMT-005', 'FMT-006', 'FMT-007', 'FMT-109'}


def execution_evidence(job, bundle):
    rounds = job.get('repair_rounds', 0)
    history = job.get('repair_commits', [])
    return {
        'skill-delivery': {'rule_ids': ['FMT-001'], 'status': 'pass',
            'package_digest': bundle.get('package_digest'),
            'instruction_files': list(bundle.get('instructions', {})),
            'reason': 'The provider injects every complete effective skill file and validates the frozen package before each role call; this proves delivery, not model understanding'},
        'local-committer': {'rule_ids': ['FMT-004', 'FMT-005', 'FMT-109'],
            'status': 'pass' if rounds == 0 or history else 'unknown',
            'commits': history,
            'reason': 'No repair yet' if rounds == 0 else 'Exact scoped transactions with before/after digests; absent historical evidence is not a pass'},
        'repair-limit': {'rule_ids': ['FMT-006'], 'status': 'pass' if rounds <= 2 else 'fail',
            'attempts': rounds, 'reason': 'Persisted attempt counter, including rejected proposals'},
        'candidate-retention': {'rule_ids': ['FMT-007'], 'status': 'pass',
            'draft_digest': digest(canonical(job['draft']).encode()),
            'reason': 'Candidate remains persisted and readable; user requires unresolved quality issues to block formal acceptance'},
    }


def protected_context(source, draft):
    literals = protected_objects(source)
    objects={o['id']:o for o in source['objects']}
    return [{'source_id': sid, 'kind': next(o['kind'] for o in source['objects'] if o['id'] == sid),
             'literal': text, 'block_ids': [b['id'] for b in draft['blocks'] if text and text in b['markdown']]}
            for sid, current in literals.items() for text in source_literals(objects[sid],current) if text]


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
