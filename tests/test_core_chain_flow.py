from types import SimpleNamespace

import pytest

from sourceloom.active_composition import ActiveComposition
from sourceloom.checks import freeze
from sourceloom.durable import Queue
from sourceloom.store import digest
from sourceloom.providers import ProviderResult
from sourceloom.writing import canonical
from tests.test_production import prepared, skill


@pytest.fixture
def core_job(tmp_path, skill):
    store, _, project, bundle = prepared(tmp_path, skill)
    queue = Queue(store, pipeline='active_composition_v2')
    job = queue.enqueue(project['id'], bundle)
    obj = job['source']['objects'][0]
    sid = obj['id']
    inv = dict(job['source'])
    inv['object_responsibilities'] = {sid: dict(preserve=True,present=True,explain=False)}
    inv['obligations'] = [dict(id='fact-1', object_id=sid, statement=obj['text'],
        conditions=[], quantities=[], negations=[], status='unreviewed')]
    job['inventory'] = freeze(inv)
    job['object_responsibilities'] = inv['object_responsibilities']
    job['plan'] = dict(title='改写',objective='保留原意',research_gaps=[],units=[dict(
        id='unit-1',title='正文',objective='保留原意',obligation_ids=['fact-1'],
        prerequisites=[],stages=['保留条件'],object_ids=[sid],proof_questions=[])])
    job['draft'] = dict(blocks=[dict(id='block-1',unit_id='unit-1',kind='explanation',
        markdown='我们假设 x 为正',obligation_ids=['fact-1'],object_ids=[sid],
        evidence=[dict(source_id=sid,quote=obj['text'])])])
    job['active_plans'] = [dict(obligations=[dict(id='fact-1',source_id=sid,
        meaning=obj['text'])],evidence_bindings=[])]
    job['active_groups'] = [[sid]]
    job['writing_batches'] = [dict(id='unit-1',source_ids=[sid])]
    job['stage'] = 'active_integrity'
    host = SimpleNamespace(store=store,config={},queue=queue,owner='test')
    return ActiveComposition(host), job, sid


@pytest.mark.parametrize('finding,expected_verdict,expected_publication', [
    (None, 'PASS', 'Verified'),
    (dict(invariant='I4',verdict='UNKNOWN',block_id='block-1',source_id='s1',
        output_quote='',source_quote='',problem='该细节尚无法确认',required_change=''),
     'UNKNOWN', 'Candidate'),
])
def test_core_integrity_reviews_complete_candidate_once(
        core_job, finding, expected_verdict, expected_publication):
    engine, job, sid = core_job
    calls=[]
    if finding:
        finding['source_id']=sid

    def review_once(current, key, payload, draft, source, allowed, protected_literals=()):
        calls.append(('active_integrity',payload['review_scope']))
        assert payload['required_i3_block_ids']==['block-1']
        return dict(checked_source_ids=[sid],checked_block_ids=['block-1'],
            findings=[finding] if finding else [],i3_provenance_checked=True,
            i3_block_assessments=[dict(block_id='block-1',status='NO_ADDED_FACTS')],
            added_fact_claims=[])

    engine.review_integrity_once=review_once
    assert engine.step(job)=='queued'
    assert job['stage']=='active_deliver'
    assert engine.step(job)=='completed'
    assert calls==[('active_integrity','whole_candidate')]
    assert job['integrity_result']['verdict']==expected_verdict
    assert job['publication_status']==expected_publication
    assert job['integrity_result']['draft_digest']==digest(canonical(job['draft']).encode())


