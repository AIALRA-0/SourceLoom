"""Read-only example projections and reversible personal-library organization.

Examples live beside the existing store, never as personal project rows. All
reading and compilation uses the same processor as personal material.
"""
import copy
import json
import re
import time

from . import processor
from .store import Conflict, digest, identity


def archived(project):
    return bool((project.get('library') or {}).get('archived'))


def summary(project):
    result = {key: project.get(key) for key in ('id', 'title', 'state', 'revision', 'created')}
    result['library'] = project.get('library') or {'kind': 'personal', 'archived': False}
    result['title'] = result['library'].get('display_name') or result['title']
    result['trashed'] = bool(project.get('trashed'))
    result['folder'] = project.get('folder')
    return result


def set_archive(store, pid, value, reason='用户整理材料库'):
    """Retain the complete project, versions, imports and immutable bytes."""
    from .processor_tree import ProcessorTree, name_key
    if hasattr(store, 'is_example') and store.is_example(pid):
        raise Conflict('示例为只读材料；请先创建试用副本')
    tree = ProcessorTree(store)
    with tree.library.transaction() as cx:
        row = cx.execute('SELECT * FROM processor_material_meta WHERE id=?',(pid,)).fetchone()
        if not row:
            raise Conflict('历史任务保留归档读取，请在材料库整理当前材料')
        if row['trashed']:
            raise Conflict('回收站与归档独立，请先恢复材料')
        if bool(row['archived']) != bool(value):
            if not value:
                siblings = cx.execute('SELECT title FROM processor_material_meta WHERE parent IS ? AND id<>? AND trashed=0 AND archived=0',(row['parent'],pid))
                if any(name_key(r[0])==name_key(row['title']) for r in siblings):
                    raise Conflict('目标位置存在同名项目，请改名后恢复')
                if any(name_key(r[0])==name_key(row['title']) for r in cx.execute("SELECT name FROM library_folders WHERE scope='processor' AND parent IS ? AND trashed=0",(row['parent'],))):
                    raise Conflict('目标位置存在同名项目，请改名后恢复')
            paths=['$.library.kind','personal','$.library.archived',int(bool(value)),'$.library.updated',time.time()]
            if value:
                paths += ['$.library.archived_at',time.time(),'$.library.archive_reason',str(reason)[:500]]
            else:
                paths += ['$.library.restored_at',time.time()]
            cx.execute('UPDATE projects SET body=json_set(body,'+','.join('?' for _ in paths)+",'$.library_revision',COALESCE(json_extract(body,'$.library_revision'),0)+1) WHERE id=?",(*paths,pid))
    return store.get(pid)


def rename(store, pid, title):
    from .processor_tree import ProcessorTree
    tree = ProcessorTree(store)
    with tree.store.connect() as cx:
        row = cx.execute('SELECT revision FROM processor_material_meta WHERE id=?',(pid,)).fetchone()
    if not row:
        raise Conflict('项目不存在或是只读示例')
    tree.actions(dict(action='rename',items=[dict(kind='document',id=pid,revision=row[0])],title=title,request_id=identity()))
    return store.get(pid)


