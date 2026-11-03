"""Operações de banco usadas em mais de um lugar."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Shop, ShopSettings


def criar_loja(session: Session, nome: str, shopee_shop_id: int | None = None) -> Shop:
    """Cria a loja já com suas configurações padrão (respostas automáticas desligadas)."""
    shop = Shop(nome=nome, shopee_shop_id=shopee_shop_id)
    shop.settings = ShopSettings(auto_publicar=False)
    session.add(shop)
    session.flush()
    return shop


def garantir_lojas_mock(session: Session, client) -> None:
    """No modo mock, cria no banco as lojas de exemplo que ainda não existem."""
    for shopee_id in client.lojas():
        if buscar_loja_por_shopee_id(session, shopee_id) is None:
            criar_loja(session, f"Loja mock {shopee_id}", shopee_id)
    session.commit()


def buscar_loja_por_shopee_id(session: Session, shopee_shop_id: int) -> Shop | None:
    return session.scalar(select(Shop).where(Shop.shopee_shop_id == shopee_shop_id))
