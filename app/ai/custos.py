"""Custo estimado das chamadas ao Claude.

Preços em USD por 1 milhão de tokens (entrada, saída), para prompts de até 100 mil
tokens — avaliações ficam muito abaixo disso. Conferir em docs.claude.com ao mudar de modelo.
"""

from dataclasses import dataclass

PRECOS_POR_MILHAO: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
MULT_CACHE_ESCRITA = 1.25  # cache de 5 minutos
MULT_CACHE_LEITURA = 0.10
DESCONTO_LOTE = 0.50  # Batch API


@dataclass
class Uso:
    modelo: str
    tokens_entrada: int = 0  # sem cache
    tokens_saida: int = 0
    cache_escrita: int = 0
    cache_leitura: int = 0
    lote: bool = False

    @property
    def tokens_entrada_total(self) -> int:
        return self.tokens_entrada + self.cache_escrita + self.cache_leitura

    @property
    def custo_usd(self) -> float:
        precos = PRECOS_POR_MILHAO.get(self.modelo)
        if precos is None:
            return 0.0
        entrada, saida = (p / 1_000_000 for p in precos)
        custo = (
            self.tokens_entrada * entrada
            + self.cache_escrita * entrada * MULT_CACHE_ESCRITA
            + self.cache_leitura * entrada * MULT_CACHE_LEITURA
            + self.tokens_saida * saida
        )
        return custo * DESCONTO_LOTE if self.lote else custo


def uso_da_resposta(usage, modelo: str, lote: bool = False) -> Uso:
    """Converte o `usage` devolvido pela API."""
    return Uso(
        modelo=modelo,
        tokens_entrada=usage.input_tokens or 0,
        tokens_saida=usage.output_tokens or 0,
        cache_escrita=getattr(usage, "cache_creation_input_tokens", None) or 0,
        cache_leitura=getattr(usage, "cache_read_input_tokens", None) or 0,
        lote=lote,
    )
