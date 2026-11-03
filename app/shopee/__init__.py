from app.config import Settings, get_settings
from app.shopee.base import ShopeeClient


def get_shopee_client(settings: Settings | None = None) -> ShopeeClient:
    """Devolve o cliente falso ou o real, conforme USE_MOCK_SHOPEE."""
    settings = settings or get_settings()
    if settings.use_mock_shopee:
        from app.shopee.mock_client import MockShopeeClient

        return MockShopeeClient(settings.mock_reviews_path)
    raise NotImplementedError(
        "O cliente real da Shopee ainda não foi implementado (passo 6). "
        "Use USE_MOCK_SHOPEE=true por enquanto."
    )
