"""Importa os anúncios da loja para o cadastro de produtos (aba Produtos).

Regra: só preenche o que está vazio. O que o vendedor escreveu (material, tamanho,
normal da impressão 3D, cuidados, observações) nunca é sobrescrito. O nome e a
descrição do anúncio são sempre atualizados.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ProductInfo, Shop
from app.shopee.base import ShopeeClient, ShopeeItem

MAX_DESCRICAO = 4000


@dataclass
class ResultadoImportacao:
    criados: int = 0
    atualizados: int = 0


def _material(item: ShopeeItem) -> str:
    for nome, valor in item.atributos.items():
        if "material" in nome.lower():
            return valor
    return ""


def _descricao(item: ShopeeItem) -> str:
    partes = [item.descricao[:MAX_DESCRICAO]] if item.descricao else []
    if item.embalagem_cm:
        c, l, a = item.embalagem_cm
        partes.append(f"Embalagem (não é o tamanho da peça): {c} x {l} x {a} cm.")
    if item.peso_kg:
        partes.append(f"Peso com embalagem: {item.peso_kg} kg.")
    return "\n".join(partes)


def importar_produtos(session: Session, loja: Shop, client: ShopeeClient) -> ResultadoImportacao:
    resultado = ResultadoImportacao()
    agora = datetime.now(UTC)
    existentes = {
        p.item_id: p
        for p in session.scalars(select(ProductInfo).where(ProductInfo.shop_id == loja.id))
    }
    for item in client.get_items(loja.shopee_shop_id):
        produto = existentes.get(item.item_id)
        if produto is None:
            produto = ProductInfo(shop_id=loja.id, item_id=item.item_id, nome=item.nome,
                                  material="", tamanho="", normal_impressao_3d="", cuidados="",
                                  observacoes="")
            session.add(produto)
            resultado.criados += 1
        else:
            resultado.atualizados += 1
        produto.nome = item.nome or produto.nome
        if not produto.material:
            produto.material = _material(item)
        produto.descricao_anuncio = _descricao(item)
        produto.importado_em = agora
    session.commit()
    return resultado
