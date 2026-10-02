from fastapi.testclient import TestClient

from sourceloom.app import create_app
from sourceloom.config import load_config


def test_pdf_modules_worker_and_wasm_have_executable_mime(tmp_path):
    config = load_config() | {"data_dir": str(tmp_path / "data"), "auth_mode": "local"}
    with TestClient(create_app(config)) as client:
        for path in (
            "/static/vendor/pdfjs-6.3.289/legacy/build/pdf.mjs",
            "/static/vendor/pdfjs-6.3.289/legacy/build/pdf.worker.mjs",
        ):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["content-type"].split(";")[0] == "application/javascript"
            assert response.headers["x-content-type-options"] == "nosniff"


def test_product_reader_has_local_versioned_assets_and_no_external_runtime():
    from pathlib import Path

    static = Path(__file__).resolve().parents[1] / "sourceloom/static"
    html = (static / "processor.html").read_text(encoding="utf-8")
    import re

    for asset in ("processor_workbench.css", "processor.js"):
        assert re.search(r'/static/' + re.escape(asset) + r'\?v=[a-z0-9-]+"', html)
    pdf = (static / "processor_pdf.js").read_text(encoding="utf-8")
    assert "pdfjs-6.3.289" in pdf
    assert "https://cdn" not in pdf
    assert "window.getSelection" in pdf
