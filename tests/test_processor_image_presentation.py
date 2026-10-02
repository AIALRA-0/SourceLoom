"""Source placement survives old candidates, manual import and native export."""
import copy
import json
import zipfile
from io import BytesIO

from bs4 import BeautifulSoup
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from sourceloom import processor
from sourceloom.export import image_presentation, safe_html
from sourceloom.store import Store


def placement(width, height):
    return dict(page=1, bbox=[0, 0, width, height], unit='pdf_point')


def test_same_attachment_retains_specific_occurrence_sizes_and_unknown_is_not_guessed():
    source = dict(id='icon', kind='image', resource_id='a'*64, text='Source', locator='a.pdf/page[1]/image[1]',
                  placements=[placement(10, 9), placement(320, 288)])
    assert image_presentation(source)['role'] == 'source-image'
    assert image_presentation(source, 0)['role'] == 'inline-component'
    assert image_presentation(source, 1)['role'] == 'source-image'
    icon = BeautifulSoup(processor.object_html(source | {'presentation_occurrence': 0}), 'html.parser')
    study = BeautifulSoup(processor.object_html(source | {'presentation_occurrence': 1}), 'html.parser')
    assert icon.img['src'] == study.img['src']
    assert icon.img['width'] == '20' and icon.figure is None
    assert 'height' not in icon.img.attrs
    assert 'width' not in study.img.attrs and study.figure is not None
    for role in ['inline-icon', 'table-symbol']:
        explicit = BeautifulSoup(processor.object_html(source | {'presentation_role': role}), 'html.parser')
        assert explicit.img['width'] == '20'
    unknown = source | {'placements': []}
    assert 'width' not in BeautifulSoup(processor.object_html(unknown), 'html.parser').img.attrs
    # A photograph embedded in a table is not automatically a tiny symbol.
    assert image_presentation(source | {'parent_kind': 'table'})['role'] == 'source-image'


def test_manual_dimensions_survive_sanitizing_without_active_css():
    html = safe_html('<img src="assets/test" width="20" height="19" style="width: 1.1em; height: auto; margin: 0">'
                     '<img src="assets/test" width="-1" style="position: fixed; background: url(https://bad)">')
    images = BeautifulSoup(html, 'html.parser').find_all('img')
    assert images[0]['width'] == '20' and '1.1em' in images[0]['style']
    assert 'width' not in images[1].attrs and 'style' not in images[1].attrs


def test_legacy_pdf_read_projection_and_export_preserve_frozen_material(tmp_path):
    output = BytesIO()
    pdf = canvas.Canvas(output)
    pdf.drawString(50, 750, 'Synthetic source placement fixture')
    # High-resolution bitmap, small source placement; unrelated study remains large.
    pdf.drawImage(ImageReader(Image.new('RGB', (830, 773), 'yellow')), 50, 700, width=10.69, height=9.96)
    pdf.drawImage(ImageReader(Image.new('RGB', (800, 500), 'blue')), 50, 350, width=320, height=200)
    pdf.save()
    original = output.getvalue()
    store = Store(tmp_path)
    project = processor.create(store, 'Synthetic placement regression')
    project = processor.prepare(store, project['id'], [('source.pdf', original)])
    def old_inventory(current):
        for row in current['inventory']['objects'] + current['processor']['resources']:
            for key in ['placements', 'placement_count', 'purpose_hint']:
                row.pop(key, None)
    project = store.change(project['id'], old_inventory)
    markdown = '# Source dimensions\n\n'+'\n\n'.join(row['marker'] for row in project['processor']['resources'])
    project = processor.save_result(store, project['id'], markdown)
    frozen = copy.deepcopy(project)
    projected = processor.presentation_project(project, store.read_blob)
    images = [obj for obj in projected['inventory']['objects'] if obj['kind'] == 'image']
    compact = next(obj for obj in images if image_presentation(obj)['role'] == 'inline-component')
    study = next(obj for obj in images if image_presentation(obj)['role'] == 'source-image')
    for target in ['preview', 'readweave']:
        doc = BeautifulSoup(processor.compile_result(projected, markdown, target)['html'], 'html.parser')
        assert doc.select_one('img[data-source-id="'+compact['id']+'"]')['width'] == '20'
        assert 'width' not in doc.select_one('img[data-source-id="'+study['id']+'"]')
    with zipfile.ZipFile(BytesIO(processor.export_package(store, project))) as archive:
        note = json.loads(archive.read('!!!meta.json'))['files'][0]
        attachments = {row['title']: row for row in note['attachments']}
        assert archive.read(attachments['article.md']['dataFileName']).decode() == markdown
        assert archive.read(attachments['source.pdf']['dataFileName']) == original
        doc = BeautifulSoup(archive.read('material.html'), 'html.parser')
        assert doc.select_one('img[data-source-id="'+compact['id']+'"]')['width'] == '20'
        assert 'height' not in doc.select_one('img[data-source-id="'+compact['id']+'"]')
        page = next(obj for obj in projected['inventory']['objects'] if obj['kind'] == 'page')
        reference = doc.select_one('a[data-source-id="'+page['id']+'"]')
        assert reference is not None and 'page[1]' in reference.get_text()
        assert archive.read(reference['href']) == store.read_blob(page['resource_id'])
        assert not doc.select('details, img[data-source-role="page-reference"]')
        assert len(doc.select('img')) == len(images)
    preview = BeautifulSoup(processor.compile_result(projected, markdown)['html'], 'html.parser')
    assert preview.select_one('details>summary') is not None
    assert preview.select_one('details img') is not None
    assert project == frozen and store.get(project['id']) == frozen
