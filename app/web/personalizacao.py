"""Tela "Personalização": abas Regras · Exemplos · Produtos · Testar.

Por enquanto só a aba Regras (etapa 5c). As regras salvas aqui entram no prompt de
todas as análises seguintes (ver app/ai/prompts.py).
"""

import re
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.ai.contexto import RegrasLoja
from app.ai.prompts import render_regras_loja
from app.db.models import AuditLog, Shop
from app.web.routes import AUTOR_PAINEL, _contexto, get_db, loja_atual, templates

router = APIRouter(prefix="/personalizacao")

ABAS = [
    ("regras", "Regras", True),
    ("exemplos", "Exemplos", False),
    ("produtos", "Produtos", False),
    ("testar", "Testar", False),
]

TONS = [
    ("descontraido", "Descontraído", "Que bom que o vaso ficou lindo aí! Valeu por mostrar a foto."),
    ("neutro", "Neutro", "Ficamos contentes que o vaso chegou bem. Obrigado pela foto."),
    ("formal", "Formal", "Agradecemos o retorno sobre o vaso e a foto enviada."),
]
EMOJIS = [
    ("nunca", "Nunca", "Sem emoji em nenhuma resposta."),
    ("pouco", "Pouco", "No máximo um, só em elogios."),
    ("a_vontade", "À vontade", "Pode usar, sem exagero."),
]
TAMANHOS = [
    ("curta", "Curta", "1 a 2 frases."),
    ("media", "Média", "2 a 3 frases."),
]
CATEGORIAS_OFERTA = [
    ("defeito", "Defeito", "Ex.: Pedir foto pelo chat e oferecer troca da peça."),
    ("quebrado_no_envio", "Quebrou no envio", "Ex.: Pedir foto da peça e da caixa pelo chat; reenviamos sem custo."),
    ("atraso", "Atraso", "Ex.: Explicar que o prazo é da transportadora e pedir o número do pedido no chat."),
    ("produto_diferente", "Produto diferente", "Ex.: Pedir foto pelo chat para conferirmos com o pedido."),
    ("duvida", "Dúvida", "Ex.: Responder se souber; se não, convidar para o chat."),
]

MAX_ITENS = 50
MAX_ITEM = 200
MAX_OFERTA = 500
MAX_ASSINATURA = 120


@dataclass
class FormRegras:
    """Valores do formulário (como texto) + erros por campo."""

    tom: str
    assinatura: str
    uso_emoji: str
    tamanho_resposta: str
    ofertas: dict[str, str]
    nunca_dizer: str
    palavras_proibidas: str
    respostas_prontas: str
    erros: dict[str, str] = field(default_factory=dict)

    @classmethod
    def da_loja(cls, regras: RegrasLoja) -> "FormRegras":
        return cls(
            tom=regras.tom,
            assinatura=regras.assinatura,
            uso_emoji=regras.uso_emoji,
            tamanho_resposta=regras.tamanho_resposta,
            ofertas={c: regras.oferta_por_categoria.get(c, "") for c, _, _ in CATEGORIAS_OFERTA},
            nunca_dizer="\n".join(regras.nunca_dizer),
            palavras_proibidas="\n".join(regras.palavras_proibidas),
            respostas_prontas="\n".join(regras.respostas_prontas_5_estrelas),
        )

    def para_regras(self) -> RegrasLoja:
        return RegrasLoja(
            tom=self.tom,
            assinatura=self.assinatura.strip(),
            uso_emoji=self.uso_emoji,
            tamanho_resposta=self.tamanho_resposta,
            oferta_por_categoria={c: t.strip() for c, t in self.ofertas.items() if t.strip()},
            nunca_dizer=_linhas(self.nunca_dizer),
            palavras_proibidas=_linhas(self.palavras_proibidas, separadores=r"[\n,;]"),
            respostas_prontas_5_estrelas=_linhas(self.respostas_prontas),
        )

    def validar(self) -> bool:
        e = self.erros
        if self.tom not in {v for v, _, _ in TONS}:
            e["tom"] = "Escolha um dos tons."
        if self.uso_emoji not in {v for v, _, _ in EMOJIS}:
            e["uso_emoji"] = "Escolha uma das opções de emoji."
        if self.tamanho_resposta not in {v for v, _, _ in TAMANHOS}:
            e["tamanho_resposta"] = "Escolha um dos tamanhos."
        if len(self.assinatura.strip()) > MAX_ASSINATURA:
            e["assinatura"] = f"Use no máximo {MAX_ASSINATURA} caracteres."
        for cat, texto in self.ofertas.items():
            if len(texto.strip()) > MAX_OFERTA:
                e[f"oferta_{cat}"] = f"Use no máximo {MAX_OFERTA} caracteres."
        listas = {
            "nunca_dizer": _linhas(self.nunca_dizer, limitar=False),
            "palavras_proibidas": _linhas(self.palavras_proibidas, r"[\n,;]", limitar=False),
            "respostas_prontas": _linhas(self.respostas_prontas, limitar=False),
        }
        for nome, itens in listas.items():
            if len(itens) > MAX_ITENS:
                e[nome] = f"No máximo {MAX_ITENS} itens."
            elif any(len(i) > MAX_ITEM for i in itens):
                e[nome] = f"Cada linha pode ter no máximo {MAX_ITEM} caracteres."
        return not e


