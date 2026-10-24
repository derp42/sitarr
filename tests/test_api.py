import pytest
from fastapi.testclient import TestClient

from sitarr.app import create_app
from sitarr.config import Config, Server, Settings


@pytest.fixture
def client(tmp_path):
    config = Config(tmp_path)
    config.save(Settings(servers=[Server(id="home", url="http://jellyfin", token="hidden-secret")]))
    app = create_app(config, start_worker=False)
    with TestClient(app) as c:
        c.auth = (config.username, config.password)
        yield c


def test_ui_and_api_require_auth(client):
    assert client.get("/health", auth=("wrong", "wrong")).status_code == 200
    assert client.get("/", auth=("wrong", "wrong")).status_code == 401
    assert client.get("/api/settings", auth=("wrong", "wrong")).status_code == 401
    assert client.get("/").status_code == 200


def test_masked_secrets_survive_settings_save(client):
    settings = client.get("/api/settings").json()
    assert settings["servers"][0]["token"] == "********"
    assert "hidden-secret" not in client.get("/api/settings").text
    assert client.put("/api/settings", json=settings).status_code == 200
    assert client.app.state.config.settings.servers[0].token == "hidden-secret"


def test_csrf_write_rejected(client):
    assert (
        client.post("/api/jobs", headers={"Origin": "https://evil.test"}, json={"kind": "scan"}).status_code
        == 403
    )
    assert client.post("/api/jobs", json={"kind": "scan"}).status_code == 200


def test_bad_server_settings_do_not_leak_secrets(client):
    settings = client.get("/api/settings").json()
    settings["servers"][0].update(url="file:///etc/passwd", token="private-value")
    response = client.put("/api/settings", json=settings)
    assert response.status_code == 422
    assert "private-value" not in response.text


def test_jobs_are_deduplicated_and_unknown_title_rejected(client):
    first = client.post("/api/jobs", json={"kind": "scan"}).json()
    second = client.post("/api/jobs", json={"kind": "scan"}).json()
    assert first == second
    assert client.post("/api/jobs", json={"kind": "resolve", "key": "missing"}).status_code == 404


def test_security_headers_and_static_assets(client):
    assert "frame-ancestors 'none'" in client.get("/").headers["content-security-policy"]
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200
