"""Painel web: fila de aprovação (aprovar, editar, descartar) e interruptor "Respostas automáticas".

Ainda sem login (etapa 5f): o painel mostra a primeira loja ativa e registra as ações
com o autor "painel".
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from functools import lru_cache

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import get_session
from app.db.models import (
    AuditLog,
    DraftStatus,
    OrigemExemplo,
    PublicadoPor,
    ReplyDraft,
    ReplyExample,
    Review,
    ReviewStatus,
    Shop,
)
from app.config import get_settings
from app.db.repo import consulta_lojas_operaveis, salvar_exemplo_da_review, texto_da_avaliacao
from app.shopee import get_shopee_client
from app.shopee.base import LIMITE_RESPOSTA, ShopeeClient, ShopeeError

AUTOR_PAINEL = "painel"  # vira o nome do usuário quando houver login
BRASILIA = timezone(timedelta(hours=-3))  # sem horário de verão desde 2019

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
router = APIRouter()

CATEGORIAS = {
    "elogio": ("Elogio", "pilula--ok"),
    "defeito": ("Defeito", "pilula--erro"),
    "quebrado_no_envio": ("Quebrou no envio", "pilula--erro"),
    "atraso": ("Atraso", "pilula--aviso"),
    "produto_diferente": ("Produto diferente", "pilula--laranja"),
    "duvida": ("Dúvida", "pilula--azul"),
    "outro": ("Outro", ""),
}
MESES = ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN", "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"]


def _data_curta(d: datetime | None) -> str:
    return f"{d.day:02d} {MESES[d.month - 1]}" if d else ""


def _data_hora(d: datetime | None) -> str:
    """O banco guarda em UTC (o SQLite devolve sem fuso); mostra no horário de Brasília."""
    if not d:
        return ""
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(BRASILIA).strftime("%d/%m/%Y %H:%M")


def _sem_prefixo_revisao(motivo: str | None) -> str:
    motivo = (motivo or "").strip()
    for prefixo in ("Precisa de você:", "Precisa de you:"):
        if motivo.lower().startswith(prefixo.lower()):
            motivo = motivo[len(prefixo):].strip()
            break
    return motivo[:1].upper() + motivo[1:]


def _url_segura(url: str | None) -> str:
    """Só links http(s) da mídia viram link/imagem (evita javascript: etc.)."""
    return url if url and url.lower().startswith(("https://", "http://")) else "#"


templates.env.filters.update(
    data_curta=_data_curta,
    data_hora=_data_hora,
    sem_prefixo_revisao=_sem_prefixo_revisao,
    url_segura=_url_segura,
    texto_avaliacao=texto_da_avaliacao,
)


def get_db() -> Iterator[Session]:
    with get_session() as session:
        yield session


def loja_atual(db: Session = Depends(get_db)) -> Shop | None:
    return db.scalar(consulta_lojas_operaveis(get_settings().use_mock_shopee).order_by(Shop.id))


def _ultima_alteracao_chave(db: Session, loja: Shop) -> AuditLog | None:
    return db.scalar(
        select(AuditLog)
        .where(
            AuditLog.shop_id == loja.id,
            AuditLog.acao.in_(["auto_publicar_ligado", "auto_publicar_desligado"]),
        )
        .order_by(AuditLog.criado_em.desc(), AuditLog.id.desc())
    )


def _contexto(db: Session, loja: Shop | None, pagina: str, **extra) -> dict:
    return {
        "loja": loja,
        "pagina": pagina,
        "categorias": CATEGORIAS,
        "ultima_alteracao": _ultima_alteracao_chave(db, loja) if loja else None,
        **extra,
    }


@router.get("/")
def inicio():
    return RedirectResponse("/fila", status_code=302)


def _pendentes(db: Session, loja: Shop | None) -> list[tuple[Review, ReplyDraft]]:
    if loja is None:
        return []
    return list(
        db.execute(
            select(Review, ReplyDraft)
            .join(ReplyDraft, ReplyDraft.review_id == Review.id)
            .where(
                Review.shop_id == loja.id,
                Review.status == ReviewStatus.NA_FILA,
                ReplyDraft.status == DraftStatus.PENDENTE,
            )
            # Primeiro o que precisa de você, depois o mais grave, depois o mais antigo.
            .order_by(
                Review.precisa_revisao.desc(),
                Review.gravidade.desc(),
                Review.avaliado_em,
                Review.id,
            )
        )
    )


def _exemplos_salvos(db: Session, loja: Shop | None) -> set[tuple[str, str]]:
    """Pares (avaliação, resposta) que já são exemplo, para marcar "Marcada como ótima"."""
    if loja is None:
        return set()
    return set(
        db.execute(
            select(ReplyExample.texto_avaliacao, ReplyExample.resposta_ideal).where(
                ReplyExample.shop_id == loja.id, ReplyExample.ativo.is_(True)
            )
        ).all()
    )


def _contexto_resumo(db: Session, loja: Shop | None) -> dict:
    itens = _pendentes(db, loja)
    return {
        "itens": itens,
        "revisao": sum(1 for r, _ in itens if r.precisa_revisao),
        "exemplos_salvos": _exemplos_salvos(db, loja),
    }


@router.get("/fila")
def fila(request: Request, db: Session = Depends(get_db), loja: Shop | None = Depends(loja_atual)):
    return templates.TemplateResponse(
        request, "fila.html", _contexto(db, loja, "fila", **_contexto_resumo(db, loja))
    )


def _alterar_chave(request: Request, db: Session, loja: Shop | None, ligar: bool):
    if loja is None:
        return RedirectResponse("/fila", status_code=303)
    if loja.settings.auto_publicar != ligar:
        loja.settings.auto_publicar = ligar
        db.add(
            AuditLog(
                shop_id=loja.id,
                acao="auto_publicar_ligado" if ligar else "auto_publicar_desligado",
                autor=AUTOR_PAINEL,
            )
        )
        db.commit()
    return templates.TemplateResponse(
        request, "partials/interruptor.html", _contexto(db, loja, "fila")
    )


@router.post("/loja/auto-publicar/ligar")
def ligar_auto_publicar(request: Request, db: Session = Depends(get_db), loja: Shop | None = Depends(loja_atual)):
    return _alterar_chave(request, db, loja, True)


@router.post("/loja/auto-publicar/desligar")
def desligar_auto_publicar(request: Request, db: Session = Depends(get_db), loja: Shop | None = Depends(loja_atual)):
    return _alterar_chave(request, db, loja, False)


# --- Ações da fila (HTMX: cada uma devolve o card atualizado) ---


@lru_cache
def _shopee_padrao() -> ShopeeClient:
    # Uma instância por processo: o mock guarda as respostas em memória.
    return get_shopee_client()


def get_shopee() -> ShopeeClient:
    return _shopee_padrao()


def _item(db: Session, loja: Shop | None, review_id: int) -> tuple[Review, ReplyDraft]:
    """A avaliação da loja atual e o rascunho mais recente dela."""
    review = db.get(Review, review_id)
    if loja is None or review is None or review.shop_id != loja.id:
        raise HTTPException(status_code=404, detail="Avaliação não encontrada.")
    draft = db.scalar(
        select(ReplyDraft).where(ReplyDraft.review_id == review.id).order_by(ReplyDraft.id.desc())
    )
    if draft is None:
        raise HTTPException(status_code=404, detail="Avaliação sem resposta sugerida.")
    return review, draft


def _card(request: Request, db: Session, loja: Shop, review: Review, draft: ReplyDraft,
          modo: str = "ver", erro: str = "", texto: str | None = None, status_code: int = 200):
    pendente = review.status == ReviewStatus.NA_FILA and draft.status == DraftStatus.PENDENTE
    template = "partials/card_fila.html" if pendente else "partials/card_resolvido.html"
    ctx = _contexto(
        db, loja, "fila", review=review, draft=draft, modo=modo, erro=erro,
        texto=texto if texto is not None else (draft.texto_final or draft.texto_ia),
        atualizar_resumo=True, **_contexto_resumo(db, loja),
    )
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


@router.post("/fila/{review_id}/otima")
def marcar_otima(
    review_id: int,
    request: Request,
    texto: str = Form(""),
    db: Session = Depends(get_db),
    loja: Shop | None = Depends(loja_atual),
):
    """Botão "Marcar como ótima": a resposta vira exemplo para a IA.

    Na fila vale o texto do campo; depois de publicada, o texto publicado.
    """
    review, draft = _item(db, loja, review_id)
    pendente = draft.status == DraftStatus.PENDENTE
    resposta = (texto if pendente else (draft.texto_final or "")).strip() or draft.texto_ia
    if draft.status == DraftStatus.DESCARTADO:
        return _ja_tratada(request, db, loja, review, draft)
    salvar_exemplo_da_review(db, review, resposta, OrigemExemplo.MARCADA_OTIMA)
    db.add(AuditLog(shop_id=loja.id, acao="exemplo_marcado_otimo", autor=AUTOR_PAINEL,
                    detalhes={"review_id": review.id}))
    db.commit()
    return _card(request, db, loja, review, draft, texto=resposta if pendente else None)


def _ja_tratada(request, db, loja, review, draft):
    return _card(request, db, loja, review, draft, status_code=409)


def _erro_no_texto(texto: str) -> str:
    if not texto:
        return "A resposta não pode ficar vazia."
    if len(texto) > LIMITE_RESPOSTA:
        return f"A Shopee aceita no máximo {LIMITE_RESPOSTA} caracteres (agora são {len(texto)})."
    return ""


def _guardar_texto(draft: ReplyDraft, texto: str) -> None:
    draft.texto_final = texto
    draft.editado = texto != draft.texto_ia.strip()


@router.get("/fila/{review_id}/card")
def card(review_id: int, request: Request, db: Session = Depends(get_db), loja: Shop | None = Depends(loja_atual)):
    review, draft = _item(db, loja, review_id)
    return _card(request, db, loja, review, draft)


@router.get("/fila/{review_id}/editar")
def editar(review_id: int, request: Request, db: Session = Depends(get_db), loja: Shop | None = Depends(loja_atual)):
    review, draft = _item(db, loja, review_id)
    return _card(request, db, loja, review, draft, modo="editar")


@router.post("/fila/{review_id}/salvar")
def salvar(
    review_id: int,
    request: Request,
    texto: str = Form(""),
    db: Session = Depends(get_db),
    loja: Shop | None = Depends(loja_atual),
):
    review, draft = _item(db, loja, review_id)
    if draft.status != DraftStatus.PENDENTE:
        return _ja_tratada(request, db, loja, review, draft)
    texto = texto.strip()
    if erro := _erro_no_texto(texto):
        return _card(request, db, loja, review, draft, modo="editar", texto=texto,
                     erro=erro, status_code=422)
    _guardar_texto(draft, texto)
    db.commit()
    return _card(request, db, loja, review, draft)


@router.post("/fila/{review_id}/aprovar")
def aprovar(
    review_id: int,
    request: Request,
    texto: str = Form(""),
    db: Session = Depends(get_db),
    loja: Shop | None = Depends(loja_atual),
    shopee: ShopeeClient = Depends(get_shopee),
):
    review, draft = _item(db, loja, review_id)
    if review.status != ReviewStatus.NA_FILA or draft.status != DraftStatus.PENDENTE:
        return _ja_tratada(request, db, loja, review, draft)
    texto = texto.strip()
    if erro := _erro_no_texto(texto):
        return _card(request, db, loja, review, draft, modo="editar", texto=texto,
                     erro=erro, status_code=422)

    try:
        shopee.reply_comment(loja.shopee_shop_id, review.comment_id, texto)
    except ShopeeError as e:
        db.add(AuditLog(shop_id=loja.id, acao="falha_publicacao", autor=AUTOR_PAINEL,
                        detalhes={"review_id": review.id, "erro": str(e)}))
        db.commit()
        return _card(request, db, loja, review, draft, texto=texto, status_code=502,
                     erro=f"A Shopee não aceitou a resposta: {e}. Nada foi publicado; tente de novo.")

    _guardar_texto(draft, texto)
    draft.status = DraftStatus.PUBLICADO
    draft.publicado_por = PublicadoPor.HUMANO
    draft.publicado_por_usuario = AUTOR_PAINEL
    draft.publicado_em = datetime.now(UTC)
    review.status = ReviewStatus.PUBLICADA
    if draft.editado:
        # Aprendizado: a versão que você corrigiu vira exemplo para as próximas respostas.
        salvar_exemplo_da_review(db, review, texto, OrigemExemplo.EDICAO)
    db.add(AuditLog(shop_id=loja.id, acao="resposta_publicada", autor=AUTOR_PAINEL,
                    detalhes={"review_id": review.id, "comment_id": review.comment_id,
                              "editada": draft.editado}))
    db.commit()
    return _card(request, db, loja, review, draft)


@router.post("/fila/{review_id}/descartar")
def descartar(review_id: int, request: Request, db: Session = Depends(get_db), loja: Shop | None = Depends(loja_atual)):
    review, draft = _item(db, loja, review_id)
    if review.status != ReviewStatus.NA_FILA or draft.status != DraftStatus.PENDENTE:
        return _ja_tratada(request, db, loja, review, draft)
    draft.status = DraftStatus.DESCARTADO
    review.status = ReviewStatus.DESCARTADA
    db.add(AuditLog(shop_id=loja.id, acao="resposta_descartada", autor=AUTOR_PAINEL,
                    detalhes={"review_id": review.id}))
    db.commit()
    return _card(request, db, loja, review, draft)


@router.post("/fila/{review_id}/desfazer")
def desfazer_descarte(review_id: int, request: Request, db: Session = Depends(get_db), loja: Shop | None = Depends(loja_atual)):
    review, draft = _item(db, loja, review_id)
    if review.status != ReviewStatus.DESCARTADA or draft.status != DraftStatus.DESCARTADO:
        return _ja_tratada(request, db, loja, review, draft)
    draft.status = DraftStatus.PENDENTE
    review.status = ReviewStatus.NA_FILA
    db.add(AuditLog(shop_id=loja.id, acao="descarte_desfeito", autor=AUTOR_PAINEL,
                    detalhes={"review_id": review.id}))
    db.commit()
    return _card(request, db, loja, review, draft)
