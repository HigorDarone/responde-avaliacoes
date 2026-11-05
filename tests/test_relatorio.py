"""Relatório "O que melhorar": filtros, contagens e análise com IA (Claude simulado)."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import relatorio as rel
from app.config import Settings
from app.db.models import AuditLog, RelatorioMelhoria, Review
from app.db.repo import criar_loja
from app.web.main import create_app
from app.web.relatorio import get_claude, get_relogio
from app.web.routes import get_config, get_db
from tests.fake_claude import FakeClaude, resposta_claude

AGORA = datetime(2026, 11, 5, 12, tzinfo=UTC)
CONFIG = Settings(_env_file=None, use_mock_shopee=True, anthropic_api_key="sk-teste")


@pytest.fixture
def loja(session):
    loja = criar_loja(session, "Minha loja", 100001)
    session.commit()
    return loja


def _review(session, loja, n, estrelas, texto="", produto="Vaso", categoria=None, dias=1, motivo=None):
    r = Review(shop_id=loja.id, comment_id=n, item_id=1, produto_nome=produto, estrelas=estrelas,
               texto=texto, categoria=categoria, motivo=motivo, avaliado_em=AGORA - timedelta(days=dias))
    session.add(r)
    session.commit()
    return r


@pytest.fixture
def avaliacoes(session, loja):
    return [
        _review(session, loja, 1, 1, "A cor veio bem diferente da foto", categoria="produto_diferente"),
        _review(session, loja, 2, 2, "Cor desbotada, nada a ver com o anúncio", categoria="produto_diferente",
                produto="Suporte"),
        _review(session, loja, 3, 1, "Chegou quebrado", categoria="quebrado_no_envio"),
        _review(session, loja, 4, 3, "", categoria=None),  # sem texto, ainda sem análise
        _review(session, loja, 5, 4, "Bom, mas demorou", categoria="atraso"),
        _review(session, loja, 6, 5, "Perfeito", categoria="elogio"),
        _review(session, loja, 7, 1, "Antiga, quebrou", categoria="defeito", dias=200),
    ]


# --- Filtros e contagens ---


def test_filtro_da_requisicao_valida_valores():
    f = rel.Filtro.da_requisicao(["2", "9", "x", "1", "2"], "abc", "  Vaso  ")
    assert f == rel.Filtro(estrelas=(1, 2), dias=90, produto="Vaso")
    assert rel.Filtro.da_requisicao(None, "7", None) == rel.Filtro()  # período fora da lista = 90
    assert rel.Filtro.da_requisicao(["5"], "0", "").estrelas == (1, 2, 3)  # 5★ não entra


def test_filtra_por_estrelas_periodo_e_produto(session, loja, avaliacoes):
    ids = lambda f: sorted(r.comment_id for r in rel.avaliacoes(session, loja.id, f, AGORA))
    assert ids(rel.Filtro()) == [1, 2, 3, 4]
    assert ids(rel.Filtro(estrelas=(1, 2, 3, 4))) == [1, 2, 3, 4, 5]
    assert ids(rel.Filtro(dias=0)) == [1, 2, 3, 4, 7]
    assert ids(rel.Filtro(estrelas=(1,), dias=365)) == [1, 3, 7]
    assert ids(rel.Filtro(produto="Suporte")) == [2]


def test_nao_mistura_lojas(session, loja, avaliacoes):
    outra = criar_loja(session, "Outra", 200002)
    session.commit()
    assert rel.avaliacoes(session, outra.id, rel.Filtro(), AGORA) == []


def test_resumo(session, loja, avaliacoes):
    resumo = rel.resumir(rel.avaliacoes(session, loja.id, rel.Filtro(), AGORA))
    assert (resumo.total, resumo.com_texto) == (4, 3)
    assert resumo.por_estrela == {1: 2, 2: 1, 3: 1, 4: 0}
    assert resumo.por_categoria[0] == ("produto_diferente", 2)
    assert ("sem_analise", 1) in resumo.por_categoria
    assert resumo.por_produto[0][:2] == ("Vaso", 3)


# --- Análise com IA ---

RESPOSTA_IA = {
    "resumo": "A maior parte das reclamações é sobre a cor.",
    "temas": [
        {"titulo": "Peça quebrada", "descricao": "Chegou quebrada.", "sugestao": "Reforçar a embalagem.",
         "prioridade": "alta", "avaliacoes": [3]},
        {"titulo": "Cor diferente da foto", "descricao": "A cor não bate com o anúncio.",
         "sugestao": "Fotografar com luz natural.", "prioridade": "alta",
         "avaliacoes": [1, 2, 2, 999, 3]},  # repetido, inexistente e já usado: limpos
        {"titulo": "Tema sem avaliação válida", "descricao": "x", "sugestao": "y", "prioridade": "baixa",
         "avaliacoes": [12345]},
    ],
}


def test_gerar_com_ia_limpa_ids_e_ordena(session, loja, avaliacoes):
    reviews = rel.avaliacoes(session, loja.id, rel.Filtro(), AGORA)
    ids = {r.comment_id: r.id for r in reviews}
    resposta = {**RESPOSTA_IA, "temas": [
        {**t, "avaliacoes": [ids.get(i, i) for i in t["avaliacoes"]]} for t in RESPOSTA_IA["temas"]
    ]}
    claude = FakeClaude(resposta_claude(resposta))
    analise, uso = rel.gerar_com_ia(claude, "claude-haiku-5-5", reviews)

    assert [t.titulo for t in analise.temas] == ["Cor diferente da foto", "Peça quebrada"]
    assert analise.temas[0].avaliacoes == [ids[1], ids[2]]
    assert uso.custo_usd > 0

    [chamada] = claude.messages.chamadas
    texto = chamada["messages"][0]["content"]
    assert "A cor veio bem diferente da foto" in texto and f"[{ids[1]}]" in texto
    assert f"[{ids[4]}]" not in texto  # sem texto e sem motivo: não vai para a IA
    assert chamada["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert chamada["output_config"]["format"]["type"] == "json_schema"


def test_gerar_sem_texto_nao_chama_ia(session, loja):
    r = _review(session, loja, 10, 1, "")
    claude = FakeClaude()
    with pytest.raises(rel.RelatorioError, match="Nenhuma avaliação com texto"):
        rel.gerar_com_ia(claude, "claude-haiku-5-5", [r])
    assert claude.messages.chamadas == []


@pytest.mark.parametrize("stop, dados, erro", [
    ("refusal", RESPOSTA_IA, "recusou"),
    ("max_tokens", RESPOSTA_IA, "grande demais"),
    ("end_turn", "não é json", "inválida"),
])
def test_gerar_erros(session, loja, avaliacoes, stop, dados, erro):
    reviews = rel.avaliacoes(session, loja.id, rel.Filtro(), AGORA)
    with pytest.raises(rel.RelatorioError, match=erro):
        rel.gerar_com_ia(FakeClaude(resposta_claude(dados, stop_reason=stop)), "claude-haiku-5-5", reviews)


# --- Tela ---


@pytest.fixture
def claude():
    return FakeClaude(resposta_claude(RESPOSTA_IA))


@pytest.fixture
def client(session, claude):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_config] = lambda: CONFIG
    app.dependency_overrides[get_claude] = lambda: claude
    app.dependency_overrides[get_relogio] = lambda: (lambda: AGORA)
    return TestClient(app)


def test_tela_mostra_contagens_e_lista(client, avaliacoes):
    html = client.get("/relatorio").text
    assert "O que <span" in html and 'href="/relatorio"' in html
    assert "Produto diferente" in html and "Sem classificação" in html
    assert "A cor veio bem diferente da foto" in html
    assert "Perfeito" not in html and "Antiga, quebrou" not in html  # 5★ e fora do período
    assert "Gerar relatório com IA" in html


def test_tela_filtrada_por_produto(client, avaliacoes):
    html = client.get("/relatorio", params={"produto": "Suporte", "estrelas": ["1", "2"]}).text
    assert "Cor desbotada" in html and "A cor veio bem diferente" not in html


def test_tela_sem_resultados(client, loja):
    assert "Nenhuma avaliação com esses filtros" in client.get("/relatorio").text


def test_gerar_pela_tela_salva_e_mostra_de_novo(client, session, loja, avaliacoes, claude):
    ids = {r.comment_id: r.id for r in avaliacoes}
    claude.messages._respostas = [resposta_claude({**RESPOSTA_IA, "temas": [
        {**RESPOSTA_IA["temas"][1], "avaliacoes": [ids[1], ids[2]]},
    ]})]
    r = client.post("/relatorio/gerar?estrelas=1&estrelas=2&estrelas=3&dias=90")
    assert r.status_code == 200
    assert "Cor diferente da foto" in r.text and "Fotografar com luz natural." in r.text
    assert "Ver 2 avaliações" in r.text and "Gerar de novo" in r.text

    salvo = session.scalar(select(RelatorioMelhoria))
    assert salvo.filtros == {"estrelas": [1, 2, 3], "dias": 90, "produto": ""}
    assert salvo.total_avaliacoes == 3 and salvo.custo_estimado_usd > 0
    assert session.scalar(select(AuditLog).where(AuditLog.acao == "relatorio_gerado"))

    # Ao voltar com os mesmos filtros, o relatório salvo aparece sem chamar a IA de novo.
    chamadas = len(claude.messages.chamadas)
    assert "Cor diferente da foto" in client.get("/relatorio?estrelas=1&estrelas=2&estrelas=3&dias=90").text
    assert "Cor diferente da foto" not in client.get("/relatorio?estrelas=1&dias=90").text  # outro filtro
    assert len(claude.messages.chamadas) == chamadas


def test_gerar_sem_chave(client, avaliacoes):
    client.app.dependency_overrides[get_claude] = lambda: None
    r = client.post("/relatorio/gerar")
    assert r.status_code == 502 and "ANTHROPIC_API_KEY" in r.text


def test_gerar_com_falha_da_api(client, avaliacoes, claude):
    def explode(**_):
        raise RuntimeError("rede caiu")

    claude.messages.create = explode
    r = client.post("/relatorio/gerar")
    assert r.status_code == 502 and "Não consegui falar com o Claude" in r.text
