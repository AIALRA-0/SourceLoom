from sourceloom.ingest import intake
from sourceloom.source_context import classify_web_chrome
from sourceloom.store import Store


def _gap(obj, label):
    return {"id": label, "object_id": obj["id"], "reason": "needs source review",
            "locator": obj["locator"]}


def test_uploaded_article_resolves_chrome_gaps_but_keeps_article_svg(tmp_path):
    html = (
        '<html><body><nav><ul><li><button>Menu</button></li></ul></nav>'
        '<main><h1>Article</h1><p>' + 'Article facts remain visible. ' * 30 +
        '</p><svg aria-label="Article chart"><text>42</text></svg></main>'
        '<footer><svg aria-hidden="true" alt="Social icon"><path d="M1 1"/></svg></footer>'
        '</body></html>'
    ).encode()
    store = Store(tmp_path)
    source = intake(store, [("article.html", html)])
    nav = next(obj for obj in source["objects"] if obj["locator"].startswith("article.html/node[1]/"))
    article = next(obj for obj in source["objects"] if obj["kind"] == "image"
                   and obj["locator"].startswith("article.html/node[2]/"))
    footer = next(obj for obj in source["objects"] if obj["kind"] == "image"
                  and obj["locator"].startswith("article.html/node[3]/"))
    source["unknown"] = [_gap(nav, "g-nav"), _gap(article, "g-article"), _gap(footer, "g-footer")]

    classified = classify_web_chrome(store, source)
    by_id = {obj["id"]: obj for obj in classified["objects"]}

    assert by_id[nav["id"]]["source_scope"] == "site_chrome"
    assert by_id[footer["id"]]["source_scope"] == "site_chrome"
    assert by_id[article["id"]].get("source_scope") != "site_chrome"
    assert {gap["id"] for gap in classified["unknown"]} == {"g-article"}
    assert {gap["id"] for gap in classified["resolved_chrome_gaps"]} == {"g-nav", "g-footer"}
    assert store.read_blob(source["originals"][0]["sha256"]) == html


def test_uploaded_nav_without_article_scope_is_not_discarded(tmp_path):
    html = b'<html><body><nav><p>Navigation example</p></nav></body></html>'
    store = Store(tmp_path)
    source = intake(store, [("example.html", html)])
    nav = next(obj for obj in source["objects"] if "Navigation example" in obj.get("text", ""))
    source["unknown"] = [_gap(nav, "g-nav-example")]

    classified = classify_web_chrome(store, source)

    assert classified["unknown"] == source["unknown"]
    assert next(obj for obj in classified["objects"] if obj["id"] == nav["id"]).get("source_scope") is None
