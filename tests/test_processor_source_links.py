"""Source-relative links are recovered from recorded identity, never a guess."""
import copy
from io import BytesIO
import zipfile

from bs4 import BeautifulSoup
import pytest

from sourceloom import processor
from sourceloom.source_links import known_link_targets, restore_source_links
from sourceloom.store import Store, digest


@pytest.mark.parametrize('fmt', ['md','html'])
def test_recorded_relative_links_survive_projection_preview_and_export(tmp_path, fmt):
    markdown = '[Related section](guide/next.html)\n\n[Root reference](/reference/)\n'
    raw = markdown.encode() if fmt=='md' else (
        '<p><a href="guide/next.html">Related section</a></p>'
        '<p><a href="/reference/">Root reference</a></p>').encode()
    store = Store(tmp_path)
    p = processor.create(store,'Controlled relative links')
    p = processor.prepare(store,p['id'],[('source.'+fmt,raw)],source_url='https://example.org/articles/current.html')
    inventory = copy.deepcopy(p['inventory'])
    assert '(https://example.org/articles/guide/next.html)' in p['processor']['source_text']
    saved = processor.save_result(store,p['id'],markdown)
    version = processor.active_version(saved)
    assert version['markdown']==markdown and version['digest']==digest(markdown.encode())
    for target in ('preview','readweave'):
        doc = BeautifulSoup(processor.compile_result(saved,markdown,target=target)['html'],'html.parser')
        assert doc.find('a',href='https://example.org/articles/guide/next.html')
        assert doc.find('a',href='https://example.org/reference/')
    with zipfile.ZipFile(BytesIO(processor.export_package(store,saved))) as z:
        doc = BeautifulSoup(z.read('material.html'),'html.parser')
        assert doc.find('a',href='https://example.org/articles/guide/next.html')
    assert saved['inventory']==inventory


def test_published_document_base_wins_over_raw_download_url_without_new_inference(tmp_path):
    raw = (b'---\nslug: Web/Example/topic\n---\n\n'
           b'[Sibling](sibling)\n\n[Root](/en-US/docs/Web/Other)')
    store = Store(tmp_path)
    p = processor.create(store,'Controlled published document base')
    p = processor.prepare(store,p['id'],[('source.md',raw)],
        source_url='https://raw.githubusercontent.com/mdn/content/main/files/en-us/example/index.md')
    links = {o['original_target']:o['target'] for o in p['inventory']['objects'] if o['kind']=='link'}
    assert links['sibling']=='https://developer.mozilla.org/en-US/docs/Web/Example/sibling'
    assert links['sibling'] in p['processor']['source_text']
    assert 'https://raw.githubusercontent.com/mdn/content/main/files/en-us/example/sibling' not in p['processor']['source_text']
    returned = '[Sibling](sibling)\n\n[Root](/en-US/docs/Web/Other)'
    doc = BeautifulSoup(processor.compile_result(p,returned)['html'],'html.parser')
    assert doc.find('a',href=links['sibling']) and doc.find('a',href=links['/en-US/docs/Web/Other'])


def test_unknown_relative_links_and_ambiguous_saved_targets_are_not_guessed(tmp_path):
    store = Store(tmp_path)
    p = processor.create(store,'Controlled ambiguous links')
    p = processor.prepare(store,p['id'],[('source.md',b'[A](next.html)')])
    # Two legitimate saved document bases may resolve one literal differently.
    p['inventory']['objects'] += [
        dict(id='link-extra-1',kind='link',original_target='next.html',target='https://one.example/next.html'),
        dict(id='link-extra-2',kind='link',original_target='next.html',target='https://two.example/next.html')]
    assert 'next.html' not in known_link_targets(p['inventory'])
    returned = '[A](next.html)\n\n[Unknown](unseen.html)'
    doc = BeautifulSoup(processor.compile_result(p,returned)['html'],'html.parser')
    assert not doc.find('a',href=True)
    assert 'A' in doc.get_text() and 'Unknown' in doc.get_text()


@pytest.mark.parametrize('unsafe', ['javascript:alert(1)','data:text/html,evil','file:///secret',
                                    'https://user:secret@example.org/a','https://[broken'])
