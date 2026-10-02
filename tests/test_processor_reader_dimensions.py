"""Reader size metadata must not alter frozen source or model task packs."""

from io import BytesIO

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from sourceloom import processor
from sourceloom.processor_api import register
from sourceloom.store import Store


@pytest.fixture
def reader(tmp_path):
    store = Store(tmp_path)
    project = processor.create(store, 'Reader dimensions fixture')
    image = BytesIO()
    Image.new('RGB', (40, 30), 'blue').save(image, 'PNG')
    project = processor.prepare(store, project['id'], [('figure.png', image.getvalue())])
    pid = project['id']
    # Simulate an older saved page preview sharing the same frozen bitmap.
    def add_page(current):
        original = current['processor']['resources'][0]
        current['processor']['resources'].append(dict(
            original, id='src-page', kind='page', locator='fixture.pdf/page[1]',
            marker='{{source:src-page}}'))
        current['inventory']['objects'].append(dict(
            id='src-page', kind='page', text='Synthetic original page',
            locator='fixture.pdf/page[1]', resource_id=original['sha256']))
    store.change(pid, add_page)
    app = FastAPI()
    register(app, store, {})
    try:
        with TestClient(app) as client:
            yield client, store, pid
    finally:
        app.state.processor_executor.shutdown(wait=False, cancel_futures=True)


def test_saved_reader_dimensions_preserve_project_and_task_pack(reader):
    client, store, pid = reader
    route = f'/api/processor/projects/{pid}'
    frozen = store.get(pid)
    pack = client.get(route+'/pack').json()

    response = client.get(route)

    assert response.status_code == 200
    resources = response.json()['processor']['resources']
    assert [(r['kind'], r['width'], r['height']) for r in resources] == [
        ('image', 40, 30), ('page', 40, 30)]
    assert store.get(pid) == frozen
    assert client.get(route+'/pack').json() == pack
    assert all('width' not in r and 'height' not in r for r in pack['resources'])


def test_same_frozen_bitmap_dimensions_are_cached_across_resources_and_reads(reader, monkeypatch):
    client, store, pid = reader
    reads = []
    read_blob = store.read_blob
    def observed_read(key):
        reads.append(key)
        return read_blob(key)
    monkeypatch.setattr(store, 'read_blob', observed_read)

    for _ in range(2):
        response = client.get(f'/api/processor/projects/{pid}')
        assert response.status_code == 200
        assert all(r['width'] == 40 for r in response.json()['processor']['resources'])

    assert len(reads) == 1


def test_unreadable_or_missing_images_leave_detail_available(reader):
    client, store, pid = reader
    invalid_key = store.blob(b'Not an image')
    def add_unavailable(current):
        current['processor']['resources'].extend([
            dict(id='src-invalid', kind='image', label='Invalid', locator='invalid.png',
                 marker='{{source:src-invalid}}', sha256=invalid_key),
            dict(id='src-missing', kind='page', label='Missing', locator='missing.pdf/page[1]',
                 marker='{{source:src-missing}}', sha256='0'*64),
            dict(id='src-no-bytes', kind='image', label='Unresolved', locator='unresolved.png',
                 marker='{{source:src-no-bytes}}'),
        ])
    store.change(pid, add_unavailable)
    frozen = store.get(pid)

    response = client.get(f'/api/processor/projects/{pid}')

    assert response.status_code == 200
    resources = response.json()['processor']['resources']
    assert all('width' not in r and 'height' not in r for r in resources[2:])
    assert store.get(pid) == frozen
