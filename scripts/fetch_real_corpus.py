"""Fetch recorded public inputs privately; never call a model or report a quality pass."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path

import httpx


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--id',action='append',help='Fetch only these recorded material IDs')
    parser.add_argument('--output',type=Path,default=Path('.local/real-corpus'))
    args=parser.parse_args()
    manifest=Path(__file__).resolve().parents[1]/'evals/real-corpus.json'
    materials=json.loads(manifest.read_text(encoding='utf-8'))['materials']
    if args.id:
        unknown=set(args.id)-{m['id'] for m in materials}
        if unknown:parser.error('Unknown material ID: '+', '.join(sorted(unknown)))
        materials=[m for m in materials if m['id'] in args.id]
    args.output.mkdir(parents=True,exist_ok=True)

    def fetch(item):
        target=args.output/(item['id']+'.'+item['format'])
        report={'id':item['id'],'expected_sha256':item['sha256']}
        try:
            if target.exists():
                raw=target.read_bytes()
                status='existing'
            else:
                with httpx.stream('GET',item['url'],follow_redirects=True,timeout=45) as response:
                    response.raise_for_status();chunks=[];size=0
                    for chunk in response.iter_bytes():
                        size+=len(chunk)
                        if size>25*1024*1024:raise ValueError('download exceeds 25 MB')
                        chunks.append(chunk)
                    raw=b''.join(chunks)
                status='downloaded'
            actual=hashlib.sha256(raw).hexdigest()
            report.update(actual_sha256=actual,bytes=len(raw))
            if actual!=item['sha256']:
                report['status']='different_source_version'
                if not target.exists():
                    # Preserve a changed download separately, never relabel it as
                    # the frozen input used by the recorded trials.
                    variant=target.with_name(target.stem+'.'+actual[:12]+target.suffix)
                    if not variant.exists():variant.write_bytes(raw)
                return report
            if not target.exists():
                with target.open('xb') as output:output.write(raw)
            report['status']=status
        except (httpx.HTTPError,OSError,ValueError) as exc:
            report.update(status='failed',error=str(exc))
        return report

    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(fetch,materials))
    report={'scope':'source download only, no generation or quality verification','materials':results}
    (args.output/'download-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    return int(any(r['status'] not in {'existing','downloaded'} for r in results))


if __name__=='__main__':
    raise SystemExit(main())