def test_unsafe_saved_targets_cannot_authorize_a_relative_or_active_link(unsafe):
    inv = dict(objects=[dict(kind='link',original_target='relative',target=unsafe)])
    assert not known_link_targets(inv)
    doc = BeautifulSoup(restore_source_links(
        '<a href="'+unsafe+'">unsafe</a>',{}),'html.parser')
    assert not doc.find('a',href=True)


def test_one_unsafe_conflict_blocks_an_otherwise_safe_saved_target():
    inv = dict(objects=[dict(kind='link',original_target='relative',target=target)
                        for target in ['https://example.org/a','javascript:bad()']])
    assert not known_link_targets(inv)
    # Inconsistent imported metadata cannot bless an explicit active scheme.
    doc = BeautifulSoup(restore_source_links('<a href="javascript:bad()">text</a>',
                        {'javascript:bad()':'https://example.org/a'}),'html.parser')
    assert not doc.find('a',href=True)


@pytest.mark.parametrize('raw,expected', [
    ('http:www.example.org/report?q=1#part','http://www.example.org/report?q=1#part'),
    ('HTTPS:/www.example.org/a','https://www.example.org/a'),
    ('http:missing-host',None),
    ('http:www.example.org:invalid/a',None),
    ('https:www.example.org/a\\evil',None),
    ('https://user:secret@www.example.org/a',None),
])
def test_native_web_protocol_normalization_keeps_safe_host_only(raw, expected):
    doc = BeautifulSoup(restore_source_links('<a href="'+raw+'">Reference</a>',{}),'html.parser')
    assert doc.find('a').get('href')==expected


def test_fragment_literal_code_and_local_image_keep_their_existing_roles(tmp_path):
    store = Store(tmp_path)
    p = processor.create(store,'Controlled local roles')
    p = processor.prepare(store,p['id'],[('source.md',b'[Reference](next.html)')],
                          source_url='https://example.org/current.html')
    returned = ('<a id="chapter"></a>\n\n[Chapter](#chapter)\n\n'
                '<img src="assets/'+'a'*64+'" alt="local">\n\n'
                '```html\n<a href="next.html">literal</a>\n```')
    compiled = processor.compile_result(p,returned)
    doc = BeautifulSoup(compiled['html'],'html.parser')
    assert doc.find('a',href='#chapter')
    assert doc.find('img')['src']=='assets/'+'a'*64
    assert doc.find('pre').get_text()=='<a href="next.html">literal</a>\n'


def test_projection_builds_one_inventory_link_index(tmp_path, monkeypatch):
    store = Store(tmp_path)
    p = processor.create(store,'Controlled repeated links')
    p = processor.prepare(store,p['id'],[('source.md',('[Related](next.html)\n\n'*80).encode())],
                          source_url='https://example.org/current.html')
    original = processor.known_link_targets
    calls = []
    def counted(inv):
        calls.append(inv)
        return original(inv)
    monkeypatch.setattr(processor,'known_link_targets',counted)
    text = processor.source_text_projection(p['inventory'])
    assert len(calls)==1
    assert text.count('(https://example.org/next.html)')==80


def test_original_table_uses_the_same_recorded_link_map_without_editing_cells(tmp_path):
    raw = (b'<table><tr><th>Resource</th><th>Value</th></tr>'
           b'<tr><td><a href="next.html">First</a></td><td>12</td></tr>'
           b'<tr><td>Second</td><td>24</td></tr></table>')
    store = Store(tmp_path)
    p = processor.create(store,'Controlled table links')
    p = processor.prepare(store,p['id'],[('source.html',raw)],source_url='https://example.org/current.html')
    table = next(o for o in p['inventory']['objects'] if o['kind']=='table')
    returned = '{{source:'+table['id']+'}}'
    doc = BeautifulSoup(processor.compile_result(p,returned)['html'],'html.parser')
    assert doc.find('a',href='https://example.org/next.html')
    assert [cell.get_text() for cell in doc.select('table td')]==['First','12','Second','24']
    assert table['raw'].encode()==raw
