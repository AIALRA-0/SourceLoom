from io import BytesIO

from PIL import Image

from sourceloom.ingest import intake
from sourceloom.materials import bind_web_material_manifest,require_complete_web_materials
from sourceloom.store import Store
from sourceloom.writing import protected_objects
from sourceloom.media import reading_draft
from sourceloom.source_context import classify_web_chrome


def png():
    out=BytesIO();Image.new('RGB',(40,30),'white').save(out,'PNG');return out.getvalue()


def test_rendered_browser_inventory_binds_video_canvas_table_and_links(tmp_path):
    html=b'''<main><article>
      <video data-sourceloom-capture-id="web-object-0001" data-sourceloom-capture-kind="video"
             src="https://media.example/video.mp4"></video>
      <canvas data-sourceloom-capture-id="web-object-0002" data-sourceloom-capture-kind="canvas"></canvas>
      <table data-sourceloom-capture-id="web-object-0003" data-sourceloom-capture-kind="table"><tr><td>7</td></tr></table>
      <a data-sourceloom-capture-id="web-object-0004" data-sourceloom-capture-kind="link" href="/details">Details</a>
    </article></main>'''
    captures=png()
    aliases={'capture://web-object-0001':'web-captures/video.png',
             'capture://web-object-0002':'web-captures/canvas.png'}
    source=intake(Store(tmp_path),[('snapshot.html',html),('web-captures/video.png',captures),
        ('web-captures/canvas.png',captures)],'https://example.org/article',aliases)
    rows=[dict(id='web-object-0001',kind='video',scope='article',target='https://media.example/video.mp4'),
          dict(id='web-object-0002',kind='canvas',scope='article',target=''),
          dict(id='web-object-0003',kind='table',scope='article',target=''),
          dict(id='web-object-0004',kind='link',scope='article',target='https://example.org/details')]
    source=bind_web_material_manifest(source,{'images':[],'rendered_objects':rows,'rendered':True})
    require_complete_web_materials(source)
    captured={o['capture_id']:o for o in source['objects'] if o.get('capture_id')}
    assert captured['web-object-0001']['kind']=='media'
    assert captured['web-object-0001']['media_type']=='video'
    assert captured['web-object-0001']['resource_id']
    assert captured['web-object-0002']['kind']=='media'
    assert captured['web-object-0003']['kind']=='table'
    assert captured['web-object-0004']['kind']=='link'
    assert 'https://media.example/video.mp4' in protected_objects(source)[captured['web-object-0001']['id']]
    sid=captured['web-object-0001']['id'];literal=protected_objects(source)[sid]
    draft=reading_draft({'inventory':source,'draft':{'blocks':[dict(id='b',kind='object',
        markdown=literal,embedded_object_ids=[sid])]}})
    assert '<details><summary>原视频 · ' in draft['blocks'][0]['markdown']


def test_rendered_top_navigation_is_archived_without_hiding_article_link(tmp_path):
    raw=b'''<body><a data-sourceloom-capture-id="top" href="/manifesto">Manifesto</a>
      <a data-sourceloom-capture-id="body" href="/paper">Paper</a></body>'''
    source=intake(Store(tmp_path),[('snapshot.html',raw)],'https://example.org/post')
    rows=[dict(id='top',kind='link',scope='page',y=20),
          dict(id='body',kind='link',scope='page',y=700)]
    source=bind_web_material_manifest(source,{'images':[],'rendered_objects':rows,'rendered':True})
    result=classify_web_chrome(Store(tmp_path),source)
    links={obj['text']:obj for obj in result['objects'] if obj['kind']=='link'}
    assert links['Manifesto']['source_scope']=='site_chrome'
    assert links['Manifesto']['visual_classification']['method']=='rendered_top_navigation_position'
    assert links['Paper'].get('source_scope') is None


def test_dynamic_capture_failure_blocks_generation_before_writer(tmp_path):
    source=intake(Store(tmp_path),[('snapshot.html',b'<main><p>Text</p></main>')],
                  'https://example.org/article')
    source=bind_web_material_manifest(source,{'images':[],'rendered_objects':[],'rendered':False})
    try:require_complete_web_materials(source)
    except ValueError as exc:assert '动态网页尚未完成' in str(exc)
    else:raise AssertionError('missing rendered page must not reach generation')


