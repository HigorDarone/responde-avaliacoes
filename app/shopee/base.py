"""Interface comum ao cliente real da Shopee e ao cliente falso (mock)."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

# reply_comment aceita de 1 a 500 caracteres (erro product.comment_length_invalid).
LIMITE_RESPOSTA = 500


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


@dataclass
class ShopeeItem:
    """Um anúncio da loja (Shopee: get_item_base_info)."""

    item_id: int
    nome: str
    descricao: str = ""
    atributos: dict[str, str] = field(default_factory=dict)
    peso_kg: str = ""
    embalagem_cm: tuple[int, int, int] | None = None  # comprimento, largura, altura da EMBALAGEM


class ShopeeClient(ABC):
    """`shopee_shop_id` é o id da loja na Shopee (não o id interno do banco)."""

    @abstractmethod
    def get_unreplied_comments(self, shopee_shop_id: int) -> list[ShopeeComment]:
        """Avaliações da loja que ainda não têm resposta (Shopee: get_comment)."""

    @abstractmethod
    def reply_comment(self, shopee_shop_id: int, comment_id: int, texto: str) -> None:
        """Publica a resposta de uma avaliação (Shopee: reply_comment)."""

    @abstractmethod
    def get_items(self, shopee_shop_id: int) -> list[ShopeeItem]:
        """Anúncios ativos da loja (Shopee: get_item_list + get_item_base_info)."""


def validar_resposta(texto: str) -> None:
    if not texto.strip():
        raise ShopeeError("A resposta não pode ser vazia.")
    if len(texto) > LIMITE_RESPOSTA:
        raise ShopeeError(f"A Shopee aceita no máximo {LIMITE_RESPOSTA} caracteres na resposta.")
