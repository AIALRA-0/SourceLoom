import json
import zipfile
from io import BytesIO

from sourceloom.active_composition import (
    legacy_artifacts,
    planned_object_responsibilities,
    validate_written,
    writing_batches,
)
from sourceloom.checks import inspect_draft
from sourceloom.durable import Queue
from sourceloom.export import export_zip
from sourceloom.store import Store
from tests.test_active_composition import plan as sample_plan, source as sample_source


def _knowledge_delta():
    return dict(established_concepts=[], explained_obligations=['f1'],
                unresolved_prerequisites=[], next_bridge='')


def test_hidden_object_stays_archived_but_out_of_writer_and_candidate(tmp_path):
    store = Store(tmp_path)
    image_bytes = b'original image bytes kept in the resource archive'
    image_hash = store.blob(image_bytes)
    source = sample_source()
    source['objects'][1].update(kind='image', text='', resource_id='image-1',
        source_scope='article_media', alt='Archive-only chart')
    source['resources'] = [dict(id='image-1', name='chart.png', sha256=image_hash,
        size=len(image_bytes), mime='image/png')]
    composition = sample_plan()
    composition['object_responsibilities'] = [
        dict(source_id='s1', present=True, explain=False),
        dict(source_id='s2', present=False, explain=True),
    ]

    responsibilities = planned_object_responsibilities([composition], source)
    assert responsibilities == {
        's1': dict(preserve=True, present=True, explain=False),
        's2': dict(preserve=True, present=False, explain=True),
    }
    visible_ids = {sid for sid, role in responsibilities.items() if role['present']}
    batches = writing_batches([composition], source, present_source_ids=visible_ids)
    assert len(batches) == 1
    assert batches[0]['source_ids'] == ['s1']
    assert batches[0]['obligation_ids'] == ['f1']

    inventory, legacy_plan = legacy_artifacts([composition], source, batches, responsibilities)
    assert {obj['id'] for obj in inventory['objects']} == {'s1', 's2'}
    assert [obligation['id'] for obligation in inventory['obligations']] == ['f1']
    assert inventory['resources'][0]['id'] == 'image-1'
    draft, _, _ = validate_written(
        dict(blocks=[dict(id='n1-copy', kind='explanation',
            markdown='We may use the items when they are ready.',
            obligation_ids=['f1'], source_ids=['s1'])],
            coverage=[dict(obligation_id='f1', block_id='n1-copy',
                output_quote='We may use the items when they are ready.')],
            knowledge_delta=_knowledge_delta()),
        batches[0], inventory, None, {'blocks': []}, [composition['obligations'][0]])
    assert 's2' not in draft['blocks'][0]['object_ids']
    assert 's2' not in draft['blocks'][0]['embedded_object_ids']
    assert 'Archive-only chart' not in draft['blocks'][0]['markdown']

    project = store.create('Object responsibility boundary')
    project.update(inventory=inventory, plan=legacy_plan, draft=draft)
    with zipfile.ZipFile(BytesIO(export_zip(store, project))) as archive:
        metadata = json.loads(archive.read('!!!meta.json'))
        image_attachment = next(item for item in metadata['files'][0]['attachments']
                                if item['title'] == 'chart.png')
        assert archive.read(image_attachment['dataFileName']) == image_bytes
        assert b'chart.png' not in archive.read('material.html')


def test_present_protected_object_is_compiled_with_exact_source_bytes():
    source = sample_source()
    source['objects'] = source['objects'][:1]
    literal = '```python\nx = 3\n```'
    source['objects'][0].update(kind='code', text='x = 3', fence_raw=literal)
    composition = sample_plan()
    composition['obligations'] = composition['obligations'][:1]
    composition['obligations'][0].update(quote='x = 3', meaning='The code assigns 3 to x')
    composition['nodes'][0].update(source_ids=['s1'], obligation_ids=['f1'])
    composition['object_responsibilities'] = [dict(source_id='s1', present=True, explain=False)]
    responsibilities = planned_object_responsibilities([composition], source)
    batches = writing_batches([composition], source, present_source_ids={'s1'})
    inventory, _ = legacy_artifacts([composition], source, batches, responsibilities)

    draft, _, _ = validate_written(
        dict(blocks=[dict(id='n1-code', kind='object', markdown='{{source:s1}}',
            obligation_ids=['f1'], source_ids=['s1'])],
            coverage=[dict(obligation_id='f1', block_id='n1-code', output_quote='x = 3')],
            knowledge_delta=_knowledge_delta()),
        batches[0], inventory, None, {'blocks': []}, composition['obligations'])

    assert draft['blocks'][0]['markdown'] == literal
    assert draft['blocks'][0]['object_ids'] == ['s1']
    assert draft['blocks'][0]['embedded_object_ids'] == ['s1']


def test_legacy_inventory_without_responsibilities_still_requires_protected_objects():
    source = sample_source()
    source['objects'] = source['objects'][:1]
    literal = '```python\nx = 3\n```'
    source['objects'][0].update(kind='code', text='x = 3', fence_raw=literal)
    inventory = source | dict(frozen=True, object_responsibilities=None, obligations=[], resources=[])
    inventory.pop('object_responsibilities')
    omitted = {'blocks': [dict(id='b1', unit_id='u1', kind='explanation', markdown='说明',
        obligation_ids=[], object_ids=[], embedded_object_ids=[], evidence=[])]}

    findings = inspect_draft(inventory, omitted)

    assert any(item['code'] == 'protected_object' for item in findings)
    preserved = {'blocks': [dict(id='b1', unit_id='u1', kind='object', markdown=literal,
        obligation_ids=[], object_ids=['s1'], embedded_object_ids=['s1'], evidence=[])]}
    assert not inspect_draft(inventory, preserved)
