from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.ai.analyzer import AnaliseIA, Analisador
from app.config import Settings
from app.db.models import AuditLog, DraftStatus, ReplyDraft, Review, ReviewStatus
from app.db.repo import criar_loja
from app.shopee.base import ShopeeError
from app.shopee.mock_client import MockShopeeClient
from app.worker import Decisao, Robo, decidir
from tests.fake_claude import ClaudePorEstrelas

LOJA_SHOPEE = 100001
TOTAL_MOCK = 11  # avaliações sem resposta no mock
POSITIVAS_MOCK = {9000001, 9000002, 9000003, 9000004, 9000009}  # 4 e 5 estrelas


class Relogio:
    def __init__(self, agora=datetime(2026, 11, 3, 12, tzinfo=UTC)):
        self.agora = agora

    def __call__(self):
        return self.agora


@pytest.fixture
def loja(session):
    loja = criar_loja(session, "Loja mock", LOJA_SHOPEE)
    session.commit()
    return loja


def _robo(shopee, claude=None, relogio=None, **settings_kw):
    settings = Settings(_env_file=None, anthropic_api_key="", **{"use_batch_api": False, **settings_kw})
    claude = claude or ClaudePorEstrelas()
    return Robo(shopee, Analisador(client=claude, settings=settings), settings, relogio or Relogio())


def _ligar(session, loja):
    loja.settings.auto_publicar = True
    session.commit()


def _contar(session, modelo, *filtros):
    return session.scalar(select(func.count()).select_from(modelo).where(*filtros))


# --- Regra de decisão ---


def _analise(sentimento="positivo", precisa_revisao=False):
    return AnaliseIA(
        sentimento=sentimento,
        categoria="elogio",
        gravidade=1,
        precisa_revisao=precisa_revisao,
        resposta="ok",
        motivo="ok",
    )


@pytest.mark.parametrize(
    ("auto", "sentimento", "revisao", "esperado"),
    [
        (False, "positivo", False, Decisao.FILA),
        (False, "negativo", False, Decisao.FILA),
        (True, "positivo", False, Decisao.PUBLICAR),
        (True, "positivo", True, Decisao.FILA),
        (True, "neutro", False, Decisao.FILA),
        (True, "negativo", False, Decisao.FILA),
    ],
)
def test_regra_de_decisao(auto, sentimento, revisao, esperado):
    assert decidir(auto, _analise(sentimento, revisao)) == esperado


# --- Chave "Respostas automáticas" ---


def test_chave_desligada_nunca_publica(session, loja, mock_client):
    assert loja.settings.auto_publicar is False  # padrão de loja nova
    rel = _robo(mock_client).executar_ciclo(session)

    assert mock_client.respostas_enviadas == []
    assert rel.publicadas == 0 and rel.na_fila == TOTAL_MOCK
    assert _contar(session, Review, Review.status == ReviewStatus.NA_FILA) == TOTAL_MOCK
    assert _contar(session, ReplyDraft, ReplyDraft.status == DraftStatus.PENDENTE) == TOTAL_MOCK


def test_chave_ligada_publica_so_positivas(session, loja, mock_client):
    _ligar(session, loja)
    rel = _robo(mock_client).executar_ciclo(session)

    assert {r.comment_id for r in mock_client.respostas_enviadas} == POSITIVAS_MOCK
    assert rel.publicadas == len(POSITIVAS_MOCK)
    assert rel.na_fila == TOTAL_MOCK - len(POSITIVAS_MOCK)

    publicadas = session.scalars(select(ReplyDraft).where(ReplyDraft.status == DraftStatus.PUBLICADO)).all()
    assert all(d.publicado_por == "robo" and d.publicado_em and d.texto_final for d in publicadas)
    assert _contar(session, AuditLog, AuditLog.acao == "resposta_publicada") == len(POSITIVAS_MOCK)
    # Nenhuma negativa ou neutra publicada.
    negativas = session.scalars(select(Review).where(Review.sentimento != "positivo")).all()
    assert negativas and all(r.status == ReviewStatus.NA_FILA for r in negativas)


def test_ligar_a_chave_nao_publica_o_que_ja_esta_na_fila(session, loja, mock_client):
    robo = _robo(mock_client)
    robo.executar_ciclo(session)  # tudo vai para a fila
    _ligar(session, loja)
    robo.executar_ciclo(session)

    assert mock_client.respostas_enviadas == []
    assert _contar(session, Review, Review.status == ReviewStatus.NA_FILA) == TOTAL_MOCK


def test_falha_ao_publicar_vai_para_a_fila(session, loja, mock_client):
    _ligar(session, loja)

    def falha(*_):
        raise ShopeeError("fora do ar")

    mock_client.reply_comment = falha
    rel = _robo(mock_client).executar_ciclo(session)

    assert rel.publicadas == 0 and rel.na_fila == TOTAL_MOCK
    assert _contar(session, AuditLog, AuditLog.acao == "falha_publicacao") == len(POSITIVAS_MOCK)


# --- Idempotência ---


def test_rodar_duas_vezes_nao_duplica_nem_reanalisa(session, loja, mock_client):
    claude = ClaudePorEstrelas()
    robo = _robo(mock_client, claude)
    primeiro = robo.executar_ciclo(session)
    segundo = robo.executar_ciclo(session)

    assert primeiro.novas == TOTAL_MOCK and segundo.novas == 0
    assert _contar(session, Review) == TOTAL_MOCK
    assert _contar(session, ReplyDraft) == TOTAL_MOCK
    assert len(claude.chamadas) == TOTAL_MOCK - 1  # a 5★ sem texto não chama a IA