def test_core_integrity_uses_one_provider_generation_even_with_partial_receipt(
        core_job,monkeypatch):
    engine,job,sid=core_job
    requests=[]
    def generate(self,pid,role,payload,schema,current,key,cancelled):
        requests.append((role,key))
        return ProviderResult('SUCCESS',value=dict(checked_source_ids=[sid],
            checked_block_ids=['block-1'],findings=[],i3_provenance_checked=True,
            i3_block_assessments=[],added_fact_claims=[]))
    monkeypatch.setattr('sourceloom.active_composition.Provider.generate',generate)
    monkeypatch.setattr(engine.queue,'cancelled',lambda *args:False)
    monkeypatch.setattr(engine,'turn',lambda *args,**kwargs:
        pytest.fail('whole integrity entered generic correction loop'))
    assert engine.step(job)=='queued'
    assert engine.step(job)=='completed'
    assert len(requests)==1
    assert job['integrity_result']['invariants']['I3']=='UNKNOWN'
    assert job['publication_status']=='Candidate'


def test_core_writing_checkpoints_do_not_own_review_or_repair(core_job):
    engine, job, sid = core_job
    job['writing_batches']=[dict(id='unit-1'),dict(id='unit-2')]
    job['draft']={'blocks':[]}
    job['unit_index']=0
    job['active_checkpoints']=[]
    job['knowledge_memory']=[]
    candidate=dict(draft={'blocks':[dict(id='block-1',unit_id='unit-1',kind='explanation',
        markdown='第一部分',obligation_ids=[],object_ids=[],evidence=[])]},
        coverage=[],delta=dict(established_concepts=[],concept_evidence=[]))
    assert engine.commit_candidate(job,job['writing_batches'][0],candidate)=='queued'
    assert job['stage']=='active_write'
    assert job['active_checkpoints'][0]['content_reviews']==[]
    candidate['draft']['blocks'][0]['id']='block-2'
    candidate['draft']['blocks'][0]['unit_id']='unit-2'
    assert engine.commit_candidate(job,job['writing_batches'][1],candidate)=='queued'
    assert job['stage']=='active_integrity'
    assert all(not point['content_reviews'] for point in job['active_checkpoints'])


def test_core_integrity_uses_one_focused_repair_and_targeted_confirmation(core_job, monkeypatch):
    engine, job, sid=core_job
    calls=[]
    def local_repair(bundle, draft, proposal, allowed, work):
        assert allowed=={'block-1'}
        updated={'blocks':[dict(block) for block in draft['blocks']]}
        updated['blocks'][0]['markdown']='我们假设 x 可能为正'
        return updated
    monkeypatch.setattr('sourceloom.active_composition.repair',local_repair)

    def turn(current, key, role, schema, source, ids, payload, validate):
        calls.append((role,payload.get('review_scope') or payload.get('repair_scope')))
        if role=='active_patch':
            return validate(dict(document_digest=digest(canonical(current['draft']).encode()),
                edits=[dict(block_id='block-1',old_text='为正',new_text='可能为正',
                            reason='恢复原文的不确定性')]),None)
        return validate(dict(checked_source_ids=[sid],checked_block_ids=['block-1'],findings=[]),None)

    engine.turn=turn
    def review_once(current,key,payload,draft,source,allowed,protected_literals=()):
        calls.append(('active_integrity','whole_candidate'))
        issue=dict(invariant='I4',verdict='FAIL',block_id='block-1',source_id=sid,
            output_quote='为正',source_quote='',problem='把可能性写成确定性',
            required_change='只恢复原文的不确定性')
        return dict(checked_source_ids=[sid],checked_block_ids=['block-1'],
            findings=[issue],i3_provenance_checked=True,
            i3_block_assessments=[dict(block_id='block-1',status='NO_ADDED_FACTS')],
            added_fact_claims=[])
    engine.review_integrity_once=review_once
    assert engine.step(job)=='queued'
    assert job['stage']=='active_integrity_repair'
    assert engine.step(job)=='queued'
    assert job['stage']=='active_deliver'
    assert engine.step(job)=='completed'
    assert job['integrity_result']['verdict']=='PASS'
    assert calls==[('active_integrity','whole_candidate'),('active_patch',
        'one focused transaction for the complete candidate'),
        ('active_integrity','changed_blocks_only')]
