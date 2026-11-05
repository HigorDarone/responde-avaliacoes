"""API usada pela extensão do Chrome (modo extensão, sem a API oficial da Shopee).

Fluxo de cada passada da extensão:
1. POST /api/extensao/avaliacoes  — manda as avaliações "Para Responder" lidas do Seller Center;
   o painel salva as novas (idempotente) e analisa em segundo plano.
2. GET  /api/extensao/envios      — busca as respostas aprovadas (robô ou pessoa) para publicar.
3. POST /api/extensao/envios/{id} — conta se publicou ou qual foi o erro.

A extensão se identifica pelo cabeçalho X-Chave-Extensao (gerado em Conexão Shopee).
"""

import logging
import secrets
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import get_session
from app.db.models import DraftStatus, ProductInfo, ReplyDraft, Review, ReviewStatus, Shop
from app.envio import comment_id_da_extensao, confirmar_envio, registrar_falha
from app.web.routes import BRASILIA, get_config, get_db

log = logging.getLogger("extensao")
router = APIRouter(prefix="/api/extensao")
MAX_ENVIOS_POR_PASSADA = 5


def gerar_chave() -> str:
    return "ext_" + secrets.token_urlsafe(32)


def loja_da_extensao(x_chave_extensao: str = Header(""), db: Session = Depends(get_db)) -> Shop:
    if not x_chave_extensao:
        raise HTTPException(status_code=401, detail="Falta a chave da extensão.")
    loja = db.scalar(select(Shop).where(Shop.extensao_token == x_chave_extensao, Shop.ativo.is_(True)))
    if loja is None:
        raise HTTPException(status_code=401, detail="Chave da extensão inválida. Gere outra em Conexão Shopee.")
    return loja


# --- Processamento em segundo plano (análise com a IA) ---


def _processar(loja_id: int) -> None:
    from app.ai.analyzer import Analisador
    from app.worker import Robo

    settings = get_config()
    with get_session() as session:
        loja = session.get(Shop, loja_id)
        if loja is None:
            return
        # No modo extensão o robô não busca nem publica pela API: só analisa e decide.
        robo = Robo(None, Analisador(), settings)
        rel = robo.processar_loja(session, loja, buscar=False)
        log.info("Extensão, loja %s: %s", loja_id, rel)


def get_processador():
    """Trocável nos testes."""
    return _processar


# --- Endpoints ---


class Midia(BaseModel):
    tipo: str = Field(pattern="^(foto|video)$")
    url: str = Field(max_length=1000)


class AvaliacaoLida(BaseModel):
    pedido: str = Field(min_length=1, max_length=40)
    produto: str = Field(min_length=1, max_length=300)
    variacao: str = Field(default="", max_length=500)
    estrelas: int = Field(ge=1, le=5)
    texto: str = Field(default="", max_length=3000)
    midias: list[Midia] = Field(default_factory=list, max_length=20)
    comprador: str = Field(default="", max_length=200)
    data: str = Field(default="", max_length=20)  # como aparece na página: "03/11/2026 10:55"

    def avaliado_em(self) -> datetime | None:
        try:
            return datetime.strptime(self.data.strip(), "%d/%m/%Y %H:%M").replace(tzinfo=BRASILIA)
        except ValueError:
            return None


class LoteAvaliacoes(BaseModel):
    avaliacoes: list[AvaliacaoLida] = Field(max_length=200)


@router.get("/status")
def status(loja: Shop = Depends(loja_da_extensao)):
    return {"loja": loja.nome, "auto_publicar": loja.settings.auto_publicar,
            "max_envios_por_passada": MAX_ENVIOS_POR_PASSADA}


@router.post("/avaliacoes")
def receber_avaliacoes(
    lote: LoteAvaliacoes,
    tarefas: BackgroundTasks,
    loja: Shop = Depends(loja_da_extensao),
    db: Session = Depends(get_db),
    processar=Depends(get_processador),
):
    produtos = {
        p.nome.strip().lower(): p.item_id
        for p in db.scalars(select(ProductInfo).where(ProductInfo.shop_id == loja.id))
    }
    ids = {comment_id_da_extensao(a.pedido, a.produto): a for a in lote.avaliacoes}
    existentes = set(db.scalars(
        select(Review.comment_id).where(Review.shop_id == loja.id, Review.comment_id.in_(list(ids)))
    ))
    novas = 0
    for comment_id, a in ids.items():
        if comment_id in existentes:
            continue
        db.add(Review(
            shop_id=loja.id,
            comment_id=comment_id,
            pedido=a.pedido.strip(),
            item_id=produtos.get(a.produto.strip().lower(), 0),
            produto_nome=a.produto.strip(),
            estrelas=a.estrelas,
            texto=a.texto.strip(),
            midias=[m.model_dump() for m in a.midias],
            comprador=a.comprador.strip(),
            avaliado_em=a.avaliado_em(),
            status=ReviewStatus.NOVA,
        ))
        novas += 1
    db.commit()
    # Analisa as novas e também o que ficou pendente (falha de rede antes, lote da Batch API).
    pendentes = db.scalar(select(Review.id).where(
        Review.shop_id == loja.id, Review.status.in_([ReviewStatus.NOVA, ReviewStatus.EM_ANALISE])
    ).limit(1))
    if pendentes:
        tarefas.add_task(processar, loja.id)
    return {"recebidas": len(lote.avaliacoes), "novas": novas}


@router.get("/envios")
def envios(loja: Shop = Depends(loja_da_extensao), db: Session = Depends(get_db)):
    """Respostas aprovadas esperando a extensão publicar (as mais antigas primeiro)."""
    itens = db.execute(
        select(ReplyDraft, Review)
        .join(Review, ReplyDraft.review_id == Review.id)
        .where(ReplyDraft.shop_id == loja.id, ReplyDraft.status == DraftStatus.AGUARDANDO_ENVIO)
        .order_by(ReplyDraft.id)
        .limit(MAX_ENVIOS_POR_PASSADA)
    ).all()
    return {"envios": [
        {"id": d.id, "pedido": r.pedido, "produto": r.produto_nome, "texto": d.texto_final}
        for d, r in itens
    ]}


class ResultadoEnvio(BaseModel):
    ok: bool
    erro: str = Field(default="", max_length=1000)


@router.post("/envios/{draft_id}")
def resultado_envio(draft_id: int, resultado: ResultadoEnvio, loja: Shop = Depends(loja_da_extensao),
                    db: Session = Depends(get_db)):
    draft = db.get(ReplyDraft, draft_id)
    if draft is None or draft.shop_id != loja.id:
        raise HTTPException(status_code=404, detail="Envio não encontrado.")
    if draft.status != DraftStatus.AGUARDANDO_ENVIO:
        return {"status": draft.status}  # repetido: ignora
    review = db.get(Review, draft.review_id)
    if resultado.ok:
        confirmar_envio(db, loja, review, draft)
        status = "publicado"
    else:
        voltou = registrar_falha(db, loja, review, draft, resultado.erro or "erro desconhecido")
        status = "de_volta_na_fila" if voltou else "vai_tentar_de_novo"
    db.commit()
    return {"status": status}
