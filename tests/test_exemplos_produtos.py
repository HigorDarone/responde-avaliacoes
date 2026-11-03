from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai.contexto import carregar_contexto
from app.db.models import DraftStatus, ProductInfo, ReplyDraft, ReplyExample, Review, ReviewStatus
from app.db.repo import criar_loja
from app.shopee.base import ShopeeComment
from app.web.main import create_app
from app.web.routes import get_db, get_shopee

COMENTARIO_MOCK = 9000005  # existe no mock, sem resposta
SUGESTAO = "Poxa, sentimos muito! Chama a gente no chat."


@pytest.fixture
def loja(session):
    loja = criar_loja(session, "Loja mock", 100001)
    session.commit()
    return loja


@pytest.fixture
def client(session, mock_client):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_shopee] = lambda: mock_client
    return TestClient(app)


def _review(session, loja, comment_id=COMENTARIO_MOCK, texto="Chegou com a cauda quebrada.",
            item_id=5004, produto="Miniatura Dragão", status=ReviewStatus.NA_FILA):
    r = Review(
        shop_id=loja.id, comment_id=comment_id, item_id=item_id, produto_nome=produto, estrelas=1,
        texto=texto, status=status, sentimento="negativo", categoria="quebrado_no_envio", gravidade=3,
        avaliado_em=datetime(2026, 10, 30, tzinfo=UTC),
    )
    session.add(r)
    session.flush()
    session.add(ReplyDraft(shop_id=loja.id, review_id=r.id, texto_ia=SUGESTAO))
    session.commit()
    return r


def _exemplos(session):
    session.expire_all()
    return session.scalars(select(ReplyExample).order_by(ReplyExample.id)).all()


def _comentario(texto="A cauda chegou quebrada", item_id=5004):
    return ShopeeComment(1, item_id, "Miniatura Dragão", 1, texto, "x", datetime(2026, 11, 1))


# --- Aprendizado pelas edições ---


def test_publicar_editada_vira_exemplo(client, session, loja):
    review = _review(session, loja)
    client.post(f"/fila/{review.id}/aprovar", data={"texto": "Poxa! Manda foto no chat que a gente resolve."})

    [ex] = _exemplos(session)
    assert ex.origem == "edicao" and ex.ativo
    assert ex.texto_avaliacao == "Chegou com a cauda quebrada."
    assert ex.resposta_ideal == "Poxa! Manda foto no chat que a gente resolve."
    assert (ex.categoria, ex.sentimento) == ("quebrado_no_envio", "negativo")
    # E já entra na próxima análise parecida.
    ctx = carregar_contexto(session, loja.id, _comentario())
    assert [e.resposta_ideal for e in ctx.exemplos] == [ex.resposta_ideal]


def test_publicar_sem_editar_nao_cria_exemplo(client, session, loja):
    review = _review(session, loja)
    client.post(f"/fila/{review.id}/aprovar", data={"texto": SUGESTAO})
    assert _exemplos(session) == []


def test_card_publicado_mostra_que_virou_exemplo(client, session, loja):
    review = _review(session, loja)
    r = client.post(f"/fila/{review.id}/aprovar", data={"texto": "Versão minha."})
    assert "Virou exemplo (sua edição)" in r.text


# --- Marcar como ótima ---


def test_marcar_otima_na_fila_usa_o_texto_do_campo(client, session, loja):
    review = _review(session, loja)
    r = client.post(f"/fila/{review.id}/otima", data={"texto": "Texto do campo"})

    [ex] = _exemplos(session)
    assert ex.origem == "marcada_otima" and ex.resposta_ideal == "Texto do campo"
    assert "Exemplo da loja" in r.text
    assert "Marcar como ótima" not in r.text
    assert session.get(Review, review.id).status == ReviewStatus.NA_FILA  # não publica nada


def test_marcar_otima_duas_vezes_nao_duplica(client, session, loja):
    review = _review(session, loja)
    client.post(f"/fila/{review.id}/otima", data={"texto": SUGESTAO})
    client.post(f"/fila/{review.id}/otima", data={"texto": SUGESTAO})
    assert len(_exemplos(session)) == 1


