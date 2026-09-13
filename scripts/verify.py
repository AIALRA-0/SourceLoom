"""One bounded deterministic suite; never invokes paid providers."""

import json
from pathlib import Path
import subprocess
import sys
import time


def main():
    start=time.monotonic()
    output=Path('.local/verification');output.mkdir(parents=True,exist_ok=True)
    command=[sys.executable,'-m','pytest','--junitxml='+str(output/'pytest.xml')]
    try:
        result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',timeout=120)
        report={'command':command,'exit_code':result.returncode,'elapsed_seconds':round(time.monotonic()-start,2),
                'scope':'deterministic synthetic tests; no real model calls','stdout':result.stdout,'stderr':result.stderr}
    except subprocess.TimeoutExpired:
        report={'exit_code':124,'elapsed_seconds':120,'error':'bounded verification deadline exceeded'}
    (output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    return report['exit_code']


if __name__=='__main__':
    raise SystemExit(main())
