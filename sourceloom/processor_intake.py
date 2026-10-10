"""Bounded background preparation for the existing material upload endpoint."""
import time
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore

from . import processor
from .store import Conflict

ACTIVE_PHASES = {'queued', 'saving_original', 'parsing_file', 'pdf_pages',
                 'extracting_resources', 'building_index', 'fetching_url'}


class MaterialIntake:
    def __init__(self, store):
        self.store = store
        self.slots = BoundedSemaphore(3)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='material-intake')
        # Upload bytes survive restart; preparation is resumed only by an
        # explicit action, never by silently repeating a model request.
        for saved in store.list():
            state = saved.get('processor') or {}
            if (state.get('intake_progress') or {}).get('phase') in ACTIVE_PHASES:
                self.fail(saved['id'], '服务已重启，准备中断；已上传原件保留，可继续准备')

    def fail(self, pid, message):
        self.store.change(pid, lambda p: p['processor'].update(intake_progress={
            **(p['processor'].get('intake_progress') or {}),
            'phase': 'failed', 'detail': str(message)[:240], 'updated': time.time(),
            'resumable': bool(p['processor'].get('intake_uploads'))}))

    def submit(self, pid, uploads=None, *, resume=False):
        current = processor.project(self.store, pid)
        state = current['processor']
        if current.get('trashed'):
            raise Conflict('请先恢复回收站材料')
        phase = (state.get('intake_progress') or {}).get('phase')
        if phase in ACTIVE_PHASES:
            if resume:
                return {'id': pid, 'intake_progress': state['intake_progress'], 'reused': True}
            raise Conflict('这份材料正在准备，请查询原进度，不要重复上传')
        if current.get('inventory') and not resume:
            raise Conflict('原件已冻结；不同材料请建立新项目')
        if not self.slots.acquire(blocking=False):
            raise Conflict('材料准备队列已满，请稍后再提交；已有任务继续处理')
        reserved = False
        try:
            inputs = state.get('intake_uploads') if resume else [
                {'name': name, 'sha256': self.store.blob(raw), 'size': len(raw)} for name, raw in uploads]
            if not inputs:
                raise Conflict('没有可继续准备的原件，请选择文件')
            def reserve(p):
                if (p['processor'].get('intake_progress') or {}).get('phase') in ACTIVE_PHASES:
                    raise Conflict('这份材料已经在准备，未重复启动')
                if p.get('trashed') or (p.get('inventory') and not resume):
                    raise Conflict('当前材料不可重新上传')
                p['processor'].update(intake_uploads=inputs, intake_progress={
                    'phase': 'queued', 'completed': 0, 'total': len(inputs),
                    'detail': '原件已接收，等待准备', 'started': time.time(), 'updated': time.time()})
            p = self.store.change(pid, reserve)
            reserved = True
            self.pool.submit(self.run, pid, inputs)
            return {'id': pid, 'intake_progress': p['processor']['intake_progress']}
        except Exception as error:
            if reserved:
                self.fail(pid, error)
            self.slots.release()
            raise

    def run(self, pid, inputs):
        try:
            current = processor.project(self.store, pid)
            if current.get('inventory'):
                pack = processor.task_pack(self.store, pid)
                def complete(p):
                    if p.get('trashed'):
                        raise Conflict('材料已移入回收站，未继续准备')
                    p['processor'].update(pack_digest=pack['digest'], policy_digest=pack['template_digest'],
                                          intake_progress={'phase': 'complete', 'updated': time.time()})
                self.store.change(pid, complete)
            else:
                processor.prepare(self.store, pid, [(row['name'], self.store.read_blob(row['sha256'])) for row in inputs])
        except Exception as error:
            self.fail(pid, error)
        finally:
            self.slots.release()

    def close(self):
        self.pool.shutdown(wait=True)
