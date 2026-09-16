import copy
from io import BytesIO

import pytest
from PIL import Image

from sourceloom.active_composition import validate_names, concept_presence, patch_rounds, reserve_patch_attempt
from sourceloom.active_resources import Resources
from sourceloom.ingest import intake
from sourceloom.reading import presentation
from sourceloom.store import Store
from sourceloom.visual_sources import classify_transparent, decorative_resource
from sourceloom.writing import protected_objects


@pytest.mark.parametrize('markup,excluded', [
    ('<img src="blank.png" alt="">', True),
    ('<img src="blank.png">', False),
    ('<img src="blank.png" alt="Meaningful label">', False),
    ('<img src="blank.png" alt="" title="Meaningful title">', False),
])
def test_decorative_requires_pixels_and_explicit_source_role(tmp_path, markup, excluded):
    store = Store(tmp_path)
    raw = BytesIO()
    Image.new('RGBA', (4, 4), (0, 0, 0, 0)).save(raw, 'PNG')
    source = intake(store, [('source.html', markup.encode()), ('blank.png', raw.getvalue())])
    result = classify_transparent(store, source)
    obj = next(o for o in result['objects'] if o['kind'] == 'image')
    assert decorative_resource(obj) is excluded
    assert (obj['id'] not in protected_objects(result)) is excluded
    assert result['resources'] == source['resources']
    assert store.read_blob(obj['resource_id']) == raw.getvalue()
    assert '透明占位图' not in obj['text']


def term():
    return dict(id='complexity', chinese_name='多项式时间', english_name='Polynomial Time',
                naming_status='verified', naming_note='', abbreviations=[],
                name_evidence=[dict(resource_id='reference', quote='Polynomial Time')])


def test_name_evidence_must_match_saved_resource(tmp_path):
    resources = Resources(Store(tmp_path), {'objects': []})
    resources.add('reference', 'Polynomial Time is the term in this reference.')
    plan = {'concepts': [term()]}
    assert validate_names(plan, resources) == plan
    bad = copy.deepcopy(plan)
    bad['concepts'][0]['english_name'] = 'Invented Official Long Name'
    with pytest.raises(ValueError, match='对应的原文证据'):
        validate_names(bad, resources)
    bad = copy.deepcopy(plan)
    bad['concepts'][0]['name_evidence'][0]['quote'] = 'Nonexistent source quotation'
    with pytest.raises(ValueError, match='已保存来源'):
        validate_names(bad, resources)
    bad = copy.deepcopy(plan)
    bad['concepts'][0]['naming_status'] = 'unsearched'
    with pytest.raises(ValueError, match='尚未查证'):
        validate_names(bad, resources)


def test_name_evidence_resolves_only_whitespace_and_existing_verified_pairing(tmp_path):
    resources=Resources(Store(tmp_path),{'objects':[]})
    resources.add('reference','Polynomial\nTime')
    resources.add('np','NP stands for Nondeterministic Polynomial Time.',kind='external',
                  scope='name_evidence_excerpt',abbreviation='NP',english_name='Nondeterministic Polynomial Time')
    concept=term()
    concept['abbreviations']=[dict(short='NP',chinese='非确定性多项式时间',english='Nondeterministic Polynomial Time')]
    result=validate_names({'concepts':[concept]},resources)['concepts'][0]
    assert result['name_evidence'][0]['quote']=='Polynomial\nTime'
    assert result['name_evidence'][1]['resource_id']=='np'
    concept=term();concept['abbreviations']=[dict(short='WRONG',chinese='名称',english='Nondeterministic Polynomial Time')]
    with pytest.raises(ValueError,match='对应的原文证据'):
        validate_names({'concepts':[concept]},resources)



def test_abbreviations_must_reach_actual_body():
    concept = term()
    concept['abbreviations'] = [dict(short='NP', chinese='非确定性多项式时间',
                                    english='Nondeterministic Polynomial Time')]
    draft = {'blocks': [dict(id='b', kind='explanation', markdown='多项式时间（Polynomial Time）')]}
    with pytest.raises(ValueError, match='正文中遗漏'):
        concept_presence([concept], draft)
    draft['blocks'][0]['markdown'] += '\n非确定性多项式时间（Nondeterministic Polynomial Time）'
    concept_presence([concept], draft)


def test_numbered_projection_is_reversible_and_keeps_numeric_titles():
    p = {'draft': {'blocks': [dict(id='b', markdown='## 起点\n### 原因\n## 2026 年记录')]}}
    result = presentation(p, 'numbered')
    assert result['draft']['blocks'][0]['markdown'] == '## 1. 起点\n### 1.1. 原因\n## 2026 年记录'
    assert presentation(result, 'none')['draft'] == p['draft']
    assert p['draft']['blocks'][0]['markdown'].startswith('## 起点')


def test_local_patch_limit_counts_content_and_format_together():
    assert patch_rounds({'patch_history': [{}], 'format_records': [
        {'edits': []}, {'edits': [{'old_text': 'x', 'new_text': 'y'}]}]}) == 2
    candidate={}
    reserve_patch_attempt(candidate)
    reserve_patch_attempt(candidate)
    with pytest.raises(ValueError,match='两轮'):
        reserve_patch_attempt(candidate)
    assert patch_rounds(candidate)==2


def test_content_link_addresses_reach_agents_and_resume_old_catalog(tmp_path):
    source={'objects':[dict(id='link',kind='link',text='Read more',locator='input/a',
                            target='https://example.org/topic',original_target='./topic')]}
    store=Store(tmp_path)
    resources=Resources(store,source)
    assert resources.catalog()[0]['target']=='https://example.org/topic'
    old=copy.deepcopy(resources.state)
    old['entries']['link'].pop('target');old['entries']['link'].pop('original_target')
    resumed=Resources(store,source,old)
    assert resumed.catalog()[0]['target']=='https://example.org/topic'
    changed=copy.deepcopy(source);changed['objects'][0]['target']='https://example.org/different'
    with pytest.raises(ValueError,match='版本发生变化'):
        Resources(store,changed,resumed.state)
