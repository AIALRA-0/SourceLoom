"""The clean reader must resume an unfinished user import after a restart."""

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from sourceloom.store import Store


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_reader(root, port):
    script = Path(__file__).resolve().parents[1] / "scripts/processor_user_entry.py"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [sys.executable, str(script), "--data", str(root), "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError("Reader exited during startup")
        try:
            if httpx.get(base + "/api/processor/projects", timeout=1).status_code == 200:
                return process, base
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    process.terminate()
    process.wait(timeout=5)
    raise AssertionError("Reader did not start")


def test_restart_keeps_unfinished_user_import(tmp_path):
    root = tmp_path / "user-data"
    Store(root)
    (root / "snapshot-manifest.json").write_text(
        json.dumps({"private_config_copied": False}), encoding="utf-8"
    )
    port = _free_port()
    process, base = _start_reader(root, port)
    try:
        headers = {"origin": base, "x-sourceloom": "1"}
        created = httpx.post(base + "/api/processor/projects", json={"title": "User import"}, headers=headers)
        assert created.status_code == 200, created.text
        project = created.json()
        uploaded = httpx.post(
            base + f"/api/processor/projects/{project['id']}/upload",
            files={"files": ("source.txt", b"A real source paragraph for later handoff.", "text/plain")},
            headers=headers,
            timeout=20,
        )
        assert uploaded.status_code == 200, uploaded.text
        assert not uploaded.json()["processor"]["versions"]
    finally:
        process.terminate()
        process.wait(timeout=10)

    process, base = _start_reader(root, port)
    try:
        projects = httpx.get(base + "/api/processor/projects").json()
        assert any(item["id"] == project["id"] for item in projects)
        detail = httpx.get(base + f"/api/processor/projects/{project['id']}")
        assert detail.status_code == 200
        assert not detail.json()["processor"]["versions"]
    finally:
        process.terminate()
        process.wait(timeout=10)
