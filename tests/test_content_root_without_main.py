from sourceloom.ingest import intake
from sourceloom.source_context import classify_web_chrome
from sourceloom.store import Store


def test_unique_heading_in_named_content_archives_legacy_page_chrome(tmp_path):
    article = ('Ocean currents carry water through the sea. ' * 12).encode()
    raw = (b'<html><body><header><div class="menu">'
           b'<a href="/education">Education</a>'
           b'<!-- <li class="dropdown education">site menu</li> -->'
           b'</div></header><div class="container wrapper">'
           b'<div class="inner_content"><h1>Ocean Currents</h1><p>' + article +
           b'</p><!-- layout implementation note -->'
           b'<img src="/current.png" alt="Current map"></div></div>'
           b'<footer><p>Site contact information</p></footer></body></html>')
    store = Store(tmp_path)
    source = intake(store, [('snapshot.html', raw)],
                    source_url='https://example.org/articles/currents')

    result = classify_web_chrome(store, source)
    by_text = {obj['text']: obj for obj in result['objects']}
    assert by_text['Education']['source_scope'] == 'site_chrome'
    assert by_text['Site contact information']['source_scope'] == 'site_chrome'
    assert by_text['Ocean Currents'].get('source_scope') != 'site_chrome'
    assert next(obj for obj in result['objects'] if 'layout implementation note' in obj.get('raw', '')
                and obj['kind'] == 'metadata')['source_scope'] == 'source_metadata'
    assert any(obj['kind'] == 'image' and obj.get('source_scope') != 'site_chrome'
               for obj in result['objects'])
    assert store.read_blob(source['originals'][0]['sha256']) == raw


def test_short_or_ambiguous_content_wrapper_is_not_assumed_article(tmp_path):
    raw = (b'<html><body><header><a href="/home">Home</a></header>'
           b'<div class="content"><h1>Title</h1><p>Too short.</p></div>'
           b'<div class="content"><h1>Other</h1><p>Also short.</p></div>'
           b'</body></html>')
    store = Store(tmp_path)
    source = intake(store, [('snapshot.html', raw)],
                    source_url='https://example.org/articles/short')

    result = classify_web_chrome(store, source)
    assert next(obj for obj in result['objects'] if obj['text'] == 'Home').get('source_scope') != 'site_chrome'
