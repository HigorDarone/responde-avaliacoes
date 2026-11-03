"""Chama o Claude e devolve a análise estruturada de uma avaliação.

Dois modos:
- imediato (`analisar`): uma chamada por avaliação, bom para testes e para a aba "Testar";
- lote (`criar_lote` + `resultados_lote`): Batch API, 50% mais barata, para o robô.

Avaliações 5 estrelas sem texto e sem mídia não chamam a IA: recebem uma resposta
pronta sorteada entre as da loja.
"""

import base64
import json
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.ai.contexto import ContextoLoja
from app.ai.custos import Uso, uso_da_resposta
from app.ai.prompts import montar_system, render_mensagem
from app.config import Settings, get_settings
from app.shopee.base import ShopeeComment

MAX_TOKENS = 2048  # o Haiku 5.5 pensa antes de responder, e o raciocínio conta aqui
EFFORT = "medium"

RESPOSTAS_PRONTAS_PADRAO = [
    "Obrigado pelas 5 estrelas! Que bom que deu tudo certo com o seu pedido.",
    "Valeu demais pela avaliação! Esperamos te ver de novo por aqui.",
    "Muito obrigado pela nota máxima! Qualquer coisa, é só chamar a gente.",
]

CATEGORIAS = [
    "elogio", "defeito", "quebrado_no_envio", "atraso", "produto_diferente", "duvida", "outro",
]

# Schema enviado em output_config.format (a API garante JSON nesse formato).
SCHEMA_ANALISE = {
    "type": "object",
    "properties": {
        "sentimento": {"type": "string", "enum": ["positivo", "neutro", "negativo"]},
        "categoria": {"type": "string", "enum": CATEGORIAS},
        "gravidade": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "precisa_revisao": {"type": "boolean"},
        "resposta": {"type": "string"},
        "motivo": {"type": "string"},
    },
    "required": ["sentimento", "categoria", "gravidade", "precisa_revisao", "resposta", "motivo"],
    "additionalProperties": False,
}


class AnaliseIA(BaseModel):
    """O JSON que o Claude devolve, validado."""

    model_config = ConfigDict(extra="forbid")

    sentimento: Literal["positivo", "neutro", "negativo"]
    categoria: Literal[
        "elogio", "defeito", "quebrado_no_envio", "atraso", "produto_diferente", "duvida", "outro"
    ]
    gravidade: int = Field(ge=1, le=5)
    precisa_revisao: bool
    resposta: str = Field(min_length=1)
    motivo: str


class AnaliseError(Exception):
    """A IA não devolveu uma análise utilizável (recusa, corte, JSON inválido...)."""


@dataclass(frozen=True)
class Imagem:
    """Imagem já reduzida (lado maior ~1000px), pronta para enviar."""

    media_type: str  # image/jpeg, image/png, image/webp ou image/gif
    dados: bytes


@dataclass
class ResultadoAnalise:
    analise: AnaliseIA
    origem: Literal["ia", "pronta"]
    uso: Uso


@dataclass
class PedidoAnalise:
    """Uma avaliação para o modo lote. `custom_id`: letras, números, - e _ (até 64)."""

    custom_id: str
    comentario: ShopeeComment
    contexto: ContextoLoja
    imagens: Sequence[Imagem] = field(default_factory=tuple)
    dificil: bool = False


def precisa_ia(comentario: ShopeeComment) -> bool:
    return not (comentario.estrelas == 5 and not comentario.texto.strip() and not comentario.midias)


