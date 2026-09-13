"""One synthetic, bounded subscription trial; no automatic retries or acceptance."""
import json
from pathlib import Path
import time
from sourceloom.config import load_config
from sourceloom.store import Store
from sourceloom.ingest import intake
from sourceloom.checks import freeze, inspect_draft, review_complete
from sourceloom.pipeline import Pipeline


def main():
    out=Path('.local/live-trial-cli');out.mkdir(exist_ok=True)
    if (out/'project.json').exists():
        raise SystemExit('Trial already has an identity; inspect it instead of replaying')
    c=load_config();c['data_dir']=str(out/'data')
    store=Store(c['data_dir']);pipeline=Pipeline(store,c)
    p=store.create('独立角色真实小样：固定部分与局部加速',budget=0)
    raw=('这是专门为 SourceLoom 编写的合成材料，不是外部测量结论\n\n'
         '一个任务原来耗时 100 毫秒，其中固定部分为 20 毫秒，可加速部分为 80 毫秒\n\n'
         '若只有可加速部分的执行速度提高为原来的 2 倍，并且没有额外开销，则总耗时为 20 + 80 / 2 = 60 毫秒，不能据此说整个任务都快了 2 倍').encode()
    inv=intake(store,[('bounded-example.txt',raw)])
    store.change(p['id'],lambda p:p.update(inventory=inv,max_calls=4,goal='把合成材料做成一个且仅一个教学单元，保留所有条件、数值和否定，按七步从第一性原理讲清，总正文控制在 600 个中文字内'))
    (out/'project.json').write_text(json.dumps({'project':p['id'],'created':time.time(),'max_calls':4}),encoding='utf-8')
    started=time.monotonic();records=[]
    try:
        for role in ['inventory','planner','generator','reviewer']:
            if time.monotonic()-started>900:raise ValueError('Trial wall budget reached')
            p=store.get(p['id'])
            if role=='planner':store.change(p['id'],lambda p:p.update(inventory=freeze(p['inventory'])))
            if role=='generator' and len(p['plan']['units'])!=1:raise ValueError('Trial permits one generation call only')
            job=pipeline.start(p['id'],role)
            while True:
                job=store.job(job['id'])
                if job['status'] not in {'queued','running'}:break
                time.sleep(2)
            records.append({'role':role,'status':job['status'],'error':job.get('error'),'seconds':round(job.get('finished',time.time())-job['created'],1)})
            print(json.dumps(records[-1],ensure_ascii=False),flush=True)
            if job['status']!='completed':break
    finally:
        p=store.get(p['id'])
        report={'kind':'real subscription calls on original synthetic text','project':p['id'],'model':c['model'],'effort':c['effort'],
                'records':records,'elapsed_seconds':round(time.monotonic()-started,1),'calls':len(store.costs(p['id'])),
                'mechanical_findings':inspect_draft(p['inventory'],p['draft'],p['plan']) if p['draft'] else None,
                'independent_review_complete':review_complete(p) if p['draft'] else False,
                'accepted_by_user':False,'usage':[x['body'] for x in store.costs(p['id'])]}
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        (out/'artifacts.json').write_text(json.dumps(p,ensure_ascii=False,indent=2),encoding='utf-8')
        pipeline.executor.shutdown(wait=False,cancel_futures=True)

if __name__=='__main__':main()
