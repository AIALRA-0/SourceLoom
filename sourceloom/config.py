"""Private runtime configuration; public responses expose capabilities only."""

import json
import os
from pathlib import Path


def load_config():
    path = Path(os.environ.get("SOURCELOOM_CONFIG", ".local/config.json"))
    private = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    return {"data_dir":os.environ.get("SOURCELOOM_DATA", "data"),
            "provider":"manual", "model":"deepseek-flash", "effort":"medium",
            "base_url":"https://api.deepseek.com/v1", "api_key":"",
            "codex_executable":"codex", "codex_ignore_user_config":True,
            "input_price":0.30, "output_price":1.20, "max_output_tokens":6000,
            "call_timeout":180, "job_timeout":0, "max_input_bytes":450000,
            "router_max_objective_chars":300000,
            "daily_budget_usd":2.0,"daily_call_limit":80,
            "provider_options":{},"role_options":{},"role_providers":{},
            "total_budget_usd":10.0,"subscription_call_limit":None,
            "resume_failed_chat_writer_with_primary":False,
            "resume_failed_chat_review_with_primary":False,
            "resume_unqueryable_subscription_once":False,
            "resume_unqueryable_router_once":False,
            "max_repair_rounds":32,
            "max_plan_repairs":6,
            "worker_concurrency":2,
            "generation_pipeline":"active_composition_v1",
            "active_plan_source_chars":12000,"active_node_source_chars":6500,
            "active_resource_rounds":8,
            "active_revision_limit":2,
            "external_worker":True,
            "writing_skill_dir":os.environ.get("HUMAN_READABLE_SKILL_DIR", ""),
            "auth_mode":"local", "allowed_subject":"", "public_origin":"",
            "readweave_url":"", "readweave_token":"", "readweave_parent":"",
            "fetch_enabled":False, **private}


def writing_snapshot(config):
    from .skills import instruction_files
    if not config.get('writing_skill_dir'):
        raise ValueError('真实生成需要指定完整中文写作技能目录')
    return instruction_files(config['writing_skill_dir'])
