"""Painel web: fila de aprovação e interruptor "Respostas automáticas".

Ainda sem login (etapa 5f): o painel mostra a primeira loja ativa e registra as ações
com o autor "painel".
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import get_session
from app.db.models import AuditLog, DraftStatus, ReplyDraft, Review, ReviewStatus, Shop

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
)


def get_db() -> Iterator[Session]:
    with get_session() as session:
        yield session


def loja_atual(db: Session = Depends(get_db)) -> Shop | None:
    return db.scalar(select(Shop).where(Shop.ativo.is_(True)).order_by(Shop.id))


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


@router.get("/fila")
def fila(request: Request, db: Session = Depends(get_db), loja: Shop | None = Depends(loja_atual)):
    itens: list[tuple[Review, ReplyDraft]] = []
    if loja:
        itens = list(
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
    revisao = sum(1 for r, _ in itens if r.precisa_revisao)
    return templates.TemplateResponse(
        request, "fila.html", _contexto(db, loja, "fila", itens=itens, revisao=revisao)
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
