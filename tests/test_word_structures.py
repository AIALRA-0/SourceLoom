from defusedxml import ElementTree as ET
from sourceloom.word_structures import W,A,R,bullet_definition,cached_fields


def test_bullet_uses_definition_and_rejects_numbering_and_picture_markers():
    paragraph=ET.fromstring(f'<w:p xmlns:w="{W}"><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr></w:p>')
    template=f'<w:numbering xmlns:w="{W}"><w:abstractNum w:abstractNumId="7"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/><w:lvlText w:val="•"/></w:lvl></w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="7"/></w:num></w:numbering>'
    result=bullet_definition(paragraph,ET.fromstring(template))
    assert result['marker']=='•' and result['level']==0
    assert bullet_definition(paragraph,ET.fromstring(template.replace('bullet','decimal'))) is None
    assert bullet_definition(paragraph,ET.fromstring(template.replace('•','%1.'))) is None
    assert bullet_definition(paragraph,ET.fromstring(template.replace('</w:lvl>','<w:lvlPicBulletId w:val="1"/></w:lvl>'))) is None
    assert bullet_definition(paragraph,None) is None


def test_cached_fields_preserve_cached_result_and_never_evaluate_instructions():
    template=f'<w:p xmlns:w="{W}" xmlns:a="{A}" xmlns:r="{R}"><w:fldChar w:fldCharType="begin"/><w:instrText> PAGE </w:instrText><w:fldChar w:fldCharType="separate"/><w:t>2</w:t><w:fldChar w:fldCharType="end"/></w:p>'
    result=cached_fields(ET.fromstring(template),False)
    assert result==[{'instruction':' PAGE ','result_text':'2','image_ids':[],'kind':'cached_page_number','evaluated':False}]
    assert cached_fields(ET.fromstring(template.replace(' PAGE ',' DDE external ')),False) is None
    assert cached_fields(ET.fromstring(template.replace('w:fldCharType="end"','w:fldCharType="begin"')),False) is None
    picture=template.replace(' PAGE ',' INCLUDEPICTURE "https://example.org/picture" ').replace('<w:t>2</w:t>','<a:blip r:embed="rId7"/>')
    assert cached_fields(ET.fromstring(picture),False) is None
    assert cached_fields(ET.fromstring(picture),True)[0]['kind']=='cached_embedded_picture'