def test_marcar_otima_depois_de_publicada(client, session, loja):
    review = _review(session, loja)
    client.post(f"/fila/{review.id}/aprovar", data={"texto": SUGESTAO})
    r = client.post(f"/fila/{review.id}/otima")
    [ex] = _exemplos(session)
    assert ex.resposta_ideal == SUGESTAO and ex.origem == "marcada_otima"
    assert "Exemplo da loja" in r.text


def test_nao_marca_descartada(client, session, loja):
    review = _review(session, loja)
    client.post(f"/fila/{review.id}/descartar")
    assert client.post(f"/fila/{review.id}/otima").status_code == 409
    assert _exemplos(session) == []


def test_fila_mostra_botao_e_etiqueta(client, session, loja):
    review = _review(session, loja)
    assert "Marcar como ótima" in client.get("/fila").text
    client.post(f"/fila/{review.id}/otima", data={"texto": SUGESTAO})
    html = client.get("/fila").text
    assert "Exemplo da loja" in html and "Marcar como ótima" not in html


# --- Aba Exemplos ---


def _novo_exemplo(**kw):
    return {"categoria": "elogio", "sentimento": "positivo", "texto_avaliacao": "Amei o vaso",
            "resposta_ideal": "Que bom que o vaso agradou!", "filtro": "", **kw}


def test_criar_exemplo_manual(client, session, loja):
    r = client.post("/personalizacao/exemplos", data=_novo_exemplo())
    assert r.status_code == 200 and "Exemplo adicionado" in r.text
    [ex] = _exemplos(session)
    assert ex.origem == "manual" and ex.texto_avaliacao == "Amei o vaso"
    assert "Que bom que o vaso agradou!" in client.get("/personalizacao/exemplos").text


@pytest.mark.parametrize(
    "dados",
    [
        {"categoria": "inventada"},
        {"sentimento": "feliz"},
        {"texto_avaliacao": "  "},
        {"resposta_ideal": ""},
        {"resposta_ideal": "x" * 2001},
    ],
)
def test_exemplo_invalido(client, session, loja, dados):
    r = client.post("/personalizacao/exemplos", data=_novo_exemplo(**dados))
    assert r.status_code == 422
    assert _exemplos(session) == []


def test_exemplo_duplicado(client, session, loja):
    client.post("/personalizacao/exemplos", data=_novo_exemplo())
    r = client.post("/personalizacao/exemplos", data=_novo_exemplo())
    assert r.status_code == 422 and "já existe" in r.text
    assert len(_exemplos(session)) == 1


def test_desativar_ativar_excluir(client, session, loja):
    client.post("/personalizacao/exemplos", data=_novo_exemplo())
    ex_id = _exemplos(session)[0].id

    client.post(f"/personalizacao/exemplos/{ex_id}/desativar")
    assert _exemplos(session)[0].ativo is False
    assert carregar_contexto(session, loja.id, _comentario("Amei")).exemplos == []

    client.post(f"/personalizacao/exemplos/{ex_id}/ativar")
    assert _exemplos(session)[0].ativo is True

    client.post(f"/personalizacao/exemplos/{ex_id}/excluir")
    assert _exemplos(session) == []


def test_filtro_por_categoria(client, session, loja):
    client.post("/personalizacao/exemplos", data=_novo_exemplo())
    client.post("/personalizacao/exemplos", data=_novo_exemplo(
        categoria="atraso", sentimento="negativo", texto_avaliacao="Demorou", resposta_ideal="Desculpa a demora"))
    html = client.get("/personalizacao/exemplos?categoria=atraso").text
    assert "Desculpa a demora" in html and "Que bom que o vaso agradou!" not in html


def test_marca_exemplos_fora_do_limite(client, session, loja):
    loja.settings.max_exemplos_por_categoria = 2
    base = datetime(2026, 11, 1, tzinfo=UTC)
    session.add_all([
        ReplyExample(shop_id=loja.id, categoria="elogio", sentimento="positivo", texto_avaliacao=f"a{i}",
                     resposta_ideal=f"r{i}", criado_em=base - timedelta(days=i))
        for i in range(3)
    ])
    session.commit()
    html = client.get("/personalizacao/exemplos").text
    assert html.count("Fora do limite") == 1


