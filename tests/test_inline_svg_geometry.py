"""HTML SVG geometry survives safe rasterization and every display target."""
import copy
import json
from io import BytesIO
import zipfile

from bs4 import BeautifulSoup
from PIL import Image, ImageChops
import pytest

from sourceloom import processor
from sourceloom.export import image_presentation
from sourceloom.ingest import intake
from sourceloom.network import rasterize_svg
from sourceloom.store import Store


SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="46" height="46" '
       'viewBox="0 0 360 360" preserveAspectRatio="xMidYMid meet">'
       '<circle cx="180" cy="180" r="100" fill="blue"/></svg>')


def test_html_projection_matches_xml_geometry_without_changing_source(tmp_path):
    store = Store(tmp_path)
    raw = ('<main><p>Article</p>'+SVG+'</main>').encode()
    original = bytes(raw)
    source = intake(store, [('source.html', raw)])
    obj = next(o for o in source['objects'] if o.get('source_format') == 'inline-svg')
    # The parser's raw is derived HTML, not the original saved source bytes.
    assert 'viewbox=' in obj['raw'] and 'viewBox=' in SVG
    assert store.read_blob(source['originals'][0]['sha256']) == original
    expected = Image.open(BytesIO(rasterize_svg(SVG.encode()))).convert('RGBA')
    actual = Image.open(BytesIO(store.read_blob(obj['resource_id']))).convert('RGBA')
    assert actual.size == (512, 512)
    assert all(channel.getbbox() is None
               for channel in ImageChops.difference(actual, expected).split())
    # A clipped full-page shape would not have transparent corners.
    assert actual.getpixel((0, 0))[3] == 0
    assert actual.getpixel((256, 256)) == (0, 0, 255, 255)


@pytest.mark.parametrize('width,height', [('46','46'), ('106px','30px')])
def test_source_placement_survives_preview_native_export_and_old_inventory(tmp_path, width, height):
    store = Store(tmp_path)
    p = processor.create(store, 'SVG geometry')
    raw = SVG.replace('width="46"', 'width="'+width+'"').replace('height="46"', 'height="'+height+'"')
    p = processor.prepare(store, p['id'], [('source.html', ('<main>'+raw+'</main>').encode())])
    obj = next(o for o in p['inventory']['objects'] if o.get('source_format') == 'inline-svg')
    markdown = '# Article\n\n{{source:'+obj['id']+'}}'
    p = processor.save_result(store, p['id'], markdown)
    frozen = copy.deepcopy(p)
    wanted = [str(round(float(value.removesuffix('px')))) for value in (width, height)]
    for target in ('preview', 'readweave'):
        doc = BeautifulSoup(processor.compile_result(p, markdown, target=target)['html'], 'html.parser')
        assert [doc.img[axis] for axis in ('width', 'height')] == wanted
    with zipfile.ZipFile(BytesIO(processor.export_package(store, p))) as z:
        doc = BeautifulSoup(z.read('material.html'), 'html.parser')
        assert [doc.img[axis] for axis in ('width', 'height')] == wanted
        note = json.loads(z.read('!!!meta.json'))['files'][0]
        attachments = {a['title']: a for a in note['attachments']}
        assert z.read(attachments['article.md']['dataFileName']).decode() == markdown
        assert z.read(attachments['source.html']['dataFileName']) == ('<main>'+raw+'</main>').encode()
    assert p == frozen and store.get(p['id']) == frozen


@pytest.mark.parametrize('raw', [
    '<svg viewbox="0 0 46 46"/>',
    '<svg width="100%" height="46"/>',
    '<svg width="46"/>',
    '<svg width="0" height="46"/>',
    '<svg width="100000" height="46"/>',
])
def test_missing_relative_or_invalid_placement_is_not_guessed(raw):
    assert 'source_css_dimensions' not in image_presentation(dict(source_format='inline-svg', raw=raw))


def test_mixed_occurrence_geometry_and_unsafe_svg_are_not_weakened():
    obj = dict(source_format='inline-svg', raw=SVG,
               placements=[dict(unit='pdf_point', bbox=[0, 0, 10, 10]),
                           dict(unit='pdf_point', bbox=[0, 0, 320, 320])])
    assert 'source_css_dimensions' not in image_presentation(obj)
    assert image_presentation(obj)['role'] == 'source-image'
    with pytest.raises(ValueError):
        rasterize_svg(b'<svg viewbox="0 0 10 10"><script>alert(1)</script></svg>')
