"""Operações de banco usadas em mais de um lugar."""

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.db.models import ReplyExample, Review, Shop, ShopSettings

SEM_TEXTO = "(avaliação sem texto)"


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


def consulta_lojas_operaveis(usar_mock: bool, usar_extensao: bool = False) -> Select:
    """Lojas ativas que o sistema consegue usar.

    Modo extensão: as que têm chave da extensão. API real: só as conectadas (com token).
    Mock: as lojas de exemplo.
    """
    consulta = select(Shop).where(Shop.ativo.is_(True))
    if usar_extensao:
        return consulta.where(Shop.extensao_token.is_not(None))
    consulta = consulta.where(Shop.shopee_shop_id.is_not(None))
    if not usar_mock:
        consulta = consulta.where(Shop.refresh_token.is_not(None))
    return consulta


def buscar_loja_por_shopee_id(session: Session, shopee_shop_id: int) -> Shop | None:
    return session.scalar(select(Shop).where(Shop.shopee_shop_id == shopee_shop_id))


def texto_da_avaliacao(review: Review) -> str:
    return review.texto.strip() or SEM_TEXTO


def exemplo_existente(session: Session, shop_id: int, texto_avaliacao: str, resposta: str) -> ReplyExample | None:
    return session.scalar(
        select(ReplyExample).where(
            ReplyExample.shop_id == shop_id,
            ReplyExample.texto_avaliacao == texto_avaliacao,
            ReplyExample.resposta_ideal == resposta,
        )
    )


def salvar_exemplo_da_review(session: Session, review: Review, resposta: str, origem: str) -> ReplyExample:
    """Transforma uma avaliação + resposta em exemplo para a IA (sem duplicar).

    Se o mesmo par já existir, só garante que está ativo.
    """
    texto = texto_da_avaliacao(review)
    resposta = resposta.strip()
    existente = exemplo_existente(session, review.shop_id, texto, resposta)
    if existente:
        existente.ativo = True
        return existente
    exemplo = ReplyExample(
        shop_id=review.shop_id,
        categoria=review.categoria or "outro",
        sentimento=review.sentimento or "neutro",
        texto_avaliacao=texto,
        resposta_ideal=resposta,
        origem=origem,
        ativo=True,
    )
    session.add(exemplo)
    return exemplo
