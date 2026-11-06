"""Botão "Aprovar as N" da fila: só as 5★ sem texto e sem mídia (resposta pronta)."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db.models import AuditLog, DraftStatus, ReplyDraft, Review, ReviewStatus
from app.db.repo import criar_loja
from app.shopee.base import ShopeeError
from app.web.main import create_app
from app.web.routes import get_config, get_db, get_shopee

PRONTA = "Obrigado pelas 5 estrelas!"


class ShopeeFalsa:
    def __init__(self, falhar_em=()):
        self.enviadas: list[tuple[int, str]] = []
        self.falhar_em = set(falhar_em)

    def reply_comment(self, shop_id, comment_id, texto):
        if comment_id in self.falhar_em:
            raise ShopeeError("limite da API")
        self.enviadas.append((comment_id, texto))


@pytest.fixture
def loja(session):
    loja = criar_loja(session, "Loja", 100001)
    session.commit()
    return loja


def _na_fila(session, loja, comment_id, estrelas=5, texto="", midias=None, precisa_revisao=False):
    r = Review(shop_id=loja.id, comment_id=comment_id, item_id=1, produto_nome="Vaso", estrelas=estrelas,
               texto=texto, midias=midias or [], status=ReviewStatus.NA_FILA, precisa_revisao=precisa_revisao,
               avaliado_em=datetime(2026, 11, 1, tzinfo=UTC))
    session.add(r)
    session.flush()
    session.add(ReplyDraft(shop_id=loja.id, review_id=r.id, texto_ia=PRONTA if estrelas == 5 else "Sentimos muito."))
    session.commit()
    return r


@pytest.fixture
def fila(session, loja):
    return {
        "simples1": _na_fila(session, loja, 1),
        "simples2": _na_fila(session, loja, 2),
        "com_texto": _na_fila(session, loja, 3, texto="Amei!"),
        "com_foto": _na_fila(session, loja, 4, midias=[{"tipo": "foto", "url": "https://x/y.jpg"}]),
        "revisao": _na_fila(session, loja, 5, precisa_revisao=True),
        "ruim": _na_fila(session, loja, 6, estrelas=1),
    }


def _app(session, shopee, extensao):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_shopee] = lambda: shopee
    app.dependency_overrides[get_config] = lambda: Settings(_env_file=None, use_extensao=extensao)
    return TestClient(app)


def _status(session, review):
    session.expire_all()
    d = session.scalar(select(ReplyDraft).where(ReplyDraft.review_id == review.id))
    return session.get(Review, review.id).status, d.status


def test_botao_aparece_com_a_contagem(session, fila):
    html = _app(session, ShopeeFalsa(), False).get("/fila").text
    assert "Aprovar as 2" in html and "<strong>2</strong> avaliações 5★ sem texto" in html


def test_sem_simples_nao_mostra_botao(session, loja):
    _na_fila(session, loja, 9, estrelas=2)
    assert "aprovar-simples" not in _app(session, ShopeeFalsa(), False).get("/fila").text


def test_modo_api_publica_so_as_simples(session, fila):
    shopee = ShopeeFalsa()
    r = _app(session, shopee, False).post("/fila/aprovar-simples", headers={"HX-Request": "true"})

    assert r.status_code == 200 and r.headers["HX-Refresh"] == "true"
    assert sorted(shopee.enviadas) == [(1, PRONTA), (2, PRONTA)]
    assert _status(session, fila["simples1"]) == (ReviewStatus.PUBLICADA, DraftStatus.PUBLICADO)
    for nome in ("com_texto", "com_foto", "revisao", "ruim"):
        assert _status(session, fila[nome]) == (ReviewStatus.NA_FILA, DraftStatus.PENDENTE), nome
    log = session.scalar(select(AuditLog).where(AuditLog.acao == "aprovacao_em_massa"))
    assert log.detalhes == {"tipo": "5_estrelas_sem_texto", "aprovadas": 2, "falhas": 0, "via": "api"}


def test_modo_api_falha_fica_na_fila(session, fila):
    shopee = ShopeeFalsa(falhar_em={2})
    _app(session, shopee, False).post("/fila/aprovar-simples")
    assert shopee.enviadas == [(1, PRONTA)]
    assert _status(session, fila["simples2"]) == (ReviewStatus.NA_FILA, DraftStatus.PENDENTE)
    log = session.scalar(select(AuditLog).where(AuditLog.acao == "aprovacao_em_massa"))
    assert (log.detalhes["aprovadas"], log.detalhes["falhas"]) == (1, 1)


def test_modo_extensao_vai_para_envio_sem_chamar_api(session, fila):
    shopee = ShopeeFalsa()
    client = _app(session, shopee, True)
    r = client.post("/fila/aprovar-simples", follow_redirects=False)

    assert r.status_code == 303 and r.headers["location"] == "/fila"  # sem HTMX: volta para a fila
    assert shopee.enviadas == []
    assert _status(session, fila["simples1"]) == (ReviewStatus.ENVIANDO, DraftStatus.AGUARDANDO_ENVIO)
    assert _status(session, fila["ruim"]) == (ReviewStatus.NA_FILA, DraftStatus.PENDENTE)
    assert "Aprovar as" not in client.get("/fila").text  # não sobrou nenhuma simples
