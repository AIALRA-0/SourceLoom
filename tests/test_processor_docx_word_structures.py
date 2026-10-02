from io import BytesIO
import zipfile

from sourceloom import processor
from sourceloom.store import Store


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def test_processor_prepare_resolves_saved_word_bullet_before_freezing(tmp_path):
    body = (f'<w:document xmlns:w="{W}"><w:body><w:p>'
            '<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
            '<w:r><w:t>第一项正文</w:t></w:r></w:p></w:body></w:document>')
    numbering = (f'<w:numbering xmlns:w="{W}">'
                 '<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0">'
                 '<w:numFmt w:val="bullet"/><w:lvlText w:val="•"/>'
                 '</w:lvl></w:abstractNum><w:num w:numId="1">'
                 '<w:abstractNumId w:val="1"/></w:num></w:numbering>')
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("word/document.xml", body)
        archive.writestr("word/numbering.xml", numbering)
    store = Store(tmp_path)
    project = processor.create(store, "Word bullet integration")
    project = processor.prepare(store, project["id"], [("source.docx", output.getvalue())])
    inventory = project["inventory"]
    paragraph = next(obj for obj in inventory["objects"] if obj["kind"] == "text")
    assert paragraph["word_list"]["marker"] == "•"
    assert paragraph["word_list"]["format"] == "bullet"
    assert inventory["word_structure_resolution_version"] == 1
    assert not inventory["unknown"]
    assert len(inventory["resolved_word_structure_gaps"]) == 1
    assert "• 第一项正文" in processor.task_pack(store, project["id"])["source_text"]
