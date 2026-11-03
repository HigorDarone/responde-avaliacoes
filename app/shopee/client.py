"""Cliente real da Shopee Open Platform v2.

Conferido na documentação oficial (open.shopee.com, nov/2026):
- Assinatura: HMAC-SHA256 (hex minúsculo) com a partner_key sobre
  partner_id + path + timestamp (+ access_token + shop_id nas APIs de loja).
- Autorização (OAuth): página /auth com partner_id, auth_type=seller, redirect_uri,
  response_type=code e state; a Shopee devolve ?code=...&shop_id=... (code vale 10 min, uso único).
- Tokens: /api/v2/auth/token/get e /api/v2/auth/access_token/get. access_token vale 4 h,
  refresh_token 30 dias (cada renovação devolve um refresh_token novo).
- Avaliações: v2.product.get_comment (page_size até 100, cursor; traz media com fotos/vídeos)
  e v2.product.reply_comment (1 a 500 caracteres; não dá para responder de novo).
- Produtos: v2.product.get_item_list (até 100 por página) + get_item_base_info (até 50 ids).
"""

import hashlib
import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlencode

import httpx

from app.config import Settings
from app.shopee.base import (
    Midia,
    ShopeeClient,
    ShopeeComment,
    ShopeeError,
    ShopeeItem,
    validar_resposta,
)

HOST_API = {
    "live": "https://partner.shopeemobile.com",
    "sandbox": "https://openplatform.sandbox.test-stable.shopee.sg",
}
URL_AUTORIZACAO = {
    ("live", "br"): "https://open.shopee.com.br/auth",
    ("live", "global"): "https://open.shopee.com/auth",
    ("sandbox", "br"): "https://open.sandbox.test-stable.shopee.com.br/auth",
    ("sandbox", "global"): "https://open.sandbox.test-stable.shopee.com/auth",
}
RENOVAR_ANTES = timedelta(minutes=10)
ERROS_DE_TOKEN = {"error_auth", "invalid_acceess_token", "invalid_access_token", "error_token"}


def assinar(partner_key: str, partner_id: int, path: str, timestamp: int,
            access_token: str = "", shop_id: int | str = "") -> str:
    base = f"{partner_id}{path}{timestamp}{access_token}{shop_id}"
    return hmac.new(partner_key.encode(), base.encode(), hashlib.sha256).hexdigest()


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expira_em: datetime


class TokenStore(Protocol):
    """Onde os tokens de cada loja ficam guardados (no app: tabela shops)."""

    def obter(self, shopee_shop_id: int) -> Tokens | None: ...
    def salvar(self, shopee_shop_id: int, tokens: Tokens) -> None: ...


class _Base:
    def __init__(self, settings: Settings, http: httpx.Client | None = None,
                 relogio: Callable[[], float] = time.time):
        if not settings.shopee_partner_id or not settings.shopee_partner_key:
            raise ShopeeError("Faltam SHOPEE_PARTNER_ID e SHOPEE_PARTNER_KEY no .env.")
        self.settings = settings
        self.partner_id = int(settings.shopee_partner_id)
        self.partner_key = settings.shopee_partner_key
        self.host = HOST_API[settings.shopee_env]
        self.http = http or httpx.Client(timeout=20)
        self.relogio = relogio

    def _comuns(self, path: str, access_token: str = "", shop_id: int | str = "") -> dict:
        ts = int(self.relogio())
        params = {
            "partner_id": self.partner_id,
            "timestamp": ts,
            "sign": assinar(self.partner_key, self.partner_id, path, ts, access_token, shop_id),
        }
        if access_token:
            params |= {"access_token": access_token, "shop_id": shop_id}
        return params

    def _ler(self, resposta: httpx.Response) -> dict:
        try:
            dados = resposta.json()
        except ValueError as e:
            raise ShopeeError(f"Resposta inválida da Shopee (HTTP {resposta.status_code}).") from e
        if dados.get("error"):
            erro = ShopeeError(f"{dados['error']}: {dados.get('message', '')}".strip(": "))
            erro.codigo = dados["error"]
            raise erro
        if resposta.status_code >= 400:
            raise ShopeeError(f"HTTP {resposta.status_code} da Shopee.")
        return dados


