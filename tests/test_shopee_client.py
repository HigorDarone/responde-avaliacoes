"""Cliente real da Shopee, com as respostas da API simuladas (sem rede)."""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.config import Settings
from app.shopee.base import ShopeeError
from app.shopee.client import ShopeeAuth, ShopeeRealClient, Tokens, assinar

AGORA = 1_793_700_000  # timestamp fixo
PARTNER_ID = 2001234
PARTNER_KEY = "chave-de-teste"
LOJA = 777


def _settings(**kw):
    return Settings(_env_file=None, use_mock_shopee=False, shopee_partner_id=str(PARTNER_ID),
                    shopee_partner_key=PARTNER_KEY, **kw)


class Memoria:
    def __init__(self, tokens=None):
        self.tokens = tokens
        self.salvos = []

    def obter(self, shop_id):
        return self.tokens

    def salvar(self, shop_id, tokens):
        self.tokens = tokens
        self.salvos.append(tokens)


def _tokens(validos_por=timedelta(hours=3)):
    return Tokens("AT-1", "RT-1", datetime.fromtimestamp(AGORA, UTC) + validos_por)


class FalsaShopee:
    """Responde por path e guarda os pedidos."""

    def __init__(self, rotas):
        self.rotas = rotas
        self.pedidos: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.pedidos.append(request)
        resposta = self.rotas[request.url.path]
        corpo = resposta(request) if callable(resposta) else resposta
        return httpx.Response(200, json=corpo)

    def query(self, i=-1):
        return {k: v[0] for k, v in parse_qs(self.pedidos[i].url.query.decode()).items()}


def _cliente(rotas, tokens=None, **kw):
    falsa = FalsaShopee(rotas)
    store = Memoria(tokens if tokens is not None else _tokens())
    cliente = ShopeeRealClient(_settings(**kw), store, httpx.Client(transport=httpx.MockTransport(falsa)),
                               relogio=lambda: AGORA)
    return cliente, falsa, store


# --- Assinatura ---


def test_assinatura_igual_ao_exemplo_da_documentacao():
    base = f"{PARTNER_ID}/api/v2/product/get_comment{AGORA}AT-1{LOJA}"
    esperado = hmac.new(PARTNER_KEY.encode(), base.encode(), hashlib.sha256).hexdigest()
    assert assinar(PARTNER_KEY, PARTNER_ID, "/api/v2/product/get_comment", AGORA, "AT-1", LOJA) == esperado
    assert esperado == esperado.lower() and len(esperado) == 64


def test_assinatura_publica_sem_token():
    base = f"{PARTNER_ID}/api/v2/auth/token/get{AGORA}"
    esperado = hmac.new(PARTNER_KEY.encode(), base.encode(), hashlib.sha256).hexdigest()
    assert assinar(PARTNER_KEY, PARTNER_ID, "/api/v2/auth/token/get", AGORA) == esperado


def test_vetor_fixo():
    # Valor calculado à parte, para pegar qualquer mudança na ordem da string base.
    assert assinar("k", 1, "/p", 2, "t", 3) == hmac.new(b"k", b"1/p2t3", hashlib.sha256).hexdigest()


def test_chamada_de_loja_leva_parametros_comuns():
    cliente, falsa, _ = _cliente({"/api/v2/shop/get_shop_info": {"error": "", "shop_name": "Minha Loja 3D"}})
    assert cliente.nome_da_loja(LOJA) == "Minha Loja 3D"
    q = falsa.query()
    assert q["partner_id"] == str(PARTNER_ID) and q["timestamp"] == str(AGORA)
    assert q["access_token"] == "AT-1" and q["shop_id"] == str(LOJA)
    assert q["sign"] == assinar(PARTNER_KEY, PARTNER_ID, "/api/v2/shop/get_shop_info", AGORA, "AT-1", LOJA)
    assert str(falsa.pedidos[0].url).startswith("https://openplatform.sandbox.test-stable.shopee.sg/")


def test_host_de_producao():
    cliente, falsa, _ = _cliente({"/api/v2/shop/get_shop_info": {"error": "", "shop_name": "x"}}, shopee_env="live")
    cliente.nome_da_loja(LOJA)
    assert falsa.pedidos[0].url.host == "partner.shopeemobile.com"


def test_sem_credenciais():
    with pytest.raises(ShopeeError, match="SHOPEE_PARTNER_ID"):
        ShopeeRealClient(Settings(_env_file=None, shopee_partner_id="", shopee_partner_key=""), Memoria())


# --- OAuth ---


