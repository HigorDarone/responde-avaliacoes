"""Relatório "O que melhorar": junta as avaliações de poucas estrelas e mostra os motivos.

Duas partes:
- contagens (grátis, sem IA): por estrela, por categoria da análise e por produto;
- análise com IA (sob demanda, 1 chamada): agrupa as reclamações em temas concretos
  ("cor diferente da foto", "peça quebrou na entrega"...), com as avaliações de cada tema
  e uma sugestão prática do que mudar. Fica salva no banco com tokens e custo.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.custos import Uso, uso_da_resposta
from app.db.models import RelatorioMelhoria, Review

PERIODOS = {30: "30 dias", 90: "90 dias", 365: "12 meses", 0: "Tudo"}
MAX_AVALIACOES_IA = 300  # as mais recentes; limita o custo de uma chamada
MAX_TEXTO_IA = 500
MAX_TOKENS_RELATORIO = 6000


@dataclass(frozen=True)
class Filtro:
    estrelas: tuple[int, ...] = (1, 2, 3)
    dias: int = 90  # 0 = tudo
    produto: str = ""

    @classmethod
    def da_requisicao(cls, estrelas: list[str] | None, dias: str | None, produto: str | None) -> "Filtro":
        validas = sorted({int(e) for e in (estrelas or []) if str(e).isdigit() and 1 <= int(e) <= 4})
        try:
            d = int(dias) if dias not in (None, "") else 90
        except ValueError:
            d = 90
        return cls(
            estrelas=tuple(validas) or (1, 2, 3),
            dias=d if d in PERIODOS else 90,
            produto=(produto or "").strip()[:300],
        )

    def como_dict(self) -> dict:
        return {"estrelas": list(self.estrelas), "dias": self.dias, "produto": self.produto}


def _quando():
    return func.coalesce(Review.avaliado_em, Review.criado_em)


def avaliacoes(session: Session, shop_id: int, filtro: Filtro, agora: datetime) -> list[Review]:
    consulta = select(Review).where(Review.shop_id == shop_id, Review.estrelas.in_(filtro.estrelas))
    if filtro.dias:
        consulta = consulta.where(_quando() >= agora - timedelta(days=filtro.dias))
    if filtro.produto:
        consulta = consulta.where(Review.produto_nome == filtro.produto)
    return list(session.scalars(consulta.order_by(_quando().desc(), Review.id.desc())))


def produtos_com_reclamacao(session: Session, shop_id: int) -> list[str]:
    """Produtos com alguma avaliação de 1 a 4 estrelas (para o filtro)."""
    return list(session.scalars(
        select(Review.produto_nome)
        .where(Review.shop_id == shop_id, Review.estrelas <= 4, Review.produto_nome != "")
        .group_by(Review.produto_nome)
        .order_by(func.count(Review.id).desc(), Review.produto_nome)
    ))


@dataclass
class Resumo:
    total: int = 0
    com_texto: int = 0
    por_estrela: dict[int, int] = field(default_factory=dict)
    por_categoria: list[tuple[str, int]] = field(default_factory=list)
    por_produto: list[tuple[str, int, list[tuple[str, int]]]] = field(default_factory=list)

    @property
    def maior_categoria(self) -> int:
        return max((n for _, n in self.por_categoria), default=0)

    @property
    def maior_produto(self) -> int:
        return max((n for _, n, _ in self.por_produto), default=0)


def resumir(reviews: list[Review], max_produtos: int = 8) -> Resumo:
    cat = Counter(r.categoria or "sem_analise" for r in reviews)
    por_produto: dict[str, Counter] = {}
    for r in reviews:
        por_produto.setdefault(r.produto_nome or "Sem nome", Counter())[r.categoria or "sem_analise"] += 1
    produtos = sorted(por_produto.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))[:max_produtos]
    estrelas = Counter(r.estrelas for r in reviews)
    return Resumo(
        total=len(reviews),
        com_texto=sum(1 for r in reviews if (r.texto or "").strip()),
        por_estrela={e: estrelas.get(e, 0) for e in (1, 2, 3, 4)},
        por_categoria=cat.most_common(),
        por_produto=[(nome, sum(c.values()), c.most_common()) for nome, c in produtos],
    )


# --- Análise com IA ---

SCHEMA_RELATORIO = {
    "type": "object",
    "properties": {
        "resumo": {"type": "string"},
        "temas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "titulo": {"type": "string"},
                    "descricao": {"type": "string"},
                    "sugestao": {"type": "string"},
                    "prioridade": {"type": "string", "enum": ["alta", "media", "baixa"]},
                    "avaliacoes": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["titulo", "descricao", "sugestao", "prioridade", "avaliacoes"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["resumo", "temas"],
    "additionalProperties": False,
}

SISTEMA_RELATORIO = """\
Você é consultor de qualidade de uma loja da Shopee que vende peças feitas em impressão 3D.
Recebe avaliações de poucas estrelas e escreve, em português do Brasil, um relatório do que a loja
pode melhorar.

Como agrupar:
- Junte as avaliações em TEMAS concretos e específicos, do jeito que o cliente falaria
  (ex.: "Cor diferente da foto do anúncio", "Peça chegou quebrada", "Tamanho menor que o esperado",
  "Demora na entrega"). Evite temas vagos como "Qualidade" ou "Problemas diversos".
