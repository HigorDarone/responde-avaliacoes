from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import AuditLog, DraftStatus, ReplyDraft, Review, ReviewStatus
from app.db.repo import criar_loja
from app.web.main import create_app
from app.web.routes import get_db, get_shopee

LOJA_SHOPEE = 100001  # a loja do mock
COMENTARIO_MOCK = 9000005  # existe no mock e está sem resposta
SUGESTAO = "Poxa, sentimos muito! Chama a gente no chat."


@pytest.fixture
def loja(session):
    loja = criar_loja(session, "Loja mock", LOJA_SHOPEE)
    session.commit()
    return loja


@pytest.fixture
def client(session, mock_client):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_shopee] = lambda: mock_client
    return TestClient(app)


@pytest.fixture
def review(session, loja):
    return _na_fila(session, loja, COMENTARIO_MOCK)


def _na_fila(session, loja, comment_id, **kw):
    r = Review(
        shop_id=loja.id,
        comment_id=comment_id,
        item_id=5004,
        produto_nome="Miniatura Dragão",
        estrelas=1,
        texto="Chegou com a cauda quebrada.",
        status=ReviewStatus.NA_FILA,
        sentimento="negativo",
        categoria="quebrado_no_envio",
        gravidade=3,
        avaliado_em=datetime(2026, 10, 30, tzinfo=UTC),
        **kw,
    )
    session.add(r)
    session.flush()
    session.add(ReplyDraft(shop_id=loja.id, review_id=r.id, texto_ia=SUGESTAO))
    session.commit()
    return r


def _draft(session, review):
    session.expire_all()
    return session.scalar(select(ReplyDraft).where(ReplyDraft.review_id == review.id))


def _acoes(session):
    return [a.acao for a in session.scalars(select(AuditLog).order_by(AuditLog.id))]


# --- Aprovar e publicar ---


def test_aprovar_sem_editar_publica_a_sugestao(client, session, review, mock_client):
    r = client.post(f"/fila/{review.id}/aprovar", data={"texto": SUGESTAO})

    assert r.status_code == 200
    assert "Publicada" in r.text
    assert [(e.comment_id, e.texto) for e in mock_client.respostas_enviadas] == [(COMENTARIO_MOCK, SUGESTAO)]
    d = _draft(session, review)
    assert d.status == DraftStatus.PUBLICADO
    assert d.texto_final == SUGESTAO and d.editado is False
    assert d.publicado_por == "humano" and d.publicado_por_usuario == "painel" and d.publicado_em
    assert session.get(Review, review.id).status == ReviewStatus.PUBLICADA
    assert _acoes(session) == ["resposta_publicada"]


def test_aprovar_com_texto_editado(client, session, review, mock_client):
    client.post(f"/fila/{review.id}/aprovar", data={"texto": "  Texto meu, revisado.  "})

    assert mock_client.respostas_enviadas[0].texto == "Texto meu, revisado."
    d = _draft(session, review)
    assert d.texto_ia == SUGESTAO  # a versão da IA fica guardada
    assert d.texto_final == "Texto meu, revisado." and d.editado is True


def test_aprovar_texto_vazio_nao_publica(client, session, review, mock_client):
    r = client.post(f"/fila/{review.id}/aprovar", data={"texto": "   "})

    assert r.status_code == 422
    assert "não pode ficar vazia" in r.text
    assert mock_client.respostas_enviadas == []
    assert _draft(session, review).status == DraftStatus.PENDENTE


def test_falha_da_shopee_mantem_na_fila(client, session, loja, mock_client):
    review = _na_fila(session, loja, 123)  # não existe no mock: a "Shopee" recusa
    r = client.post(f"/fila/{review.id}/aprovar", data={"texto": "Oi"})

    assert r.status_code == 502
    assert "Nada foi publicado" in r.text
    assert _draft(session, review).status == DraftStatus.PENDENTE
    assert session.get(Review, review.id).status == ReviewStatus.NA_FILA
    assert _acoes(session) == ["falha_publicacao"]


