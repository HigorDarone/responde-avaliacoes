from datetime import datetime

from app.ai.contexto import ContextoLoja, Exemplo, InfoProduto, RegrasLoja
from app.ai.prompts import SISTEMA_BASE, montar_system, render_mensagem, render_regras_loja
from app.shopee.base import Midia, ShopeeComment


def _comentario(texto="A base veio torta.", midias=()):
    return ShopeeComment(
        comment_id=1,
        item_id=5002,
        produto_nome="Suporte de Headset",
        estrelas=2,
        texto=texto,
        comprador="x",
        criado_em=datetime(2026, 11, 1),
        midias=list(midias),
    )


def test_system_tem_base_e_regras_com_cache():
    blocos = montar_system(ContextoLoja(shop_id=1))
    assert blocos[0]["text"] == SISTEMA_BASE
    assert "Regras desta loja" in blocos[1]["text"]
    assert all(b["cache_control"] == {"type": "ephemeral"} for b in blocos)


def test_regras_da_loja_aparecem_no_prompt():
    regras = RegrasLoja(
        tom="formal",
        assinatura="— Equipe Demo 3D",
        uso_emoji="nunca",
        tamanho_resposta="media",
        oferta_por_categoria={"defeito": "Oferecer troca pelo chat.", "atraso": "Pedir desculpas."},
        nunca_dizer=["prazo de entrega"],
        palavras_proibidas=["barato"],
    )
    texto = render_regras_loja(regras)
    for trecho in [
        "formal", "— Equipe Demo 3D", "nunca use emoji", "2 a 3 frases",
        "defeito: Oferecer troca pelo chat.", "prazo de entrega", "barato",
    ]:
        assert trecho in texto


def test_regras_sem_ofertas_usam_padrao_sem_promessas():
    texto = render_regras_loja(RegrasLoja())
    assert "chat da Shopee" in texto
    assert "sem assinatura" in texto


def test_regras_sao_deterministicas_para_o_cache():
    a = RegrasLoja(oferta_por_categoria={"defeito": "x", "atraso": "y"})
    b = RegrasLoja(oferta_por_categoria={"atraso": "y", "defeito": "x"})
    assert render_regras_loja(a) == render_regras_loja(b)


def test_mensagem_tem_avaliacao_produto_e_exemplos():
    contexto = ContextoLoja(
        shop_id=1,
        exemplos=[Exemplo("defeito", "negativo", "Veio torto", "Poxa, chama a gente no chat!")],
        produto=InfoProduto(nome="Suporte de Headset", material="PETG", cuidados="Não deixar no sol"),
    )
    texto = render_mensagem(_comentario(), contexto, n_imagens=0)

    assert "<avaliacao>" in texto and "A base veio torta." in texto
    assert "Estrelas: 2 de 5" in texto
    assert "Material: PETG" in texto
    assert "Cuidados de uso: Não deixar no sol" in texto
    assert "Tamanho:" not in texto  # campos vazios não entram
    assert "Resposta ideal: Poxa, chama a gente no chat!" in texto


def test_produto_sem_cadastro_avisa_a_ia():
    texto = render_mensagem(_comentario(), ContextoLoja(shop_id=1), n_imagens=0)
    assert "não cadastrou informações" in texto
    assert "<exemplos>" not in texto


def test_midia_sem_imagem_disponivel_e_avisada():
    c = _comentario(midias=[Midia("video", "https://example.com/v.mp4")])
    texto = render_mensagem(c, ContextoLoja(shop_id=1), n_imagens=0)
    assert "0 foto(s) e 1 vídeo(s)" in texto
    assert "nenhuma imagem disponível" in texto


def test_avaliacao_sem_texto():
    texto = render_mensagem(_comentario(texto=""), ContextoLoja(shop_id=1), n_imagens=0)
    assert "Texto: (sem texto)" in texto
