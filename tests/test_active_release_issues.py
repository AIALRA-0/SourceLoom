from sourceloom import checks


def _project(pipeline):
    return {
        "inventory": {"unknown": [], "inventory_review": None},
        "draft": {"blocks": []},
        "plan": None,
        "production": {"pipeline": pipeline},
        "revision": 1,
        "accepted_revision": 0,
    }


def test_active_review_does_not_inherit_legacy_inventory_warning(monkeypatch):
    monkeypatch.setattr(checks, "inspect_draft", lambda *args, **kwargs: [])
    monkeypatch.setattr(checks, "review_complete", lambda project: True)

    issues = checks.release_issues(_project("active_composition_v2"))

    assert {issue["code"] for issue in issues} == {"acceptance"}


def test_legacy_pipeline_still_requires_inventory_review(monkeypatch):
    monkeypatch.setattr(checks, "inspect_draft", lambda *args, **kwargs: [])
    monkeypatch.setattr(checks, "review_complete", lambda project: True)

    issues = checks.release_issues(_project(None))

    assert {issue["code"] for issue in issues} == {"inventory_review", "acceptance"}
