"""Interface comum ao cliente real da Shopee e ao cliente falso (mock)."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


class ShopeeError(Exception):
    """Erro devolvido pela Shopee (ou simulado pelo mock)."""


@dataclass(frozen=True)
class Midia:
    tipo: Literal["foto", "video"]
    url: str


@dataclass
class ShopeeComment:
    """Uma avaliação de comprador, no formato usado internamente pelo sistema."""

    comment_id: int
    item_id: int
    produto_nome: str
    estrelas: int
    texto: str
    comprador: str
    criado_em: datetime
    midias: list[Midia] = field(default_factory=list)
    resposta: str | None = None

    @property
    def tem_midia(self) -> bool:
        return bool(self.midias)


class ShopeeClient(ABC):
    """`shopee_shop_id` é o id da loja na Shopee (não o id interno do banco)."""

    @abstractmethod
    def get_unreplied_comments(self, shopee_shop_id: int) -> list[ShopeeComment]:
        """Avaliações da loja que ainda não têm resposta (Shopee: get_comment)."""

    @abstractmethod
    def reply_comment(self, shopee_shop_id: int, comment_id: int, texto: str) -> None:
        """Publica a resposta de uma avaliação (Shopee: reply_comment)."""