- Cada avaliação entra em no máximo um tema (o principal). Avaliação sem texto e sem motivo claro
  fica de fora. Elogios não viram tema.
- Liste em "avaliacoes" os números [N] das avaliações de cada tema, exatamente como recebidos.
- Ordene os temas do que mais aparece para o que menos aparece; até 8 temas.

Em cada tema:
- "descricao": 1 a 2 frases com o que os clientes relatam, sem inventar nada além do texto.
- "sugestao": ação prática e específica que a loja pode tomar (anúncio, fotos, embalagem,
  calibração da impressora, conferência antes do envio, descrição de medidas...).
- "prioridade": alta se aparece muito ou é grave (quebrado, faltando peça); baixa se é raro e leve.

Conhecimento de impressão 3D: linhas de camada leves são normais do processo (sugira deixar isso
claro no anúncio); peças quebradas, deformadas ou faltando partes são defeito.

"resumo": 2 a 3 frases com o panorama e o tema que mais vale atacar primeiro.
Não cite nomes de clientes."""


class TemaIA(BaseModel):
    model_config = ConfigDict(extra="forbid")

    titulo: str = Field(min_length=1)
    descricao: str
    sugestao: str
    prioridade: Literal["alta", "media", "baixa"]
    avaliacoes: list[int]


class RelatorioIA(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resumo: str
    temas: list[TemaIA]


class RelatorioError(Exception):
    pass


def _linha(r: Review) -> str:
    texto = " ".join((r.texto or "").split())[:MAX_TEXTO_IA] or "(sem texto)"
    partes = [f"[{r.id}] {r.estrelas} estrela(s)", f"Produto: {r.produto_nome or '?'}"]
    if r.categoria:
        partes.append(f"Categoria: {r.categoria}")
    partes.append(f"Texto: {texto}")
    if r.motivo:
        partes.append(f"Análise anterior: {' '.join(r.motivo.split())[:200]}")
    if r.midias:
        partes.append(f"Mídias: {len(r.midias)}")
    return " | ".join(partes)


def para_ia(reviews: list[Review]) -> list[Review]:
    """Só as que dizem algo (texto ou motivo da análise), as mais recentes primeiro."""
    uteis = [r for r in reviews if (r.texto or "").strip() or (r.motivo or "").strip()]
    return uteis[:MAX_AVALIACOES_IA]


def montar_requisicao(reviews: list[Review], modelo: str) -> dict:
    corpo = "\n".join(_linha(r) for r in reviews)
    return {
        "model": modelo,
        "max_tokens": MAX_TOKENS_RELATORIO,
        "system": [{"type": "text", "text": SISTEMA_RELATORIO, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": f"Avaliações ({len(reviews)}):\n{corpo}"}],
        "output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA_RELATORIO}},
    }


def gerar_com_ia(client, modelo: str, reviews: list[Review]) -> tuple[RelatorioIA, Uso]:
    enviadas = para_ia(reviews)
    if not enviadas:
        raise RelatorioError("Nenhuma avaliação com texto nesse filtro: não há o que a IA analisar.")
    params = montar_requisicao(enviadas, modelo)
    resposta = client.messages.create(**params)
    if resposta.stop_reason == "refusal":
        raise RelatorioError("O Claude recusou gerar o relatório.")
    if resposta.stop_reason == "max_tokens":
        raise RelatorioError("O relatório ficou grande demais. Filtre por produto ou período menor.")
    texto = next((b.text for b in resposta.content if b.type == "text"), None)
    try:
        rel = RelatorioIA.model_validate(json.loads(texto or ""))
    except (json.JSONDecodeError, ValidationError) as e:
        raise RelatorioError(f"Resposta inválida do Claude: {e}") from e

    # Só ids que foram enviados, sem repetir entre temas; tema vazio sai.
    validos, usados = {r.id for r in enviadas}, set()
    temas = []
    for t in rel.temas:
        ids = [i for i in dict.fromkeys(t.avaliacoes) if i in validos and i not in usados]
        usados.update(ids)
        if ids:
            temas.append(t.model_copy(update={"avaliacoes": ids}))
    peso = {"alta": 0, "media": 1, "baixa": 2}
    temas.sort(key=lambda t: (-len(t.avaliacoes), peso[t.prioridade]))
    return rel.model_copy(update={"temas": temas}), uso_da_resposta(resposta.usage, modelo)


def salvar(session: Session, shop_id: int, filtro: Filtro, rel: RelatorioIA, uso: Uso, total: int) -> RelatorioMelhoria:
    salvo = RelatorioMelhoria(
        shop_id=shop_id,
        filtros=filtro.como_dict(),
        resultado=rel.model_dump(),
        total_avaliacoes=total,
        modelo_usado=uso.modelo,
        tokens_entrada=uso.tokens_entrada_total,
        tokens_saida=uso.tokens_saida,
        custo_estimado_usd=uso.custo_usd,
    )
    session.add(salvo)
    session.commit()
    return salvo


def ultimo(session: Session, shop_id: int, filtro: Filtro) -> RelatorioMelhoria | None:
    """O relatório mais recente gerado com estes mesmos filtros."""
    for r in session.scalars(
        select(RelatorioMelhoria)
        .where(RelatorioMelhoria.shop_id == shop_id)
        .order_by(RelatorioMelhoria.criado_em.desc(), RelatorioMelhoria.id.desc())
        .limit(50)
    ):
        if r.filtros == filtro.como_dict():
            return r
    return None