@pytest.mark.parametrize(
    ("env", "regiao", "host"),
    [("live", "br", "open.shopee.com.br"), ("sandbox", "br", "open.sandbox.test-stable.shopee.com.br"),
     ("live", "global", "open.shopee.com")],
)
def test_url_de_autorizacao(env, regiao, host):
    auth = ShopeeAuth(_settings(shopee_env=env, shopee_regiao=regiao), relogio=lambda: AGORA)
    url = urlparse(auth.url_autorizacao("http://localhost:8000/shopee/callback", "abc"))
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == host and url.path == "/auth"
    assert q == {"partner_id": str(PARTNER_ID), "auth_type": "seller", "response_type": "code",
                 "redirect_uri": "http://localhost:8000/shopee/callback", "state": "abc"}


def test_troca_do_code_por_tokens():
    falsa = FalsaShopee({"/api/v2/auth/token/get": {
        "error": "", "access_token": "AT-novo", "refresh_token": "RT-novo", "expire_in": 14400}})
    auth = ShopeeAuth(_settings(), httpx.Client(transport=httpx.MockTransport(falsa)), relogio=lambda: AGORA)
    t = auth.obter_tokens("CODE-1", LOJA)

    assert (t.access_token, t.refresh_token) == ("AT-novo", "RT-novo")
    assert t.expira_em == datetime.fromtimestamp(AGORA, UTC) + timedelta(hours=4)
    assert json.loads(falsa.pedidos[0].content) == {"partner_id": PARTNER_ID, "code": "CODE-1", "shop_id": LOJA}
    q = falsa.query()
    assert "access_token" not in q
    assert q["sign"] == assinar(PARTNER_KEY, PARTNER_ID, "/api/v2/auth/token/get", AGORA)


def test_erro_na_troca_do_code():
    falsa = FalsaShopee({"/api/v2/auth/token/get": {"error": "error_param", "message": "code expirou"}})
    auth = ShopeeAuth(_settings(), httpx.Client(transport=httpx.MockTransport(falsa)), relogio=lambda: AGORA)
    with pytest.raises(ShopeeError, match="code expirou"):
        auth.obter_tokens("velho", LOJA)


# --- Renovação de tokens ---

RENOVA = {"/api/v2/auth/access_token/get": {
    "error": "", "access_token": "AT-2", "refresh_token": "RT-2", "expire_in": 14400}}
SHOP_INFO = {"/api/v2/shop/get_shop_info": {"error": "", "shop_name": "x"}}


def test_renova_antes_de_vencer():
    cliente, falsa, store = _cliente(RENOVA | SHOP_INFO, tokens=_tokens(timedelta(minutes=5)))
    cliente.nome_da_loja(LOJA)

    assert [p.url.path for p in falsa.pedidos] == ["/api/v2/auth/access_token/get", "/api/v2/shop/get_shop_info"]
    assert json.loads(falsa.pedidos[0].content)["refresh_token"] == "RT-1"
    assert store.tokens.refresh_token == "RT-2"  # o refresh_token novo é guardado
    assert falsa.query()["access_token"] == "AT-2"


def test_nao_renova_se_ainda_vale():
    cliente, falsa, store = _cliente(RENOVA | SHOP_INFO)
    cliente.nome_da_loja(LOJA)
    assert [p.url.path for p in falsa.pedidos] == ["/api/v2/shop/get_shop_info"]
    assert store.salvos == []


def test_token_recusado_renova_e_tenta_de_novo():
    respostas = iter([{"error": "invalid_acceess_token", "message": "Invalid access_token."},
                      {"error": "", "shop_name": "Loja"}])
    cliente, falsa, store = _cliente(RENOVA | {"/api/v2/shop/get_shop_info": lambda r: next(respostas)})
    assert cliente.nome_da_loja(LOJA) == "Loja"
    assert store.tokens.access_token == "AT-2"


def test_loja_nao_conectada():
    cliente, _, _ = _cliente({}, tokens=None)
    cliente.tokens.tokens = None
    with pytest.raises(ShopeeError, match="não está conectada"):
        cliente.nome_da_loja(LOJA)


# --- Avaliações ---


def _comentario(cid, item_id=5001, reply=None, hidden=False, **kw):
    c = {"comment_id": cid, "comment": f"texto {cid}", "buyer_username": "ana", "item_id": item_id,
         "rating_star": 4, "create_time": AGORA, "hidden": hidden, **kw}
    if reply:
        c["comment_reply"] = {"reply": reply, "hidden": False}
    return c


