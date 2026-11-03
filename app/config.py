"""Configuração lida do arquivo .env (ou das variáveis de ambiente)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

RAIZ_PROJETO = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=RAIZ_PROJETO / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # IA
    anthropic_api_key: str = ""
    claude_model: str = "claude-haiku-5-5"
    claude_model_dificil: str = ""
    use_batch_api: bool = True
    limite_avaliacoes_mes: int = 500

    # Shopee
    shopee_env: Literal["sandbox", "live"] = "sandbox"
    shopee_regiao: Literal["br", "global"] = "br"  # o Brasil tem página de autorização própria
    shopee_partner_id: str = ""
    shopee_partner_key: str = ""
    shopee_redirect_url: str = "http://localhost:8000/shopee/callback"
    use_mock_shopee: bool = True
    mock_reviews_path: Path = RAIZ_PROJETO / "data" / "mock_reviews.json"

    # Banco
    database_url: str = "sqlite:///./data/app.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
