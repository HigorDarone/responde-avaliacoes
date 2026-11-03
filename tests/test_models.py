import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    AuditLog,
    DraftStatus,
    ProductInfo,
    ReplyDraft,
    ReplyExample,
    Review,
    ReviewStatus,
)
from app.db.repo import buscar_loja_por_shopee_id, criar_loja


def _review(shop_id: int, comment_id: int) -> Review:
    return Review(shop_id=shop_id, comment_id=comment_id, item_id=1, estrelas=5)


def test_loja_nova_nasce_com_respostas_automaticas_desligadas(session):
    loja = criar_loja(session, "Minha loja", 123)
    session.commit()
    assert loja.settings is not None
    assert loja.settings.auto_publicar is False
    assert loja.settings.tom == "descontraido"
    assert loja.settings.oferta_por_categoria == {}
    assert buscar_loja_por_shopee_id(session, 123) is loja


def test_mesma_avaliacao_nao_entra_duas_vezes_na_mesma_loja(session):
    loja = criar_loja(session, "Loja", 1)
    session.add(_review(loja.id, 42))
    session.commit()

    session.add(_review(loja.id, 42))
    with pytest.raises(IntegrityError):
        session.commit()


def test_mesmo_comment_id_em_lojas_diferentes_e_permitido(session):
    a = criar_loja(session, "A", 1)
    b = criar_loja(session, "B", 2)
    session.add_all([_review(a.id, 42), _review(b.id, 42)])
    session.commit()


def test_review_exige_loja_existente(session):
    session.add(_review(999, 1))
    with pytest.raises(IntegrityError):
        session.commit()


def test_fluxo_completo_dos_modelos(session):
    loja = criar_loja(session, "Loja", 1)
    review = _review(loja.id, 7)
    session.add(review)
    session.flush()

    draft = ReplyDraft(shop_id=loja.id, review_id=review.id, texto_ia="Obrigado!")
    session.add_all(
        [
            draft,
            ReplyExample(
                shop_id=loja.id,
                categoria="elogio",
                sentimento="positivo",
                texto_avaliacao="Amei",
                resposta_ideal="Que bom!",
            ),
            ProductInfo(shop_id=loja.id, item_id=5001, nome="Vaso", material="PLA"),
            AuditLog(shop_id=loja.id, acao="auto_publicar_ligado", autor="dono"),
        ]
    )
    session.commit()

    assert review.status == ReviewStatus.NOVA
    assert review.drafts == [draft]
    assert draft.status == DraftStatus.PENDENTE
    assert draft.editado is False
    assert draft.publicado_por is None


def test_produto_unico_por_loja(session):
    loja = criar_loja(session, "Loja", 1)
    session.add_all(
        [
            ProductInfo(shop_id=loja.id, item_id=5001, nome="Vaso"),
            ProductInfo(shop_id=loja.id, item_id=5001, nome="Vaso 2"),
        ]
    )
    with pytest.raises(IntegrityError):
        session.commit()
