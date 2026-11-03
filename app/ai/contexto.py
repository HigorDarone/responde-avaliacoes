"""Tudo que a IA precisa saber sobre a loja para responder uma avaliação.

As classes aqui são cópias simples (sem SQLAlchemy) das configurações da loja,
para o analisador funcionar igual no robô, na aba "Testar" e nos testes.
"""

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.exemplos import selecionar_exemplos
from app.db.models import ProductInfo, ReplyExample, ShopSettings
from app.shopee.base import ShopeeComment


@dataclass
class RegrasLoja:
    tom: str = "descontraido"
    assinatura: str = ""
    uso_emoji: str = "pouco"
    tamanho_resposta: str = "curta"
    oferta_por_categoria: dict[str, str] = field(default_factory=dict)
    nunca_dizer: list[str] = field(default_factory=list)
    palavras_proibidas: list[str] = field(default_factory=list)
    respostas_prontas_5_estrelas: list[str] = field(default_factory=list)

    @classmethod
    def de_settings(cls, s: ShopSettings) -> "RegrasLoja":
        return cls(
            tom=s.tom,
            assinatura=s.assinatura,
            uso_emoji=s.uso_emoji,
            tamanho_resposta=s.tamanho_resposta,
            oferta_por_categoria=dict(s.oferta_por_categoria or {}),
            nunca_dizer=list(s.nunca_dizer or []),
            palavras_proibidas=list(s.palavras_proibidas or []),
            respostas_prontas_5_estrelas=list(s.respostas_prontas_5_estrelas or []),
        )


@dataclass
class Exemplo:
    categoria: str
    sentimento: str
    texto_avaliacao: str
    resposta_ideal: str


@dataclass
class InfoProduto:
    nome: str
    material: str = ""
    tamanho: str = ""
    normal_impressao_3d: str = ""
    cuidados: str = ""
    observacoes: str = ""
    descricao_anuncio: str = ""


@dataclass
class ContextoLoja:
    shop_id: int
    regras: RegrasLoja = field(default_factory=RegrasLoja)
    exemplos: list[Exemplo] = field(default_factory=list)
    produto: InfoProduto | None = None


def carregar_contexto(
    session: Session, shop_id: int, comentario: ShopeeComment, n_exemplos: int = 4
) -> ContextoLoja:
    settings = session.scalar(select(ShopSettings).where(ShopSettings.shop_id == shop_id))
    regras = RegrasLoja.de_settings(settings) if settings else RegrasLoja()
    limite_por_categoria = settings.max_exemplos_por_categoria if settings else 30

    # Só os N exemplos ativos mais recentes de cada categoria entram na disputa.
    todos = session.scalars(
        select(ReplyExample)
        .where(ReplyExample.shop_id == shop_id, ReplyExample.ativo.is_(True))
        .order_by(ReplyExample.criado_em.desc(), ReplyExample.id.desc())
    )
    por_categoria: dict[str, int] = defaultdict(int)
    candidatos: list[Exemplo] = []
    for e in todos:
        if por_categoria[e.categoria] >= limite_por_categoria:
            continue
        por_categoria[e.categoria] += 1
        candidatos.append(Exemplo(e.categoria, e.sentimento, e.texto_avaliacao, e.resposta_ideal))

    produto = session.scalar(
        select(ProductInfo).where(
            ProductInfo.shop_id == shop_id, ProductInfo.item_id == comentario.item_id
        )
    )
    info = (
        InfoProduto(
            nome=produto.nome,
            material=produto.material,
            tamanho=produto.tamanho,
            normal_impressao_3d=produto.normal_impressao_3d,
            cuidados=produto.cuidados,
            observacoes=produto.observacoes,
            descricao_anuncio=produto.descricao_anuncio or "",
        )
        if produto
        else None
    )

    return ContextoLoja(
        shop_id=shop_id,
        regras=regras,
        exemplos=selecionar_exemplos(comentario.texto, comentario.estrelas, candidatos, n_exemplos),
        produto=info,
    )