def test_reiniciar_o_robo_nao_duplica(session, loja, mock_client):
    _ligar(session, loja)
    _robo(mock_client).executar_ciclo(session)
    # Um cliente novo "esqueceu" as respostas: tudo aparece sem resposta de novo.
    novo = MockShopeeClient(Settings(_env_file=None).mock_reviews_path)
    rel = _robo(novo).executar_ciclo(session)

    assert rel.novas == 0
    assert novo.respostas_enviadas == []
    assert _contar(session, Review) == TOTAL_MOCK


# --- Custos e erros ---


def test_registra_tokens_e_custo(session, loja, mock_client):
    _robo(mock_client).executar_ciclo(session)
    r = session.scalar(select(Review).where(Review.comment_id == 9000001))
    assert r.modelo_usado == "claude-haiku-5-5"
    assert r.tokens_entrada > 0 and r.tokens_saida > 0 and r.custo_estimado_usd > 0
    assert r.processado_em is not None

    pronta = session.scalar(select(Review).where(Review.comment_id == 9000002))
    assert pronta.custo_estimado_usd == 0


def test_falha_da_api_deixa_para_o_proximo_ciclo(session, loja, mock_client):
    rel = _robo(mock_client, ClaudePorEstrelas(falhar_api=True)).executar_ciclo(session)
    # A primeira avaliação já falha; o ciclo da loja para e nada se perde.
    assert rel.novas == TOTAL_MOCK and rel.na_fila == 0 and rel.erros == 0
    assert _contar(session, Review, Review.status == ReviewStatus.NOVA) == TOTAL_MOCK

    rel = _robo(mock_client).executar_ciclo(session)
    assert rel.na_fila == TOTAL_MOCK
    assert _contar(session, Review, Review.status == ReviewStatus.NOVA) == 0


def test_loja_inativa_e_ignorada(session, loja, mock_client):
    loja.ativo = False
    session.commit()
    rel = _robo(mock_client).executar_ciclo(session)
    assert rel.novas == 0 and _contar(session, Review) == 0


# --- Limite mensal ---


def test_limite_mensal(session, loja, mock_client):
    loja.settings.limite_avaliacoes_mes = 4
    session.commit()
    relogio = Relogio()
    robo = _robo(mock_client, relogio=relogio)

    rel = robo.executar_ciclo(session)
    assert rel.na_fila == 4 and rel.lojas_no_limite == [loja.id]
    assert _contar(session, Review, Review.status == ReviewStatus.NOVA) == TOTAL_MOCK - 4

    assert robo.executar_ciclo(session).na_fila == 0  # mesmo mês: nada

    relogio.agora = datetime(2026, 12, 1, 8, tzinfo=UTC)
    assert robo.executar_ciclo(session).na_fila == 4  # mês novo, mais 4


def test_limite_do_env_quando_loja_nao_define(session, loja, mock_client):
    rel = _robo(mock_client, limite_avaliacoes_mes=2).executar_ciclo(session)
    assert rel.na_fila == 2


# --- Modo lote (Batch API) ---


def test_modo_lote_envia_e_recolhe_depois(session, loja, mock_client):
    _ligar(session, loja)
    claude = ClaudePorEstrelas()
    robo = _robo(mock_client, claude, use_batch_api=True)

    rel = robo.executar_ciclo(session)
    # A 5★ sem texto é resolvida na hora (e publicada); o resto vai para o lote.
    assert rel.publicadas == 1 and rel.enviadas_lote == TOTAL_MOCK - 1
    assert _contar(session, Review, Review.status == ReviewStatus.EM_ANALISE) == TOTAL_MOCK - 1
    assert claude.chamadas == []  # nada no modo imediato

    rel = robo.executar_ciclo(session)  # lote ainda processando
    assert rel.publicadas == 0 and rel.enviadas_lote == 0

    claude.messages.batches.terminar_todos()
    rel = robo.executar_ciclo(session)
    assert rel.publicadas == len(POSITIVAS_MOCK) - 1
    assert rel.na_fila == TOTAL_MOCK - len(POSITIVAS_MOCK)
    assert _contar(session, Review, Review.status == ReviewStatus.EM_ANALISE) == 0
    r = session.scalar(select(Review).where(Review.comment_id == 9000001))
    assert r.lote_id == "msgbatch_1"


def test_lote_respeita_a_chave_no_momento_do_resultado(session, loja, mock_client):
    _ligar(session, loja)
    claude = ClaudePorEstrelas()
    robo = _robo(mock_client, claude, use_batch_api=True)
    robo.executar_ciclo(session)
    mock_client.respostas_enviadas.clear()

    loja.settings.auto_publicar = False  # desligou enquanto o lote processava
    session.commit()
    claude.messages.batches.terminar_todos()
    robo.executar_ciclo(session)

    assert mock_client.respostas_enviadas == []


def test_lote_conta_no_limite_mensal(session, loja, mock_client):
    loja.settings.limite_avaliacoes_mes = 5
    session.commit()
    rel = _robo(mock_client, use_batch_api=True).executar_ciclo(session)
    assert rel.enviadas_lote + rel.na_fila + rel.publicadas == 5