class Analisador:
    def __init__(self, client=None, settings: Settings | None = None, rng: random.Random | None = None):
        self.settings = settings or get_settings()
        self._client = client
        self._rng = rng or random.Random()

    @property
    def client(self):
        # Criado só quando necessário: respostas prontas e testes não precisam de chave.
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key or None)
        return self._client

    def modelo(self, dificil: bool = False) -> str:
        if dificil and self.settings.claude_model_dificil:
            return self.settings.claude_model_dificil
        return self.settings.claude_model

    # --- Montagem ---

    def montar_requisicao(
        self,
        comentario: ShopeeComment,
        contexto: ContextoLoja,
        imagens: Sequence[Imagem] = (),
        dificil: bool = False,
    ) -> dict:
        conteudo: list[dict] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img.media_type,
                    "data": base64.standard_b64encode(img.dados).decode("ascii"),
                },
            }
            for img in imagens
        ]
        conteudo.append({"type": "text", "text": render_mensagem(comentario, contexto, len(imagens))})
        return {
            "model": self.modelo(dificil),
            "max_tokens": MAX_TOKENS,
            "system": montar_system(contexto),
            "messages": [{"role": "user", "content": conteudo}],
            "output_config": {
                "effort": EFFORT,
                "format": {"type": "json_schema", "schema": SCHEMA_ANALISE},
            },
        }

    # --- Modo imediato ---

    def analisar(
        self,
        comentario: ShopeeComment,
        contexto: ContextoLoja,
        imagens: Sequence[Imagem] = (),
        dificil: bool = False,
    ) -> ResultadoAnalise:
        if not precisa_ia(comentario):
            return self.resposta_pronta(contexto)
        params = self.montar_requisicao(comentario, contexto, imagens, dificil)
        resposta = self.client.messages.create(**params)
        return interpretar_resposta(resposta, params["model"], contexto, lote=False)

    def resposta_pronta(self, contexto: ContextoLoja) -> ResultadoAnalise:
        opcoes = contexto.regras.respostas_prontas_5_estrelas or RESPOSTAS_PRONTAS_PADRAO
        texto = self._rng.choice(opcoes)
        if contexto.regras.assinatura:
            texto = f"{texto} {contexto.regras.assinatura}"
        analise = AnaliseIA(
            sentimento="positivo",
            categoria="elogio",
            gravidade=1,
            precisa_revisao=False,
            resposta=texto,
            motivo="5 estrelas sem texto e sem mídia: resposta pronta da loja.",
        )
        return ResultadoAnalise(analise, "pronta", Uso(modelo="nenhum"))

    # --- Modo lote (Batch API) ---

    def criar_lote(self, pedidos: Sequence[PedidoAnalise]) -> str:
        """Envia as avaliações para a Batch API e devolve o id do lote."""
        sem_ia = [p.custom_id for p in pedidos if not precisa_ia(p.comentario)]
        if sem_ia:
            raise ValueError(f"Use resposta_pronta() para estas avaliações, não o lote: {sem_ia}")
        requests = [
            {
                "custom_id": p.custom_id,
                "params": self.montar_requisicao(p.comentario, p.contexto, p.imagens, p.dificil),
            }
            for p in pedidos
        ]
        return self.client.messages.batches.create(requests=requests).id

    def resultados_lote(
        self, batch_id: str, contextos: Mapping[str, ContextoLoja]
    ) -> dict[str, ResultadoAnalise | AnaliseError] | None:
        """None se o lote ainda está processando; senão, resultado (ou erro) por custom_id."""
        lote = self.client.messages.batches.retrieve(batch_id)
        if lote.processing_status != "ended":
            return None

        saida: dict[str, ResultadoAnalise | AnaliseError] = {}
        for item in self.client.messages.batches.results(batch_id):
            r = item.result
            if r.type != "succeeded":
                detalhe = getattr(r, "error", None)
                saida[item.custom_id] = AnaliseError(f"Lote: {r.type} {detalhe or ''}".strip())
                continue
            try:
                saida[item.custom_id] = interpretar_resposta(
                    r.message, r.message.model, contextos[item.custom_id], lote=True
                )
            except AnaliseError as e:
                saida[item.custom_id] = e
        return saida


def interpretar_resposta(resposta, modelo: str, contexto: ContextoLoja, lote: bool) -> ResultadoAnalise:
    if resposta.stop_reason == "refusal":
        raise AnaliseError("O Claude recusou analisar esta avaliação.")
    if resposta.stop_reason == "max_tokens":
        raise AnaliseError("A resposta do Claude foi cortada (max_tokens).")

    texto = next((b.text for b in resposta.content if b.type == "text"), None)
    if texto is None:
        raise AnaliseError("O Claude não devolveu texto.")
    try:
        analise = AnaliseIA.model_validate(json.loads(texto))
    except (json.JSONDecodeError, ValidationError) as e:
        raise AnaliseError(f"JSON inválido do Claude: {e}") from e

    _checar_palavras_proibidas(analise, contexto.regras.palavras_proibidas)
    return ResultadoAnalise(analise, "ia", uso_da_resposta(resposta.usage, modelo, lote))


def _checar_palavras_proibidas(analise: AnaliseIA, proibidas: Sequence[str]) -> None:
    """Rede de segurança: se a IA usou uma palavra proibida, vai para revisão humana."""
    texto = analise.resposta.lower()
    usadas = [p for p in proibidas if p.strip() and p.lower() in texto]
    if usadas:
        analise.precisa_revisao = True
        analise.motivo = f"Precisa de você: resposta usa palavra proibida ({', '.join(usadas)}). {analise.motivo}"
