"""Start the clean, local SourceLoom reader from a reviewed data snapshot."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True, help="Reviewed local snapshot; never a bundled personal store")
    parser.add_argument("--config", type=Path, help="Existing ignored private config outside the snapshot")
    parser.add_argument("--port", type=int, default=18802)
    args = parser.parse_args()
    root = args.data.resolve()
    manifest_file = root / "snapshot-manifest.json"
    if not manifest_file.is_file() or not (root / "state.sqlite3").is_file():
        raise SystemExit("Clean user snapshot is missing or unverified")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if manifest.get("private_config_copied") is not False:
        raise SystemExit("Snapshot must exclude private runtime configuration")
    with sqlite3.connect(root / "state.sqlite3") as db:
        projects = [json.loads(raw) for (raw,) in db.execute("SELECT body FROM projects")]
    if any("固定回执" in p.get("title", "") for p in projects):
        raise SystemExit("User snapshot contains a fixed test fixture")
    private_config = args.config.resolve() if args.config else None
    if private_config and (not private_config.is_file() or private_config.is_relative_to(root)):
        raise SystemExit("Private config must be an existing file outside the user snapshot")
    os.environ["SOURCELOOM_DATA"] = str(root)
    os.environ["SOURCELOOM_CONFIG"] = str(private_config or root / "no-private-config.json")
    os.environ.pop("SOURCELOOM_ACCEPTANCE", None)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from sourceloom.app import create_app
    from sourceloom.config import load_config
    import uvicorn
    config = load_config()
    config["data_dir"] = str(root)
    config["auth_mode"] = "local"
    if private_config is None:
        config.update(api_key="", readweave_url="", readweave_token="",
                      readweave_parent="", processor_provider={})
    print(f"Clean local SourceLoom entry: http://127.0.0.1:{args.port}/", flush=True)
    uvicorn.run(create_app(config), host="127.0.0.1", port=args.port, proxy_headers=False)


if __name__ == "__main__":
    main()
