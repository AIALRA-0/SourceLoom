"""Append-only source revisions preserve the complete preceding state."""

import copy
from .contracts import Obligation
from .ingest import MAX_OBJECTS, MAX_EXPANDED
from .store import Conflict, identity


def reopen(project, reason):
    inv = project.get('inventory')
    if project['active_job'] or not inv or not inv['frozen']:
        raise Conflict('只有没有执行中任务的冻结清单才能建立补漏版本')
    if project.get('incremental_pending'):
        raise Conflict('先完成当前补漏版本，再开启下一版本')
    if not reason.strip():
        raise ValueError('请记录这次补漏的原因')
    project['baseline'] = dict(inventory_digest=inv['digest'], revision=project['revision'],
        obligation_ids=[o['id'] for o in inv['obligations']],
        plan=copy.deepcopy(project['plan']), draft=copy.deepcopy(project['draft']))
    inv.update(id=identity(), version=inv['version']+1, frozen=False, digest=None,
               inventory_review=None, parent_digest=inv['digest'], change_reason=reason)
    project.update(plan=None, review=None, accepted_revision=None,
                   revision=project['revision']+1, state='inventoried', incremental_pending=True)
    project.pop('readweave', None)


def append_intake(existing, incoming):
    if existing['frozen']:
        raise Conflict('先建立补漏版本，再追加材料')
    inv = copy.deepcopy(existing)
    if len(inv['objects'])+len(incoming['objects']) > MAX_OBJECTS:
        raise ValueError('追加后超过 5000 个源对象')
    if sum(r['size'] for r in inv['originals']+incoming['originals']) > MAX_EXPANDED:
        raise ValueError('累计原件超过 100 MB，已有版本保留')
    prefix = 'add-'+identity()[:12]+'-'
    mapping = {o['id']:prefix+o['id'] for o in incoming['objects']}
    for source in incoming['objects']:
        obj = copy.deepcopy(source)
        obj['id'] = mapping[obj['id']]
        if obj.get('parent_id'):obj['parent_id'] = mapping[obj['parent_id']]
        # Keep source scope distinct even when two uploads reuse an HTML anchor.
        obj['source_scope'] = prefix+obj.get('source_scope',obj['locator'].split('/')[0])
        obj['source_url'] = incoming.get('source_url')
        inv['objects'].append(obj)
    for source in incoming['obligations']:
        ob = copy.deepcopy(source)
        ob.update(id=prefix+ob['id'], object_id=mapping[ob['object_id']])
        inv['obligations'].append(ob)
    for source in incoming['unknown']:
        gap = copy.deepcopy(source)
        gap.update(id=prefix+gap['id'], object_id=mapping.get(gap.get('object_id'), ''))
        inv['unknown'].append(gap)
    inv['originals'].extend(copy.deepcopy(incoming['originals']))
    inv['resources'].extend(copy.deepcopy(incoming['resources']))
    inv['inventory_review'] = None
    return inv


def append_obligations(inventory, additions):
    if inventory['frozen']:raise Conflict('先建立补漏版本，再追加义务')
    parsed = [Obligation.model_validate(a).model_dump() for a in additions]
    if not parsed:raise ValueError('至少追加一项义务')
    if len(inventory['obligations'])+len(parsed)>10000:raise ValueError('义务数量超过当前范围')
    known = {o['id'] for o in inventory['objects']}
    ids = {o['id'] for o in inventory['obligations']}
    for ob in parsed:
        if not ob['id'] or ob['id'] in ids or ob['object_id'] not in known or not ob['statement'].strip():
            raise ValueError('义务身份重复、内容为空或源对象不存在')
        ids.add(ob['id'])
    inventory['obligations'].extend(parsed)
    inventory['inventory_review'] = None


def pending_obligations(project):
    retained = set(project.get('baseline', {}).get('obligation_ids', [])) if project.get('incremental_pending') and project.get('baseline', {}).get('draft') else set()
    return [o for o in project['inventory']['obligations'] if o['id'] not in retained]
