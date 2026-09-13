"""Independent, bounded calls. Uncertain submissions are never replayed."""

import json
import time
import subprocess
from pathlib import Path
import httpx

from .store import Conflict, identity


class Uncertain(RuntimeError):
    pass


def strict_schema(schema):
    """Codex structured output requires every object property to be required."""
    if isinstance(schema,list):return [strict_schema(v) for v in schema]
    if not isinstance(schema,dict):return schema
    result={k:strict_schema(v) for k,v in schema.items() if k!='default'}
    if result.get('type')=='object' and 'properties' in result:
        result['required']=list(result['properties'])
        result['additionalProperties']=False
    return result


def parse_json(text):
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.split("\n", 1)[1].rsplit("```",1)[0].strip()
    return json.loads(text)


class Provider:
    def __init__(self, store, config):
        self.store, self.config = store, config

    def call(self, pid, role, payload, schema, job, cancelled=lambda:False):
        c = self.config
        instruction = (Path(__file__).parent / "roles" / f"{role}.md").read_text(encoding="utf-8")
        prompt = json.dumps(payload, ensure_ascii=False)
        input_bytes = len((instruction+prompt+json.dumps(schema)).encode())
        if input_bytes > c["max_input_bytes"]:
            raise Conflict("当前角色输入超过范围上限，请按教学单元拆分；没有截断原文")
        if cancelled():
            raise Conflict("任务已取消")
        call_id = identity()
        reserve = (input_bytes*c["input_price"] + c["max_output_tokens"]*c["output_price"])/1e6 if c["provider"]=="openai-compatible" else 0
        self.store.reserve(pid, call_id, reserve, dict(role=role,model=c["model"],input_bytes=input_bytes,channel=c["provider"]),
                           c.get('daily_budget_usd',2.0),c.get('daily_call_limit',80))
        job["calls"].append(dict(id=call_id,role=role,status="submitted",channel=c['provider'],
                                 unit_id=job.get('current_unit_id'),
                                 upstream_base=c['base_url'] if c['provider']=='router' else None))
        self.store.put_job(job)
        headers = {"Authorization":"Bearer "+c["api_key"], "Content-Type":"application/json"}
        try:
            if c["provider"] == "codex-cli":
                work=self.store.root/'calls'/call_id
                work.mkdir(parents=True)
                schema_file=work/'response-schema.json';result_file=work/'result.json'
                schema_file.write_text(json.dumps(strict_schema(schema)),encoding='utf-8')
                args=[c['codex_executable'],'exec','--ephemeral','--skip-git-repo-check','--sandbox','read-only',
                      '--json','--color','never','--model',c['model'],'-c','model_reasoning_effort="'+c['effort']+'"',
                      '--output-schema',str(schema_file),'--output-last-message',str(result_file),'-']
                if c.get('codex_ignore_user_config',True):args.insert(2,'--ignore-user-config')
                # This channel uses existing login only. It does not bypass failed isolation.
                try:
                    with (work/'events.jsonl').open('wb') as stdout,(work/'stderr.txt').open('wb') as stderr:
                        proc=subprocess.run(args,input=(instruction+'\nReturn only the requested JSON. Do not use tools, delegate, or access files.\n'+prompt).encode(),
                                            cwd=work,stdout=stdout,stderr=stderr,timeout=c['call_timeout'],
                                            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess,'CREATE_NO_WINDOW') else 0)
                except subprocess.TimeoutExpired:
                    raise Uncertain('本机 Codex 达到等待上限，保留事件与原结果，不自动重发') from None
                if proc.returncode or not result_file.exists():
                    raise Uncertain('本机 Codex 未成功返回，诊断保存在本地调用目录')
                usage=None
                for line in (work/'events.jsonl').read_text(encoding='utf-8',errors='replace').splitlines():
                    try:event=json.loads(line)
                    except ValueError:continue
                    if event.get('type')=='turn.completed':usage=event.get('usage')
                self.store.settle(call_id,0,dict(channel='subscription',usage=usage,billing_note='订阅用量未折算金额'))
                job['calls'][-1].update(status='completed',artifact_dir=str(work))
                self.store.put_job(job)
                return parse_json(result_file.read_text(encoding='utf-8'))
            if c["provider"] == "openai-compatible":
                options=c.get('provider_options',{})|c.get('role_options',{}).get(role,{})
                if set(options)-{'thinking','reasoning_effort'}:raise ValueError('通道选项只能配置思考模式与程度')
                messages = [{"role":"system","content":instruction+"\nReturn only JSON matching this schema:\n"+json.dumps(schema)},
                            {"role":"user","content":prompt}]
                with httpx.Client(timeout=c["call_timeout"],follow_redirects=False) as client:
                    response = client.post(c["base_url"].rstrip("/")+"/chat/completions",headers=headers,
                        json=dict(model=c["model"], messages=messages,max_tokens=c["max_output_tokens"],response_format={"type":"json_object"})|options)
                if response.status_code >= 400:
                    # Response bodies can contain account details; keep only the status.
                    raise Uncertain(f"模型请求返回 {response.status_code}，本次未自动重发")
                body = response.json()
                # Keep the exact returned artifact even when it is truncated or malformed.
                job['calls'][-1].update(response_blob=self.store.blob(json.dumps(body,ensure_ascii=False).encode()),
                                        finish_reason=body.get('choices',[{}])[0].get('finish_reason'))
                self.store.put_job(job)
                usage = body.get("usage", {})
                actual = ((usage["prompt_tokens"]*c["input_price"] + usage["completion_tokens"]*c["output_price"])/1e6
                          if "prompt_tokens" in usage and "completion_tokens" in usage else None)
                self.store.settle(call_id,actual,dict(usage=usage,model=body.get("model"),channel=c["provider"],
                    options=options,cost_measurement='usage multiplied by configured conservative rates; not a supplier invoice'))
                if body["choices"][0].get("finish_reason") != "stop":
                    raise ValueError("模型输出未正常结束，已保存用量，不接受截断候选")
                return parse_json(body["choices"][0]["message"]["content"])
            if c["provider"] == "router":
                task = dict(objective=instruction+"\nReturn only the requested JSON artifact. Do not use tools, delegate, or access files.\n"+prompt,taskKind="bounded",model=c["model"],effort=c["effort"],
                            sessionMode="ephemeral",deadlineMs=int(c["call_timeout"]*1000),replayable=False,
                            validation={"responseSchema":schema,"checks":[],"acceptanceTests":[]},
                            permissions={"preset":"restricted","filesystem":"read","network":"none","allowedHosts":[],
                                         "requireApprovalForWrites":False,"requireApprovalForExternalActions":False},
                            budget={"maxOutputTokens":c["max_output_tokens"],"maxAttempts":1},
                            expectedOutput="A JSON object conforming exactly to validation.responseSchema")
                with httpx.Client(timeout=25,follow_redirects=False) as client:
                    response = client.post(c["base_url"].rstrip("/")+"/api/v1/jobs",headers=headers | {"Idempotency-Key":call_id},
                                           json={"task":task,"metadata":{"project":"sourceloom","role":role,"call":call_id}})
                    if response.status_code >= 400:
                        if response.status_code in {400,401,403,422}:
                            self.store.settle(call_id,0,dict(channel='router',status='rejected',http_status=response.status_code))
                            try:code=response.json().get('error',{}).get('code','request_rejected')
                            except (ValueError,AttributeError):code='request_rejected'
                            raise ValueError('转发器未接单：'+str(code)[:80])
                        raise Uncertain(f"转发器返回 {response.status_code}，保留原调用身份")
                    body = response.json()
                    jid = body.get("id",body.get("jobId"))
                    if not jid:
                        raise Uncertain("转发器未返回可查询任务身份")
                    job["calls"][-1]["upstream_id"] = jid
                    self.store.put_job(job)
                    start = time.monotonic()
                    while time.monotonic()-start < c["call_timeout"]:
                        if cancelled():
                            client.post(c["base_url"].rstrip("/")+f"/api/v1/jobs/{jid}/cancel",headers=headers | {"Idempotency-Key":call_id+"-cancel"})
                            raise Uncertain("已请求取消，原上游任务身份保留")
                        response = client.get(c["base_url"].rstrip("/")+f"/api/v1/jobs/{jid}",headers=headers)
                        response.raise_for_status()
                        body = response.json()
                        if body["status"] in {"succeeded","completed"}:
                            # The deployed controller returns output on GET /jobs/:id.
                            # Older prose mentions /result, but that route does not exist.
                            self.store.settle(call_id,0,dict(channel="subscription",usage=body.get("usage"),upstream_id=jid,
                                                           billing_note="未折算订阅额度，不表示免费或无限"))
                            job["calls"][-1]["status"] = "completed"
                            self.store.put_job(job)
                            content = body.get("output")
                            if isinstance(content,dict):
                                content = content.get("structured",content.get("text",content))
                            return parse_json(content) if isinstance(content,str) else content
                        if body["status"] in {"failed","cancelled","timed_out","expired","awaiting_approval"}:
                            raise Uncertain("上游未成功完成，状态："+body["status"])
                        time.sleep(2)
                raise Uncertain("达到本次等待上限，继续查询原任务，不自动重发")
            raise ValueError("当前为人工任务包通道，请导出任务包后导回结果")
        except (httpx.HTTPError, Uncertain) as exc:
            self.store.settle(call_id,None,dict(role=role,channel=c["provider"],error=type(exc).__name__))
            raise Uncertain(str(exc) if isinstance(exc,Uncertain) else "上游结果不确定，原调用及预留费用保留") from None

    def recover(self, job):
        """Query the existing call only; never submit a replacement request."""
        call=job['calls'][-1]
        channel=call.get('channel',self.config['provider'])
        if channel=='codex-cli':
            work=self.store.root/'calls'/call['id']
            if not (work/'result.json').exists():return None
            events=(work/'events.jsonl').read_text(encoding='utf-8',errors='replace')
            if not any('turn.completed' in line for line in events.splitlines()):return None
            result=parse_json((work/'result.json').read_text(encoding='utf-8'))
        elif channel=='router':
            if not call.get('upstream_id'):
                raise Conflict('上游未返回任务身份，请通过转发器原始记录核对接单状态')
            if call.get('upstream_base')!=self.config['base_url']:
                raise Conflict('模型通道地址已变化，请恢复原配置后查询')
            with httpx.Client(timeout=20,follow_redirects=False) as client:
                r=client.get(self.config['base_url'].rstrip('/')+'/api/v1/jobs/'+call['upstream_id'],
                             headers={'Authorization':'Bearer '+self.config['api_key']})
                r.raise_for_status();body=r.json()
            if body['status']!='succeeded':
                if body['status'] in {'failed','cancelled','expired'}:
                    raise ValueError('原上游任务已结束：'+body['status'])
                return None
            result=body['output']
            if isinstance(result,dict):result=result.get('structured',result.get('text',result))
            if isinstance(result,str):result=parse_json(result)
        elif channel=='openai-compatible' and call.get('response_blob'):
            body=json.loads(self.store.read_blob(call['response_blob']))
            if body['choices'][0].get('finish_reason')!='stop':
                raise Conflict('原响应已保存但被截断，不能当作完整结果恢复')
            result=parse_json(body['choices'][0]['message']['content'])
        else:
            raise Conflict('该通道没有任务查询接口，请导回原调用结果或保留暂停状态')
        # Unknown subscription consumption stays unknown even when content is recovered.
        call['status']='recovered';self.store.put_job(job)
        return result
