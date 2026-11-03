import base64
import random
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.ai.analyzer import (
    SCHEMA_ANALISE,
    AnaliseError,
    AnaliseIA,
    Analisador,
    Imagem,
    PedidoAnalise,
    precisa_ia,
)
from app.ai.contexto import ContextoLoja, RegrasLoja
from app.config import Settings
from app.shopee.base import Midia, ShopeeComment
from tests.fake_claude import ANALISE_OK, FakeClaude, resposta_claude


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, anthropic_api_key="", **kw)


def _comentario(estrelas=5, texto="Ficou lindo!", midias=()) -> ShopeeComment:
    return ShopeeComment(
        comment_id=1,
        item_id=5001,
        produto_nome="Vaso Geométrico",
        estrelas=estrelas,
        texto=texto,
        comprador="x",
        criado_em=datetime(2026, 11, 1),
        midias=list(midias),
    )


def _analisador(fake=None, **settings_kw) -> tuple[Analisador, FakeClaude]:
    fake = fake or FakeClaude()
    return Analisador(client=fake, settings=_settings(**settings_kw)), fake


def test_analise_valida_vira_resultado_com_custo():
    analisador, fake = _analisador()
    r = analisador.analisar(_comentario(), ContextoLoja(shop_id=1))

    assert r.origem == "ia"
    assert r.analise == AnaliseIA(**ANALISE_OK)
    assert r.uso.modelo == "claude-haiku-5-5"
    assert r.uso.tokens_entrada_total == 1300
    assert r.uso.tokens_saida == 120
    # 300 * 0.10 + 1000 * 0.10 * 0.1 + 120 * 0.50, por milhão
    assert r.uso.custo_usd == pytest.approx((30 + 10 + 60) / 1_000_000)
    assert len(fake.messages.chamadas) == 1


