"""Cliente falso do Claude: devolve respostas prontas e guarda o que recebeu."""

import json
from types import SimpleNamespace


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
