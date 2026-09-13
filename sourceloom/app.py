"""Single-user application; production requires a trusted authenticated proxy."""

from contextlib import asynccontextmanager
import copy
import json
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .checks import apply_patch, freeze, inspect_draft, release_issues, review_complete
from .config import load_config
from .demo import create_demo
from .export import export_zip, render
from .ingest import intake, MAX_FILE, SAFE_IMAGE
from .network import fetch
from .pipeline import Pipeline, SCHEMAS
from .parse_worker import isolated_intake
from .store import Store, Conflict, digest
from .versions import append_intake, append_obligations, pending_obligations


class Create(BaseModel):
    model_config=ConfigDict(extra="forbid")
    title:str=Field(min_length=1,max_length=180)
    goal:str=Field(default="完整保留材料，并从必要前提逐步讲清",max_length=2000)
    mode:str="rewrite"
    budget_usd:float=Field(default=0.5,ge=0,le=20)


def create_app(config=None):
    config=config or load_config()
    store=Store(config["data_dir"])
    pipeline=Pipeline(store,config)
    @asynccontextmanager
    async def lifespan(app):
        yield
        pipeline.executor.shutdown(wait=False,cancel_futures=True)
    app=FastAPI(title="SourceLoom",version="0.1.0",docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    app.state.store=store
    app.state.pipeline=pipeline

    @app.middleware("http")
    async def security(request:Request,call_next):
        if request.url.path!="/health":
            if config["auth_mode"]=="proxy":
                if (request.client.host not in {"127.0.0.1","::1"}
                    or request.headers.get("x-aialra-authenticated")!="1"
                    or not config["allowed_subject"]
                    or request.headers.get("x-aialra-sub")!=config["allowed_subject"]):
                    return JSONResponse({"error":"请通过已授权的登录入口访问"},status_code=401)
            elif request.client.host not in {"127.0.0.1","::1","testclient"}:
                return JSONResponse({"error":"本地模式仅允许当前电脑访问"},status_code=403)
            if request.method not in {"GET","HEAD","OPTIONS"}:
                origin=request.headers.get("origin")
                expected=config.get("public_origin") or str(request.base_url).rstrip("/")
                if (origin and origin!=expected) or request.headers.get("x-sourceloom")!="1":
                    return JSONResponse({"error":"请求来源或操作标记无效"},status_code=403)
                length=request.headers.get("content-length")
                if length and int(length)>MAX_FILE*4:
                    return JSONResponse({"error":"本次上传超过大小限制"},status_code=413)
        response=await call_next(request)
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["Referrer-Policy"]="no-referrer"
        response.headers["Cache-Control"]="no-store"
        if "Content-Security-Policy" not in response.headers:
            response.headers["Content-Security-Policy"]="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'"
        return response

    @app.exception_handler(Conflict)
    async def conflict(_,exc):
        return JSONResponse({"error":str(exc)},status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(_,exc):
        return JSONResponse({"error":str(exc)[:400]},status_code=400)

    @app.exception_handler(KeyError)
    async def missing(_,exc):
        return JSONResponse({"error":"材料或任务不存在"},status_code=404)

    @app.get("/health")
    def health():
        return {"status":"ok","version":"0.1.0"}

    @app.get("/api/config")
    def public_config():
        return {"provider":config["provider"],"model":config["model"],"effort":config["effort"],
                "writing_policy_ready":bool(config["writing_skill_dir"]),"fetch_enabled":config["fetch_enabled"],
                "daily_budget_usd":config['daily_budget_usd'],"daily_call_limit":config['daily_call_limit'],
                "readweave_configured":bool(config["readweave_url"] and config["readweave_token"] and config["readweave_parent"])}

    @app.get("/api/projects")
    def projects():
        return [{k:p.get(k) for k in ["id","title","mode","state","created","revision","active_job","synthetic"]} for p in store.list()]

    @app.post("/api/projects")
    def create(body:Create):
        if body.mode not in {"rewrite","research"}:
            raise ValueError("选择保真改写或调查成教材")
        p=store.create(body.title,body.mode,body.budget_usd)
        return store.change(p["id"],lambda p:p.update(goal=body.goal))

    @app.post("/api/demo")
    def demo():
        return create_demo(store)

    @app.get("/api/projects/{pid}")
    def detail(pid:str):
        p=store.get(pid)
        p["mechanical_findings"]=inspect_draft(p["inventory"],p["draft"],p["plan"]) if p["draft"] else []
        p["release_issues"]=release_issues(p) if p["draft"] else []
        p["costs"]=store.costs(pid)
        p["jobs"]=store.jobs(pid)
        p["events"]=store.events(pid)
        return p

    def assign_inventory(pid,inv):
        def update(p):
            if p["active_job"]:
                raise Conflict("角色正在执行，稍后追加材料")
            if p['inventory']:
                p.update(inventory=append_intake(p['inventory'],inv),revision=p['revision']+1,state='inventoried')
            else:p.update(inventory=inv,state="inventoried")
        p=store.change(pid,update)
        store.event(pid,"intake",dict(objects=len(inv["objects"]),unknown=len(inv["unknown"])))
        return p

    @app.post("/api/projects/{pid}/upload")
    def upload(pid:str,files:list[UploadFile]=File(...)):
        store.get(pid)
        if len(files)>100:
            raise ValueError("单次最多上传 100 个文件")
        uploads=[]
        total=0
        for file in files:
            raw=file.file.read(MAX_FILE+1)
            total+=len(raw)
            if len(raw)>MAX_FILE or total>MAX_FILE*4:
                raise ValueError('单文件最多 25 MB，本次最多 100 MB')
            uploads.append((file.filename or "material.txt",raw))
        return assign_inventory(pid,isolated_intake(store,uploads))

    @app.post("/api/projects/{pid}/url")
    def import_url(pid:str,body:dict):
        if not config["fetch_enabled"]:
            raise Conflict("当前运行配置未启用网页获取，可以上传保存的网页")
        raw,mime,url=fetch(str(body.get("url", "")))
        suffix={"text/html":"html","text/plain":"txt","application/pdf":"pdf"}[mime]
        return assign_inventory(pid,isolated_intake(store,[("snapshot."+suffix,raw)],source_url=url))

    @app.post("/api/projects/{pid}/freeze")
    def freeze_inventory(pid:str):
        def update(p):
            if p["active_job"] or not p["inventory"] or p["inventory"]["frozen"]:
                raise Conflict("清单不存在、已冻结或角色仍在执行")
            if p.get('incremental_pending') and p['baseline'].get('draft') and not pending_obligations(p):
                raise Conflict('补漏版本至少需要一项新增材料义务')
            p["inventory"]=freeze(p["inventory"])
            p["state"]="frozen"
        return store.change(pid,update)

    @app.post('/api/projects/{pid}/revise')
    def revise(pid:str,body:dict):
        return store.revise_sources(pid,body['revision'],body['inventory_digest'],str(body.get('reason',''))[:2000])

    @app.get('/api/projects/{pid}/source-versions')
    def source_versions(pid:str):
        store.get(pid)
        return store.source_versions(pid)

    @app.post('/api/projects/{pid}/obligations')
    def add_obligations(pid:str,body:dict):
        def update(p):
            if p['active_job'] or not p['inventory']:raise Conflict('先接入材料并等待角色结束')
            append_obligations(p['inventory'],body['additions'])
            p['revision']+=1
        return store.change(pid,update,body['revision'])

    @app.post("/api/projects/{pid}/run/{role}")
    def run(pid:str,role:str):
        return pipeline.start(pid,role)

    @app.get("/api/projects/{pid}/task-pack/{role}")
    def task_pack(pid:str,role:str):
        if role not in SCHEMAS:
            raise ValueError("未知角色")
        raw=json.dumps(pipeline.task_pack(pid,role),ensure_ascii=False,indent=2).encode()
        return Response(raw,media_type="application/json",headers={"Content-Disposition":f'attachment; filename="sourceloom-{role}.json"'})

    @app.post("/api/projects/{pid}/task-result/{role}")
    def task_result(pid:str,role:str,body:dict):
        if role not in SCHEMAS:
            raise ValueError("未知角色")
        result=pipeline.commit(pid,role,body["result"],body["base_revision"],body["inventory_digest"])
        return result

    @app.post("/api/jobs/{jid}/cancel")
    def cancel(jid:str):
        return pipeline.cancel(jid)

    @app.post('/api/jobs/{jid}/recover')
    def recover(jid:str):
        return pipeline.recover(jid)

    @app.post('/api/jobs/{jid}/resume')
    def resume(jid:str):
        return pipeline.resume(jid)

    @app.post("/api/projects/{pid}/patch")
    def patch(pid:str,body:dict):
        allowed=body.get("allowed_block_ids",[])
        def update(p):
            if p["active_job"] or p.get('incremental_pending'):
                raise Conflict("角色执行中，稍后再修改")
            draft=apply_patch(p,body["patch"],set(allowed))
            p.update(draft=draft,revision=p["revision"]+1,repair_rounds=p["repair_rounds"]+1,review=None,accepted_revision=None,state="generated")
        return store.change(pid,update,body["patch"]["base_revision"])

    @app.post("/api/projects/{pid}/demo-break")
    def demo_break(pid:str):
        def update(p):
            if not p.get("synthetic") or p.get("demo_removed") or p["active_job"]:
                raise Conflict("仅对没有运行任务的合成演示注入一次缺口")
            index=next(i for i,b in enumerate(p["draft"]["blocks"]) if "额外开销" in b["markdown"] and b["kind"]=="source")
            p["demo_removed"]={"index":index,"block":p["draft"]["blocks"].pop(index),"revision":p["revision"]+1}
            p.update(revision=p["revision"]+1,review=None,accepted_revision=None)
        return store.change(pid,update)

    @app.post("/api/projects/{pid}/demo-restore")
    def demo_restore(pid:str):
        def update(p):
            removed=p.get("demo_removed")
            if not p.get("synthetic") or not removed or p["revision"]!=removed["revision"]:
                raise Conflict("没有可安全恢复的当前演示补丁")
            p["draft"]["blocks"].insert(removed["index"],removed["block"])
            p.update(demo_removed=None,revision=p["revision"]+1,review=None,accepted_revision=None)
        return store.change(pid,update)

    @app.post("/api/projects/{pid}/accept")
    def accept(pid:str,body:dict):
        def update(p):
            if p["active_job"] or inspect_draft(p["inventory"],p["draft"],p["plan"]) or not review_complete(p):
                raise Conflict("当前版本尚未完成结构与独立语义审核，不能登记交付接受")
            p["accepted_revision"]=p["revision"]
        p=store.change(pid,update,body["revision"])
        store.event(pid,"user_acceptance",dict(revision=p["revision"],feedback=body.get("feedback", "")))
        return p

    @app.get("/api/projects/{pid}/preview")
    def preview(pid:str):
        p=store.get(pid)
        content=render(p,lambda key:f"/api/projects/{pid}/assets/{key}")
        return HTMLResponse('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><link rel="stylesheet" href="/static/article.css"></head><body>'+content+"</body></html>",
                            headers={"Content-Security-Policy":"sandbox allow-same-origin; default-src 'none'; img-src 'self'; style-src 'self'; frame-ancestors 'self'"})

    @app.get("/api/projects/{pid}/assets/{key}")
    def asset(pid:str,key:str):
        p=store.get(pid)
        resource=next((r for r in p["inventory"]["resources"] if r["id"]==key),None)
        if not resource:
            raise KeyError(key)
        raw=store.read_blob(key)
        return Response(raw,media_type=resource["mime"] if resource["mime"] in SAFE_IMAGE else "application/octet-stream",
                        headers={"Content-Disposition":"inline" if resource["mime"] in SAFE_IMAGE else 'attachment; filename="source-object.bin"'})

    @app.get("/api/projects/{pid}/export")
    def export(pid:str,release:bool=False):
        return Response(export_zip(store,store.get(pid),release),media_type="application/zip",
                        headers={"Content-Disposition":'attachment; filename="SourceLoom-ReadWeave-Candidate.zip"'})

    @app.post('/api/projects/{pid}/readweave')
    def send_readweave(pid:str):
        from .readweave import import_candidate
        with pipeline.lock:
            if store.get(pid)['active_job']:raise Conflict('角色执行中，稍后导入')
            return import_candidate(store,config,pid)

    @app.get("/")
    def index():
        return FileResponse(Path(__file__).parent/"static"/"index.html")

    examples=store.root/"examples"
    examples.mkdir(exist_ok=True)
    app.mount("/examples",StaticFiles(directory=examples,html=True),name="examples")
    app.mount("/static",StaticFiles(directory=Path(__file__).parent/"static"),name="static")
    return app
