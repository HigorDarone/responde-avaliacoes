"""Cliente falso da Shopee, usado até a API ser liberada.

Lê as avaliações de `data/mock_reviews.json` e guarda as respostas só em memória
(o arquivo nunca é alterado). `respostas_enviadas` permite aos testes verificar
quantas vezes `reply_comment` foi chamado.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.shopee.base import Midia, ShopeeClient, ShopeeComment, ShopeeError


@dataclass(frozen=True)
class RespostaEnviada:
    shopee_shop_id: int
    comment_id: int
    texto: str


class MockShopeeClient(ShopeeClient):
    def __init__(self, caminho: str | Path):
        dados = json.loads(Path(caminho).read_text(encoding="utf-8"))
        self._comentarios: dict[int, list[ShopeeComment]] = {}
        for loja in dados["lojas"]:
            self._comentarios[loja["shop_id"]] = [_ler_comentario(c) for c in loja["avaliacoes"]]
        self.respostas_enviadas: list[RespostaEnviada] = []

    def lojas(self) -> list[int]:
        return list(self._comentarios)

    def get_unreplied_comments(self, shopee_shop_id: int) -> list[ShopeeComment]:
        return [c for c in self._comentarios.get(shopee_shop_id, []) if c.resposta is None]

    def reply_comment(self, shopee_shop_id: int, comment_id: int, texto: str) -> None:
        if not texto.strip():
            raise ShopeeError("A resposta não pode ser vazia.")
        comentario = next(
            (c for c in self._comentarios.get(shopee_shop_id, []) if c.comment_id == comment_id),
            None,
        )
        if comentario is None:
            raise ShopeeError(f"Avaliação {comment_id} não encontrada na loja {shopee_shop_id}.")
        if comentario.resposta is not None:
            raise ShopeeError(f"Avaliação {comment_id} já foi respondida.")
        comentario.resposta = texto
        self.respostas_enviadas.append(RespostaEnviada(shopee_shop_id, comment_id, texto))


def _ler_comentario(c: dict) -> ShopeeComment:
    return ShopeeComment(
        comment_id=c["comment_id"],
        item_id=c["item_id"],
        produto_nome=c["produto_nome"],
        estrelas=c["estrelas"],
        texto=c.get("texto", ""),
        comprador=c.get("comprador", ""),
        criado_em=datetime.fromisoformat(c["criado_em"]),
        midias=[Midia(tipo=m["tipo"], url=m["url"]) for m in c.get("midias", [])],
        resposta=c.get("resposta"),
    )
