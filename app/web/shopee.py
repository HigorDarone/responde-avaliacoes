"""Tela "Conexão Shopee": autorizar a loja (OAuth) e ver o estado da conexão."""

import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import AuditLog, Shop
from app.db.repo import criar_loja
from app.shopee.base import ShopeeError
from app.shopee.client import ShopeeAuth, ShopeeRealClient, Tokens
from app.web.routes import AUTOR_PAINEL, _contexto, get_config, get_db, loja_atual, templates

router = APIRouter(prefix="/shopee")
COOKIE_STATE = "shopee_oauth_state"


class _TokensDaConexao:
    """Usa os tokens recém-obtidos para uma chamada logo após conectar (já estão no banco)."""

    def __init__(self, tokens: Tokens):
        self.tokens = tokens

    def obter(self, shopee_shop_id: int) -> Tokens:
        return self.tokens

    def salvar(self, shopee_shop_id: int, tokens: Tokens) -> None:
        self.tokens = tokens


def get_auth(settings: Settings = Depends(get_config)) -> ShopeeAuth:
    return ShopeeAuth(settings)


def _pronta_para_conectar(s: Settings) -> str:
    """Motivo para ainda não dar para conectar, ou "" se der."""
    if s.use_mock_shopee:
        return "O sistema está em modo de teste (USE_MOCK_SHOPEE=true no .env)."
    if not s.shopee_partner_id or not s.shopee_partner_key:
        return "Faltam SHOPEE_PARTNER_ID e SHOPEE_PARTNER_KEY no .env."
    return ""


def _pagina(request: Request, db: Session, loja: Shop | None, settings: Settings,
            ok: str = "", erro: str = "", status_code: int = 200, nova_chave: str = ""):
    conectadas = db.scalars(
        select(Shop).where(Shop.refresh_token.is_not(None)).order_by(Shop.id)
    ).all()
    return templates.TemplateResponse(
        request, "shopee.html",
        _contexto(db, loja, "shopee", settings=settings, conectadas=conectadas,
                  bloqueio=_pronta_para_conectar(settings), ok=ok, erro=erro,
                  agora=datetime.now(UTC), nova_chave=nova_chave,
                  url_painel=str(request.base_url).rstrip("/")),
        status_code=status_code,
    )


@router.get("")
def conexao(request: Request, ok: str = "", db: Session = Depends(get_db),
            loja: Shop | None = Depends(loja_atual), settings: Settings = Depends(get_config)):
    return _pagina(request, db, loja, settings, ok=ok)


@router.post("/extensao/chave")
def gerar_chave_extensao(request: Request, db: Session = Depends(get_db),
                         loja: Shop | None = Depends(loja_atual), settings: Settings = Depends(get_config)):
    """Cria (ou troca) a chave que a extensão do Chrome usa. Trocar invalida a anterior."""
    from app.web.extensao import gerar_chave

    if loja is None:
        loja = criar_loja(db, "Minha loja")
    loja.extensao_token = gerar_chave()
    db.add(AuditLog(shop_id=loja.id, acao="chave_extensao_gerada", autor=AUTOR_PAINEL, detalhes={}))
    db.commit()
    return _pagina(request, db, loja, settings, nova_chave=loja.extensao_token)


@router.get("/conectar")
def conectar(request: Request, db: Session = Depends(get_db), loja: Shop | None = Depends(loja_atual),
             settings: Settings = Depends(get_config)):
    if motivo := _pronta_para_conectar(settings):
        return _pagina(request, db, loja, settings, erro=motivo, status_code=400)
    auth = get_auth(settings)
    state = secrets.token_urlsafe(24)  # protege contra outro site forjar o retorno
    resposta = RedirectResponse(auth.url_autorizacao(settings.shopee_redirect_url, state), status_code=302)
    resposta.set_cookie(COOKIE_STATE, state, max_age=900, httponly=True, samesite="lax")
    return resposta


@router.get("/callback")
def callback(
    request: Request,
    code: str = "",
    shop_id: int | None = None,
    state: str = "",
    main_account_id: int | None = None,
    db: Session = Depends(get_db),
    loja: Shop | None = Depends(loja_atual),
    settings: Settings = Depends(get_config),
    auth: ShopeeAuth = Depends(get_auth),
):
    esperado = request.cookies.get(COOKIE_STATE)
    if not esperado or not secrets.compare_digest(esperado, state):
        return _pagina(request, db, loja, settings, status_code=400,
                       erro="O retorno da Shopee não confere com o pedido de conexão. Tente conectar de novo.")
    if main_account_id and not shop_id:
        return _pagina(request, db, loja, settings, status_code=400,
                       erro="Autorize com a conta da loja (não com a conta principal) por enquanto.")
    if not code or not shop_id:
        return _pagina(request, db, loja, settings, status_code=400,
                       erro="A Shopee não devolveu a autorização. Tente conectar de novo.")

    try:
        tokens: Tokens = auth.obter_tokens(code, shop_id)
    except ShopeeError as e:
        return _pagina(request, db, loja, settings, status_code=502, erro=f"A Shopee recusou a conexão: {e}")

    conectada = db.scalar(select(Shop).where(Shop.shopee_shop_id == shop_id))
    nova = conectada is None
    if nova:
        conectada = criar_loja(db, f"Loja {shop_id}", shop_id)
    conectada.ativo = True
    conectada.access_token = tokens.access_token
    conectada.refresh_token = tokens.refresh_token
    conectada.token_expira_em = tokens.expira_em
    db.add(AuditLog(shop_id=conectada.id, acao="shopee_conectada", autor=AUTOR_PAINEL,
                    detalhes={"shopee_shop_id": shop_id, "ambiente": settings.shopee_env}))
    db.commit()

    # Nome da loja: melhor esforço; a conexão já está salva mesmo se falhar.
    try:
        cliente = ShopeeRealClient(settings, _TokensDaConexao(tokens), auth.http)
        conectada.nome = cliente.nome_da_loja(shop_id)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()

    resposta = RedirectResponse("/shopee?ok=1", status_code=303)
    resposta.delete_cookie(COOKIE_STATE)
    return resposta
