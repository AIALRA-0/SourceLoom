import copy
import json
import zipfile
from io import BytesIO

from bs4 import BeautifulSoup
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from sourceloom import processor
from sourceloom.pdf_resource_groups import marker_groups
from sourceloom.store import Store


def material(tmp_path):
    data = BytesIO()
    pdf = canvas.Canvas(data, pagesize=(300, 400))
    red = ImageReader(Image.new('RGB', (100, 100), 'red'))
    blue = ImageReader(Image.new('RGB', (100, 100), 'blue'))
    pdf.drawImage(red, 20, 240, width=90, height=90)
    pdf.drawImage(red, 130, 240, width=90, height=90)
    pdf.drawImage(blue, 30, 250, width=50, height=50)
    # These vector marks must survive; compositing extracted bitmaps alone
    # would omit the source's annotation and shared caption.
    pdf.line(20, 235, 230, 235)
    pdf.drawString(20, 210, 'Source composite caption')
    pdf.save()
    store = Store(tmp_path)
    p = processor.create(store, 'PDF components')
    p = processor.prepare(store, p['id'], [('components.pdf', data.getvalue())])
    images = [o for o in p['inventory']['objects'] if o['kind'] == 'image']
    return store, p, images


def test_page_composition_keeps_overlays_reuse_members_caption_and_original(tmp_path):
    store, p, images = material(tmp_path)
    original = copy.deepcopy(p['inventory'])
    md = '# Figure\n\n' + '\n\n'.join('{{source:' + o['id'] + '}}' for o in images) + '\n\n中文图注\n'
    p = processor.save_result(store, p['id'], md)
    version = processor.active_version(p)
    assert version['markdown'] == md and p['inventory'] == original
    assert version['semantic_status'] == 'not_reviewed'
    derived = version['derived_resources'][0]
    assert derived['composition_scope'] == 'whole_original_page'
    restored = Image.open(BytesIO(store.read_blob(derived['sha256'])))
    assert restored.size == (600, 800)
    assert restored.getpixel((80, 220))[:3] == (0, 0, 255)  # Overlay in source coordinates.
    assert max(restored.getpixel((120, 330))[:3]) < 150  # Original vector annotation.
    compiled = processor.compile_result(p, md)
    soup = BeautifulSoup(compiled['html'], 'html.parser')
    assert len(soup.select('figure img')) == 1
    assert '中文图注' in soup.get_text()
    assert set(compiled['inserted_source_ids']) == {o['id'] for o in images}
    group = next(m for m in compiled['source_map'] if m['mapping'] == 'original_page_composition')
    assert set(group['source_ids']) == {o['id'] for o in images}
    assert compiled['mechanical_pass']
    with zipfile.ZipFile(BytesIO(processor.export_package(store, p))) as archive:
        soup = BeautifulSoup(archive.read('material.html'), 'html.parser')
        assert len(soup.select('img')) == 1
        assert Image.open(BytesIO(archive.read(soup.img['src']))).size == (600, 800)
        note = json.loads(archive.read('!!!meta.json'))['files'][0]
        assert len([r for r in note['attachments'] if r['mime'].startswith('image/')]) >= 4


def test_group_binding_expires_when_member_order_or_material_changes(tmp_path):
    store, p, images = material(tmp_path)
    md = '\n'.join('{{source:' + o['id'] + '}}' for o in images)
    p = processor.save_result(store, p['id'], md)
    reversed_md = '\n'.join('{{source:' + o['id'] + '}}' for o in reversed(images))
    old_assets = processor.active_version(p)['derived_resources']
    compiled = processor.compile_result(p, reversed_md, derived_resources=old_assets)
    assert 'original_page_composition' not in [m['mapping'] for m in compiled['source_map']]
    foreign = copy.deepcopy(p)
    foreign['processor']['source_digest'] = '0' * 64
    compiled = processor.compile_result(foreign, md, derived_resources=old_assets)
    assert 'original_page_composition' not in [m['mapping'] for m in compiled['source_map']]


def test_grouping_does_not_absorb_code_or_unknown_reference_markers(tmp_path):
    store, p, images = material(tmp_path)
    lines = ['{{source:' + o['id'] + '}}' for o in images]
    assert not marker_groups(p, '```\n' + '\n'.join(lines) + '\n```', {})
    assert not marker_groups(p, '\n'.join(lines), {images[0]['id']: 'reference'})
    assert not marker_groups(p, '\n\nSeparate figure caption\n\n'.join(lines), {})
    broken = copy.deepcopy(p)
    broken['inventory']['resources'] = []
    assert not marker_groups(broken, '\n'.join(lines), {})


def test_repeated_page_group_is_a_light_reference_not_another_full_page(tmp_path):
    store, p, images = material(tmp_path)
    group = '\n'.join('{{source:' + o['id'] + '}}' for o in images)
    md = group + '\n\nFirst figure caption\n\n' + group + '\n\nSecond figure caption\n'
    p = processor.save_result(store, p['id'], md)
    preview = processor.compile_result(p, md)
    soup = BeautifulSoup(preview['html'], 'html.parser')
    assert len(soup.select('figure img')) == 1
    assert len(soup.select('details img')) == 1
    assert len({m['block_id'] for m in preview['source_map']}) == len(preview['source_map'])
    native = processor.compile_result(p, md, 'readweave')
    soup = BeautifulSoup(native['html'], 'html.parser')
    assert len(soup.select('img')) == 1 and len(soup.select('a[href^="assets/"]')) == 1
    assert 'First figure caption' in soup.get_text() and 'Second figure caption' in soup.get_text()
