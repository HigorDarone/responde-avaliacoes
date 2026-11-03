"""Cliente falso do Claude: devolve respostas prontas e guarda o que recebeu."""

import json
import re
from types import SimpleNamespace

import anthropic


def resposta_claude(dados: dict | str, stop_reason: str = "end_turn", model="claude-haiku-5-5"):
    texto = dados if isinstance(dados, str) else json.dumps(dados, ensure_ascii=False)
    return SimpleNamespace(
        model=model,
        stop_reason=stop_reason,
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=texto),
        ],
        usage=SimpleNamespace(
            input_tokens=300,
            output_tokens=120,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=1000,
        ),
    )


ANALISE_OK = {
    "sentimento": "positivo",
    "categoria": "elogio",
    "gravidade": 1,
    "precisa_revisao": False,
    "resposta": "Que bom que o vaso ficou lindo na sua estante!",
    "motivo": "Elogio ao acabamento e à entrega.",
}


class FakeMessages:
    def __init__(self, respostas):
        self._respostas = list(respostas)
        self.chamadas: list[dict] = []
        self.batches = FakeBatches()

    def create(self, **params):
        self.chamadas.append(params)
        return self._respostas.pop(0) if len(self._respostas) > 1 else self._respostas[0]


class FakeBatches:
    def __init__(self):
        self.criados: list[list[dict]] = []
        self.status = "in_progress"
        self.itens: list = []

    def create(self, requests):
        self.criados.append(list(requests))
        return SimpleNamespace(id="msgbatch_teste")

    def retrieve(self, batch_id):
        return SimpleNamespace(id=batch_id, processing_status=self.status)

    def results(self, batch_id):
        return iter(self.itens)


class FakeClaude:
    def __init__(self, *respostas):
        self.messages = FakeMessages(respostas or [resposta_claude(ANALISE_OK)])


# --- Claude falso que "lê" a avaliação, para testar o robô de ponta a ponta ---


def analise_por_texto(texto_prompt: str) -> dict:
    """4-5★ = positivo; 3★ = neutro; 1-2★ = negativo. "dinheiro de volta" = precisa de revisão."""
    estrelas = int(re.search(r"Estrelas: (\d) de 5", texto_prompt).group(1))
    sentimento = "positivo" if estrelas >= 4 else "neutro" if estrelas == 3 else "negativo"
    return {
        "sentimento": sentimento,
        "categoria": "elogio" if sentimento == "positivo" else "defeito",
        "gravidade": 1 if sentimento == "positivo" else 3,
        "precisa_revisao": "dinheiro de volta" in texto_prompt,
        "resposta": f"Resposta para avaliação de {estrelas} estrelas.",
        "motivo": "teste",
    }


def _texto_do_pedido(params: dict) -> str:
    return params["messages"][0]["content"][-1]["text"]


class ClaudePorEstrelas:
    def __init__(self, falhar_api: bool = False):
        self.messages = SimpleNamespace(create=self._create, batches=BatchesPorEstrelas())
        self.chamadas: list[dict] = []
        self.falhar_api = falhar_api

    def _create(self, **params):
        if self.falhar_api:
            raise anthropic.APIConnectionError(request=httpx_request())
        self.chamadas.append(params)
        return resposta_claude(analise_por_texto(_texto_do_pedido(params)))


class BatchesPorEstrelas:
    def __init__(self):
        self.lotes: dict[str, list[dict]] = {}
        self.terminados: set[str] = set()

    def create(self, requests):
        batch_id = f"msgbatch_{len(self.lotes) + 1}"
        self.lotes[batch_id] = list(requests)
        return SimpleNamespace(id=batch_id)

    def terminar_todos(self):
        self.terminados = set(self.lotes)

    def retrieve(self, batch_id):
        status = "ended" if batch_id in self.terminados else "in_progress"
        return SimpleNamespace(id=batch_id, processing_status=status)

    def results(self, batch_id):
        for req in self.lotes[batch_id]:
            msg = resposta_claude(analise_por_texto(_texto_do_pedido(req["params"])))
            yield SimpleNamespace(
                custom_id=req["custom_id"], result=SimpleNamespace(type="succeeded", message=msg)
            )


def httpx_request():
    import httpx2

    return httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
