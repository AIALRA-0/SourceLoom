from sourceloom.ingest import intake
from sourceloom.materials import bind_web_material_manifest,require_complete_web_materials
from sourceloom.store import Store
from sourceloom.writing import protected_objects


def link_source(tmp_path):
    html=(b'<main><article><a data-sourceloom-capture-id="link-1" '
          b'href="/source-destination">Open source</a></article></main>')
    source=intake(Store(tmp_path),[('snapshot.html',html)],'https://example.org/start')
    link=next(obj for obj in source['objects'] if obj['kind']=='link')
    return source,link


def bind_link(source, rendered_target):
    return bind_web_material_manifest(source,{
        'images':[],
        'rendered_objects':[dict(id='link-1',kind='link',scope='article',
                                 target=rendered_target)],
        'rendered':True,
    })


def test_js_rewritten_link_keeps_both_destinations_and_records_nonblocking_discrepancy(tmp_path):
    source,link=link_source(tmp_path)
    parser_target=link['target']
    original_target=link['original_target']

    result=bind_link(source,'https://example.org/client-destination')
    rebound=next(obj for obj in result['objects'] if obj['id']==link['id'])
    capture=result['web_snapshot']['rendered_capture']['objects'][0]
    discrepancies=result['web_snapshot']['link_target_discrepancies']

    assert rebound['original_target']==original_target
    assert rebound['target']==parser_target
    assert rebound['rendered_target']=='https://example.org/client-destination'
    assert rebound['rendered_target_discrepancy']['source_id']==link['id']
    assert capture['source_id']==link['id']
    assert capture['target_comparison']=='canonical_mismatch'
    assert len(discrepancies)==1
    assert discrepancies[0]==rebound['rendered_target_discrepancy']
    assert discrepancies[0]['original_target']==original_target
    assert discrepancies[0]['parser_target']==parser_target
    assert discrepancies[0]['rendered_target']=='https://example.org/client-destination'

    # Generation intake remains available, and the protected source link keeps
    # the parser destination rather than silently switching to the live one.
    require_complete_web_materials(result)
    literal=protected_objects(result)[link['id']]
    assert parser_target in literal
    assert 'client-destination' not in literal


def test_canonically_equivalent_link_spellings_do_not_create_discrepancies(tmp_path):
    cases=(
        'https://example.org/source-destination/',
        'https://example.org/source-destination#',
    )
    for index,rendered_target in enumerate(cases):
        source,link=link_source(tmp_path/f'case-{index}')
        result=bind_link(source,rendered_target)
        rebound=next(obj for obj in result['objects'] if obj['id']==link['id'])
        capture=result['web_snapshot']['rendered_capture']['objects'][0]

        assert rebound['original_target']=='/source-destination'
        assert rebound['target']=='https://example.org/source-destination'
        assert rebound['rendered_target']==rendered_target
        assert 'rendered_target_discrepancy' not in rebound
        assert capture['target_comparison']=='canonical_match'
        assert result['web_snapshot']['link_target_discrepancies']==[]
        require_complete_web_materials(result)
