import os
import tempfile

# Must be set BEFORE the app is imported: each service reads its configuration from the environment.
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/schedule_test.db"
os.environ["EXPIRY_SWEEP_INTERVAL_SECONDS"] = "0"  # no background thread in unit tests

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.models import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
