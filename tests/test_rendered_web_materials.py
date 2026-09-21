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
