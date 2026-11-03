from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import AuditLog, DraftStatus, ReplyDraft, Review, ReviewStatus
from app.db.repo import criar_loja
from app.web.main import create_app
from app.web.routes import _data_hora, _sem_prefixo_revisao, _url_segura, get_db


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


def _na_fila(session, loja, comment_id, texto="Chegou quebrado", resposta="Poxa, sentimos muito!", **kw):
    review = Review(
        shop_id=loja.id,
        comment_id=comment_id,
        item_id=1,
        produto_nome=kw.pop("produto", "Vaso Low Poly"),
        estrelas=kw.pop("estrelas", 1),
        texto=texto,
        status=kw.pop("status", ReviewStatus.NA_FILA),
        sentimento="negativo",
        categoria=kw.pop("categoria", "quebrado_no_envio"),
        gravidade=kw.pop("gravidade", 3),
        avaliado_em=datetime(2026, 11, 2, tzinfo=UTC),
        **kw,
    )
    session.add(review)
    session.flush()
    session.add(ReplyDraft(shop_id=loja.id, review_id=review.id, texto_ia=resposta))
    session.commit()
    return review


# --- Fila ---


def test_inicio_redireciona_para_a_fila(client, loja):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/fila"


def test_fila_mostra_cards(client, session, loja):
    _na_fila(session, loja, 1, midias=[{"tipo": "foto", "url": "https://example.com/a.jpg"}])
    html = client.get("/fila").text

    assert "1 resposta" in html
    assert "Vaso Low Poly" in html
    assert "Chegou quebrado" in html
    assert "Poxa, sentimos muito!" in html  # resposta sugerida no textarea
    assert "Quebrou no envio" in html
    assert 'src="https://example.com/a.jpg"' in html
    assert "02 NOV" in html


def test_fila_so_mostra_pendentes_da_loja(client, session, loja):
    outra = criar_loja(session, "Outra", 2)
    _na_fila(session, loja, 1, texto="aparece")
    _na_fila(session, loja, 2, texto="ja publicada", status=ReviewStatus.PUBLICADA)
    _na_fila(session, outra, 3, texto="de outra loja")
    html = client.get("/fila").text

    assert "aparece" in html
    assert "ja publicada" not in html
    assert "de outra loja" not in html


def test_fila_ignora_rascunho_descartado(client, session, loja):
    review = _na_fila(session, loja, 1, texto="descartada")
    session.scalar(select(ReplyDraft).where(ReplyDraft.review_id == review.id)).status = DraftStatus.DESCARTADO
    session.commit()
    assert "descartada" not in client.get("/fila").text


def test_precisa_de_voce_vem_primeiro(client, session, loja):
    _na_fila(session, loja, 1, texto="grave mas ok", gravidade=4)
    _na_fila(session, loja, 2, texto="precisa revisar", gravidade=1, precisa_revisao=True,
             motivo="Precisa de você: pediu reembolso.")
    html = client.get("/fila").text

    assert html.index("precisa revisar") < html.index("grave mas ok")
    assert "Pediu reembolso." in html
    assert "1</strong> precisa" in html


def test_fila_vazia(client, loja):
    assert "Fila vazia" in client.get("/fila").text


def test_sem_loja(client):
    html = client.get("/fila").text
    assert "Ainda não há loja cadastrada" in html
    assert 'id="interruptor"' not in html


def test_texto_do_comprador_e_escapado(client, session, loja):
    _na_fila(session, loja, 1, texto="<script>alert(1)</script>")
    html = client.get("/fila").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_midia_com_link_perigoso_nao_vira_link(client, session, loja):
    _na_fila(session, loja, 1, midias=[{"tipo": "foto", "url": "javascript:alert(1)"}])
    html = client.get("/fila").text
    assert "javascript:" not in html


# --- Interruptor "Respostas automáticas" ---


def test_interruptor_aparece_desligado_por_padrao(client, loja):
    html = client.get("/fila").text
    assert 'id="interruptor"' in html
    assert "Desligadas" in html
    assert 'id="confirmar-auto"' in html  # ligar pede confirmação


def test_ligar_e_desligar_registra_no_historico(client, session, loja):
    r = client.post("/loja/auto-publicar/ligar")
    assert r.status_code == 200
    assert "Ligadas" in r.text
    assert 'id="confirmar-auto"' not in r.text  # desligar é direto, sem confirmação
    session.refresh(loja.settings)
    assert loja.settings.auto_publicar is True

    r = client.post("/loja/auto-publicar/desligar")
    assert "Desligadas" in r.text
    session.refresh(loja.settings)
    assert loja.settings.auto_publicar is False

    log = session.scalars(select(AuditLog).order_by(AuditLog.id)).all()
    assert [(l.acao, l.autor) for l in log] == [
        ("auto_publicar_ligado", "painel"),
        ("auto_publicar_desligado", "painel"),
    ]


def test_ligar_duas_vezes_registra_uma(client, session, loja):
    client.post("/loja/auto-publicar/ligar")
    client.post("/loja/auto-publicar/ligar")
    assert len(session.scalars(select(AuditLog)).all()) == 1


def test_ligar_nao_publica_o_que_ja_esta_na_fila(client, session, loja):
    _na_fila(session, loja, 1, estrelas=5, categoria="elogio")
    client.post("/loja/auto-publicar/ligar")
    assert session.scalar(select(Review)).status == ReviewStatus.NA_FILA


# --- Filtros ---


def test_filtros():
    assert _data_hora(datetime(2026, 11, 3, 15, 59)) == "03/11/2026 12:59"  # UTC -> Brasília
    assert _sem_prefixo_revisao("Precisa de você: falta info") == "Falta info"
    assert _sem_prefixo_revisao("Precisa de you: x") == "X"
    assert _url_segura("https://a.com/x.jpg") == "https://a.com/x.jpg"
    assert _url_segura("javascript:alert(1)") == "#"
