import copy

import pytest

from sourceloom.durable import Queue
from sourceloom.production import Production
from sourceloom.store import Store, digest
from tests.test_active_composition import plan
from tests.test_production import prepared, skill


@pytest.mark.parametrize(('post_patch_finding','legacy_checkpoint'),[
    (False,False),(True,False),(False,True)])
def test_v2_revalidates_one_exact_post_patch_candidate(
        tmp_path,skill,monkeypatch,post_patch_finding,legacy_checkpoint):
    from sourceloom.writing import canonical

    store,_,project,bundle=prepared(tmp_path,skill)
    job=Queue(store,pipeline='active_composition_v2').enqueue(project['id'],bundle)
    # This fixture exercises the persisted pre-cutover v2 review path.
    job['core_chain_version']=0
    store.put_job(job)
    calls=[]
    reviewed_candidates=[]

    def provider(self,pid,role,payload,schema,job,cancelled):
        calls.append(role)
        if role=='active_plan':
            result=plan();result['obligations']=result['obligations'][:1]
            sid=payload['assigned_source_ids'][0]
            result['obligations'][0].update(source_id=sid,quote='',
                source_span_ids=[span['id'] for span in payload['source_spans']])
            result['nodes'][0].update(source_ids=[sid],obligation_ids=['f1'])
        elif role=='active_write':
            result=dict(blocks=[dict(id='n1-b1',kind='explanation',
                markdown='## 适用条件\n\n我们假设 `x` 为负',obligation_ids=['f1'],
                source_ids=payload['node']['source_ids'])],knowledge_delta=dict(
                    established_concepts=[],explained_obligations=['f1'],
                    unresolved_prerequisites=[],next_bridge=''))
        elif role=='active_review':
            candidate=canonical(payload['actual_draft'])
            reviewed_candidates.append(candidate)
            unresolved='为负' in candidate or (post_patch_finding and '为正' in candidate)
            quote='为负' if '为负' in candidate else '为正'
            result=dict(findings=[dict(block_id='n1-b1',output_quote=quote,
                problem='Python 注释格式仍有问题',required_change='将注释对齐到统一列')] if unresolved else [],
                checked_obligation_ids=['f1'])
        elif role=='active_patch':
            result=dict(document_digest=payload['document_digest'],edits=[dict(
                block_id='n1-b1',old_text='为负',new_text='为正',reason='保留原文条件')])
        else:
            pytest.fail('Unexpected provider role: '+role)
        return dict(gaps=[],actions=[],ready_reason='已读取所需原件',result=result)

    monkeypatch.setattr('sourceloom.active_composition.Provider.call',provider)

    def committer(bundle,draft,proposal,allowed,work):
        assert allowed=={'n1-b1'}
        assert proposal['document_digest']==digest(canonical(draft).encode())
        changed=copy.deepcopy(draft)
        changed['blocks'][0]['markdown']=changed['blocks'][0]['markdown'].replace('为负','为正')
        return changed

    monkeypatch.setattr('sourceloom.active_composition.repair',committer)
    monkeypatch.setattr('sourceloom.active_composition.scan',lambda bundle,draft,work:dict(
        canonical_digest=digest(canonical(draft).encode()),format=dict(findings=[],candidates=[])))
    monkeypatch.setattr('sourceloom.active_composition.inspect_draft',lambda *args,**kwargs:[])

    engine=Production(Store(store.root),{'generation_pipeline':'active_composition_v2'})
    simulated_old_checkpoint=False
    for _ in range(12):
        assert engine.run_once()
        saved=store.job(job['id'])
        if (legacy_checkpoint and not simulated_old_checkpoint and calls
                and calls[-1]=='active_patch'
                and saved['stage']=='active_review'):
            # Older saved v2 jobs stopped here with active_format and no
            # post-patch review marker. Resume that persisted state verbatim.
            saved['stage']='active_format'
            saved['active_candidate'].pop('post_patch_review_required',None)
            store.put_job(saved)
            simulated_old_checkpoint=True
        if saved['status']=='ready_for_review':break
        assert saved['status']=='queued',saved.get('error')

    assert saved['status']=='ready_for_review'
    assert calls==['active_plan','active_write','active_review','active_patch','active_review']
    assert '我们假设 `x` 为正' in reviewed_candidates[-1]
    assert len(saved['active_checkpoints'][0]['content_reviews'])==2
    assert (saved['active_checkpoints'][0]['content_reviews'][-1]['draft_digest']
            ==saved['active_checkpoints'][0]['draft_digest'])
    final=store.get(project['id'])
    if post_patch_finding:
        assert saved['active_checkpoints'][0]['unresolved_content_findings']
        assert final['independent_review']['status']=='issues_recorded'
        assert final['production']['semantic_status']=='not_independently_reviewed'
    else:
        assert not saved['active_checkpoints'][0]['unresolved_content_findings']
        assert not saved['active_checkpoints'][0]['content_reviews'][-1]['findings']
        assert final['independent_review']['status']=='passed'
        assert final['production']['semantic_status']=='passed'
