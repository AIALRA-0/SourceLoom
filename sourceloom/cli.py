"""Local command entry point shares the application's storage and export code."""

import argparse
from pathlib import Path
import json

from .config import load_config
from .store import Store
from .demo import create_demo
from .export import export_zip


def main():
    parser=argparse.ArgumentParser(prog="sourceloom")
    sub=parser.add_subparsers(dest="command",required=True)
    serve=sub.add_parser("serve")
    serve.add_argument("--port",type=int,default=8765)
    sub.add_parser("demo")
    export=sub.add_parser("export")
    export.add_argument("project")
    export.add_argument("output")
    args=parser.parse_args()
    config=load_config()
    if args.command=="serve":
        import uvicorn
        from .app import create_app
        uvicorn.run(create_app(config),host="127.0.0.1",port=args.port,proxy_headers=False)
    else:
        store=Store(config["data_dir"])
        if args.command=="demo":
            print(json.dumps({k:v for k,v in create_demo(store).items() if k in {"id","title","state"}},ensure_ascii=False))
        elif args.command=="export":
            path=Path(args.output)
            raw=export_zip(store,store.get(args.project))
            with path.open("xb") as f:
                f.write(raw)
            print(str(path.resolve()))


if __name__=="__main__":
    main()
