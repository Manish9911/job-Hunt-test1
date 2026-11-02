import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app


@pytest.fixture()
def app(tmp_path):
    return create_app(database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}")


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client