def test_requisicao_usa_schema_cache_e_modelo_configurado():
    analisador, fake = _analisador(claude_model="modelo-x")
    analisador.analisar(_comentario(), ContextoLoja(shop_id=1))
    params = fake.messages.chamadas[0]

    assert params["model"] == "modelo-x"
    assert params["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA_ANALISE}
    assert all(b["cache_control"] == {"type": "ephemeral"} for b in params["system"])
    assert "temperature" not in params  # o Haiku 5.5 recusa parâmetros de amostragem
    assert params["messages"][-1]["role"] == "user"  # sem prefill


def test_schema_tem_os_mesmos_campos_do_modelo_pydantic():
    assert set(SCHEMA_ANALISE["properties"]) == set(AnaliseIA.model_fields)
    assert set(SCHEMA_ANALISE["required"]) == set(AnaliseIA.model_fields)


@pytest.mark.parametrize(
    ("dificil", "modelo_dificil", "esperado"),
    [
        (False, "claude-sonnet-5-5", "claude-haiku-5-5"),
        (True, "claude-sonnet-5-5", "claude-sonnet-5-5"),
        (True, "", "claude-haiku-5-5"),  # sem modelo difícil configurado, usa o padrão
    ],
)
def test_modelo_dificil(dificil, modelo_dificil, esperado):
    analisador, fake = _analisador(claude_model_dificil=modelo_dificil)
    analisador.analisar(_comentario(), ContextoLoja(shop_id=1), dificil=dificil)
    assert fake.messages.chamadas[0]["model"] == esperado


def test_imagens_vao_em_base64_antes_do_texto():
    analisador, fake = _analisador()
    img = Imagem("image/jpeg", b"\xff\xd8bytes")
    c = _comentario(midias=[Midia("foto", "https://example.com/a.jpg")])
    analisador.analisar(c, ContextoLoja(shop_id=1), imagens=[img])

    conteudo = fake.messages.chamadas[0]["messages"][0]["content"]
    assert conteudo[0]["type"] == "image"
    assert conteudo[0]["source"]["data"] == base64.standard_b64encode(img.dados).decode()
    assert conteudo[-1]["type"] == "text"
    assert "1 imagem(ns) anexada(s)" in conteudo[-1]["text"]


def test_cinco_estrelas_sem_texto_nem_midia_usa_resposta_pronta_sem_ia():
    fake = FakeClaude()
    analisador = Analisador(client=fake, settings=_settings(), rng=random.Random(0))
    regras = RegrasLoja(respostas_prontas_5_estrelas=["Valeu!"], assinatura="— Loja 3D")
    r = analisador.analisar(_comentario(texto="  "), ContextoLoja(shop_id=1, regras=regras))

    assert r.origem == "pronta"
    assert r.analise.resposta == "Valeu! — Loja 3D"
    assert r.analise.sentimento == "positivo"
    assert r.uso.custo_usd == 0
    assert fake.messages.chamadas == []


@pytest.mark.parametrize(
    ("estrelas", "texto", "midias", "esperado"),
    [
        (5, "", [], False),
        (5, "Top", [], True),
        (5, "", [Midia("foto", "u")], True),
        (4, "", [], True),
    ],
)
def test_precisa_ia(estrelas, texto, midias, esperado):
    assert precisa_ia(_comentario(estrelas, texto, midias)) is esperado


@pytest.mark.parametrize(
    "resposta",
    [
        resposta_claude(ANALISE_OK, stop_reason="refusal"),
        resposta_claude(ANALISE_OK, stop_reason="max_tokens"),
        resposta_claude("isto não é json"),
        resposta_claude({**ANALISE_OK, "categoria": "inventada"}),
        resposta_claude({**ANALISE_OK, "gravidade": 9}),
        resposta_claude({**ANALISE_OK, "resposta": ""}),
    ],
    ids=["recusa", "cortada", "nao-json", "categoria-invalida", "gravidade-invalida", "resposta-vazia"],
)
def test_respostas_ruins_viram_erro(resposta):
    analisador, _ = _analisador(FakeClaude(resposta))
    with pytest.raises(AnaliseError):
        analisador.analisar(_comentario(), ContextoLoja(shop_id=1))


def test_palavra_proibida_manda_para_revisao():
    analisador, _ = _analisador()
    contexto = ContextoLoja(shop_id=1, regras=RegrasLoja(palavras_proibidas=["ESTANTE"]))
    r = analisador.analisar(_comentario(), contexto)
    assert r.analise.precisa_revisao is True
    assert r.analise.motivo.startswith("Precisa de você:")


def test_todas_as_avaliacoes_mock_passam_pelo_analisador(mock_client):
    analisador, fake = _analisador()
    comentarios = mock_client.get_unreplied_comments(100001)
    resultados = [analisador.analisar(c, ContextoLoja(shop_id=1)) for c in comentarios]

    assert len(resultados) == len(comentarios)
    assert sum(r.origem == "pronta" for r in resultados) == 1  # só a 5★ sem texto
    assert len(fake.messages.chamadas) == len(comentarios) - 1
    for params, c in zip(fake.messages.chamadas, [c for c in comentarios if precisa_ia(c)]):
        texto = params["messages"][0]["content"][-1]["text"]
        assert c.texto in texto
        assert f"Estrelas: {c.estrelas} de 5" in texto


# --- Batch API ---


def test_criar_lote_envia_um_pedido_por_avaliacao():
    analisador, fake = _analisador()
    ctx = ContextoLoja(shop_id=1)
    pedidos = [PedidoAnalise(f"1-{i}", _comentario(texto=f"t{i}"), ctx) for i in range(3)]

    assert analisador.criar_lote(pedidos) == "msgbatch_teste"
    enviados = fake.messages.batches.criados[0]
    assert [r["custom_id"] for r in enviados] == ["1-0", "1-1", "1-2"]
    assert enviados[0]["params"]["output_config"]["format"]["type"] == "json_schema"


def test_criar_lote_recusa_avaliacao_de_resposta_pronta():
    analisador, _ = _analisador()
    with pytest.raises(ValueError):
        analisador.criar_lote([PedidoAnalise("1-1", _comentario(texto=""), ContextoLoja(shop_id=1))])


def test_resultados_lote_em_andamento_devolve_none():
    analisador, _ = _analisador()
    assert analisador.resultados_lote("msgbatch_teste", {}) is None


def test_resultados_lote_terminado():
    fake = FakeClaude()
    fake.messages.batches.status = "ended"
    fake.messages.batches.itens = [
        SimpleNamespace(
            custom_id="1-ok",
            result=SimpleNamespace(type="succeeded", message=resposta_claude(ANALISE_OK)),
        ),
        SimpleNamespace(
            custom_id="1-ruim",
            result=SimpleNamespace(type="succeeded", message=resposta_claude("{}")),
        ),
        SimpleNamespace(custom_id="1-exp", result=SimpleNamespace(type="expired")),
    ]
    analisador, _ = _analisador(fake)
    ctx = ContextoLoja(shop_id=1)
    r = analisador.resultados_lote("msgbatch_teste", {"1-ok": ctx, "1-ruim": ctx, "1-exp": ctx})

    assert r["1-ok"].analise.categoria == "elogio"
    assert r["1-ok"].uso.lote is True
    assert r["1-ok"].uso.custo_usd == pytest.approx((30 + 10 + 60) / 1_000_000 / 2)
    assert isinstance(r["1-ruim"], AnaliseError)
    assert isinstance(r["1-exp"], AnaliseError)
