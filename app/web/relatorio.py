"""Tela "Relatório": o que melhorar, a partir das avaliações de poucas estrelas."""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app import relatorio as rel
from app.config import Settings
from app.db.models import AuditLog, Shop
from app.web.routes import AUTOR_PAINEL, _contexto, get_config, get_db, loja_atual, templates

log = logging.getLogger("relatorio")
router = APIRouter(prefix="/relatorio")

PRIORIDADES = {"alta": ("Prioridade alta", "pilula--erro"), "media": ("Prioridade média", "pilula--aviso"),
               "baixa": ("Prioridade baixa", "")}


def get_claude(settings: Settings = Depends(get_config)):
    """Cliente do Claude (None sem chave no .env); trocável nos testes."""
    if not settings.anthropic_api_key:
        return None
    import anthropic

    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def get_relogio():
    return lambda: datetime.now(UTC)


def _filtro(estrelas: list[str] | None, dias: str | None, produto: str | None) -> rel.Filtro:
    return rel.Filtro.da_requisicao(estrelas, dias, produto)


def _dados(db: Session, loja: Shop, filtro: rel.Filtro, agora: datetime) -> dict:
    reviews = rel.avaliacoes(db, loja.id, filtro, agora)
    return {
        "filtro": filtro,
        "reviews": reviews,
        "por_id": {r.id: r for r in reviews},
        "resumo": rel.resumir(reviews),
        "produtos": rel.produtos_com_reclamacao(db, loja.id),
        "periodos": rel.PERIODOS,
        "prioridades": PRIORIDADES,
        "para_ia": len(rel.para_ia(reviews)),
    }


@router.get("")
def pagina(
    request: Request,
    estrelas: list[str] | None = Query(None),
    dias: str | None = None,
    produto: str | None = None,
    db: Session = Depends(get_db),
    loja: Shop | None = Depends(loja_atual),
    relogio=Depends(get_relogio),
):
    extra = {}
    if loja:
        filtro = _filtro(estrelas, dias, produto)
        extra = _dados(db, loja, filtro, relogio())
        extra["salvo"] = rel.ultimo(db, loja.id, filtro)
    return templates.TemplateResponse(request, "relatorio.html", _contexto(db, loja, "relatorio", **extra))


@router.post("/gerar")
def gerar(
    request: Request,
    estrelas: list[str] | None = Query(None),
    dias: str | None = None,
    produto: str | None = None,
    db: Session = Depends(get_db),
    loja: Shop | None = Depends(loja_atual),
    settings: Settings = Depends(get_config),
    claude=Depends(get_claude),
    relogio=Depends(get_relogio),
):
    if loja is None:
        return templates.TemplateResponse(request, "partials/relatorio_ia.html",
                                          {"erro": "Nenhuma loja.", "salvo": None}, status_code=409)
    filtro = _filtro(estrelas, dias, produto)
    dados = _dados(db, loja, filtro, relogio())
    erro, salvo = "", None
    if claude is None:
        erro = "Falta a ANTHROPIC_API_KEY no .env."
    else:
        try:
            analise, uso = rel.gerar_com_ia(claude, settings.claude_model, dados["reviews"])
            salvo = rel.salvar(db, loja.id, filtro, analise, uso, dados["para_ia"])
            db.add(AuditLog(shop_id=loja.id, acao="relatorio_gerado", autor=AUTOR_PAINEL,
                            detalhes={"relatorio_id": salvo.id, "filtros": filtro.como_dict(),
                                      "custo_usd": round(uso.custo_usd, 6)}))
            db.commit()
        except rel.RelatorioError as e:
            erro = str(e)
        except Exception as e:  # rede, chave inválida, limite da API...
            log.exception("Falha ao gerar relatório")
            erro = f"Não consegui falar com o Claude: {e.__class__.__name__}. Tente de novo em instantes."
    return templates.TemplateResponse(
        request, "partials/relatorio_ia.html",
        {**dados, "salvo": salvo, "erro": erro, "categorias": _contexto(db, loja, "relatorio")["categorias"]},
        status_code=200 if not erro else 502,
    )
