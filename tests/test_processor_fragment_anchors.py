"""Native article bookmarks survive without allowing arbitrary UI IDs."""
import copy
from io import BytesIO
import zipfile

from bs4 import BeautifulSoup
import pytest

from sourceloom import processor
from sourceloom.export import safe_html
from sourceloom.store import Store,digest


@pytest.mark.parametrize('anchor',['contributor-license-agreements','chapter_2','A1'])
def test_safe_explicit_anchor_and_fragment_survive_sanitizer(anchor):
    raw=f'<a id="{anchor}" onclick="bad()"></a><a href="#{anchor}">Go</a>'
    doc=BeautifulSoup(processor.safe_html(raw),'html.parser')
    assert doc.find(id=anchor) is not None
    assert doc.find('a',href='#'+anchor) is not None
    assert 'onclick' not in str(doc)
    assert 'id=' not in safe_html(raw)
    assert 'id=' not in processor.safe_html('<p id="arbitrary-ui-id">Text</p>')


@pytest.mark.parametrize('anchor',['a b','../chapter','a/other','1-invalid','a:bad','x'*161])
def test_unsafe_or_unsupported_anchor_names_are_not_whitelisted(anchor):
    raw=f'<a id="{anchor}" href="javascript:alert(1)" onfocus="bad()">Text</a>'
    doc=BeautifulSoup(processor.safe_html(raw),'html.parser')
    assert not doc.find(id=True)
    assert 'onfocus' not in str(doc) and 'javascript:' not in str(doc)


def test_duplicate_bookmarks_in_one_fragment_do_not_select_an_arbitrary_target():
    doc=BeautifulSoup(processor.safe_html('<a id="duplicate"></a><a id="duplicate"></a>'),'html.parser')
    assert not doc.find(id='duplicate')


def test_cross_resource_fragment_and_readweave_export_keep_one_real_target(tmp_path):
    store=Store(tmp_path)
    project=processor.create(store,'Synthetic bookmark article')
    prepared=processor.prepare(store,project['id'],[('source.md',b'# Source\n\n```python\nprint(1)\n```')])
    code=next(r for r in prepared['processor']['resources'] if r['kind']=='code')
    markdown=('# Article\n\n<a id="chapter-one"></a>\n\n## First section\n\n'
              +code['marker']+'\n\n[Return to first section](#chapter-one)\n\n'
              '[Missing target remains missing](#not-present)')
    inventory=copy.deepcopy(prepared['inventory'])
    saved=processor.save_result(store,project['id'],markdown)
    assert processor.active_version(saved)['markdown']==markdown
    assert processor.active_version(saved)['digest']==digest(markdown.encode())
    for target in ('preview','readweave'):
        doc=BeautifulSoup(processor.compile_result(saved,markdown,target=target)['html'],'html.parser')
        assert len(doc.find_all(id='chapter-one'))==1
        assert doc.find('a',href='#chapter-one') is not None
        assert doc.find(id='not-present') is None
        assert doc.find('pre').get_text()=='print(1)\n'
    with zipfile.ZipFile(BytesIO(processor.export_package(store,saved))) as archive:
        doc=BeautifulSoup(archive.read('material.html'),'html.parser')
        assert len(doc.find_all(id='chapter-one'))==1
        assert doc.find('a',href='#chapter-one') is not None
    assert saved['inventory']==inventory


def test_duplicate_bookmarks_across_resource_chunks_are_not_ambiguous(tmp_path):
    store=Store(tmp_path)
    project=processor.create(store,'Synthetic duplicate bookmarks')
    prepared=processor.prepare(store,project['id'],[('source.md',b'```text\noriginal\n```')])
    code=next(r for r in prepared['processor']['resources'] if r['kind']=='code')
    markdown='<a id="duplicate"></a>\n\n'+code['marker']+'\n\n<a id="duplicate"></a>\n\n[Go](#duplicate)'
    for target in ('preview','readweave'):
        doc=BeautifulSoup(processor.compile_result(prepared,markdown,target=target)['html'],'html.parser')
        assert not doc.find(id='duplicate')
        assert doc.find('a',href='#duplicate') is not None


def test_source_bookmark_and_literal_code_anchor_remain_in_their_existing_roles(tmp_path):
    store=Store(tmp_path)
    project=processor.create(store,'Synthetic source bookmark')
    prepared=processor.prepare(store,project['id'],[('source.md',b'Frozen source')])
    markdown=('<div id="loom-source-footnote-1"><p>Actual note</p></div>\n\n'
              '[Note](#loom-source-footnote-1)\n\n```html\n<a id="literal-code"></a>\n```')
    doc=BeautifulSoup(processor.compile_result(prepared,markdown)['html'],'html.parser')
    assert doc.find(id='loom-source-footnote-1') is not None
    assert doc.find('a',href='#loom-source-footnote-1') is not None
    assert doc.find(id='literal-code') is None
    assert '<a id="literal-code"></a>' in doc.find('pre').get_text()