def blob_keys(value):
    """Only actual blob references, not every content/manifest SHA digest."""
    result = set()
    def visit(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {'sha256', 'response_markdown_blob'} and isinstance(child, str) and re.fullmatch('[0-9a-f]{64}', child):
                    result.add(child)
                else:
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
    visit(value)
    return result


class LibraryStore:
    """A small dispatcher; personal persistence remains the existing Store."""
    def __init__(self, store):
        self.personal = store
        self.root = store.root
        self.catalog_root = self.root / 'library' / 'examples'
        self._stamp = None
        self._catalog = {}

    def __getattr__(self, name):
        return getattr(self.personal, name)

    def _load(self):
        path = self.catalog_root / 'catalog.json'
        stamp = path.stat().st_mtime_ns if path.exists() else None
        if stamp != self._stamp:
            data = json.loads(path.read_text(encoding='utf-8')) if stamp else {}
            self._catalog = {p['id']: p for p in data.get('projects', [])}
            self._stamp = stamp
        return self._catalog

    def is_example(self, pid):
        return pid in self._load()

    def get(self, pid):
        catalog = self._load()
        if pid in catalog:
            return copy.deepcopy(catalog[pid])
        return self.personal.get(pid)

    def change(self, pid, update, expected=None):
        if self.is_example(pid):
            raise Conflict('示例为只读材料；请先创建试用副本')
        return self.personal.change(pid, update, expected)

    def costs(self, pid):
        return [] if self.is_example(pid) else self.personal.costs(pid)

    def jobs(self, pid):
        return [] if self.is_example(pid) else self.personal.jobs(pid)

    def read_blob(self, key):
        if not re.fullmatch('[0-9a-f]{64}', key):
            raise ValueError('无效资源身份')
        # Do not mask corruption of a personal source with a matching catalog
        # copy: the existing store's integrity check must still reject it.
        if (self.root / 'blobs' / key).exists():
            return self.personal.read_blob(key)
        path = self.catalog_root / 'blobs' / key
        if path.exists():
            raw = path.read_bytes()
            if digest(raw) != key:
                raise Conflict('示例原件字节已经变化')
            return raw
        return self.personal.read_blob(key)

    def examples(self):
        rows = []
        for project in self._load().values():
            library = project['library']
            if library.get('supplement'):
                continue
            row = summary(project)
            row.update({key: copy.deepcopy(library.get(key)) for key in
                        ('label', 'format', 'tags', 'range', 'details', 'source_url',
                         'reading_language', 'content_kind')})
            row['readonly'] = True
            thumbnail = next((r for r in project['processor']['resources']
                              if r.get('sha256') and r['kind'] in {'page', 'image'}), None)
            row['thumbnail_url'] = ('/api/processor/projects/'+project['id']+'/files/'+thumbnail['sha256']
                                    if thumbnail else None)
            row['open_url'] = '/?material='+project['id']
            rows.append(row)
        return rows

    def trial(self, pid):
        if not self.is_example(pid):
            raise KeyError(pid)
        template = self.get(pid)
        # Copy shared immutable bytes into the personal SHA pool so archiving a
        # trial, or replacing the example catalog, cannot break its resources.
        for key in blob_keys(template):
            self.personal.blob(self.read_blob(key))
        created = processor.create(self.personal, template['title']+' · 试用副本',
                                   template['processor'].get('preferences', ''))
        candidate = copy.deepcopy(template['processor'])
        candidate['requests'] = []
        for key in ('last_issue_result', 'issue_previews', 'pending_issue_preview'):
            candidate.pop(key, None)
        active_index = next((i for i, version in enumerate(candidate['versions'])
                             if version['id'] == candidate['active_version']), None)
        if active_index is None:
            raise Conflict('示例的当前成稿版本不存在')
        for version in candidate['versions']:
            version.update(id=identity(), origin='example_copy', request_id=None, created=time.time())
            for key in ('issue_action', 'issue_decisions', 'parent_version'):
                version.pop(key, None)
            # User confirmation/undo relations belong to the template version.
            # The new copy exposes its own unresolved questions for confirmation.
            version['representations'] = []
        candidate['active_version'] = candidate['versions'][active_index]['id']
        def update(project):
            project.update(inventory=copy.deepcopy(template['inventory']), processor=candidate,
                           state='candidate', revision=1,
                           library={'kind': 'personal', 'archived': False, 'example_id': pid,
                                    'example_source_digest': template['processor']['source_digest']})
        result = self.personal.change(created['id'], update)
        active = processor.version_view(result)
        def checks(project):
            project['processor']['versions'][active_index] = active
            project['processor']['checks'] = active['checks']
        return self.personal.change(created['id'], checks)