def test_nao_publica_duas_vezes(client, session, review, mock_client):
    client.post(f"/fila/{review.id}/aprovar", data={"texto": SUGESTAO})
    r = client.post(f"/fila/{review.id}/aprovar", data={"texto": SUGESTAO})

    assert r.status_code == 409
    assert len(mock_client.respostas_enviadas) == 1


def test_resposta_atualiza_a_contagem(client, session, loja, review):
    _na_fila(session, loja, 9000006)
    r = client.post(f"/fila/{review.id}/aprovar", data={"texto": SUGESTAO})
    assert 'id="resumo-fila" hx-swap-oob="true"' in r.text
    assert "1 resposta" in r.text


# --- Editar e salvar ---


def test_editar_abre_o_texto_para_edicao(client, review):
    r = client.get(f"/fila/{review.id}/editar")
    assert "Editando a resposta" in r.text
    assert "readonly" not in r.text
    assert "Salvar" in r.text and "Cancelar" in r.text


def test_card_normal_e_somente_leitura(client, review):
    r = client.get(f"/fila/{review.id}/card")
    assert "readonly" in r.text and "Editar" in r.text


def test_salvar_guarda_sem_publicar(client, session, review, mock_client):
    r = client.post(f"/fila/{review.id}/salvar", data={"texto": "Versão nova"})

    assert r.status_code == 200
    assert "Versão nova" in r.text and "Editada" in r.text
    d = _draft(session, review)
    assert d.texto_final == "Versão nova" and d.editado is True
    assert d.status == DraftStatus.PENDENTE
    assert mock_client.respostas_enviadas == []
    assert "Versão nova" in client.get("/fila").text  # continua na fila com o texto salvo


def test_salvar_vazio_volta_com_erro(client, session, review):
    r = client.post(f"/fila/{review.id}/salvar", data={"texto": ""})
    assert r.status_code == 422 and "não pode ficar vazia" in r.text
    assert _draft(session, review).texto_final is None


# --- Descartar e desfazer ---


def test_descartar_e_desfazer(client, session, review, mock_client):
    r = client.post(f"/fila/{review.id}/descartar")
    assert "Descartada" in r.text and "Desfazer" in r.text
    assert _draft(session, review).status == DraftStatus.DESCARTADO
    assert session.get(Review, review.id).status == ReviewStatus.DESCARTADA
    assert "Miniatura Dragão" not in client.get("/fila").text

    r = client.post(f"/fila/{review.id}/desfazer")
    assert "Aprovar e publicar" in r.text
    assert _draft(session, review).status == DraftStatus.PENDENTE
    assert session.get(Review, review.id).status == ReviewStatus.NA_FILA

    assert mock_client.respostas_enviadas == []
    assert _acoes(session) == ["resposta_descartada", "descarte_desfeito"]


def test_nao_desfaz_o_que_foi_publicado(client, session, review):
    client.post(f"/fila/{review.id}/aprovar", data={"texto": SUGESTAO})
    assert client.post(f"/fila/{review.id}/desfazer").status_code == 409
    assert client.post(f"/fila/{review.id}/descartar").status_code == 409


# --- Segurança ---


def test_nao_mexe_em_avaliacao_de_outra_loja(client, session, loja, mock_client):
    outra = criar_loja(session, "Outra", 2)
    alheia = _na_fila(session, outra, 1)
    for metodo, url in [
        ("post", f"/fila/{alheia.id}/aprovar"),
        ("post", f"/fila/{alheia.id}/descartar"),
        ("post", f"/fila/{alheia.id}/salvar"),
        ("get", f"/fila/{alheia.id}/editar"),
    ]:
        assert getattr(client, metodo)(url, **({"data": {"texto": "x"}} if metodo == "post" else {})).status_code == 404
    assert mock_client.respostas_enviadas == []


def test_avaliacao_inexistente(client, loja):
    assert client.post("/fila/999/aprovar", data={"texto": "x"}).status_code == 404
