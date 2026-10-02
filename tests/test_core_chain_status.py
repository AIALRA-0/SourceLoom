import json
import zipfile
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from sourceloom.active_composition import PIPELINE_V2
from sourceloom.checks import can_publish, review_complete
from sourceloom.config import load_config
from sourceloom.demo import create_demo
from sourceloom.durable import Queue
from sourceloom.export import export_zip
from sourceloom.production import Production
from sourceloom.skills import deploy_skill
from sourceloom.store import Conflict, digest
from sourceloom.writing import canonical


@pytest.fixture
def skill(tmp_path):
    root=tmp_path/'writing-skill'
    (root/'references').mkdir(parents=True)
    (root/'constitution').mkdir()
    (root/'assets').mkdir()
    (root/'SKILL.md').write_text(
        '[format](references/format-rules.md)\n'
        '[explain](references/explanation-framework.md)\n'
        '[formula](references/formula-explanation.md)\n'
        '`constitution/principles.md`', encoding='utf-8')
    for name in ('format-rules','explanation-framework','formula-explanation'):
        (root/'references'/f'{name}.md').write_text('FULL-'+name, encoding='utf-8')
    (root/'constitution'/'principles.md').write_text('FULL-CONSTITUTION', encoding='utf-8')
    (root/'assets'/'example.bin').write_bytes(b'local fixture asset')
    return root


def _app(tmp_path):
    from sourceloom.app import create_app
    return create_app(load_config() | {'data_dir':str(tmp_path/'data'), 'provider':'manual'})


def _complete_core_candidate(store, skill, verdict):
    project=create_demo(store)
    store.change(project['id'], lambda value: value.update(draft=None, plan=None, review=None))
    bundle=deploy_skill(skill, store.root)
    queue=Queue(store, pipeline=PIPELINE_V2)
    job=queue.enqueue(project['id'], bundle)
    job=store.job(job['id'])

    invariants={key:'PASS' for key in ('I1','I2','I3','I4','I5')}
    if verdict=='UNKNOWN':
        invariants['I4']='UNKNOWN'
    elif verdict=='FAIL':
        invariants['I2']='FAIL'
    draft=project['draft']
    job.update(stage='active_deliver', inventory=project['inventory'],
        source=project['inventory'], plan=project['plan'], draft=draft,
        integrity_result=dict(verdict=verdict, invariants=invariants,
            draft_digest=digest(canonical(draft).encode()),
            source_digest=project['inventory']['digest']))
    store.put_job(job)

    engine=Production(store, {})
    engine.queue=queue
    assert engine.run_once()
    saved_job=store.job(job['id'])
    saved_project=store.get(project['id'])
    return saved_job, saved_project


@pytest.mark.parametrize(('verdict','publication_status'), [
    ('PASS','Verified'), ('UNKNOWN','Candidate'), ('FAIL','Candidate')])
