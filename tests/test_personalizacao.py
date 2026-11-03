from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai.contexto import RegrasLoja, carregar_contexto
from app.ai.prompts import render_regras_loja
from app.db.models import AuditLog
from app.db.repo import criar_loja
from app.shopee.base import ShopeeComment
from app.web.main import create_app
from app.web.personalizacao import _linhas
from app.web.routes import get_db


@pytest.fixture
def client(session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    return TestClient(app)


@pytest.fixture
def loja(session):
    loja = criar_loja(session, "Loja Teste", 1)
    session.commit()
    return loja


def _dados(**kw):
    base = {
        "tom": "descontraido",
        "assinatura": "",
        "uso_emoji": "pouco",
        "tamanho_resposta": "curta",
        "oferta_defeito": "",
        "oferta_quebrado_no_envio": "",
        "oferta_atraso": "",
        "oferta_produto_diferente": "",
        "oferta_duvida": "",
        "nunca_dizer": "",
        "palavras_proibidas": "",
        "respostas_prontas": "",
    }
    return {**base, **kw}


def test_menu_e_redirecionamento(client, loja):
    r = client.get("/personalizacao", follow_redirects=False)
    assert r.headers["location"] == "/personalizacao/regras"
    html = client.get("/personalizacao/regras").text
    link_menu = html[html.index('href="/personalizacao"'):]
    assert link_menu.index('aria-current="page"') < link_menu.index("Personalização")


def test_pagina_mostra_valores_atuais_e_abas(client, session, loja):
    loja.settings.tom = "formal"
    loja.settings.assinatura = "— Equipe Teste"
    loja.settings.palavras_proibidas = ["barato", "grátis"]
    loja.settings.oferta_por_categoria = {"defeito": "Trocamos a peça."}
    session.commit()
    html = client.get("/personalizacao/regras").text

    assert 'value="formal" checked' in html
    assert 'value="— Equipe Teste"' in html
    assert "barato\ngrátis" in html
    assert "Trocamos a peça." in html
    for aba in ["Regras", "Exemplos", "Produtos", "Testar"]:
        assert aba in html
    assert "Como a IA lê suas regras" in html


def test_salvar_regras(client, session, loja):
    r = client.post("/personalizacao/regras", data=_dados(
        tom="formal",
        assinatura="  — Equipe Teste  ",
        uso_emoji="nunca",
        tamanho_resposta="media",
        oferta_defeito="Pedir foto no chat e trocar a peça.",
        oferta_atraso="   ",
        nunca_dizer="Prazo de entrega\n\nDesconto\nprazo de entrega",
        palavras_proibidas="barato, grátis; promoção\nbarato",
        respostas_prontas="Valeu!\nObrigado pelas 5 estrelas!",
    ))

    assert r.status_code == 200 and "Regras salvas" in r.text
    session.refresh(loja.settings)
    s = loja.settings
    assert (s.tom, s.uso_emoji, s.tamanho_resposta) == ("formal", "nunca", "media")
    assert s.assinatura == "— Equipe Teste"
    assert s.oferta_por_categoria == {"defeito": "Pedir foto no chat e trocar a peça."}
    assert s.nunca_dizer == ["Prazo de entrega", "Desconto"]  # sem vazio e sem repetido
    assert s.palavras_proibidas == ["barato", "grátis", "promoção"]
    assert s.respostas_prontas_5_estrelas == ["Valeu!", "Obrigado pelas 5 estrelas!"]

    log = session.scalar(select(AuditLog).where(AuditLog.acao == "regras_alteradas"))
    assert log.autor == "painel"
    assert set(log.detalhes["campos"]) == {
        "tom", "assinatura", "uso_emoji", "tamanho_resposta", "oferta_por_categoria",
        "nunca_dizer", "palavras_proibidas", "respostas_prontas_5_estrelas",
    }


def test_salvar_sem_mudancas_nao_registra(client, session, loja):
    r = client.post("/personalizacao/regras", data=_dados())
    assert "Nada mudou" in r.text
    assert session.scalars(select(AuditLog)).all() == []


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("tom", "grosso"),
        ("uso_emoji", "muito"),
        ("tamanho_resposta", "enorme"),
        ("assinatura", "x" * 121),
        ("oferta_defeito", "x" * 501),
        ("nunca_dizer", "\n".join(f"regra {i}" for i in range(51))),
        ("palavras_proibidas", "x" * 201),
    ],
)
def test_valores_invalidos_nao_salvam(client, session, loja, campo, valor):
    r = client.post("/personalizacao/regras", data=_dados(**{campo: valor}))
    assert r.status_code == 422
    assert "Nada foi salvo" in r.text
    session.refresh(loja.settings)
    assert loja.settings.tom == "descontraido" and loja.settings.assinatura == ""


def test_formulario_invalido_mantem_o_que_foi_digitado(client, loja):
    r = client.post("/personalizacao/regras", data=_dados(tom="xx", assinatura="— Minha assinatura"))
    assert 'value="— Minha assinatura"' in r.text


def test_previa_nao_salva(client, session, loja):
    r = client.post("/personalizacao/regras/previa", data=_dados(tom="formal", palavras_proibidas="barato"))
    assert r.status_code == 200
    assert 'id="previa"' in r.text
    assert "formal" in r.text and "barato" in r.text
    session.refresh(loja.settings)
    assert loja.settings.tom == "descontraido"


def test_previa_igual_ao_prompt(client, session, loja):
    client.post("/personalizacao/regras", data=_dados(oferta_defeito="Trocamos a peça."))
    session.refresh(loja.settings)
    esperado = render_regras_loja(RegrasLoja.de_settings(loja.settings))
    html = client.get("/personalizacao/regras").text
    assert "Trocamos a peça." in esperado
    assert "defeito: Trocamos a peça." in html


def test_regras_salvas_entram_na_analise(client, session, loja):
    client.post("/personalizacao/regras", data=_dados(tom="formal", palavras_proibidas="barato"))
    comentario = ShopeeComment(1, 1, "Vaso", 5, "Lindo", "x", datetime(2026, 11, 1))
    ctx = carregar_contexto(session, loja.id, comentario)
    assert ctx.regras.tom == "formal"
    assert ctx.regras.palavras_proibidas == ["barato"]


def test_sem_loja(client):
    assert "Ainda não há loja cadastrada" in client.get("/personalizacao/regras").text


def test_linhas():
    assert _linhas(" a \n\nb\nA\n c ") == ["a", "b", "c"]
    assert _linhas("x, y;z", r"[\n,;]") == ["x", "y", "z"]
    assert _linhas("") == []
