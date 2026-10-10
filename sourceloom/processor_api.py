"""The new default workbench shares one task/result format across channels."""
import copy
import json
import os
import re
import time
from collections import OrderedDict
from threading import RLock
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from urllib.parse import quote
from urllib.parse import urlsplit

from fastapi import File, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from . import processor
from .export import reading_page
from .ingest import MAX_FILE, SAFE_IMAGE
from .store import Conflict, identity, digest
from .processor_library import LibraryStore, archived, rename, set_archive, summary


class _ReadingDerivatives:
    """Per-process bounded reuse of identical, fully checked read projections."""
    def __init__(self, limit=8):
        self.limit, self.values, self.lock = limit, OrderedDict(), RLock()

    def get(self, key, build, *, clone=True):
        # Identical concurrent version/preview reads must not compile twice.
        with self.lock:
            if key not in self.values:
                self.values[key] = build()
                while len(self.values) > self.limit:
                    self.values.popitem(last=False)
            self.values.move_to_end(key)
            return copy.deepcopy(self.values[key]) if clone else self.values[key]


def register(app, store, config):
    store = LibraryStore(store)
    app.state.processor_library = store
    from .processor_tree_api import register_tree
    register_tree(app, store)
    from .processor_intake import MaterialIntake, ACTIVE_PHASES
    intake = MaterialIntake(store)
    app.state.processor_intake = intake
    app.add_event_handler('shutdown', intake.close)
    presentations, compilations, previews, issue_views = (_ReadingDerivatives() for _ in range(4))

    @lru_cache(maxsize=512)
    def image_size(sha):
        # Only read an immutable local image header; do not decode the full
        # picture or reprocess the source before presenting saved prose.
        if not re.fullmatch(r'[0-9a-f]{64}', sha):
            return {}
        from PIL import Image
        try:
            with Image.open(store.root/'blobs'/sha) as image:
                width, height = image.size
            return {'width': width, 'height': height}
        except (OSError, ValueError):
            return {}
    parsed_projects, asset_indexes, delivery_statuses = (_ReadingDerivatives() for _ in range(3))

    def read_snapshot(pid):
        # Check all current saved bytes, including changes which do not increment
        # the content revision. The cached parse stays private and read-only.
        if store.is_example(pid):
            p = store.get(pid)
            return p, digest(p)
        with store.connect() as cx:
            row = cx.execute('SELECT body FROM projects WHERE id=?', (pid,)).fetchone()
        if not row:
            raise KeyError(pid)
        key = digest(row[0].encode())
        p = parsed_projects.get((pid, key), lambda: json.loads(row[0]), clone=False)
        if 'processor' not in p:
            raise Conflict('这是历史生成任务，请在历史工作台打开')
        return p, key

    def compiled_view(p, vid=None):
        version = processor.active_version(p, vid)
        key = (p['_reading_cache_key'], version['id'])
        return compilations.get(key, lambda: processor.compile_result(p, version['markdown'],
            resource_usages=version.get('resource_usages'), representations=version.get('representations'),
            derived_resources=version.get('derived_resources'), scope_exclusions=version.get('scope_exclusions')), clone=False)

    def version_view(p, vid=None):
        version = copy.deepcopy(processor.active_version(p, vid))
        compiled = compiled_view(p, version['id'])
        # Compilation is borrowed read-only; caller-owned checks must stay isolated.
        version.update(checks=copy.deepcopy(compiled['checks']), mechanical_pass=compiled['mechanical_pass'])
        return version
    @app.middleware('http')
    async def readonly_examples(request, call_next):
        from fastapi.responses import JSONResponse
        match = re.match(r'^/api/processor/projects/([^/]+)(?:/|$)', request.url.path)
        if match and request.method not in {'GET', 'HEAD', 'OPTIONS'} and store.is_example(match.group(1)):
            return JSONResponse({'error': '示例为只读材料；请先创建试用副本'}, status_code=409)
        if match and request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            try:
                if store.get(match.group(1)).get('trashed'):
                    return JSONResponse({'error': '回收站材料为只读；请先恢复再修改'}, status_code=409)
            except KeyError:
                pass
        return await call_next(request)
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='material-handoff')
    app.state.processor_executor = pool
    # A server restart cannot resume a generation blindly. Retain any saved
    # completed receipt; an interrupted local handoff becomes explicitly unknown.
    for saved in store.list():
        if 'processor' not in saved:
            continue
        for request in saved['processor']['requests']:
            if request.get('status') != 'RUNNING':
                continue
            try:
                job = store.job(request['id'])
                from .processor_channels import classify_saved_receipt
                receipt = classify_saved_receipt(job) or dict(status='UNKNOWN',error='执行进程已中断，原请求状态未确定，未重新投递')
            except KeyError:
                receipt = dict(status='UNKNOWN',error='交接进程中断，保留请求身份，未自动投递')
            if receipt.get('status')=='SUCCESS' and receipt.get('markdown') and not receipt.get('historical_readonly'):
                processor.save_result(store,saved['id'],receipt['markdown'],origin=request['channel'],request_id=request['id'])
            def restore(p, rid=request['id'], value=receipt):
                row = next(r for r in p['processor']['requests'] if r['id']==rid)
                row.update({k:v for k,v in value.items() if k not in {'markdown','pack_digest'}})
            store.change(saved['id'],restore)
    prefix = '/api/processor/projects'

    @lru_cache(maxsize=512)
    def image_dimensions(key):
        # Frozen bytes also serve older projects that predate size metadata.
        # This is presentation metadata only; never add it to the task pack.
        from PIL import Image
        try:
            with Image.open(BytesIO(store.read_blob(key))) as picture:
                width, height = picture.size
            return dict(width=width, height=height)
        except (OSError, ValueError, Image.DecompressionBombError):
            return {}

    def detail(pid):
        p = reading_project(pid)
        if p['processor'].get('active_version'):
            active = version_view(p)
            p['processor']['versions'] = [active if v['id']==active['id'] else v
                                         for v in p['processor']['versions']]
            p['processor']['checks'] = active['checks']
        p['costs'] = store.costs(pid)
        jobs = store.jobs(pid)
        indexed = {j.get('logical_request_id') or j.get('id'):j for j in jobs}
        p['processor']['requests'] = copy.deepcopy(p['processor']['requests'])
        for request in p['processor']['requests']:
            job = indexed.get(request['id'])
            if job:
                request['call_receipt'] = job
        p['processor']['resources'] = [dict(r, usage=processor.resource_usage(p['processor'],r),
            url=f'{prefix}/{pid}/files/{r["sha256"]}',
            **(image_dimensions(r['sha256']) if r['kind'] in {'page', 'image'} else {}))
            if r.get('sha256') else r for r in p['processor']['resources']]
        p.pop('_reading_cache_key', None)
        return p

    def reading_project(pid):
        # Display geometry can be recovered for historic sources without
        # modifying persisted inventory, task identities or draft bytes.
        original, key = read_snapshot(pid)
        p = original | {'processor':dict(original['processor'])}
        inventory = p.get('inventory')
        projected = presentations.get((pid, key),
            lambda: processor.presentation_project(p, store.read_blob).get('inventory'), clone=False)
        p = p | {'inventory':projected, '_reading_cache_key':(pid, key)}
        if p.get('trashed'):
            p['library'] = (p.get('library') or {}) | {'readonly':True}
        return p

    def reading_detail(pid):
        # The workbench needs one active draft, not all historical Markdown,
        # source-object raw HTML, receipts and attachment copies.
        p = reading_project(pid)
        state = p['processor']
        active = version_view(p) if state.get('active_version') else None
        version_fields = ('id', 'digest', 'origin', 'created', 'created_at', 'label', 'source_digest')
        versions = [{k:v[k] for k in version_fields if k in v} for v in state.get('versions', [])]
        resources = [dict(r, usage=processor.resource_usage(state, r),
                         url=f'{prefix}/{pid}/files/{r["sha256"]}') if r.get('sha256') else r
                     for r in state.get('resources', [])]
        keep = ('schema', 'preferences', 'source_digest', 'source_text', 'source_map', 'page_count',
                'intake_progress', 'intake_warnings', 'warnings', 'resource_usages', 'pack_digest', 'policy_digest', 'requests')
        inventory = p.get('inventory') or {}
        return {k:p[k] for k in ('id', 'title', 'mode', 'state', 'created', 'revision', 'library', 'trashed') if k in p} | {
            'inventory': {k:inventory[k] for k in ('originals', 'page_count') if k in inventory},
            'processor': {k:state[k] for k in keep if k in state} | dict(resources=resources, versions=versions,
                active_version=state.get('active_version'), checks=active['checks'] if active else state.get('checks', [])),
            'reading_version':active}

    @app.get('/api/processor/capabilities')
    def capabilities():
        from .processor_channels import capabilities as channel_capabilities
        result = channel_capabilities(config)
        result['environment'] = 'development_acceptance' if os.environ.get('SOURCELOOM_ACCEPTANCE') == '1' else 'user'
        result['auth_mode'] = config.get('auth_mode', 'local')
        result['readweave_configured'] = all(config.get(k) for k in ('readweave_url','readweave_token','readweave_parent'))
        remote = urlsplit(str(config.get('readweave_public_url') or config.get('readweave_url') or ''))
        result['readweave_target'] = (dict(instance_url=f'{remote.scheme}://{remote.hostname}' +
            (f':{remote.port}' if remote.port else ''), parent_note_id=config.get('readweave_parent'))
            if result['readweave_configured'] and remote.scheme in {'http','https'} and remote.hostname else None)
        result['supported_input'] = ['pdf','md','txt','html','rst','docx','png','jpg','webp','gif','zip']
        return result

    @app.get('/api/processor/readweave-connection')
    def readweave_connection():
        from .readweave import connection_status
        return connection_status(config)

    @app.get(prefix)
    def projects():
        return app.state.processor_tree.material_summaries()

    @app.get('/api/processor/examples')
    def examples():
        return store.examples()

    @app.post('/api/processor/examples/{example_id}/trial')
    def trial(example_id: str):
        return detail(store.trial(example_id)['id'])

    @app.get('/api/processor/archives')
    def archives():
        return app.state.processor_tree.material_summaries(archived=True)

    @app.post(prefix+'/{pid}/archive')
    def archive(pid: str):
        set_archive(store, pid, True)
        return detail(pid)

    @app.post(prefix+'/{pid}/restore')
    def restore_archive(pid: str):
        set_archive(store, pid, False)
        return detail(pid)

    @app.post(prefix+'/{pid}/rename')
    def rename_material(pid: str, body: dict):
        rename(store, pid, body.get('title') or '')
        return detail(pid)

    @app.post(prefix)
    def create(body:dict):
        return processor.create(store, str(body.get('title') or '未命名材料'), str(body.get('preferences') or ''),
                                body.get('parent_id'))

    @app.get(prefix+'/{pid}')
    def get(pid:str, reading:bool=False):
        # Both projections already contain JSON-native saved data. Returning a
        # Response avoids a second recursive traversal of full source objects,
        # historical drafts and receipts on every polling read.
        return JSONResponse(reading_detail(pid) if reading else detail(pid))

    @app.get(prefix+'/{pid}/progress')
    def progress(pid:str):
        with store.connect() as cx:
            row = cx.execute("SELECT json_extract(body,'$.processor.intake_progress') FROM projects WHERE id=?", (pid,)).fetchone()
        if not row:
            raise KeyError(pid)
        return json.loads(row[0]) if row[0] else dict(phase='not_started')

    @app.post(prefix+'/{pid}/upload/resume')
    def resume_upload(pid:str):
        return JSONResponse(intake.submit(pid, resume=True), status_code=202)

    @app.post(prefix+'/{pid}/upload')
    def upload(pid:str, files:list[UploadFile]=File(...), background:bool=False):
        current = processor.project(store,pid)
        if (current['processor'].get('intake_progress') or {}).get('phase') in ACTIVE_PHASES:
            raise Conflict('这份材料正在准备，请查询原进度，不要重复上传')
        if not 1 <= len(files) <= 100:
            raise ValueError('一次上传 1–100 个文件')
        uploads, total = [], 0
        for file in files:
            raw = file.file.read(MAX_FILE+1)
            total += len(raw)
            if len(raw)>MAX_FILE or total>MAX_FILE*4:
                raise ValueError('单文件最多 25 MB，本次最多 100 MB')
            uploads.append((Path(file.filename or 'material.txt').name, raw))
        if background:
            return JSONResponse(intake.submit(pid, uploads), status_code=202)
        processor.prepare(store,pid,uploads)
        return detail(pid)

    @app.post(prefix+'/{pid}/url')
    def url(pid:str, body:dict):
        if not config.get('fetch_enabled'):
            raise Conflict('此实例未启用网页接入；可上传保存的网页与资源')
        from .network import fetch_bundle
        bundle = fetch_bundle(str(body.get('url') or ''), rendered=True, include_manifest=True)
        # Existing network module enforces public destinations and snapshots
        # direct article assets; no reference target-page research takes place.
        uploads, canonical_url, aliases, failures, manifest = bundle
        processor.prepare(store,pid,uploads,canonical_url,aliases,manifest)
        if failures:
            store.change(pid,lambda p:p['processor'].update(intake_warnings=failures))
        return detail(pid)

    @app.get(prefix+'/{pid}/pack')
    def pack(pid:str):
        return processor.task_pack(store,pid)

    @app.post(prefix+'/{pid}/pack')
    def preferences(pid:str,body:dict):
        value = str(body.get('preferences') or '')
        if len(value)>12000:
            raise ValueError('阅读偏好最多 12000 字符')
        processor.project(store,pid)
        store.change(pid,lambda p:p['processor'].update(preferences=value))
        pack = processor.task_pack(store,pid)
        store.change(pid,lambda p:p['processor'].update(
            pack_digest=pack['digest'],policy_digest=pack['template_digest']))
        return pack

    @app.post(prefix+'/{pid}/resources')
    def resource_usages(pid:str,body:dict):
        processor.set_resource_usages(store,pid,body.get('changes'),body.get('base_pack_digest'))
        return detail(pid)

    @app.get(prefix+'/{pid}/pack.zip')
    def download_pack(pid:str):
        from .manual_handoff import pack_zip
        pack = processor.task_pack(store, pid)
        return Response(pack_zip(store,pid,pack=pack),media_type='application/zip',
            headers={'Content-Disposition':f'attachment; filename="SourceLoom-{pack["digest"]}.zip"'})

    @app.get(prefix+'/{pid}/manual-handoff')
    def manual_handoff(pid: str):
        from .manual_handoff import view
        return view(store, pid)

    @app.get(prefix+'/{pid}/manual-handoff/files/{file_id}')
    def manual_file(pid: str, file_id: str):
        from .manual_handoff import download
        path, row = download(store, pid, file_id)
        return FileResponse(path, media_type=row.get('mime') or 'application/octet-stream',
                            filename=row['name'], headers={'Content-Security-Policy': "sandbox; default-src 'none'"})

    @app.get(prefix+'/{pid}/files/{key}')
    def asset(pid:str,key:str,download:bool=False):
        p, snapshot_key = read_snapshot(pid)
        def authorised_assets():
            inv = p.get('inventory') or {}
            derived = [r for v in p['processor']['versions'] for r in v.get('derived_resources', [])
                       if r.get('source_digest') == p['processor']['source_digest']]
            return {r['sha256']:r for r in inv.get('resources',[])+inv.get('originals',[])+derived if r.get('sha256')}
        indexed = asset_indexes.get((pid, snapshot_key), authorised_assets, clone=False)
        item = indexed.get(key)
        if not item or not re.fullmatch(r'[0-9a-f]{64}',key):
            raise KeyError(key)
        import mimetypes
        mime = item.get('mime') or mimetypes.guess_type(item['name'])[0] or 'application/octet-stream'
        inline = mime in SAFE_IMAGE or mime=='application/pdf'
        path = store.root/'blobs'/key
        # Keep the existing full-byte integrity check, including file changes
        # which restore timestamps. Stream only the requested range afterwards.
        store.read_blob(key)
        return FileResponse(path,media_type=mime if inline else 'application/octet-stream',
            headers={'Content-Disposition':('attachment' if download or not inline else 'inline')+"; filename*=UTF-8''"+quote(Path(item['name']).name),
                     'Content-Security-Policy':"sandbox; default-src 'none'",
                     'Content-Encoding':'identity', 'Cache-Control':'private, max-age=31536000, immutable'})

    @app.post(prefix+'/{pid}/result')
    def result(pid:str,body:dict):
        processor.save_result(store,pid,body.get('markdown'),origin='manual',base_version=body.get('base_version'))
        return detail(pid)

    @app.get(prefix+'/{pid}/versions/{vid}')
    def version(pid:str,vid:str):
        return version_view(reading_project(pid),vid)

    @app.get(prefix+'/{pid}/versions/{vid}/source-map')
    def version_source_map(pid:str,vid:str):
        p = processor.project(store,pid)
        v = processor.active_version(p,vid)
        return dict(version_id=vid,draft_digest=v['digest'],
                    source_digest=p['processor']['source_digest'],
                    locations=p['processor']['source_map'],blocks=v['source_map'])

    @app.post(prefix+'/{pid}/versions/{vid}/representations')
    def confirm_representation(pid:str,vid:str,body:dict):
        processor.confirm_representation(store,pid,vid,body)
        return detail(pid)

    @app.get(prefix+'/{pid}/versions/{vid}/issues')
    def reader_issues(pid:str,vid:str):
        from .processor_issues import issues
        p = reading_project(pid)
        return JSONResponse(issue_views.get((p['_reading_cache_key'], vid), lambda: issues(p, vid, store)))

    @app.get(prefix+'/{pid}/versions/{vid}/issue-image/{issue_id}')
    def reader_issue_image(pid:str,vid:str,issue_id:str):
        from .processor_issues import image_group, table_image, issues
        p=reading_project(pid)
        listing=issue_views.get((p['_reading_cache_key'],vid),lambda:issues(p,vid,store))
        group=image_group(p,vid,issue_id,store,listing=listing)
        raw,meta=table_image(p,group,store)
        return Response(raw,media_type='image/png',headers={'Cache-Control':'private, no-cache',
            'X-SourceLoom-Region-Precision':meta['precision'],
            'X-SourceLoom-Region-Bounds':','.join(str(x) for x in meta['bbox']),
            'X-SourceLoom-Region-Page':str(meta['page']),
            'X-SourceLoom-Source-SHA256':meta['native_source_sha']})

    @app.post(prefix+'/{pid}/versions/{vid}/issue-preview')
    def reader_issue_preview(pid:str,vid:str,body:dict):
        from .processor_issues import preview
        return preview(store,pid,vid,body)

    @app.post(prefix+'/{pid}/versions/{vid}/issue-apply')
    def reader_issue_apply(pid:str,vid:str,body:dict):
        from .processor_issues import apply
        apply(store,pid,vid,body)
        return detail(pid)

    @app.post(prefix+'/{pid}/versions/{vid}/issue-undo')
    def reader_issue_undo(pid:str,vid:str,body:dict=None):
        from .processor_issues import undo
        undo(store,pid,vid,(body or {}).get('preview_id'))
        return detail(pid)

    @app.get(prefix+'/{pid}/issue-operations/{preview_id}')
    def reader_issue_operation(pid:str,preview_id:str):
        from .processor_issues import operation
        return operation(store,pid,preview_id)

    @app.post(prefix+'/{pid}/select-version')
    def select(pid:str,body:dict):
        vid = str(body.get('version_id') or '')
        version = processor.active_version(processor.project(store,pid),vid)
        store.change(pid,lambda p:p['processor'].update(active_version=vid,checks=version['checks']))
        return detail(pid)

    @app.get(prefix+'/{pid}/preview')
    def preview(pid:str,version:str|None=None,reader:bool=False):
        p = reading_project(pid)
        v = processor.active_version(p,version)
        compiled = compiled_view(p, v['id'])
        from bs4 import BeautifulSoup
        def page_html():
            doc = BeautifulSoup(compiled['html'],'html.parser')
            dimensions = {r.get('sha256'):r for r in
                          p['processor'].get('resources', [])+(v.get('derived_resources') or [])}
            for img in doc.select('img[src^="assets/"]'):
                sha = img['src'][7:]
                img['src'] = f'{prefix}/{pid}/files/'+sha
                # Script-disabled preview frames ignore native image laziness.
                # The trusted parent reader hydrates only visible/near images;
                # independent previews and exported content keep their src.
                if reader:
                    img['data-reader-src'] = img.attrs.pop('src')
                img['loading'], img['decoding'] = 'lazy', 'async'
                resource = dimensions.get(sha, {})
                if reader and not (resource.get('width') and resource.get('height')):
                    resource = resource | image_size(sha)
                for axis in ('width', 'height'):
                    if axis not in img.attrs and resource.get(axis):
                        img[axis] = str(resource[axis])
            return reading_page(str(doc))
        html = previews.get((p['_reading_cache_key'], v['id'], reader), page_html)
        return HTMLResponse(html,headers={
            'Content-Security-Policy':"sandbox allow-same-origin; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'self'"})

    @app.get(prefix+'/{pid}/export')
    def export(pid:str):
        return Response(processor.export_package(store,processor.project(store,pid)),media_type='application/zip',
            headers={'Content-Disposition':'attachment; filename="SourceLoom-ReadWeave.zip"'})

    @app.post(prefix+'/{pid}/readweave')
    def readweave(pid:str):
        from .readweave import import_candidate
        processor.project(store,pid)
        return import_candidate(store,config,pid)

    @app.get(prefix+'/{pid}/readweave-status')
    def readweave_status(pid:str):
        from .readweave import import_status
        _, snapshot_key = read_snapshot(pid)
        receipts = [(path.name, digest(path.read_bytes()))
                    for path in sorted((store.root/'readweave').glob(pid+'-*.json'))]
        target = digest({k:config.get(k) for k in ('readweave_url', 'readweave_parent', 'readweave_token')})
        key = (pid, snapshot_key, digest(receipts), target)
        # import_status verifies the exact export, which is expensive. Its result
        # is reusable only while every project and durable receipt byte matches;
        # configured target/credential changes also invalidate this projection.
        return delivery_statuses.get(key, lambda: import_status(store,config,pid))

    @app.post(prefix+'/{pid}/generate',status_code=202)
    def generate(pid:str,body:dict):
        from .processor_channels import generate as handoff, capabilities
        channel = str(body.get('channel') or 'router')
        if channel != 'router':
            raise ValueError('自动交接只允许 Web Chat；不会借用其他 API、Codex、CLI 或 Runner')
        capability = capabilities(config)['router']
        if not capability['available']:
            raise ValueError(capability['reason'])
        pack = processor.task_pack(store,pid)
        request_id = str(body.get('request_id') or identity())
        if not re.fullmatch(r'[a-zA-Z0-9_-]{8,100}',request_id):
            raise ValueError('请求身份格式无效')
        repeated = False
        def claim(p):
            nonlocal repeated
            state = p['processor']
            if any(r['id']==request_id for r in state['requests']):
                repeated = True
                return
            if any(r['status']=='RUNNING' for r in state['requests']):
                raise Conflict('本材料已有交接请求正在执行')
            # UNKNOWN never disappears when the user explicitly starts a new
            # request or takes over manually. Each result is a distinct version.
            state['requests'].append(dict(id=request_id,logical_request_id=request_id,
                channel=channel,status='RUNNING',created=time.time(),pack_digest=pack['digest'],
                resource_usages=copy.deepcopy(pack['resource_selection']),
                template_version=pack['template_version'],template_digest=pack['template_digest']))
        store.change(pid,claim)
        if not repeated:
            def run():
                try:
                    receipt = handoff(store,copy.deepcopy(config),pid,pack,channel=channel,request_id=request_id)
                    if receipt.get('status')=='SUCCESS' and receipt.get('markdown') and not receipt.get('historical_readonly'):
                        processor.save_result(store,pid,receipt['markdown'],origin=channel,request_id=request_id)
                except Exception as exc:
                    # An unexpected local failure after entering the handoff
                    # cannot prove that a remote dispatch never happened.
                    receipt = dict(status='UNKNOWN',logical_request_id=request_id,error=str(exc)[:400])
                def finish(p):
                    row = next(r for r in p['processor']['requests'] if r['id']==request_id)
                    row.update({k:v for k,v in receipt.items() if k not in {'markdown','pack_digest'}},finished=time.time())
                store.change(pid,finish)
            pool.submit(run)
        return dict(request_id=request_id,reused=repeated)

    @app.post(prefix+'/{pid}/requests/{rid}/query')
    def query(pid:str,rid:str):
        from .processor_channels import query_result
        p = processor.project(store,pid)
        request = next((r for r in p['processor']['requests'] if r['id']==rid),None)
        if not request:
            raise KeyError(rid)
        receipt = query_result(store,copy.deepcopy(config),pid,rid)
        if not receipt:
            raise Conflict('原请求仍没有可用回执')
        if receipt.get('status')=='SUCCESS' and receipt.get('markdown') and not receipt.get('historical_readonly'):
            # Querying the same original result twice never creates two versions.
            if not any(v.get('request_id')==rid for v in p['processor']['versions']):
                processor.save_result(store,pid,receipt['markdown'],origin=request['channel'],request_id=rid)
        def finish(p):
            row = next(r for r in p['processor']['requests'] if r['id']==rid)
            row.update({k:v for k,v in receipt.items() if k not in {'markdown','pack_digest'}},finished=time.time())
        store.change(pid,finish)
        return detail(pid)
