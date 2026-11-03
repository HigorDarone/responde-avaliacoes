"""Tokens da Shopee guardados no banco, na tabela de lojas (um par por loja)."""

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Shop
from app.shopee.client import Tokens


class TokensNoBanco:
    """Cada leitura/gravação abre a própria sessão, para funcionar no robô e no painel."""

    def __init__(self, nova_sessao: Callable[[], Session]):
        self.nova_sessao = nova_sessao

    def obter(self, shopee_shop_id: int) -> Tokens | None:
        with self.nova_sessao() as s:
            loja = s.scalar(select(Shop).where(Shop.shopee_shop_id == shopee_shop_id))
            if loja is None or not loja.refresh_token:
                return None
            return Tokens(loja.access_token or "", loja.refresh_token, loja.token_expira_em)

    def salvar(self, shopee_shop_id: int, tokens: Tokens) -> None:
        with self.nova_sessao() as s:
            loja = s.scalar(select(Shop).where(Shop.shopee_shop_id == shopee_shop_id))
            if loja is None:
                raise ValueError(f"Loja {shopee_shop_id} não cadastrada.")
            loja.access_token = tokens.access_token
            loja.refresh_token = tokens.refresh_token
            loja.token_expira_em = tokens.expira_em
            s.commit()
