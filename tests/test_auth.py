from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.router import router


def test_api_key_authentication() -> None:
    key = "test-key-" * 4
    settings = Settings(
        yandex_api_key="yandex-key",
        yandex_folder_id="folder",
        api_key=key,
        _env_file=None,
    )
    app = FastAPI()
    app.state.settings = settings
    app.state.providers = {}
    app.include_router(router)
    client = TestClient(app)

    assert client.get("/v1/models").status_code == 401
    assert (
        client.get("/v1/models", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    assert (
        client.get("/v1/models", headers={"Authorization": f"Basic {key}"}).status_code
        == 401
    )
    assert (
        client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).status_code
        == 200
    )
