import pytest

from app.config import Settings
from app.shopee import get_shopee_client
from app.shopee.base import ShopeeError
from app.shopee.mock_client import MockShopeeClient

LOJA = 100001


def test_carrega_avaliacoes_variadas(mock_client):
    comentarios = mock_client.get_unreplied_comments(LOJA)
    assert 10 <= len(comentarios) <= 12

    estrelas = {c.estrelas for c in comentarios}
    assert {1, 2, 3, 4, 5} <= estrelas
    tipos_midia = {m.tipo for c in comentarios for m in c.midias}
    assert tipos_midia == {"foto", "video"}
    # Caso da resposta pronta: 5 estrelas sem texto e sem mídia.
    assert any(c.estrelas == 5 and not c.texto and not c.tem_midia for c in comentarios)


def test_ids_unicos(mock_client):
    ids = [c.comment_id for c in mock_client.get_unreplied_comments(LOJA)]
    assert len(ids) == len(set(ids))


def test_nao_devolve_avaliacoes_ja_respondidas(mock_client):
    ids = {c.comment_id for c in mock_client.get_unreplied_comments(LOJA)}
    assert 9000012 not in ids  # já tem resposta no JSON


def test_loja_desconhecida_nao_tem_avaliacoes(mock_client):
    assert mock_client.get_unreplied_comments(123) == []


def test_reply_registra_e_remove_da_lista(mock_client):
    mock_client.reply_comment(LOJA, 9000001, "Obrigado!")

    assert len(mock_client.respostas_enviadas) == 1
    enviada = mock_client.respostas_enviadas[0]
    assert (enviada.comment_id, enviada.texto) == (9000001, "Obrigado!")
    ids = {c.comment_id for c in mock_client.get_unreplied_comments(LOJA)}
    assert 9000001 not in ids


def test_reply_nao_altera_o_arquivo(mock_client):
    mock_client.reply_comment(LOJA, 9000001, "Obrigado!")

    novo = MockShopeeClient(Settings(_env_file=None).mock_reviews_path)
    assert 9000001 in {c.comment_id for c in novo.get_unreplied_comments(LOJA)}


@pytest.mark.parametrize(
    ("comment_id", "texto"),
    [(1, "Oi"), (9000001, "   ")],
    ids=["id-inexistente", "texto-vazio"],
)
def test_reply_invalido_gera_erro(mock_client, comment_id, texto):
    with pytest.raises(ShopeeError):
        mock_client.reply_comment(LOJA, comment_id, texto)
    assert mock_client.respostas_enviadas == []


def test_reply_duas_vezes_gera_erro(mock_client):
    mock_client.reply_comment(LOJA, 9000001, "Obrigado!")
    with pytest.raises(ShopeeError):
        mock_client.reply_comment(LOJA, 9000001, "De novo")


def test_fabrica_usa_mock_quando_configurado():
    s = Settings(_env_file=None, use_mock_shopee=True)
    assert isinstance(get_shopee_client(s), MockShopeeClient)


def test_fabrica_sem_mock_avisa_que_cliente_real_nao_existe():
    s = Settings(_env_file=None, use_mock_shopee=False)
    with pytest.raises(NotImplementedError):
        get_shopee_client(s)