def test_nested_rendered_visual_without_parser_node_becomes_a_source_object(tmp_path):
    source=intake(Store(tmp_path),[('snapshot.html',b'<main><svg><svg></svg></svg></main>'),
        ('web-captures/nested.png',png())],'https://example.org/chart')
    row=dict(id='web-object-0015',kind='image',scope='article',target='',alt='',
        preview_name='web-captures/nested.png',x=20,y=30,width=11,height=11)
    source=bind_web_material_manifest(source,{'images':[],'rendered_objects':[row],'rendered':True})
    require_complete_web_materials(source)
    captured=next(obj for obj in source['objects'] if obj.get('capture_id')=='web-object-0015')
    assert captured['kind']=='image' and captured['resource_id']
    assert captured['source_scope']=='article_media'
    rendered=source['web_snapshot']['rendered_capture']
    assert rendered['gaps']==[] and rendered['objects'][0]['source_id']==captured['id']


def test_interactive_svg_descendant_is_bound_to_complete_parent_capture(tmp_path):
    source=intake(Store(tmp_path),[('snapshot.html',b'<main><svg></svg></main>'),
        ('web-captures/graph.png',png())],'https://example.org/graph')
    rows=[dict(id='graph',kind='image',scope='article',target='',alt='',
            preview_name='web-captures/graph.png',x=100,y=200,width=400,height=120),
          dict(id='graph-link',kind='link',scope='article',target={},text='Node',
            x=120,y=220,width=80,height=30)]
    source=bind_web_material_manifest(source,{
        'images':[],'rendered_objects':rows,'rendered':True})
    require_complete_web_materials(source)
    rendered=source['web_snapshot']['rendered_capture']
    assert rendered['gaps']==[]
    assert rendered['objects'][1]['ready'] is True
    assert rendered['objects'][1]['covered_by_capture']=='graph'
    assert rendered['objects'][1]['source_id']==rendered['objects'][0]['source_id']
    child=rendered['objects'][1]
    child.update(ready=False,source_id='',resource_id='')
    child.pop('covered_by_capture')
    rendered['gaps']=[{'material_id':'graph-link','kind':'link'}]
    require_complete_web_materials(source)
    assert child['ready'] is True and rendered['gaps']==[]


def test_nested_rendered_binding_uses_nearest_ready_dom_ancestor(tmp_path):
    source=intake(Store(tmp_path),[
        ('snapshot.html',b'<main><svg data-sourceloom-capture-id="outer">'
         b'<svg data-sourceloom-capture-id="inner"><a data-sourceloom-capture-id="child">Node</a>'
         b'</svg></svg></main>'),
        ('web-captures/outer.png',png()),('web-captures/inner.png',png())],
        'https://example.org/nested')
    rows=[
        dict(id='outer',kind='image',scope='article',preview_name='web-captures/outer.png',
             x=0,y=0,width=300,height=300,parent_capture_ids=[]),
        dict(id='inner',kind='image',scope='article',preview_name='web-captures/inner.png',
             x=20,y=20,width=100,height=100,parent_capture_ids=['outer']),
        dict(id='child',kind='link',scope='article',text='Node',
             x=30,y=40,width=10,height=10,parent_capture_ids=['inner','outer']),
    ]
    source=bind_web_material_manifest(source,{'images':[],'rendered_objects':rows,'rendered':True})
    require_complete_web_materials(source)

    child=source['web_snapshot']['rendered_capture']['objects'][2]
    assert child['ready'] is True
    assert child['covered_by_capture']=='inner'
    inner=source['web_snapshot']['rendered_capture']['objects'][1]
    assert child['source_id']==inner['source_id']
    assert child['resource_id']==inner['resource_id']


def test_legacy_overlapping_rendered_parents_remain_explicitly_ambiguous(tmp_path):
    source=intake(Store(tmp_path),[
        ('snapshot.html',b'<main></main>'),
        ('web-captures/left.png',png()),('web-captures/right.png',png())],
        'https://example.org/overlap')
    rows=[
        dict(id='left',kind='image',scope='article',preview_name='web-captures/left.png',
             x=0,y=0,width=100,height=100),
        dict(id='right',kind='image',scope='article',preview_name='web-captures/right.png',
             x=50,y=0,width=100,height=100),
        dict(id='child',kind='link',scope='article',x=60,y=10,width=20,height=20),
    ]
    source=bind_web_material_manifest(source,{'images':[],'rendered_objects':rows,'rendered':True})
    child=source['web_snapshot']['rendered_capture']['objects'][2]

    try:require_complete_web_materials(source)
    except ValueError as exc:assert '动态网页材料尚未完整绑定' in str(exc)
    else:raise AssertionError('overlapping parent captures without ancestry must remain unresolved')

    assert child['ready'] is False
    assert child['source_id']=='' and child['resource_id']==''
    gap=source['web_snapshot']['rendered_capture']['gaps'][0]
    assert gap['material_id']=='child'
    assert gap['failure']=='ambiguous_parent_capture_without_dom_ancestry'
    assert set(gap['candidate_parent_captures'])=={'left','right'}
