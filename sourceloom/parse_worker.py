"""A document parser has a separate lifetime and, on Linux, OS resource limits."""
import json
from pathlib import Path
import subprocess
import sys
from .store import Store, identity
from .ingest import intake, MAX_EXPANDED


def isolated_intake(store, uploads, source_url=None, timeout=75, asset_aliases=None):
    if sum(len(raw) for _,raw in uploads)>MAX_EXPANDED:
        raise ValueError('本次上传超过 100 MB 总量限制')
    work=store.root/'intake'/identity();work.mkdir(parents=True)
    request={'root':str(store.root),'uploads':[(n,store.blob(r)) for n,r in uploads],'source_url':source_url,'asset_aliases':asset_aliases}
    (work/'request.json').write_text(json.dumps(request),encoding='utf-8')
    try:
        run=subprocess.run([sys.executable,'-m','sourceloom.parse_worker',str(work)],
            capture_output=True,timeout=timeout,creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
    except subprocess.TimeoutExpired:
        raise ValueError('接入超过 75 秒上限，原始字节已保存，请缩小材料范围') from None
    if run.returncode or not (work/'result.json').exists():
        raise ValueError('文档解析中断或超过资源限制，原始字节已保存')
    result=json.loads((work/'result.json').read_text(encoding='utf-8'))
    if 'error' in result:raise ValueError(result['error'])
    return result['inventory']


def main():
    if sys.platform=='linux':
        import resource
        resource.setrlimit(resource.RLIMIT_AS,(768*1024*1024,768*1024*1024))
        resource.setrlimit(resource.RLIMIT_CPU,(60,60))
    work=Path(sys.argv[1]);request=json.loads((work/'request.json').read_text(encoding='utf-8'))
    store=Store(request['root'])
    try:
        result={'inventory':intake(store,[(n,store.read_blob(k)) for n,k in request['uploads']],request['source_url'],request.get('asset_aliases'))}
    except Exception as exc:
        result={'error':str(exc)[:250] if isinstance(exc,ValueError) else '材料结构无法完整解析，原件已保存'}
    (work/'result.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')

if __name__=='__main__':main()
