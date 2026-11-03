from app.config import Settings, get_settings
from app.shopee.base import ShopeeClient


def get_shopee_client(settings: Settings | None = None) -> ShopeeClient:
    """Devolve o cliente falso ou o real, conforme USE_MOCK_SHOPEE."""
    settings = settings or get_settings()
    if settings.use_mock_shopee:
        from app.shopee.mock_client import MockShopeeClient

        return MockShopeeClient(settings.mock_reviews_path)

    from app.db.base import get_session
    from app.shopee.client import ShopeeRealClient
    from app.shopee.tokens import TokensNoBanco

    return ShopeeRealClient(settings, TokensNoBanco(get_session))
