"""Modelos do banco. Tudo que pertence a uma loja leva `shop_id` (multi-loja)."""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def agora() -> datetime:
    return datetime.now(UTC)


# --- Valores permitidos (guardados como texto, compatível com SQLite e Postgres) ---


class Sentimento(StrEnum):
    POSITIVO = "positivo"
    NEUTRO = "neutro"
    NEGATIVO = "negativo"


class Categoria(StrEnum):
    ELOGIO = "elogio"
    DEFEITO = "defeito"
    QUEBRADO_NO_ENVIO = "quebrado_no_envio"
    ATRASO = "atraso"
    PRODUTO_DIFERENTE = "produto_diferente"
    DUVIDA = "duvida"
    OUTRO = "outro"


class Tom(StrEnum):
    DESCONTRAIDO = "descontraido"
    NEUTRO = "neutro"
    FORMAL = "formal"


class UsoEmoji(StrEnum):
    NUNCA = "nunca"
    POUCO = "pouco"
    A_VONTADE = "a_vontade"


class TamanhoResposta(StrEnum):
    CURTA = "curta"
    MEDIA = "media"


class ReviewStatus(StrEnum):
    NOVA = "nova"  # buscada, ainda não analisada
    EM_ANALISE = "em_analise"  # enviada à Batch API, aguardando resultado
    NA_FILA = "na_fila"  # rascunho aguardando aprovação humana
    PUBLICADA = "publicada"
    DESCARTADA = "descartada"
    ERRO = "erro"


class DraftStatus(StrEnum):
    PENDENTE = "pendente"
    PUBLICADO = "publicado"
    DESCARTADO = "descartado"


class PublicadoPor(StrEnum):
    ROBO = "robo"
    HUMANO = "humano"


class OrigemExemplo(StrEnum):
    MANUAL = "manual"
    EDICAO = "edicao"
    MARCADA_OTIMA = "marcada_otima"


# --- Tabelas ---


class Shop(Base):
    __tablename__ = "shops"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(200))
    shopee_shop_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)

    # Tokens OAuth da Shopee (preenchidos quando o cliente real entrar).
    access_token: Mapped[str | None] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text)
    token_expira_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)

    settings: Mapped["ShopSettings"] = relationship(
        back_populates="shop", uselist=False, cascade="all, delete-orphan"
    )


class ShopSettings(Base):
    __tablename__ = "shop_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), unique=True)

    # Chave "Respostas automáticas": SEMPRE desligada em loja nova.
    auto_publicar: Mapped[bool] = mapped_column(Boolean, default=False)

    tom: Mapped[str] = mapped_column(String(20), default=Tom.DESCONTRAIDO)
    assinatura: Mapped[str] = mapped_column(String(200), default="")
    uso_emoji: Mapped[str] = mapped_column(String(20), default=UsoEmoji.POUCO)
    tamanho_resposta: Mapped[str] = mapped_column(String(20), default=TamanhoResposta.CURTA)

    # {"defeito": "texto livre do que oferecer", "atraso": "...", ...}
    oferta_por_categoria: Mapped[dict] = mapped_column(JSON, default=dict)
    nunca_dizer: Mapped[list] = mapped_column(JSON, default=list)
    palavras_proibidas: Mapped[list] = mapped_column(JSON, default=list)

    # Respostas prontas para 5 estrelas sem texto e sem mídia (sorteadas, sem IA).
    respostas_prontas_5_estrelas: Mapped[list] = mapped_column(JSON, default=list)

    # None = usa LIMITE_AVALIACOES_MES do .env.
    limite_avaliacoes_mes: Mapped[int | None] = mapped_column(Integer)
    max_exemplos_por_categoria: Mapped[int] = mapped_column(Integer, default=30)

    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=agora, onupdate=agora
    )

    shop: Mapped[Shop] = relationship(back_populates="settings")


