from datetime import UTC, datetime, timedelta

from app.ai.contexto import Exemplo, carregar_contexto
from app.ai.exemplos import selecionar_exemplos
from app.db.models import ProductInfo, ReplyExample
from app.db.repo import criar_loja
from app.shopee.base import ShopeeComment


def _comentario(texto="Chegou quebrado", estrelas=1, item_id=5001):
    return ShopeeComment(
        comment_id=1,
        item_id=item_id,
        produto_nome="Vaso",
        estrelas=estrelas,
        texto=texto,
        comprador="x",
        criado_em=datetime(2026, 11, 1),
    )


def _exemplo(shop_id, categoria="defeito", sentimento="negativo", texto="t", ativo=True, dias=0):
    return ReplyExample(
        shop_id=shop_id,
        categoria=categoria,
        sentimento=sentimento,
        texto_avaliacao=texto,
        resposta_ideal=f"resposta para {texto}",
        ativo=ativo,
        criado_em=datetime(2026, 11, 1, tzinfo=UTC) - timedelta(days=dias),
    )


# --- Seleção de exemplos ---


def test_prefere_mesmo_sentimento_e_texto_parecido():
    candidatos = [
        Exemplo("elogio", "positivo", "A peça chegou quebrada?", "r1"),  # texto parecido, sentimento errado
        Exemplo("atraso", "negativo", "Demorou muito para chegar", "r2"),
        Exemplo("quebrado_no_envio", "negativo", "A peça chegou quebrada na caixa", "r3"),
    ]
    escolhidos = selecionar_exemplos("Minha peça chegou toda quebrada", 1, candidatos, n=2)
    assert [e.resposta_ideal for e in escolhidos] == ["r3", "r2"]


def test_selecao_respeita_n_e_lista_vazia():
    candidatos = [Exemplo("elogio", "positivo", f"texto {i}", f"r{i}") for i in range(10)]
    assert len(selecionar_exemplos("texto", 5, candidatos, n=4)) == 4
    assert selecionar_exemplos("texto", 5, [], n=4) == []


def test_ignora_acentos_e_maiusculas():
    candidatos = [
        Exemplo("elogio", "positivo", "Luminária linda", "lum"),
        Exemplo("elogio", "positivo", "Vaso bonito", "vaso"),
    ]
    assert selecionar_exemplos("LUMINARIA perfeita", 5, candidatos, n=1)[0].resposta_ideal == "lum"


# --- Carregar do banco ---


def test_carrega_regras_produto_e_exemplos_da_loja(session):
    loja = criar_loja(session, "Loja", 1)
    outra = criar_loja(session, "Outra", 2)
    loja.settings.tom = "formal"
    loja.settings.palavras_proibidas = ["barato"]
    session.add_all(
        [
            ProductInfo(shop_id=loja.id, item_id=5001, nome="Vaso", material="PLA"),
            ProductInfo(shop_id=outra.id, item_id=5001, nome="Vaso da outra"),
            _exemplo(loja.id, texto="chegou quebrado"),
            _exemplo(loja.id, texto="inativo", ativo=False),
            _exemplo(outra.id, texto="da outra loja"),
        ]
    )
    session.commit()

    ctx = carregar_contexto(session, loja.id, _comentario())
    assert ctx.regras.tom == "formal"
    assert ctx.regras.palavras_proibidas == ["barato"]
    assert ctx.produto.nome == "Vaso" and ctx.produto.material == "PLA"
    assert [e.texto_avaliacao for e in ctx.exemplos] == ["chegou quebrado"]


def test_produto_nao_cadastrado(session):
    loja = criar_loja(session, "Loja", 1)
    session.commit()
    assert carregar_contexto(session, loja.id, _comentario(item_id=999)).produto is None


def test_limite_de_exemplos_por_categoria_mantem_os_mais_recentes(session):
    loja = criar_loja(session, "Loja", 1)
    loja.settings.max_exemplos_por_categoria = 2
    session.add_all([_exemplo(loja.id, texto=f"ex{d}", dias=d) for d in range(5)])
    session.commit()

    ctx = carregar_contexto(session, loja.id, _comentario(texto="nada a ver"), n_exemplos=10)
    assert sorted(e.texto_avaliacao for e in ctx.exemplos) == ["ex0", "ex1"]
