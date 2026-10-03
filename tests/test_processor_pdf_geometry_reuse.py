"""PDF geometry reuse preserves source bytes and every bitmap occurrence."""
import copy
from io import BytesIO

import fitz
import pytest
from PIL import Image
from pypdf import PdfReader
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from sourceloom import processor
from sourceloom.store import Store


def repeated_images_pdf():
    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=(500, 700))
    blue = ImageReader(Image.new('RGB', (32, 24), 'blue'))
    red = ImageReader(Image.new('RGB', (30, 20), 'red'))
    for page in range(2):
        pdf.drawString(40, 650, f'Synthetic page {page+1}')
        pdf.drawImage(blue, 40, 400, width=64, height=48)
        pdf.drawImage(blue, 140, 400, width=16, height=12)
        pdf.drawImage(red, 40, 300, width=60, height=40)
        pdf.showPage()
    pdf.save()
    return output.getvalue()


def source_only_intake(store, raw, monkeypatch):
    """Keep intake out of the reuse count; extraction uses actual PDF parsers."""
    key = store.blob(raw)
    inv = dict(originals=[dict(name='original.pdf', sha256=key, size=len(raw),
                              mime='application/pdf')], objects=[], resources=[])
    monkeypatch.setattr(processor, 'intake', lambda *args, **kwargs: copy.deepcopy(inv))


def test_prepare_reuses_one_document_and_page_with_identical_image_geometry(tmp_path, monkeypatch):
    raw = repeated_images_pdf()
    reader = PdfReader(BytesIO(raw))
    expected = [(page_no, index, image.data,
                 processor.pdf_image_placements(raw, page_no-1, image))
                for page_no, page in enumerate(reader.pages, 1)
                for index, image in enumerate(page.images, 1)]
    assert len(expected) == 4
    assert sorted(len(item[3]) for item in expected) == [1, 1, 2, 2]
    store = Store(tmp_path)
    source_only_intake(store, raw, monkeypatch)
    original_helper = processor.pdf_image_placements
    with monkeypatch.context() as legacy:
        legacy.setattr(processor, 'pdf_image_placements',
                       lambda raw, index, image, page=None: original_helper(raw, index, image))
        baseline = processor.create(store, 'Independent per-image geometry baseline')
        baseline = processor.prepare(store, baseline['id'], [('original.pdf', raw)])
    opened, pages, scans = [], [], []
    real_open, real_info = fitz.open, fitz.Page.get_image_info

    class TrackedDocument:
        def __init__(self, doc):
            self.doc = doc
            self.closed = False

        def __getitem__(self, index):
            page = self.doc[index]
            pages.append(page)
            return page

        def close(self):
            self.closed = True
            self.doc.close()

    def tracked_open(*args, **kwargs):
        doc = TrackedDocument(real_open(*args, **kwargs))
        opened.append(doc)
        return doc

    def counted_info(page, *args, **kwargs):
        if not getattr(page, '_image_info', None):
            scans.append(page)
        return real_info(page, *args, **kwargs)

    monkeypatch.setattr(fitz, 'open', tracked_open)
    monkeypatch.setattr(fitz.Page, 'get_image_info', counted_info)
    project = processor.create(store, 'Synthetic repeated bitmap source')
    result = processor.prepare(store, project['id'], [('original.pdf', raw)])
    images = [obj for obj in result['inventory']['objects'] if obj['kind'] == 'image']
    assert len(opened) == 1 and opened[0].closed
    assert len(pages) == 2 and len(scans) == 2
    assert all(scans[index] is pages[index] for index in range(2))
    assert result['inventory'] == baseline['inventory']
    for obj, (page_no, index, image_bytes, placements) in zip(images, expected, strict=True):
        assert obj['locator'] == f'original.pdf/page[{page_no}]/image[{index}]'
        assert obj['placements'] == placements
        assert obj['placement_count'] == len(placements)
        assert store.read_blob(obj['resource_id']) == image_bytes
    assert store.read_blob(result['inventory']['originals'][0]['sha256']) == raw


@pytest.mark.parametrize('failure', ['open', 'page', 'rects'])
def test_prepare_geometry_failure_keeps_images_and_closes_document(tmp_path, monkeypatch, failure):
    raw = repeated_images_pdf()
    store = Store(tmp_path)
    source_only_intake(store, raw, monkeypatch)
    real_open = fitz.open
    opened = []

    class FailingDocument:
        def __init__(self):
            self.doc = real_open(stream=raw, filetype='pdf')
            self.closed = False

        def __getitem__(self, index):
            if failure == 'page':
                raise RuntimeError('Controlled page geometry failure')
            return self.doc[index]

        def close(self):
            self.closed = True
            self.doc.close()

    def failed_open(*args, **kwargs):
        if failure == 'open':
            raise ValueError('Controlled optional geometry failure')
        doc = FailingDocument()
        opened.append(doc)
        return doc

    monkeypatch.setattr(fitz, 'open', failed_open)
    if failure == 'rects':
        def failed_rects(*args):
            raise ValueError('Controlled rect failure')
        monkeypatch.setattr(fitz.Page, 'get_image_rects', failed_rects)
    project = processor.create(store, 'Optional geometry failure')
    result = processor.prepare(store, project['id'], [('original.pdf', raw)])
    images = [obj for obj in result['inventory']['objects'] if obj['kind'] == 'image']
    assert len(images) == 4
    assert all(obj['placements'] == [] and obj['placement_count'] == 0 for obj in images)
    assert all(doc.closed for doc in opened)


def test_prepare_closes_shared_document_when_resource_save_raises(tmp_path, monkeypatch):
    raw = repeated_images_pdf()
    store = Store(tmp_path)
    source_only_intake(store, raw, monkeypatch)
    real_open = fitz.open
    opened = []

    def tracked_open(*args, **kwargs):
        doc = real_open(*args, **kwargs)
        opened.append(doc)
        return doc

    monkeypatch.setattr(fitz, 'open', tracked_open)
    project = processor.create(store, 'Controlled storage failure')
    def failed_blob(*args):
        raise OSError('Controlled storage failure')
    monkeypatch.setattr(store, 'blob', failed_blob)
    with pytest.raises(OSError, match='Controlled storage failure'):
        processor.prepare(store, project['id'], [('original.pdf', raw)])
    assert len(opened) == 1 and opened[0].is_closed


def test_three_argument_helper_still_closes_its_own_document_on_error(monkeypatch):
    raw = repeated_images_pdf()
    image = list(PdfReader(BytesIO(raw)).pages[0].images)[0]
    real_open = fitz.open
    opened = []

    def tracked_open(*args, **kwargs):
        doc = real_open(*args, **kwargs)
        opened.append(doc)
        return doc

    monkeypatch.setattr(fitz, 'open', tracked_open)
    def failed_rects(*args):
        raise RuntimeError('Controlled rect failure')
    monkeypatch.setattr(fitz.Page, 'get_image_rects', failed_rects)
    assert processor.pdf_image_placements(raw, 0, image) == []
    assert len(opened) == 1 and opened[0].is_closed
