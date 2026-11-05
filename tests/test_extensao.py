"""Modo extensão: API usada pela extensão do Chrome, fila de envio e regras de decisão."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai.analyzer import Analisador
from app.config import Settings
from app.db.models import AuditLog, DraftStatus, ProductInfo, ReplyDraft, Review, ReviewStatus
from app.db.repo import consulta_lojas_operaveis, criar_loja
from app.envio import MAX_TENTATIVAS, comment_id_da_extensao
from app.web.extensao import get_processador
from app.web.main import create_app
from app.web.routes import get_config, get_db
from app.worker import Robo
from tests.fake_claude import ClaudePorEstrelas

EXTENSAO = Settings(_env_file=None, use_extensao=True, use_batch_api=False, anthropic_api_key="")
CHAVE = "ext_chave-de-teste"


@pytest.fixture
def loja(session):
    loja = criar_loja(session, "Minha loja")
    loja.extensao_token = CHAVE
    session.commit()
    return loja


@pytest.fixture
def processados():
    return []


@pytest.fixture
def client(session, loja, processados):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_config] = lambda: EXTENSAO
    app.dependency_overrides[get_processador] = lambda: processados.append
    return TestClient(app, headers={"X-Chave-Extensao": CHAVE})


def _avaliacao(pedido="2611AAA", produto="Vaso Geométrico", estrelas=5, texto="Amei!", **kw):
    return {"pedido": pedido, "produto": produto, "estrelas": estrelas, "texto": texto,
            "data": "03/11/2026 10:55", **kw}


def _robo(session):
    return Robo(None, Analisador(client=ClaudePorEstrelas(), settings=EXTENSAO), EXTENSAO,
                relogio=lambda: datetime(2026, 11, 5, tzinfo=UTC))


# --- Autenticação ---


def test_sem_chave_ou_chave_errada(client):
    assert client.get("/api/extensao/status", headers={"X-Chave-Extensao": ""}).status_code == 401
    assert client.get("/api/extensao/status", headers={"X-Chave-Extensao": "outra"}).status_code == 401


def test_status(client):
    r = client.get("/api/extensao/status").json()
    assert r == {"loja": "Minha loja", "auto_publicar": False, "max_envios_por_passada": 5}


# --- Receber avaliações ---


def test_recebe_e_salva_sem_duplicar(client, session, loja, processados):
    session.add(ProductInfo(shop_id=loja.id, item_id=5001, nome="Vaso Geométrico"))
    session.commit()
    lote = {"avaliacoes": [_avaliacao(), _avaliacao(pedido="2611BBB", produto="Outro", estrelas=2, texto="Quebrou",
                                                      midias=[{"tipo": "foto", "url": "https://cf.shopee/x.jpg"}])]}
    assert client.post("/api/extensao/avaliacoes", json=lote).json() == {"recebidas": 2, "novas": 2}
    assert client.post("/api/extensao/avaliacoes", json=lote).json() == {"recebidas": 2, "novas": 0}

    reviews = session.scalars(select(Review).order_by(Review.id)).all()
    assert len(reviews) == 2
    vaso = reviews[0]
    assert vaso.comment_id == comment_id_da_extensao("2611AAA", "Vaso Geométrico")
    assert (vaso.pedido, vaso.item_id, vaso.status) == ("2611AAA", 5001, ReviewStatus.NOVA)
    assert vaso.avaliado_em.strftime("%d/%m/%Y %H:%M") == "03/11/2026 10:55"
    assert reviews[1].midias == [{"tipo": "foto", "url": "https://cf.shopee/x.jpg"}]
    assert processados == [loja.id, loja.id]  # análise disparada (havia pendentes)


@pytest.mark.parametrize("ruim", [{"estrelas": 0}, {"estrelas": 6}, {"pedido": ""},
                                  {"midias": [{"tipo": "audio", "url": "x"}]}])
def test_avaliacao_invalida(client, ruim):
    assert client.post("/api/extensao/avaliacoes", json={"avaliacoes": [_avaliacao(**ruim)]}).status_code == 422


# --- Histórico (já respondidas): só relatório ---


def test_historico_salva_sem_analisar_nem_enfileirar(client, session, loja, processados):
    lote = {"avaliacoes": [
        _avaliacao(pedido="H1", estrelas=1, texto="Cor diferente da foto", resposta_vendedor="Poxa, chama no chat!"),
        _avaliacao(pedido="H2", estrelas=3, texto="Ok"),
    ]}
    assert client.post("/api/extensao/historico", json=lote).json() == {"recebidas": 2, "novas": 2}
    assert client.post("/api/extensao/historico", json=lote).json() == {"recebidas": 2, "novas": 0}
    assert processados == []  # sem IA

    reviews = session.scalars(select(Review).order_by(Review.id)).all()
    assert {r.status for r in reviews} == {ReviewStatus.JA_RESPONDIDA}
    assert reviews[0].resposta_vendedor == "Poxa, chama no chat!" and reviews[1].resposta_vendedor is None

    # O robô não analisa, a fila e os envios continuam vazios, e o relatório mostra.
    rel = _robo(session).processar_loja(session, loja, buscar=False)
    assert rel.na_fila == 0 and rel.para_extensao == 0
    assert session.scalar(select(ReplyDraft)) is None
    assert "Cor diferente" not in client.get("/fila").text
    assert client.get("/api/extensao/envios").json() == {"envios": []}
    html = client.get("/relatorio?dias=0").text
    assert "Cor diferente da foto" in html and "Respondida antes" in html and "Poxa, chama no chat!" in html


def test_historico_nao_mexe_em_avaliacao_ja_conhecida(client, session, loja):
    client.post("/api/extensao/avaliacoes", json={"avaliacoes": [_avaliacao(pedido="P1", estrelas=2, texto="Quebrou")]})
    r = client.post("/api/extensao/historico", json={"avaliacoes": [
        _avaliacao(pedido="P1", estrelas=2, texto="Quebrou", resposta_vendedor="Resposta feita na Shopee"),
    ]})
    assert r.json()["novas"] == 0
    review = session.scalar(select(Review))
    assert review.status == ReviewStatus.NOVA and review.resposta_vendedor == "Resposta feita na Shopee"


# --- Decisão no modo extensão ---


def _receber_e_analisar(client, session, loja, *avaliacoes):
    client.post("/api/extensao/avaliacoes", json={"avaliacoes": list(avaliacoes)})
    return _robo(session).processar_loja(session, loja, buscar=False)


def test_chave_desligada_nada_vai_para_envio(client, session, loja):
    rel = _receber_e_analisar(client, session, loja, _avaliacao(), _avaliacao(pedido="X2", estrelas=1, texto="Ruim"))
    assert rel.para_extensao == 0 and rel.na_fila == 2
    assert client.get("/api/extensao/envios").json() == {"envios": []}


def test_chave_ligada_so_elogios_vao_para_envio(client, session, loja):
    loja.settings.auto_publicar = True
    session.commit()
    rel = _receber_e_analisar(client, session, loja,
                              _avaliacao(texto="Ficou lindo"),
                              _avaliacao(pedido="X2", estrelas=1, texto="Quebrou tudo"),
                              _avaliacao(pedido="X3", estrelas=3, texto="Ok"))
    assert rel.para_extensao == 1 and rel.na_fila == 2
    [envio] = client.get("/api/extensao/envios").json()["envios"]
    assert envio["pedido"] == "2611AAA" and envio["produto"] == "Vaso Geométrico"
    assert envio["texto"] == "Resposta para avaliação de 5 estrelas."
    review = session.scalar(select(Review).where(Review.pedido == "2611AAA"))
    assert review.status == ReviewStatus.ENVIANDO


def test_aprovar_no_painel_coloca_na_fila_de_envio(client, session, loja):
    _receber_e_analisar(client, session, loja,
                        _avaliacao(pedido="X2", produto="Miniatura Dragão", estrelas=1, texto="Quebrou"))
    review = session.scalar(select(Review))
    assert "Miniatura Dragão" in client.get("/fila").text
    r = client.post(f"/fila/{review.id}/aprovar", data={"texto": "Poxa, chama no chat!"})

    assert r.status_code == 200 and "aguardando a extensão" in r.text
    [envio] = client.get("/api/extensao/envios").json()["envios"]
    assert envio["texto"] == "Poxa, chama no chat!"
    draft = session.scalar(select(ReplyDraft))
    assert (draft.publicado_por, draft.publicado_por_usuario) == ("humano", "painel")
    assert "Miniatura" not in client.get("/fila").text  # saiu da fila


# --- Resultado do envio ---


def _um_envio(client, session, loja):
    loja.settings.auto_publicar = True
    session.commit()
    _receber_e_analisar(client, session, loja, _avaliacao())
    return client.get("/api/extensao/envios").json()["envios"][0]


def test_extensao_confirma_publicacao(client, session, loja):
    envio = _um_envio(client, session, loja)
    assert client.post(f"/api/extensao/envios/{envio['id']}", json={"ok": True}).json() == {"status": "publicado"}

    session.expire_all()
    draft = session.get(ReplyDraft, envio["id"])
    assert draft.status == DraftStatus.PUBLICADO and draft.publicado_em
    assert session.get(Review, draft.review_id).status == ReviewStatus.PUBLICADA
    log = session.scalar(select(AuditLog).where(AuditLog.acao == "resposta_publicada"))
    assert log.detalhes["via"] == "extensao" and log.autor == "robo"
    assert client.get("/api/extensao/envios").json() == {"envios": []}
    # Repetir a confirmação não faz nada.
    assert client.post(f"/api/extensao/envios/{envio['id']}", json={"ok": True}).json()["status"] == "publicado"


def test_falhas_voltam_para_a_fila_depois_de_3_tentativas(client, session, loja):
    envio = _um_envio(client, session, loja)
    url = f"/api/extensao/envios/{envio['id']}"
    for _ in range(MAX_TENTATIVAS - 1):
        assert client.post(url, json={"ok": False, "erro": "caixa não abriu"}).json()["status"] == "vai_tentar_de_novo"
    assert client.post(url, json={"ok": False, "erro": "caixa não abriu"}).json()["status"] == "de_volta_na_fila"

    session.expire_all()
    draft = session.get(ReplyDraft, envio["id"])
    assert draft.status == DraftStatus.PENDENTE and draft.erro_envio == "caixa não abriu"
    assert session.get(Review, draft.review_id).status == ReviewStatus.NA_FILA


def test_nao_mexe_em_envio_de_outra_loja(client, session, loja):
    outra = criar_loja(session, "Outra")
    outra.extensao_token = "ext_outra"
    session.commit()
    envio = _um_envio(client, session, loja)
    r = client.post(f"/api/extensao/envios/{envio['id']}", json={"ok": True}, headers={"X-Chave-Extensao": "ext_outra"})
    assert r.status_code == 404
    assert client.get("/api/extensao/envios", headers={"X-Chave-Extensao": "ext_outra"}).json() == {"envios": []}


# --- Painel: chave e lojas ---


def test_gerar_chave_mostra_uma_vez_e_troca(client, session, loja):
    html = client.post("/shopee/extensao/chave").text
    session.refresh(loja)
    assert loja.extensao_token.startswith("ext_") and loja.extensao_token != CHAVE
    assert loja.extensao_token in html and "Copie agora" in html
    assert loja.extensao_token not in client.get("/shopee").text  # não aparece de novo


def test_modo_extensao_usa_lojas_com_chave(session, loja):
    criar_loja(session, "Sem chave", 999)
    session.commit()
    assert [l.nome for l in session.scalars(consulta_lojas_operaveis(True, usar_extensao=True))] == ["Minha loja"]


def test_analise_uma_por_loja_e_repete_se_chegar_mais():
    import threading

    from app.web.extensao import um_por_loja

    entrou, solta = threading.Event(), threading.Event()
    chamadas = []

    @um_por_loja
    def processar(loja_id):
        chamadas.append(loja_id)
        if len(chamadas) == 1:
            entrou.set()
            solta.wait(5)

    t = threading.Thread(target=processar, args=(7,))
    t.start()
    entrou.wait(5)
    processar(7)  # chega durante a primeira: não roda junto...
    processar(7)  # ...e várias chegadas viram uma única repetição
    processar(8)  # outra loja roda normalmente
    assert chamadas == [7, 8]
    solta.set()
    t.join(5)
    assert chamadas == [7, 8, 7]
    processar(7)  # terminou: roda de novo normalmente
    assert chamadas == [7, 8, 7, 7]


def test_comment_id_estavel_e_diferente_por_produto():
    a = comment_id_da_extensao("P1", "Vaso")
    assert a == comment_id_da_extensao(" P1 ", "Vaso ")
    assert a != comment_id_da_extensao("P1", "Suporte")
    assert 0 < a < 2**63
