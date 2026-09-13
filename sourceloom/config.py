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
            "call_timeout":180, "job_timeout":1200, "max_input_bytes":90000,
            "daily_budget_usd":2.0,"daily_call_limit":80,
            "provider_options":{},"role_options":{},
            "writing_skill_dir":os.environ.get("HUMAN_READABLE_SKILL_DIR", ""),
            "auth_mode":"local", "allowed_subject":"", "public_origin":"",
            "readweave_url":"", "readweave_token":"", "readweave_parent":"",
            "fetch_enabled":False, **private}


def writing_snapshot(config):
    root = Path(config["writing_skill_dir"])
    files = ["SKILL.md", "references/format-rules.md", "references/explanation-framework.md", "references/formula-explanation.md"]
    if not config["writing_skill_dir"] or any(not (root/f).is_file() for f in files):
        raise ValueError("真实生成需要指定完整中文写作技能目录")
    return {name:(root/name).read_text(encoding="utf-8-sig") for name in files}
