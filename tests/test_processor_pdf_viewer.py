"""Fixed offline PDF display distribution: core, worker, CSS and font assets."""
import hashlib
import json
from pathlib import Path


VENDOR = Path(__file__).parents[1] / "sourceloom/static/vendor/pdfjs-6.3.289"


def test_pdfjs_fixed_distribution_matches_saved_manifest():
    manifest = json.loads((VENDOR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "6.3.289"
    assert manifest["runtime_remote_assets"] is False
    assert manifest["license"] == "Apache-2.0"
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == digest, name


def test_pdfjs_core_worker_and_styles_are_same_fixed_release():
    package = json.loads((VENDOR / "package.json").read_text(encoding="utf-8"))
    assert package["version"] == "6.3.289"
    for name in ("legacy/build/pdf.mjs", "legacy/build/pdf.worker.mjs"):
        content = (VENDOR / name).read_text(encoding="utf-8")
        assert "6.3.289" in content
    assert (VENDOR / "web/pdf_viewer.css").is_file()
    assert list((VENDOR / "cmaps").glob("*.bcmap"))
    assert list((VENDOR / "standard_fonts").glob("*.pfb"))
    assert list((VENDOR / "wasm").glob("*.wasm"))


def test_pdfjs_local_dependency_included_by_package_configuration():
    import tomllib

    config = tomllib.loads((VENDOR.parents[3] / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = config["tool"]["setuptools"]["package-data"]["sourceloom"]
    from fnmatch import fnmatch

    for relative in ("static/vendor/pdfjs-6.3.289/legacy/build/pdf.mjs", "static/vendor/pdfjs-6.3.289/cmaps/Adobe-CNS1-UCS2.bcmap"):
        assert any(fnmatch(relative, pattern) for pattern in patterns), relative


def _exercise_region_renderer(placement):
    """Execute the real renderer method with a bounded canvas/display stand-in."""
    import shutil
    import subprocess

    module = (VENDOR.parents[1] / "processor_pdf.js").read_text(encoding="utf-8")
    method = module.split("  async renderRegion(", 1)[1].split("\n  goToRegion(", 1)[0]
    method = "async renderRegion(" + method.strip()
    node = shutil.which("node")
    assert node, "Node is required by the viewer JavaScript regression suite"
    code = """
let rendered=0;
globalThis.document={createElement(){return {width:0,height:0,getContext(){return {drawImage(){}}}}}};
const viewer={
  original:{name:'first.pdf',sha256:'a'.repeat(64)},ready:Promise.resolve(),valid:()=>true,
  rows:[{pdfPage:{getViewport:()=>({width:612,height:792}),render:()=>{rendered++;return {promise:Promise.resolve()}}}}],
  METHOD
};
try{const canvas=await viewer.renderRegion(PLACEMENT,2);process.stdout.write(JSON.stringify({rendered,width:canvas.width,height:canvas.height}));}
catch(error){process.stdout.write(JSON.stringify({rendered,error:error.message}));}
""".replace("METHOD", method).replace("PLACEMENT", json.dumps(placement))
    result = subprocess.run([node, "--input-type=module", "-e", code], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(result.stdout)


def test_region_crop_refuses_other_pdf_even_with_same_page_number():
    for identity in ({"source_document": "second.pdf"}, {"document": "second.pdf"}, {"document_sha256": "b" * 64}):
        result = _exercise_region_renderer({"page": 1, "bbox": [0, 0, 10, 20], **identity})
        assert result["rendered"] == 0
        assert "error" in result


def test_region_crop_accepts_its_actual_pdf_name_and_digest():
    for identity in ({"source_document": "first.pdf"}, {"document": "first.pdf"}, {"source_sha256": "a" * 64}):
        result = _exercise_region_renderer({"page": 1, "bbox": [0, 0, 10, 20], **identity})
        assert result == {"rendered": 1, "width": 20, "height": 40}
