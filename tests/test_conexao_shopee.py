"""Tela Conexão Shopee (OAuth), importação de produtos e migração simples do banco."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text

from app.config import Settings
from app.db.base import init_db
from app.db.models import AuditLog, ProductInfo, Shop
from app.db.repo import consulta_lojas_operaveis, criar_loja
from app.shopee.base import ShopeeError
from app.shopee.client import Tokens
from app.shopee.importar import importar_produtos
from app.web.main import create_app
from app.web.routes import get_db, get_shopee
from app.web.shopee import COOKIE_STATE, get_auth, get_config

REAL = Settings(_env_file=None, use_mock_shopee=False, shopee_partner_id="2001234",
                shopee_partner_key="k", shopee_env="live", shopee_regiao="br")


class AuthFalsa:
    def __init__(self, falhar=False):
        self.falhar = falhar
        self.codigos = []
        self.http = None

    def url_autorizacao(self, redirect_uri, state):
        return f"https://open.shopee.com.br/auth?redirect_uri={redirect_uri}&state={state}"

    def obter_tokens(self, code, shop_id):
        if self.falhar:
            raise ShopeeError("error_param: code inválido")
        self.codigos.append((code, shop_id))
        return Tokens("AT", "RT", datetime.now(UTC) + timedelta(hours=4))


@pytest.fixture
def auth():
    return AuthFalsa()


@pytest.fixture
def client(session, auth, mock_client):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_config] = lambda: REAL
    app.dependency_overrides[get_auth] = lambda: auth
    app.dependency_overrides[get_shopee] = lambda: mock_client
    return TestClient(app)


def test_pagina_em_modo_mock_explica_o_que_falta(session, mock_client):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_config] = lambda: Settings(_env_file=None, use_mock_shopee=True)
    html = TestClient(app).get("/shopee").text
    assert "modo de teste" in html and "Conectar loja Shopee" not in html


def test_pagina_pronta_para_conectar(client):
    html = client.get("/shopee").text
    assert "Conectar loja Shopee" in html and "Produção (live)" in html


def test_conectar_redireciona_com_state_e_cookie(client):
    r = client.get("/shopee/conectar", follow_redirects=False)
    assert r.status_code == 302
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    assert len(state) > 20 and r.cookies.get(COOKIE_STATE) == state


def test_callback_salva_tokens_e_cria_a_loja(client, session, auth):
    client.get("/shopee/conectar", follow_redirects=False)
    state = client.cookies.get(COOKIE_STATE)
    r = client.get(f"/shopee/callback?code=C1&shop_id=555&state={state}", follow_redirects=False)

    assert r.status_code == 303 and r.headers["location"] == "/shopee?ok=1"
    assert auth.codigos == [("C1", 555)]
    loja = session.scalar(select(Shop).where(Shop.shopee_shop_id == 555))
    assert (loja.access_token, loja.refresh_token) == ("AT", "RT")
    assert loja.settings.auto_publicar is False  # loja nova sempre com respostas automáticas desligadas
    assert session.scalar(select(AuditLog).where(AuditLog.acao == "shopee_conectada")).shop_id == loja.id
    assert "Loja conectada" in client.get("/shopee?ok=1").text


def test_callback_sem_state_correto_e_recusado(client, session, auth):
    client.get("/shopee/conectar", follow_redirects=False)
    r = client.get("/shopee/callback?code=C1&shop_id=555&state=forjado")
    assert r.status_code == 400 and auth.codigos == []
    assert session.scalar(select(Shop).where(Shop.shopee_shop_id == 555)) is None


def test_callback_com_erro_da_shopee(client, session):
    client.app.dependency_overrides[get_auth] = lambda: AuthFalsa(falhar=True)
    client.get("/shopee/conectar", follow_redirects=False)
    r = client.get(f"/shopee/callback?code=x&shop_id=555&state={client.cookies.get(COOKIE_STATE)}")
    assert r.status_code == 502 and "code inválido" in r.text


def test_com_shopee_real_so_lojas_conectadas_contam(session):
    criar_loja(session, "Mock", 100001)
    real = criar_loja(session, "Real", 555)
    real.refresh_token = "RT"
    session.commit()
    assert [l.nome for l in session.scalars(consulta_lojas_operaveis(usar_mock=False))] == ["Real"]
    assert len(session.scalars(consulta_lojas_operaveis(usar_mock=True)).all()) == 2


# --- Importar produtos ---


def test_importar_preenche_sem_apagar_o_que_foi_escrito(session, mock_client):
    loja = criar_loja(session, "Loja", 100001)
    session.add(ProductInfo(shop_id=loja.id, item_id=5001, nome="Nome antigo", material="PLA fosco premium",
                            cuidados="Não lavar"))
    session.commit()

    r = importar_produtos(session, loja, mock_client)
    assert (r.criados, r.atualizados) == (4, 1)

    vaso = session.scalar(select(ProductInfo).where(ProductInfo.item_id == 5001))
    assert vaso.nome == "Vaso Geométrico Low Poly 15cm"
    assert vaso.material == "PLA fosco premium" and vaso.cuidados == "Não lavar"  # mantidos
    assert "Altura 15 cm" in vaso.descricao_anuncio
    assert "Embalagem (não é o tamanho da peça): 18 x 14 x 14 cm." in vaso.descricao_anuncio
    assert vaso.tamanho == ""  # medida da embalagem não vira tamanho
    dragao = session.scalar(select(ProductInfo).where(ProductInfo.item_id == 5004))
    assert dragao.material == "PLA silk" and dragao.importado_em


def test_importar_pela_tela(client, session):
    criar_loja(session, "Loja", 100001)
    session.commit()
    client.app.dependency_overrides[get_config] = lambda: Settings(_env_file=None, use_mock_shopee=True)
    r = client.post("/personalizacao/produtos/importar")
    assert r.status_code == 200 and "5 novos" in r.text
    assert "Descrição do anúncio na Shopee" in r.text


def test_descricao_do_anuncio_vai_para_o_prompt(session, mock_client):
    from app.ai.contexto import carregar_contexto
    from app.ai.prompts import render_mensagem
    from app.shopee.base import ShopeeComment

    loja = criar_loja(session, "Loja", 100001)
    importar_produtos(session, loja, mock_client)
    c = ShopeeComment(1, 5004, "Dragão", 4, "Posso pintar?", "x", datetime(2026, 11, 1))
    texto = render_mensagem(c, carregar_contexto(session, loja.id, c), 0)
    assert "Descrição do anúncio na Shopee: Dragão articulado" in texto


# --- Migração simples ---


def test_init_db_acrescenta_colunas_novas_sem_perder_dados(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'antigo.db'}")
    init_db(engine)
    with engine.begin() as c:
        c.execute(text("ALTER TABLE product_info DROP COLUMN descricao_anuncio"))
        c.execute(text("INSERT INTO shops (nome, ativo, criado_em) VALUES ('Antiga', 1, '2026-01-01')"))
    init_db(engine)
    colunas = {c["name"] for c in inspect(engine).get_columns("product_info")}
    assert "descricao_anuncio" in colunas
    with engine.connect() as c:
        assert c.execute(text("SELECT nome FROM shops")).scalar() == "Antiga"
