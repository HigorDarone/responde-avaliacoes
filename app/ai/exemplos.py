"""Escolhe os exemplos de resposta mais parecidos com a avaliação (TF-IDF simples, sem serviço pago)."""

import math
import re
import unicodedata
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.ai.contexto import Exemplo

STOPWORDS = {
    "que", "com", "para", "uma", "por", "mas", "mais", "muito", "isso", "esse", "essa",
    "ele", "ela", "foi", "tem", "ter", "ficou", "veio", "meu", "minha", "seu", "sua",
    "dos", "das", "nos", "nas", "pra", "pro", "bem", "ate", "como", "quando", "sim",
    "nao", "aqui", "voce", "vcs", "loja", "produto",
}


def sentimento_esperado(estrelas: int) -> str:
    """Palpite antes da análise: a categoria só é conhecida depois que a IA responde."""
    if estrelas >= 4:
        return "positivo"
    if estrelas == 3:
        return "neutro"
    return "negativo"


def _tokens(texto: str) -> list[str]:
    sem_acento = unicodedata.normalize("NFKD", texto.lower()).encode("ascii", "ignore").decode()
    return [t for t in re.findall(r"[a-z0-9]+", sem_acento) if len(t) >= 3 and t not in STOPWORDS]


def _vetor(tokens: list[str], idf: dict[str, float]) -> dict[str, float]:
    contagem = Counter(tokens)
    return {t: n * idf.get(t, 0.0) for t, n in contagem.items()}


def _cosseno(a: dict[str, float], b: dict[str, float]) -> float:
    num = sum(v * b.get(t, 0.0) for t, v in a.items())
    den = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return num / den if den else 0.0


def selecionar_exemplos(
    texto: str, estrelas: int, candidatos: list["Exemplo"], n: int = 4
) -> list["Exemplo"]:
    """Mesmo sentimento primeiro; dentro de cada grupo, os textos mais parecidos.

    Em caso de empate, mantém a ordem recebida (mais recentes primeiro).
    """
    if not candidatos or n <= 0:
        return []

    docs = [_tokens(c.texto_avaliacao) for c in candidatos]
    consulta = _tokens(texto)
    total = len(docs) + 1
    freq_doc = Counter(t for d in [*docs, consulta] for t in set(d))
    idf = {t: math.log(total / df) + 1.0 for t, df in freq_doc.items()}

    v_consulta = _vetor(consulta, idf)
    esperado = sentimento_esperado(estrelas)
    pontuados = [
        (c.sentimento == esperado, _cosseno(v_consulta, _vetor(d, idf)), -i, c)
        for i, (c, d) in enumerate(zip(candidatos, docs))
    ]
    pontuados.sort(key=lambda p: p[:3], reverse=True)
    return [p[3] for p in pontuados[:n]]
