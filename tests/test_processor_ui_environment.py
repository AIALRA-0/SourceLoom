from fastapi.testclient import TestClient

from sourceloom.app import create_app
from sourceloom.config import load_config


def test_acceptance_label_is_explicit_and_absent_from_user_instance(tmp_path, monkeypatch):
    config = load_config() | {"data_dir": str(tmp_path / "data"), "auth_mode": "local"}
    monkeypatch.delenv("SOURCELOOM_ACCEPTANCE", raising=False)
    with TestClient(create_app(config)) as user:
        assert user.get("/api/processor/capabilities").json()["environment"] == "user"
    monkeypatch.setenv("SOURCELOOM_ACCEPTANCE", "1")
    with TestClient(create_app(config | {"data_dir": str(tmp_path / "acceptance")})) as acceptance:
        assert acceptance.get("/api/processor/capabilities").json()["environment"] == "development_acceptance"