class ShopeeAuth(_Base):
    """Autorização da loja (OAuth) e troca/renovação de tokens."""

    def url_autorizacao(self, redirect_uri: str, state: str) -> str:
        url = URL_AUTORIZACAO[(self.settings.shopee_env, self.settings.shopee_regiao)]
        return url + "?" + urlencode({
            "partner_id": self.partner_id,
            "auth_type": "seller",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
        })

    def _tokens(self, path: str, corpo: dict) -> Tokens:
        r = self.http.post(self.host + path, params=self._comuns(path),
                           json={"partner_id": self.partner_id, **corpo})
        dados = self._ler(r)
        return Tokens(
            access_token=dados["access_token"],
            refresh_token=dados["refresh_token"],
            expira_em=datetime.fromtimestamp(self.relogio(), UTC)
            + timedelta(seconds=int(dados.get("expire_in", 4 * 3600))),
        )

    def obter_tokens(self, code: str, shopee_shop_id: int) -> Tokens:
        return self._tokens("/api/v2/auth/token/get", {"code": code, "shop_id": shopee_shop_id})

    def renovar(self, refresh_token: str, shopee_shop_id: int) -> Tokens:
        return self._tokens("/api/v2/auth/access_token/get",
                            {"refresh_token": refresh_token, "shop_id": shopee_shop_id})


class ShopeeRealClient(_Base, ShopeeClient):
    def __init__(self, settings: Settings, tokens: TokenStore, http: httpx.Client | None = None,
                 relogio: Callable[[], float] = time.time):
        super().__init__(settings, http, relogio)
        self.tokens = tokens
        self.auth = ShopeeAuth(settings, self.http, relogio)

    # --- Tokens ---

    def _access_token(self, shop_id: int, forcar_renovacao: bool = False) -> str:
        atuais = self.tokens.obter(shop_id)
        if atuais is None:
            raise ShopeeError(f"A loja {shop_id} não está conectada. Use \"Conectar Shopee\".")
        agora = datetime.fromtimestamp(self.relogio(), UTC)
        expira = atuais.expira_em if atuais.expira_em.tzinfo else atuais.expira_em.replace(tzinfo=UTC)
        if forcar_renovacao or expira - agora < RENOVAR_ANTES:
            atuais = self.auth.renovar(atuais.refresh_token, shop_id)
            self.tokens.salvar(shop_id, atuais)
        return atuais.access_token

    def _chamar(self, metodo: str, path: str, shop_id: int, params: dict | None = None,
                corpo: dict | None = None) -> dict:
        for tentativa in range(2):
            token = self._access_token(shop_id, forcar_renovacao=tentativa == 1)
            query = {**(params or {}), **self._comuns(path, token, shop_id)}
            r = self.http.request(metodo, self.host + path, params=query, json=corpo)
            try:
                return self._ler(r)
            except ShopeeError as e:
                # Token recusado: renova uma vez e tenta de novo.
                if tentativa == 0 and getattr(e, "codigo", "") in ERROS_DE_TOKEN:
                    continue
                raise
        raise AssertionError("inalcançável")

    # --- Loja ---

    def nome_da_loja(self, shop_id: int) -> str:
        dados = self._chamar("GET", "/api/v2/shop/get_shop_info", shop_id)
        return dados.get("shop_name") or (dados.get("response") or {}).get("shop_name") or f"Loja {shop_id}"

    # --- Avaliações ---

    def get_unreplied_comments(self, shopee_shop_id: int) -> list[ShopeeComment]:
        brutos: list[dict] = []
        cursor = ""
        while True:
            dados = self._chamar("GET", "/api/v2/product/get_comment", shopee_shop_id,
                                 params={"cursor": cursor, "page_size": 100})["response"]
            brutos += dados.get("item_comment_list", [])
            cursor = dados.get("next_cursor", "")
            if not dados.get("more") or not cursor:
                break

        sem_resposta = [
            c for c in brutos
            if not c.get("hidden") and not ((c.get("comment_reply") or c.get("cmt_reply") or {}).get("reply"))
        ]
        nomes = self._nomes_dos_itens(shopee_shop_id, {int(c["item_id"]) for c in sem_resposta})
        return [_comentario(c, nomes) for c in sem_resposta]

    def reply_comment(self, shopee_shop_id: int, comment_id: int, texto: str) -> None:
        validar_resposta(texto)
        dados = self._chamar("POST", "/api/v2/product/reply_comment", shopee_shop_id,
                             corpo={"comment_list": [{"comment_id": int(comment_id), "comment": texto}]})
        for item in dados.get("response", {}).get("result_list", []):
            if item.get("fail_error"):
                raise ShopeeError(f"{item['fail_error']}: {item.get('fail_message', '')}")

    # --- Produtos ---

    def _ids_dos_itens(self, shop_id: int) -> list[int]:
        ids, offset = [], 0
        while True:
            dados = self._chamar("GET", "/api/v2/product/get_item_list", shop_id,
                                 params={"offset": offset, "page_size": 100,
                                         "item_status": "NORMAL"})["response"]
            ids += [int(i["item_id"]) for i in dados.get("item", [])]
            if not dados.get("has_next_page"):
                return ids
            offset = dados.get("next_offset", offset + 100)

    def _base_info(self, shop_id: int, ids: list[int]) -> list[dict]:
        itens = []
        for i in range(0, len(ids), 50):
            lote = ",".join(str(x) for x in ids[i:i + 50])
            dados = self._chamar("GET", "/api/v2/product/get_item_base_info", shop_id,
                                 params={"item_id_list": lote})["response"]
            itens += dados.get("item_list", [])
        return itens

    def _nomes_dos_itens(self, shop_id: int, ids: set[int]) -> dict[int, str]:
        if not ids:
            return {}
        return {int(i["item_id"]): i.get("item_name", "") for i in self._base_info(shop_id, sorted(ids))}

    def get_items(self, shopee_shop_id: int) -> list[ShopeeItem]:
        return [_item(i) for i in self._base_info(shopee_shop_id, self._ids_dos_itens(shopee_shop_id))]