class Review(Base):
    __tablename__ = "reviews"
    # Idempotência: o mesmo comentário nunca é processado duas vezes na mesma loja.
    __table_args__ = (UniqueConstraint("shop_id", "comment_id", name="uq_review_shop_comment"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), index=True)
    comment_id: Mapped[int] = mapped_column(BigInteger)
    item_id: Mapped[int] = mapped_column(BigInteger, index=True)
    produto_nome: Mapped[str] = mapped_column(String(300), default="")
    estrelas: Mapped[int] = mapped_column(Integer)
    texto: Mapped[str] = mapped_column(Text, default="")
    # [{"tipo": "foto" | "video", "url": "..."}]
    midias: Mapped[list] = mapped_column(JSON, default=list)
    comprador: Mapped[str] = mapped_column(String(200), default="")
    avaliado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(String(20), default=ReviewStatus.NOVA, index=True)
    lote_id: Mapped[str | None] = mapped_column(String(100), index=True)  # Batch API

    # Resultado da análise da IA
    sentimento: Mapped[str | None] = mapped_column(String(20))
    categoria: Mapped[str | None] = mapped_column(String(30), index=True)
    gravidade: Mapped[int | None] = mapped_column(Integer)
    precisa_revisao: Mapped[bool] = mapped_column(Boolean, default=False)
    motivo: Mapped[str | None] = mapped_column(Text)

    # Custos da IA
    modelo_usado: Mapped[str | None] = mapped_column(String(100))
    tokens_entrada: Mapped[int] = mapped_column(Integer, default=0)
    tokens_saida: Mapped[int] = mapped_column(Integer, default=0)
    custo_estimado_usd: Mapped[float] = mapped_column(Float, default=0.0)

    processado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)

    drafts: Mapped[list["ReplyDraft"]] = relationship(back_populates="review")


class ReplyDraft(Base):
    __tablename__ = "reply_drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), index=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("reviews.id"), index=True)

    texto_ia: Mapped[str] = mapped_column(Text)
    texto_final: Mapped[str | None] = mapped_column(Text)
    editado: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default=DraftStatus.PENDENTE, index=True)

    publicado_por: Mapped[str | None] = mapped_column(String(10))  # robo | humano
    publicado_por_usuario: Mapped[str | None] = mapped_column(String(200))
    publicado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)

    review: Mapped[Review] = relationship(back_populates="drafts")


class ReplyExample(Base):
    __tablename__ = "reply_examples"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), index=True)
    categoria: Mapped[str] = mapped_column(String(30), index=True)
    sentimento: Mapped[str] = mapped_column(String(20))
    texto_avaliacao: Mapped[str] = mapped_column(Text)
    resposta_ideal: Mapped[str] = mapped_column(Text)
    origem: Mapped[str] = mapped_column(String(20), default=OrigemExemplo.MANUAL)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)


class ProductInfo(Base):
    __tablename__ = "product_info"
    __table_args__ = (UniqueConstraint("shop_id", "item_id", name="uq_product_shop_item"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), index=True)
    item_id: Mapped[int] = mapped_column(BigInteger)
    nome: Mapped[str] = mapped_column(String(300))
    material: Mapped[str] = mapped_column(String(200), default="")
    tamanho: Mapped[str] = mapped_column(String(200), default="")
    normal_impressao_3d: Mapped[str] = mapped_column(Text, default="")
    cuidados: Mapped[str] = mapped_column(Text, default="")
    observacoes: Mapped[str] = mapped_column(Text, default="")
    # Vindo do anúncio na Shopee ("Importar da Shopee"). Nunca sobrescreve os campos acima.
    descricao_anuncio: Mapped[str | None] = mapped_column(Text)
    importado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=agora, onupdate=agora
    )


class AuditLog(Base):
    """Histórico de ações: quem ligou/desligou a chave, quem publicou o quê."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), index=True)
    acao: Mapped[str] = mapped_column(String(50))  # ex.: auto_publicar_ligado
    autor: Mapped[str] = mapped_column(String(200))  # "robo" ou nome do usuário
    detalhes: Mapped[dict] = mapped_column(JSON, default=dict)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)
