"""At most one repair and one independent recheck of the already saved trial."""
import json
from pathlib import Path
import time
from sourceloom.config import load_config
from sourceloom.store import Store
from sourceloom.pipeline import Pipeline
from sourceloom.checks import review_complete,inspect_draft
from sourceloom.export import export_zip


def main():
    out=Path('.local/live-trial-cli')
    marker=out/'repair-start.json'
    if marker.exists():raise SystemExit('This bounded repair already has an identity')
    pid=json.loads((out/'project.json').read_text(encoding='utf-8'))['project']
    config=load_config();store=Store(out/'data');pipe=Pipeline(store,config)
    if len(store.costs(pid))!=4:raise SystemExit('Unexpected original call count')
    store.change(pid,lambda p:p.update(max_calls=6))
    marker.write_text(json.dumps({'project':pid,'created':time.time(),'additional_call_cap':2}),encoding='utf-8')
    records=[];start=time.monotonic()
    try:
        for role in ['repairer','reviewer']:
            job=pipe.start(pid,role)
            while True:
                job=store.job(job['id'])
                if job['status'] not in {'queued','running'}:break
                time.sleep(2)
            record={k:job.get(k) for k in ['role','status','error']};records.append(record)
            print(json.dumps(record,ensure_ascii=False),flush=True)
            if job['status']!='completed':break
    finally:
        p=store.get(pid)
        report={'records':records,'calls_total':len(store.costs(pid)),'elapsed_seconds':round(time.monotonic()-start,1),
                'review_complete':review_complete(p),'mechanical_findings':inspect_draft(p['inventory'],p['draft'],p['plan']),
                'accepted_by_user':False,'usage':[c['body'] for c in store.costs(pid)]}
        (out/'repair-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        (out/'repaired-artifacts.json').write_text(json.dumps(p,ensure_ascii=False,indent=2),encoding='utf-8')
        (out/'ReadWeave-real-candidate.zip').write_bytes(export_zip(store,p))
        pipe.executor.shutdown(wait=False,cancel_futures=True)

if __name__=='__main__':main()