def _comentario(c: dict, nomes: dict[int, str]) -> ShopeeComment:
    media = c.get("media") or {}
    midias = [Midia("foto", u) for u in media.get("image_url_list") or []]
    midias += [Midia("video", u) for u in media.get("video_url_list") or []]
    item_id = int(c["item_id"])
    return ShopeeComment(
        comment_id=int(c["comment_id"]),
        item_id=item_id,
        produto_nome=nomes.get(item_id, ""),
        estrelas=int(c.get("rating_star", 0)),
        texto=c.get("comment", "") or "",
        comprador=c.get("buyer_username", "") or "",
        criado_em=datetime.fromtimestamp(int(c.get("create_time", 0)), UTC),
        midias=midias,
    )


def _item(i: dict) -> ShopeeItem:
    descricao = i.get("description") or ""
    if not descricao and i.get("extended_description"):
        campos = i["extended_description"].get("field_list", [])
        descricao = "\n".join(f.get("text", "") for f in campos if f.get("field_type") == "text")
    atributos = {}
    for a in i.get("attribute_list") or []:
        valores = [v.get("original_value_name", "") for v in a.get("attribute_value_list") or []]
        valores = [v for v in valores if v]
        if valores:
            atributos[a.get("original_attribute_name", "")] = ", ".join(valores)
    dim = i.get("dimension") or {}
    embalagem = None
    if dim.get("package_length"):
        embalagem = (int(dim["package_length"]), int(dim.get("package_width", 0)),
                     int(dim.get("package_height", 0)))
    return ShopeeItem(
        item_id=int(i["item_id"]),
        nome=i.get("item_name", ""),
        descricao=descricao.strip(),
        atributos=atributos,
        peso_kg=str(i.get("weight") or ""),
        embalagem_cm=embalagem,
    )
