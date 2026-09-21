"""Private runtime configuration; public responses expose capabilities only."""

import json
import os
from pathlib import Path


def load_config():
    path = Path(os.environ.get("SOURCELOOM_CONFIG", ".local/config.json"))
    private = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    config={"data_dir":os.environ.get("SOURCELOOM_DATA", "data"),
            "provider":"manual", "provider_id":"", "model":"deepseek-flash", "effort":"medium",
            "base_url":"https://api.deepseek.com/v1", "api_key":"",
            "protocol":"chat_completions", "endpoint":"/responses",
            "codex_executable":"codex", "codex_ignore_user_config":True,
            "input_price":0.30, "output_price":1.20, "max_output_tokens":6000,
            "call_timeout":90, "job_timeout":900, "max_input_bytes":450000,
            "kuafu_safe_input_bytes":180000,
            "kuafu_streaming":True,
            "kuafu_stream_timeout":240,
            "kuafu_fallback_roles":[],
            "router_max_objective_chars":300000,
            "daily_budget_usd":None,"daily_call_limit":None,
            "provider_options":{},"role_options":{},"role_providers":{},"role_provider_overrides":{},
            "provider_routes":{},"provider_credentials":{},
            "total_budget_usd":None,"subscription_call_limit":None,
            "resume_failed_chat_writer_with_primary":False,
            "resume_failed_chat_review_with_primary":False,
            "resume_unqueryable_subscription_once":False,
            "resume_unqueryable_router_once":False,
            "max_repair_rounds":32,
            "max_plan_repairs":1,
            "active_revision_limit":1,
            "lightweight_v2":True,
            "worker_concurrency":2,
            "generation_pipeline":"active_composition_v2",
            "active_plan_source_chars":12000,"active_node_source_chars":6500,
            "active_v2_visual_batch_size":6,"active_v2_plan_source_chars":9000,
            "active_v2_plan_source_objects":128,"active_v2_node_source_chars":6500,
            "active_v2_node_source_objects":128,
            "active_node_concept_limit":16,
            "search_order":["tinyfish","octen","parallel"],
            "evidence_query_limit":2,"evidence_open_limit":4,
            "active_content_patch_limit":2,"active_format_patch_limit":2,
            "default_heading_numbering":"preserve","media_collapsed_default":True,"fx_rate":7.2,
            "active_resource_rounds":8,
            "external_worker":True,
            "writing_skill_dir":os.environ.get("HUMAN_READABLE_SKILL_DIR", ""),
            "auth_mode":"local", "allowed_subject":"", "public_origin":"",
            "readweave_url":"", "readweave_token":"", "readweave_parent":"",
            "readweave_registry_path":"",
            "fetch_enabled":False, **private}
    # Both the web process and the detached worker must use the same activated
    # provider snapshot.  Keeping this in load_config prevents the worker from
    # silently falling back to the older role routes after a service restart.
    from .provider_settings import apply_private_settings, load_private_settings
    return apply_private_settings(config,load_private_settings(config["data_dir"]))


def writing_snapshot(config):
    from .skills import instruction_files
    if not config.get('writing_skill_dir'):
        raise ValueError('真实生成需要指定完整中文写作技能目录')
    return instruction_files(config['writing_skill_dir'])
