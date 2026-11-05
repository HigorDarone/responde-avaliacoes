"""Robô: buscar → analisar → decidir → publicar ou enfileirar.

Um ciclo, para cada loja ativa:
1. (modo lote) recolhe os lotes da Batch API que já terminaram;
2. busca as avaliações sem resposta e salva só as que ainda não existem no banco
   (idempotência por shop_id + comment_id);
3. analisa as avaliações com status "nova", respeitando o limite mensal da loja.
   No modo lote, elas vão para a Batch API e o resultado chega num ciclo seguinte.

Regra de decisão: só publica sozinho se a chave "Respostas automáticas" da loja
estiver ligada E a avaliação for positiva E a IA não pediu revisão. Todo o resto
vai para a fila de aprovação.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

import anthropic
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.ai.analyzer import AnaliseError, AnaliseIA, Analisador, PedidoAnalise, ResultadoAnalise, precisa_ia
from app.ai.contexto import carregar_contexto
from app.config import Settings, get_settings
from app.db.repo import consulta_lojas_operaveis
from app.envio import enfileirar_envio
from app.db.models import (
    AuditLog,
    DraftStatus,
    PublicadoPor,
    ReplyDraft,
    Review,
    ReviewStatus,
    Shop,
)
from app.shopee.base import Midia, ShopeeClient, ShopeeComment, ShopeeError

log = logging.getLogger("robo")


class Decisao(StrEnum):
    PUBLICAR = "publicar"
    FILA = "fila"


def decidir(auto_publicar: bool, analise: AnaliseIA) -> Decisao:
    if auto_publicar and analise.sentimento == "positivo" and not analise.precisa_revisao:
        return Decisao.PUBLICAR
    return Decisao.FILA


@dataclass
class RelatorioCiclo:
    novas: int = 0
    publicadas: int = 0
    na_fila: int = 0
    enviadas_lote: int = 0
    para_extensao: int = 0  # aprovadas pelo robô, esperando a extensão publicar
    erros: int = 0
    lojas_no_limite: list[int] = field(default_factory=list)

    def __str__(self) -> str:
        texto = (
            f"{self.novas} novas, {self.publicadas} publicadas, {self.na_fila} na fila, "
            f"{self.enviadas_lote} enviadas em lote, {self.erros} erros"
        )
        if self.para_extensao:
            texto += f", {self.para_extensao} aguardando a extensão publicar"
        if self.lojas_no_limite:
            texto += f"; limite mensal atingido nas lojas {self.lojas_no_limite}"
        return texto


def comentario_da_review(review: Review) -> ShopeeComment:
    return ShopeeComment(
        comment_id=review.comment_id,
        item_id=review.item_id,
        produto_nome=review.produto_nome,
        estrelas=review.estrelas,
        texto=review.texto,
        comprador=review.comprador,
        criado_em=review.avaliado_em,
        midias=[Midia(tipo=m["tipo"], url=m["url"]) for m in review.midias],
    )


def inicio_do_mes(agora: datetime) -> datetime:
    return agora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class Robo:
    def __init__(
        self,
        shopee: ShopeeClient,
        analisador: Analisador,
        settings: Settings | None = None,
        relogio: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.shopee = shopee
        self.analisador = analisador
        self.settings = settings or get_settings()
        self.relogio = relogio

    @property
    def modo_lote(self) -> bool:
        return self.settings.use_batch_api

    @property
    def modo_extensao(self) -> bool:
        return self.settings.use_extensao

    def executar_ciclo(self, session: Session) -> RelatorioCiclo:
        rel = RelatorioCiclo()
        consulta = consulta_lojas_operaveis(self.settings.use_mock_shopee, self.modo_extensao)
        for loja in session.scalars(consulta).all():
            # No modo extensão quem traz as avaliações é a extensão; o robô só analisa.
            self.processar_loja(session, loja, rel, buscar=not self.modo_extensao)
        return rel

    def processar_loja(self, session: Session, loja: Shop, rel: RelatorioCiclo | None = None,
                       buscar: bool = True) -> RelatorioCiclo:
        rel = rel or RelatorioCiclo()
        try:
            if self.modo_lote:
                self._recolher_lotes(session, loja, rel)
            if buscar:
                self._buscar_novas(session, loja, rel)
            self._processar_pendentes(session, loja, rel)
        except (anthropic.APIError, ShopeeError) as e:
            # Falha de rede/API: o que não terminou continua "nova" e é tentado no próximo ciclo.
            session.rollback()
            log.warning("Loja %s: ciclo interrompido (%s)", loja.id, e)
        return rel

    # --- 1. Buscar ---

    def _buscar_novas(self, session: Session, loja: Shop, rel: RelatorioCiclo) -> None:
        comentarios = self.shopee.get_unreplied_comments(loja.shopee_shop_id)
        if not comentarios:
            return
        ja_existem = set(
            session.scalars(
                select(Review.comment_id).where(
                    Review.shop_id == loja.id,
                    Review.comment_id.in_([c.comment_id for c in comentarios]),
                )
            )
        )
        for c in comentarios:
            if c.comment_id in ja_existem:
                continue
            session.add(
                Review(
                    shop_id=loja.id,
                    comment_id=c.comment_id,
                    item_id=c.item_id,
                    produto_nome=c.produto_nome,
                    estrelas=c.estrelas,
                    texto=c.texto,
                    midias=[{"tipo": m.tipo, "url": m.url} for m in c.midias],
                    comprador=c.comprador,
                    avaliado_em=c.criado_em,
                    status=ReviewStatus.NOVA,
                )
            )
            ja_existem.add(c.comment_id)
            rel.novas += 1
        session.commit()

    # --- 2. Analisar ---

    def _vagas_no_mes(self, session: Session, loja: Shop) -> int:
        limite = loja.settings.limite_avaliacoes_mes or self.settings.limite_avaliacoes_mes
        usadas = session.scalar(
            select(func.count(Review.id)).where(
                Review.shop_id == loja.id,
                or_(
                    Review.status == ReviewStatus.EM_ANALISE,
                    Review.processado_em >= inicio_do_mes(self.relogio()),
                ),
            )
        )
        return max(limite - usadas, 0)

    def _processar_pendentes(self, session: Session, loja: Shop, rel: RelatorioCiclo) -> None:
        pendentes = session.scalars(
            select(Review)
            .where(Review.shop_id == loja.id, Review.status == ReviewStatus.NOVA)
            .order_by(Review.avaliado_em, Review.id)
        ).all()
        if not pendentes:
            return
        vagas = self._vagas_no_mes(session, loja)
        if len(pendentes) > vagas:
            # As que sobrarem continuam "novas" e entram no mês seguinte.
            log.warning("Loja %s: limite mensal atingido, %d avaliações esperando", loja.id, len(pendentes) - vagas)
            rel.lojas_no_limite.append(loja.id)
            pendentes = pendentes[:vagas]

        pedidos: list[tuple[Review, PedidoAnalise]] = []
        for review in pendentes:
            comentario = comentario_da_review(review)
            contexto = carregar_contexto(session, loja.id, comentario)
            # Imagens: o download e a redução das fotos/quadros de vídeo entram com app/media.
            if self.modo_lote and precisa_ia(comentario):
                pedidos.append((review, PedidoAnalise(f"r{review.id}", comentario, contexto)))
                continue
            try:
                resultado = self.analisador.analisar(comentario, contexto)
            except AnaliseError as e:
                self._marcar_erro(session, review, e, rel)
                continue
            self._aplicar(session, loja, review, resultado, rel)

        if pedidos:
            lote_id = self.analisador.criar_lote([p for _, p in pedidos])
            for review, _ in pedidos:
                review.status = ReviewStatus.EM_ANALISE
                review.lote_id = lote_id
            session.commit()
            rel.enviadas_lote += len(pedidos)
            log.info("Loja %s: %d avaliações enviadas no lote %s", loja.id, len(pedidos), lote_id)

    def _recolher_lotes(self, session: Session, loja: Shop, rel: RelatorioCiclo) -> None:
        em_analise = session.scalars(
            select(Review).where(Review.shop_id == loja.id, Review.status == ReviewStatus.EM_ANALISE)
        ).all()
        por_lote: dict[str, list[Review]] = {}
        for r in em_analise:
            por_lote.setdefault(r.lote_id, []).append(r)

        for lote_id, reviews in por_lote.items():
            contextos = {
                f"r{r.id}": carregar_contexto(session, loja.id, comentario_da_review(r)) for r in reviews
            }
            resultados = self.analisador.resultados_lote(lote_id, contextos)
            if resultados is None:
                continue  # ainda processando
            for r in reviews:
                resultado = resultados.get(f"r{r.id}", AnaliseError("Resultado ausente no lote."))
                if isinstance(resultado, AnaliseError):
                    self._marcar_erro(session, r, resultado, rel)
                else:
                    self._aplicar(session, loja, r, resultado, rel)

    # --- 3. Decidir e registrar ---

    def _aplicar(
        self, session: Session, loja: Shop, review: Review, resultado: ResultadoAnalise, rel: RelatorioCiclo
    ) -> None:
        a = resultado.analise
        agora = self.relogio()
        review.sentimento = a.sentimento
        review.categoria = a.categoria
        review.gravidade = a.gravidade
        review.precisa_revisao = a.precisa_revisao
        review.motivo = a.motivo
        review.modelo_usado = resultado.uso.modelo
        review.tokens_entrada = resultado.uso.tokens_entrada_total
        review.tokens_saida = resultado.uso.tokens_saida
        review.custo_estimado_usd = resultado.uso.custo_usd
        review.processado_em = agora

        draft = ReplyDraft(shop_id=loja.id, review_id=review.id, texto_ia=a.resposta)
        session.add(draft)

        # A chave é lida agora, não quando a avaliação foi buscada: desligar vale na hora.
        session.refresh(loja.settings)
        publicar = decidir(loja.settings.auto_publicar, a) == Decisao.PUBLICAR
        if publicar and self.modo_extensao:
            # Sem API: a extensão do Chrome publica e confirma depois.
            enfileirar_envio(review, draft, a.resposta, PublicadoPor.ROBO)
            rel.para_extensao += 1
        elif publicar and self._publicar(session, loja, review, a.resposta):
            draft.texto_final = a.resposta
            draft.status = DraftStatus.PUBLICADO
            draft.publicado_por = PublicadoPor.ROBO
            draft.publicado_em = agora
            review.status = ReviewStatus.PUBLICADA
            session.add(
                AuditLog(
                    shop_id=loja.id,
                    acao="resposta_publicada",
                    autor=PublicadoPor.ROBO,
                    detalhes={"review_id": review.id, "comment_id": review.comment_id},
                )
            )
            rel.publicadas += 1
        else:
            draft.status = DraftStatus.PENDENTE
            review.status = ReviewStatus.NA_FILA
            rel.na_fila += 1
        session.commit()

    def _publicar(self, session: Session, loja: Shop, review: Review, texto: str) -> bool:
        try:
            self.shopee.reply_comment(loja.shopee_shop_id, review.comment_id, texto)
            return True
        except ShopeeError as e:
            log.warning("Loja %s: falha ao publicar %s (%s); vai para a fila", loja.id, review.comment_id, e)
            session.add(
                AuditLog(
                    shop_id=loja.id,
                    acao="falha_publicacao",
                    autor=PublicadoPor.ROBO,
                    detalhes={"review_id": review.id, "erro": str(e)},
                )
            )
            return False

    def _marcar_erro(self, session: Session, review: Review, erro: Exception, rel: RelatorioCiclo) -> None:
        review.status = ReviewStatus.ERRO
        review.motivo = str(erro)
        review.processado_em = self.relogio()
        session.commit()
        rel.erros += 1
        log.warning("Avaliação %s: erro na análise (%s)", review.comment_id, erro)
