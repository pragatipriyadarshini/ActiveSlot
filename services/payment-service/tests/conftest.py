import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/payment_test.db"
os.environ["IDEMPOTENCY_WAIT_SECONDS"] = "5"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.models import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