def test_core_receipt_separates_execution_integrity_and_publication(
        tmp_path, skill, monkeypatch, verdict, publication_status):
    app=_app(tmp_path)
    store=app.state.store
    saved_job, project=_complete_core_candidate(store, skill, verdict)

    assert saved_job['status']=='completed'
    assert saved_job['core_chain_version']==1
    receipt=project['production']
    assert receipt['status']=='completed'
    assert receipt['core_chain_version']==1
    assert receipt['integrity_verdict']==verdict
    assert receipt['publication_status']==publication_status
    assert receipt.get('semantic_status') is None
    assert project['integrity_result']['verdict']==verdict
    assert project['integrity_result']['draft_digest']==digest(canonical(project['draft']).encode())
    assert project['accepted_revision'] is None

    # Legacy fields deliberately contradict the core verdict. Core gates must
    # use the integrity result, not semantic_status or independent_review.
    def add_legacy_decoys(value):
        value['production']['semantic_status']=(
            'not_independently_reviewed' if verdict=='PASS' else 'passed')
        value['independent_review']=dict(
            status='issues_recorded' if verdict=='PASS' else 'passed',
            revision=value['revision'],
            canonical_digest=digest(canonical(value['draft']).encode()))
    project=store.change(project['id'], add_legacy_decoys)
    assert review_complete(project) is (verdict=='PASS')
    assert not can_publish(project)

    # A valid ReadWeave configuration must still be rejected before any HTTP
    # client is created until the candidate has been accepted.
    from sourceloom import readweave
    network_calls=[]
    def forbidden_http(*args, **kwargs):
        network_calls.append((args,kwargs))
        raise AssertionError('readweave gate must reject before HTTP')
    with monkeypatch.context() as patcher:
        patcher.setattr(readweave.httpx, 'Client', forbidden_http)
        with pytest.raises(Conflict):
            readweave.import_candidate(store, {
                'readweave_url':'https://reader.invalid',
                'readweave_token':'test-token',
                'readweave_parent':'test-parent'}, project['id'])
    assert network_calls==[]

    if verdict=='PASS':
        with pytest.raises(Conflict):
            export_zip(store, project, release=True)

    with TestClient(app) as client:
        api_status=client.get(f"/api/projects/{project['id']}/production").json()
        assert api_status['status']=='completed'
        assert api_status['delivery_complete'] is True
        assert api_status['formal'] is (verdict=='PASS')
        assert api_status['publication_status']==publication_status
        assert api_status['integrity_verdict']==verdict
        assert api_status['semantic_status'] is None
        if verdict!='PASS':
            response=client.post(
                f"/api/projects/{project['id']}/accept",
                headers={'X-SourceLoom':'1'},
                json={'revision':project['revision']})
        else:
            response=None
    if verdict!='PASS':
        assert response.status_code==409
        current=store.get(project['id'])
        assert current['production']['publication_status']=='Candidate'
        with pytest.raises(Conflict):
            export_zip(store, current, release=True)
        return

    with TestClient(app) as client:
        response=client.post(
            f"/api/projects/{project['id']}/accept",
            headers={'X-SourceLoom':'1'},
            json={'revision':project['revision']})
    assert response.status_code==200
    accepted=store.get(project['id'])
    assert accepted['accepted_revision']==accepted['revision']
    assert accepted['production']['publication_status']=='Published'
    assert accepted['production']['status']=='completed'
    assert can_publish(accepted)

    with zipfile.ZipFile(BytesIO(export_zip(store, accepted, release=True))) as archive:
        metadata=json.loads(archive.read('!!!meta.json'))
        audit_asset=next(a for a in metadata['files'][0]['attachments']
                         if a['title']=='sourceloom-audit.json')
        audit=json.loads(archive.read(audit_asset['dataFileName']))
    assert audit['production']['publication_status']=='Published'
    assert audit['integrity_result']['verdict']=='PASS'


def test_v2_project_without_core_marker_keeps_legacy_accept_and_export_rules(tmp_path, monkeypatch):
    from sourceloom.checks import transition_delivery
    from sourceloom.store import Store

    store=Store(tmp_path)
    project=create_demo(store)
    draft_digest=digest(canonical(project['draft']).encode())
    production=dict(pipeline=PIPELINE_V2, automatic=True, status='completed',
        revision=project['revision'], manual_edits=0, issues=[], skill_digest='legacy-skill',
        canonical_digest=draft_digest, delivery_state='ready_for_review',
        semantic_status='not_independently_reviewed')
    project=store.change(project['id'], lambda value: value.update(
        production=production,
        independent_review=dict(status='passed', revision=value['revision'],
                                canonical_digest=draft_digest)))

    assert project['production'].get('core_chain_version') is None
    assert review_complete(project)
    assert transition_delivery(project, 'accept')=='accepted'
    assert project['production']['delivery_state']=='accepted'
    assert project['production'].get('publication_status') is None
    assert can_publish(project)
    accepted_project=project
    project=store.change(project['id'], lambda value: value.update(
        production=accepted_project['production'],
        accepted_revision=accepted_project['accepted_revision']))

    from sourceloom import readweave
    class ExportReached(Exception):
        pass
    def stop_before_http(*args, **kwargs):
        raise ExportReached('legacy accepted candidate passed the ReadWeave gate')
    monkeypatch.setattr(readweave, 'export_zip', stop_before_http)
    with pytest.raises(ExportReached, match='passed the ReadWeave gate'):
        readweave.import_candidate(store, {
            'readweave_url':'https://reader.invalid',
            'readweave_token':'test-token',
            'readweave_parent':'test-parent'}, project['id'])

    with zipfile.ZipFile(BytesIO(export_zip(store, project, release=True))) as archive:
        metadata=json.loads(archive.read('!!!meta.json'))
        audit_asset=next(a for a in metadata['files'][0]['attachments']
                         if a['title']=='sourceloom-audit.json')
        audit=json.loads(archive.read(audit_asset['dataFileName']))
    assert audit['production'].get('core_chain_version') is None