def test_nao_mexe_em_exemplo_de_outra_loja(client, session, loja):
    outra = criar_loja(session, "Outra", 2)
    alheio = ReplyExample(shop_id=outra.id, categoria="elogio", sentimento="positivo",
                          texto_avaliacao="x", resposta_ideal="y")
    session.add(alheio)
    session.commit()
    assert client.post(f"/personalizacao/exemplos/{alheio.id}/excluir").status_code == 404
    assert "x" not in [e.texto_avaliacao for e in _exemplos(session) if e.shop_id == loja.id]
    assert len(_exemplos(session)) == 1


# --- Aba Produtos ---


def _produto(**kw):
    return {"item_id": "5004", "nome": "Miniatura Dragão", "material": "PLA",
            "tamanho": "30 cm", "normal_impressao_3d": "Linhas de camada leves",
            "cuidados": "Evitar sol", "observacoes": "Pode pintar com acrílica e primer", **kw}


def _produtos(session):
    session.expire_all()
    return session.scalars(select(ProductInfo)).all()


def test_lista_produtos_das_avaliacoes_sem_cadastro(client, session, loja):
    _review(session, loja, 1, item_id=5004, produto="Miniatura Dragão")
    _review(session, loja, 2, item_id=5004, produto="Miniatura Dragão")
    _review(session, loja, 3, item_id=5001, produto="Vaso")
    html = client.get("/personalizacao/produtos").text
    assert "2 sem informações" in html
    assert "#5004 · 2 avaliações" in html
    assert "#5001 · 1 avaliação" in html


def test_cadastrar_a_partir_da_avaliacao_preenche_o_formulario(client, session, loja):
    r = client.get("/personalizacao/produtos?item_id=5004&nome=Miniatura%20Drag%C3%A3o")
    assert 'value="5004"' in r.text and 'value="Miniatura Dragão"' in r.text


def test_cadastrar_produto_e_entra_na_analise(client, session, loja):
    _review(session, loja, item_id=5004)
    r = client.post("/personalizacao/produtos", data=_produto())
    assert r.status_code == 200 and "informações salvas" in r.text
    assert "sem informações" not in r.text

    [p] = _produtos(session)
    assert (p.item_id, p.material, p.observacoes) == (5004, "PLA", "Pode pintar com acrílica e primer")
    ctx = carregar_contexto(session, loja.id, _comentario(item_id=5004))
    assert ctx.produto.observacoes == "Pode pintar com acrílica e primer"


def test_editar_produto(client, session, loja):
    client.post("/personalizacao/produtos", data=_produto())
    p = _produtos(session)[0]
    r = client.get(f"/personalizacao/produtos?editar={p.id}")
    assert "Salvar alterações" in r.text and 'value="PLA"' in r.text

    client.post("/personalizacao/produtos", data=_produto(produto_id=str(p.id), material="PETG"))
    [p] = _produtos(session)
    assert p.material == "PETG"


@pytest.mark.parametrize("dados", [{"item_id": "abc"}, {"item_id": "0"}, {"nome": " "}, {"cuidados": "x" * 1001}])
def test_produto_invalido(client, session, loja, dados):
    r = client.post("/personalizacao/produtos", data=_produto(**dados))
    assert r.status_code == 422
    assert _produtos(session) == []


def test_item_id_repetido(client, session, loja):
    client.post("/personalizacao/produtos", data=_produto())
    r = client.post("/personalizacao/produtos", data=_produto(nome="Outro"))
    assert r.status_code == 422 and "já está cadastrado" in r.text
    assert len(_produtos(session)) == 1


def test_excluir_produto(client, session, loja):
    client.post("/personalizacao/produtos", data=_produto())
    p = _produtos(session)[0]
    client.post(f"/personalizacao/produtos/{p.id}/excluir")
    assert _produtos(session) == []


def test_nao_mexe_em_produto_de_outra_loja(client, session, loja):
    outra = criar_loja(session, "Outra", 2)
    alheio = ProductInfo(shop_id=outra.id, item_id=1, nome="Alheio")
    session.add(alheio)
    session.commit()
    assert client.post(f"/personalizacao/produtos/{alheio.id}/excluir").status_code == 404
    assert client.get(f"/personalizacao/produtos?editar={alheio.id}").status_code == 404
    assert "Alheio" not in client.get("/personalizacao/produtos").text