def test_busca_avaliacoes_sem_resposta_com_paginas_e_midia():
    paginas = {
        "": {"error": "", "response": {"more": True, "next_cursor": "100", "item_comment_list": [
            _comentario(1, media={"image_url_list": ["https://cf.shopee/a.jpg"], "video_url_list": ["https://cf.shopee/v.mp4"]}),
            _comentario(2, reply="Obrigado!"),
        ]}},
        "100": {"error": "", "response": {"more": False, "next_cursor": "", "item_comment_list": [
            _comentario(3, item_id=5002), _comentario(4, hidden=True),
        ]}},
    }
    base_info = {"error": "", "response": {"item_list": [
        {"item_id": 5001, "item_name": "Vaso"}, {"item_id": 5002, "item_name": "Suporte"}]}}
    cliente, falsa, _ = _cliente({
        "/api/v2/product/get_comment": lambda r: paginas[parse_qs(r.url.query.decode()).get("cursor", [""])[0]],
        "/api/v2/product/get_item_base_info": base_info,
    })
    comentarios = cliente.get_unreplied_comments(LOJA)

    assert [c.comment_id for c in comentarios] == [1, 3]  # sem a respondida e sem a oculta
    c1 = comentarios[0]
    assert (c1.produto_nome, c1.estrelas, c1.texto, c1.comprador) == ("Vaso", 4, "texto 1", "ana")
    assert [(m.tipo, m.url) for m in c1.midias] == [("foto", "https://cf.shopee/a.jpg"), ("video", "https://cf.shopee/v.mp4")]
    assert comentarios[1].produto_nome == "Suporte"
    assert falsa.query(0)["page_size"] == "100"


def test_responder():
    cliente, falsa, _ = _cliente({"/api/v2/product/reply_comment": {
        "error": "", "response": {"result_list": [{"comment_id": 1}]}}})
    cliente.reply_comment(LOJA, 1, "Que bom que gostou!")
    pedido = falsa.pedidos[0]
    assert pedido.method == "POST"
    assert json.loads(pedido.content) == {"comment_list": [{"comment_id": 1, "comment": "Que bom que gostou!"}]}


def test_responder_com_falha_no_item():
    cliente, _, _ = _cliente({"/api/v2/product/reply_comment": {"error": "", "response": {"result_list": [
        {"comment_id": 1, "fail_error": "product.duplicate_request", "fail_message": "already replied"}]}}})
    with pytest.raises(ShopeeError, match="duplicate_request"):
        cliente.reply_comment(LOJA, 1, "Oi")


@pytest.mark.parametrize("texto", ["", "   ", "x" * 501])
def test_responder_valida_tamanho_sem_chamar_a_shopee(texto):
    cliente, falsa, _ = _cliente({})
    with pytest.raises(ShopeeError):
        cliente.reply_comment(LOJA, 1, texto)
    assert falsa.pedidos == []


def test_erro_geral_da_api():
    cliente, _, _ = _cliente({"/api/v2/product/reply_comment": {"error": "error_permission", "message": "sem permissão"}})
    with pytest.raises(ShopeeError, match="sem permissão"):
        cliente.reply_comment(LOJA, 1, "Oi")


# --- Produtos ---


def test_lista_produtos_com_atributos_e_descricao_estendida():
    lista = {
        "0": {"error": "", "response": {"item": [{"item_id": 1}, {"item_id": 2}], "has_next_page": True, "next_offset": 2}},
        "2": {"error": "", "response": {"item": [{"item_id": 3}], "has_next_page": False}},
    }
    base = {"error": "", "response": {"item_list": [
        {"item_id": 1, "item_name": "Vaso", "description": "Vaso em PLA", "weight": "0.12",
         "dimension": {"package_length": 18, "package_width": 14, "package_height": 14},
         "attribute_list": [{"original_attribute_name": "Material",
                             "attribute_value_list": [{"original_value_name": "PLA"}]}]},
        {"item_id": 2, "item_name": "Suporte", "description": "",
         "extended_description": {"field_list": [{"field_type": "text", "text": "Base reforçada"},
                                                  {"field_type": "image", "image_info": {}}]}},
        {"item_id": 3, "item_name": "Lua"},
    ]}}
    cliente, falsa, _ = _cliente({
        "/api/v2/product/get_item_list": lambda r: lista[parse_qs(r.url.query.decode())["offset"][0]],
        "/api/v2/product/get_item_base_info": base,
    })
    itens = cliente.get_items(LOJA)

    assert [i.item_id for i in itens] == [1, 2, 3]
    assert itens[0].atributos == {"Material": "PLA"} and itens[0].embalagem_cm == (18, 14, 14)
    assert itens[1].descricao == "Base reforçada"
    q_lista = falsa.query(0)
    assert q_lista["item_status"] == "NORMAL" and q_lista["page_size"] == "100"
    assert falsa.query(-1)["item_id_list"] == "1,2,3"
