"""Fila de envio do modo extensão.

Sem a API oficial, quem publica é a extensão do Chrome. Quando uma resposta é aprovada
(pelo robô, só elogios com a chave ligada; ou por uma pessoa no painel), ela entra na fila
como "aguardando envio". A extensão busca a fila, publica no Seller Center e confirma.
"""

import hashlib
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db.models import AuditLog, DraftStatus, PublicadoPor, ReplyDraft, Review, ReviewStatus, Shop

MAX_TENTATIVAS = 3


def comment_id_da_extensao(pedido: str, produto: str) -> int:
    """No Seller Center não aparece o id da avaliação; usamos pedido + produto (únicos por avaliação)."""
    chave = f"{pedido.strip()}|{produto.strip()}".encode()
    return int(hashlib.sha256(chave).hexdigest()[:15], 16)


def enfileirar_envio(review: Review, draft: ReplyDraft, texto: str, por: str, usuario: str | None = None) -> None:
    draft.texto_final = texto
    draft.status = DraftStatus.AGUARDANDO_ENVIO
    draft.publicado_por = por
    draft.publicado_por_usuario = usuario
    draft.erro_envio = None
    review.status = ReviewStatus.ENVIANDO


def confirmar_envio(session: Session, loja: Shop, review: Review, draft: ReplyDraft) -> None:
    draft.status = DraftStatus.PUBLICADO
    draft.publicado_em = datetime.now(UTC)
    draft.erro_envio = None
    review.status = ReviewStatus.PUBLICADA
    session.add(AuditLog(
        shop_id=loja.id, acao="resposta_publicada",
        autor=draft.publicado_por_usuario or draft.publicado_por or PublicadoPor.ROBO,
        detalhes={"review_id": review.id, "via": "extensao", "editada": bool(draft.editado)},
    ))


def registrar_falha(session: Session, loja: Shop, review: Review, draft: ReplyDraft, erro: str) -> bool:
    """Conta a tentativa; depois de MAX_TENTATIVAS volta para a fila. Devolve True se voltou."""
    draft.tentativas_envio = (draft.tentativas_envio or 0) + 1
    draft.erro_envio = erro[:500]
    session.add(AuditLog(shop_id=loja.id, acao="falha_publicacao", autor="extensao",
                         detalhes={"review_id": review.id, "erro": erro[:500],
                                   "tentativa": draft.tentativas_envio}))
    if draft.tentativas_envio >= MAX_TENTATIVAS:
        draft.status = DraftStatus.PENDENTE
        review.status = ReviewStatus.NA_FILA
        return True
    return False