def _linhas(texto: str, separadores: str = r"\n", limitar: bool = True) -> list[str]:
    """Uma linha por item; tira vazios e repetidos, mantendo a ordem."""
    itens: list[str] = []
    for parte in re.split(separadores, texto or ""):
        parte = parte.strip()
        if parte and parte.lower() not in {i.lower() for i in itens}:
            itens.append(parte)
    return itens[:MAX_ITENS] if limitar else itens


def _form(
    tom: str = Form(""),
    assinatura: str = Form(""),
    uso_emoji: str = Form(""),
    tamanho_resposta: str = Form(""),
    oferta_defeito: str = Form(""),
    oferta_quebrado_no_envio: str = Form(""),
    oferta_atraso: str = Form(""),
    oferta_produto_diferente: str = Form(""),
    oferta_duvida: str = Form(""),
    nunca_dizer: str = Form(""),
    palavras_proibidas: str = Form(""),
    respostas_prontas: str = Form(""),
) -> FormRegras:
    return FormRegras(
        tom=tom,
        assinatura=assinatura,
        uso_emoji=uso_emoji,
        tamanho_resposta=tamanho_resposta,
        ofertas={
            "defeito": oferta_defeito,
            "quebrado_no_envio": oferta_quebrado_no_envio,
            "atraso": oferta_atraso,
            "produto_diferente": oferta_produto_diferente,
            "duvida": oferta_duvida,
        },
        nunca_dizer=nunca_dizer,
        palavras_proibidas=palavras_proibidas,
        respostas_prontas=respostas_prontas,
    )


def _ctx(db: Session, loja: Shop | None, form: FormRegras | None = None, **extra) -> dict:
    form = form or (FormRegras.da_loja(RegrasLoja.de_settings(loja.settings)) if loja else None)
    return _contexto(
        db, loja, "personalizacao",
        aba="regras", abas=ABAS, form=form,
        tons=TONS, emojis=EMOJIS, tamanhos=TAMANHOS, categorias_oferta=CATEGORIAS_OFERTA,
        previa=render_regras_loja(form.para_regras()) if form else "",
        **extra,
    )


@router.get("")
def inicio():
    return RedirectResponse("/personalizacao/regras", status_code=302)


@router.get("/regras")
def regras(request: Request, db: Session = Depends(get_db), loja: Shop | None = Depends(loja_atual)):
    return templates.TemplateResponse(request, "personalizacao.html", _ctx(db, loja))


@router.post("/regras")
def salvar_regras(
    request: Request,
    form: FormRegras = Depends(_form),
    db: Session = Depends(get_db),
    loja: Shop | None = Depends(loja_atual),
):
    if loja is None:
        return RedirectResponse("/fila", status_code=303)
    if not form.validar():
        return templates.TemplateResponse(
            request, "partials/regras.html", _ctx(db, loja, form), status_code=422
        )

    novas = form.para_regras()
    s = loja.settings
    valores = {
        "tom": novas.tom,
        "assinatura": novas.assinatura,
        "uso_emoji": novas.uso_emoji,
        "tamanho_resposta": novas.tamanho_resposta,
        "oferta_por_categoria": novas.oferta_por_categoria,
        "nunca_dizer": novas.nunca_dizer,
        "palavras_proibidas": novas.palavras_proibidas,
        "respostas_prontas_5_estrelas": novas.respostas_prontas_5_estrelas,
    }
    alterados = [campo for campo, valor in valores.items() if getattr(s, campo) != valor]
    for campo in alterados:
        setattr(s, campo, valores[campo])
    if alterados:
        db.add(AuditLog(shop_id=loja.id, acao="regras_alteradas", autor=AUTOR_PAINEL,
                        detalhes={"campos": alterados}))
    db.commit()
    return templates.TemplateResponse(
        request, "partials/regras.html",
        _ctx(db, loja, salvo="Regras salvas. Valem para as próximas análises." if alterados
             else "Nada mudou."),
    )


@router.post("/regras/previa")
def previa_regras(request: Request, form: FormRegras = Depends(_form),
                  db: Session = Depends(get_db), loja: Shop | None = Depends(loja_atual)):
    """Mostra como a IA vai ler as regras do formulário, sem salvar."""
    form.validar()
    return templates.TemplateResponse(request, "partials/previa_regras.html", _ctx(db, loja, form))
