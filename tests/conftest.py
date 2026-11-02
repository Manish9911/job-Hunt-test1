import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    with TestClient(app) as test_client:
        yield test_client
