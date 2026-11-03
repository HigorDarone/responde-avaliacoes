import pytest
from sqlalchemy.orm import Session

from app.config import RAIZ_PROJETO
from app.db.base import make_engine, init_db
from app.shopee.mock_client import MockShopeeClient

MOCK_REVIEWS = RAIZ_PROJETO / "data" / "mock_reviews.json"


@pytest.fixture
def session():
    engine = make_engine("sqlite://")  # banco em memória, novo a cada teste
    init_db(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def mock_client():
    return MockShopeeClient(MOCK_REVIEWS)
