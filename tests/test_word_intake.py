from io import BytesIO
import zipfile

from sourceloom.store import Store
from sourceloom.ingest import intake

W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
MC='http://schemas.openxmlformats.org/markup-compatibility/2006'
WP='http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'
A='http://schemas.openxmlformats.org/drawingml/2006/main'


def document(body,extra=None):
    out=BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        z.writestr('word/document.xml',f'<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body>{body}</w:body></w:document>')
        for path,text in (extra or {}).items():z.writestr(path,text)
    return out.getvalue()


def test_table_cells_pronouns_link_targets_and_tail_are_preserved_once(tmp_path):
    raw=document('<w:p><w:r><w:t>我们保留你给出的条件</w:t><w:tab/><w:t>包括例外</w:t></w:r>'
                 '<w:hyperlink r:id="r1"><w:r><w:t>原资料</w:t></w:r></w:hyperlink></w:p>'
                 '<w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>6.5</w:t></w:r></w:p></w:tc>'
                 '<w:tc><w:p><w:r><w:t>否</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
                 '<w:p><w:r><w:t>最后的条件不可省略</w:t></w:r></w:p>',
                 {'word/_rels/document.xml.rels':'<Relationships><Relationship Id="r1" Target="https://example.com/source" TargetMode="External"/></Relationships>'})
    s=Store(tmp_path);inv=intake(s,[('source.docx',raw)])
    assert not inv['unknown']
    assert s.read_blob(inv['originals'][0]['sha256'])==raw
    assert inv['objects'][0]['text']=='我们保留你给出的条件\t包括例外原资料'
    assert next(o for o in inv['objects'] if o['kind']=='link')['target']=='https://example.com/source'
    table=next(o for o in inv['objects'] if o['kind']=='table')
    assert table['cells']==[[dict(text='6.5',rowspan='1',colspan='2'),dict(text='否',rowspan='1',colspan='1')]]
    assert sum(o['text']=='6.5' for o in inv['objects'])==0
    assert inv['objects'][-1]['text']=='最后的条件不可省略'


def test_revisions_vertical_merges_and_unknown_embedded_parts_remain_gaps(tmp_path):
    raw=document('<w:p><w:del><w:r><w:delText>原来的我</w:delText></w:r></w:del><w:ins><w:r><w:t>修改后的我</w:t></w:r></w:ins></w:p>'
                 '<w:tbl><w:tr><w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc></w:tr></w:tbl>',
                 {'word/charts/chart1.xml':'<chart>retained</chart>'})
    inv=intake(Store(tmp_path),[('source.docx',raw)])
    reasons=' '.join(g['reason'] for g in inv['unknown'])
    assert 'del' in reasons and 'ins' in reasons and '纵向合并' in reasons and '嵌入部件' in reasons
    assert '原来的我修改后的我' in inv['objects'][0]['text']


def test_footnote_reference_and_story_are_bound_without_duplicate_text(tmp_path):
    raw=document('<w:p><w:r><w:t>仅在条件成立时</w:t><w:footnoteReference w:id="7"/></w:r></w:p>',
                 {'word/footnotes.xml':f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="7"><w:p><w:r><w:t>还有这一项例外</w:t></w:r></w:p></w:footnote></w:footnotes>'})
    inv=intake(Store(tmp_path),[('source.docx',raw)])
    reference=next(o for o in inv['objects'] if o.get('reference_id')=='7')
    note=next(o for o in inv['objects'] if o['kind']=='footnote')
    assert reference['parent_id']==inv['objects'][0]['id']
    assert note['story']['id']=='7' and note['text']=='还有这一项例外'
    assert sum(o['text']=='还有这一项例外' for o in inv['objects'])==1


def test_alternate_content_prefers_modern_branch_without_duplicate_fallback_or_detached_media(tmp_path):
    from PIL import Image
    modern=BytesIO();Image.new('RGB',(2,2),'white').save(modern,'PNG')
    fallback=BytesIO();Image.new('RGB',(2,2),'white').save(fallback,'GIF')
    body=(f'<w:p xmlns:mc="{MC}" xmlns:wp="{WP}" xmlns:a="{A}"><w:r><w:t>唯一正文</w:t></w:r>'
          f'<mc:AlternateContent><mc:Choice Requires="wps"><w:r><wp:anchor><a:blip r:embed="rModern"/></wp:anchor></w:r></mc:Choice>'
          f'<mc:Fallback><w:r><w:pict><v:fill xmlns:v="urn:schemas-microsoft-com:vml" r:id="rFallback"/></w:pict></w:r></mc:Fallback>'
          f'</mc:AlternateContent></w:p>')
    raw=document(body,{
        'word/_rels/document.xml.rels':'<Relationships><Relationship Id="rModern" Target="media/modern.png"/><Relationship Id="rFallback" Target="media/fallback.gif"/></Relationships>',
        'word/media/modern.png':modern.getvalue(),'word/media/fallback.gif':fallback.getvalue()})
    inv=intake(Store(tmp_path),[('source.docx',raw)])
    assert sum(o.get('text')=='唯一正文' for o in inv['objects'])==1
    assert len([o for o in inv['objects'] if o['kind']=='image'])==1
    assert not any('anchor' in gap['reason'] or 'pict' in gap['reason'] or '归属' in gap['reason'] for gap in inv['unknown'])


def test_valid_vertical_merge_becomes_rowspan_without_gap(tmp_path):
    raw=document('<w:tbl>'
        '<w:tr><w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>B1</w:t></w:r></w:p></w:tc></w:tr>'
        '<w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc>'
        '<w:tc><w:p><w:r><w:t>B2</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
    inv=intake(Store(tmp_path),[('source.docx',raw)])
    table=next(o for o in inv['objects'] if o['kind']=='table')
    assert table['cells'][0][0]['rowspan']=='2'
    assert table['cells'][1]==[dict(text='B2',rowspan='1',colspan='1')]
    assert not any('纵向合并' in gap['reason'] for gap in inv['unknown'])


def test_header_image_is_preserved_as_document_metadata_without_visual_generation(tmp_path):
    from PIL import Image
    picture=BytesIO();Image.new('RGB',(40,20),'white').save(picture,'JPEG')
    header=(f'<w:hdr xmlns:w="{W}" xmlns:r="{R}" xmlns:a="{A}">'
            '<w:p><w:r><w:drawing><a:blip r:embed="rLogo"/></w:drawing></w:r></w:p></w:hdr>')
    raw=document('<w:p><w:r><w:t>正文</w:t></w:r></w:p>',{
        'word/header1.xml':header,
        'word/_rels/header1.xml.rels':'<Relationships><Relationship Id="rLogo" Target="media/logo.jpg"/></Relationships>',
        'word/media/logo.jpg':picture.getvalue()})
    inv=intake(Store(tmp_path),[('source.docx',raw)])
    image=next(o for o in inv['objects'] if o['kind']=='image')
    assert image['source_scope']=='source_metadata'
    assert image['visual_classification']=={'method':'word_story_part','source_role':'source_metadata'}
