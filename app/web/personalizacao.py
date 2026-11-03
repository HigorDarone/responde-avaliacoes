"""Tela "Personalização": abas Regras · Exemplos · Produtos · Testar.

Regras, exemplos e produtos salvos aqui entram no prompt das análises seguintes
(ver app/ai/prompts.py e app/ai/contexto.py). A aba Testar é a etapa 5c3.
"""

import re
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.contexto import RegrasLoja
from app.ai.prompts import render_regras_loja
from app.db.models import AuditLog, OrigemExemplo, ProductInfo, ReplyExample, Review, Shop
from app.db.repo import exemplo_existente
from app.web.routes import AUTOR_PAINEL, _contexto, get_db, loja_atual, templates

router = APIRouter(prefix="/personalizacao")

ABAS = [
    ("regras", "Regras", True),
    ("exemplos", "Exemplos", True),
    ("produtos", "Produtos", True),
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


# --- Aba Exemplos ---

CATEGORIAS = [
    ("elogio", "Elogio"),
    ("defeito", "Defeito"),
    ("quebrado_no_envio", "Quebrou no envio"),
    ("atraso", "Atraso"),
    ("produto_diferente", "Produto diferente"),
    ("duvida", "Dúvida"),
    ("outro", "Outro"),
]
SENTIMENTOS = [("positivo", "Positivo"), ("neutro", "Neutro"), ("negativo", "Negativo")]
ORIGENS = {
    "manual": ("Manual", ""),
    "edicao": ("Sua edição", "pilula--azul"),
    "marcada_otima": ("Marcada ótima", "pilula--ok"),
}
MAX_TEXTO = 2000


def _ctx_exemplos(db: Session, loja: Shop, categoria: str = "", novo: dict | None = None,
                  erros: dict | None = None, aviso: str = "") -> dict:
    consulta = select(ReplyExample).where(ReplyExample.shop_id == loja.id)
    if categoria:
        consulta = consulta.where(ReplyExample.categoria == categoria)
    exemplos = db.scalars(consulta.order_by(ReplyExample.criado_em.desc(), ReplyExample.id.desc())).all()

    # Quantos ativos por categoria, e quais ficam de fora do limite (os mais antigos).
    limite = loja.settings.max_exemplos_por_categoria
    ativos_por_cat = dict(
        db.execute(
            select(ReplyExample.categoria, func.count())
            .where(ReplyExample.shop_id == loja.id, ReplyExample.ativo.is_(True))
            .group_by(ReplyExample.categoria)
        ).all()
    )
    vistos: dict[str, int] = {}
    fora_do_limite: set[int] = set()
    for e in db.scalars(
        select(ReplyExample)
        .where(ReplyExample.shop_id == loja.id, ReplyExample.ativo.is_(True))
        .order_by(ReplyExample.criado_em.desc(), ReplyExample.id.desc())
    ):
        vistos[e.categoria] = vistos.get(e.categoria, 0) + 1
        if vistos[e.categoria] > limite:
            fora_do_limite.add(e.id)

    return _contexto(
        db, loja, "personalizacao",
        aba="exemplos", abas=ABAS, exemplos=exemplos, filtro=categoria,
        lista_categorias=CATEGORIAS, sentimentos=SENTIMENTOS,
        origens=ORIGENS, ativos_por_cat=ativos_por_cat, limite=limite, fora_do_limite=fora_do_limite,
        novo=novo or {"categoria": categoria or "elogio", "sentimento": "positivo",
                      "texto_avaliacao": "", "resposta_ideal": ""},
        erros=erros or {}, aviso=aviso,
    )


def _aba_exemplos(request: Request, db: Session, loja: Shop, status_code: int = 200, **kw):
    return templates.TemplateResponse(
        request, "partials/aba_exemplos.html", _ctx_exemplos(db, loja, **kw), status_code=status_code
    )


def _exemplo_da_loja(db: Session, loja: Shop | None, exemplo_id: int) -> ReplyExample:
    exemplo = db.get(ReplyExample, exemplo_id)
    if loja is None or exemplo is None or exemplo.shop_id != loja.id:
        raise HTTPException(status_code=404, detail="Exemplo não encontrado.")
    return exemplo


@router.get("/exemplos")
def exemplos(request: Request, categoria: str = "", db: Session = Depends(get_db),
             loja: Shop | None = Depends(loja_atual)):
    if loja is None:
        return templates.TemplateResponse(request, "personalizacao.html", _contexto(db, None, "personalizacao"))
    categoria = categoria if categoria in dict(CATEGORIAS) else ""
    return templates.TemplateResponse(request, "personalizacao.html", _ctx_exemplos(db, loja, categoria))


@router.post("/exemplos")
def criar_exemplo(
    request: Request,
    categoria: str = Form(""),
    sentimento: str = Form(""),
    texto_avaliacao: str = Form(""),
    resposta_ideal: str = Form(""),
    filtro: str = Form(""),
    db: Session = Depends(get_db),
    loja: Shop | None = Depends(loja_atual),
):
    if loja is None:
        raise HTTPException(status_code=404)
    novo = {"categoria": categoria, "sentimento": sentimento,
            "texto_avaliacao": texto_avaliacao.strip(), "resposta_ideal": resposta_ideal.strip()}
    erros = {}
    if categoria not in dict(CATEGORIAS):
        erros["categoria"] = "Escolha uma categoria."
    if sentimento not in dict(SENTIMENTOS):
        erros["sentimento"] = "Escolha um sentimento."
    for campo in ("texto_avaliacao", "resposta_ideal"):
        if not novo[campo]:
            erros[campo] = "Preencha este campo."
        elif len(novo[campo]) > MAX_TEXTO:
            erros[campo] = f"Use no máximo {MAX_TEXTO} caracteres."
    if not erros and exemplo_existente(db, loja.id, novo["texto_avaliacao"], novo["resposta_ideal"]):
        erros["resposta_ideal"] = "Este exemplo já existe."
    if erros:
        return _aba_exemplos(request, db, loja, status_code=422, categoria=filtro, novo=novo, erros=erros)

    db.add(ReplyExample(shop_id=loja.id, origem=OrigemExemplo.MANUAL, ativo=True, **novo))
    db.add(AuditLog(shop_id=loja.id, acao="exemplo_criado", autor=AUTOR_PAINEL,
                    detalhes={"categoria": categoria}))
    db.commit()
    return _aba_exemplos(request, db, loja, categoria=filtro, aviso="Exemplo adicionado.")


@router.post("/exemplos/{exemplo_id}/{acao}")
def alterar_exemplo(
    exemplo_id: int,
    acao: str,
    request: Request,
    filtro: str = Form(""),
    db: Session = Depends(get_db),
    loja: Shop | None = Depends(loja_atual),
):
    exemplo = _exemplo_da_loja(db, loja, exemplo_id)
    if acao == "ativar":
        exemplo.ativo = True
        aviso = "Exemplo ativado: volta a ser usado pela IA."
    elif acao == "desativar":
        exemplo.ativo = False
        aviso = "Exemplo desativado: a IA deixa de usá-lo."
    elif acao == "excluir":
        db.delete(exemplo)
        db.add(AuditLog(shop_id=loja.id, acao="exemplo_excluido", autor=AUTOR_PAINEL,
                        detalhes={"categoria": exemplo.categoria, "origem": exemplo.origem}))
        aviso = "Exemplo excluído."
    else:
        raise HTTPException(status_code=404)
    db.commit()
    return _aba_exemplos(request, db, loja, categoria=filtro, aviso=aviso)


# --- Aba Produtos ---

CAMPOS_PRODUTO = [
    ("nome", "Nome", "Ex.: Vaso Geométrico Low Poly 15cm", 1),
    ("material", "Material", "Ex.: PLA fosco", 1),
    ("tamanho", "Tamanho", "Ex.: 15 cm de altura, 10 cm de diâmetro", 1),
    ("normal_impressao_3d", "O que é normal da impressão 3D", "Ex.: Linhas de camada leves na lateral; pequenas marcas na base.", 3),
    ("cuidados", "Cuidados de uso", "Ex.: Não é à prova d'água; evitar sol direto e lava-louças.", 3),
    ("observacoes", "Observações", "Ex.: Pode ser pintado com acrílica; recomendamos primer.", 3),
]
MAX_CAMPO_PRODUTO = 1000


def _produtos_sem_cadastro(db: Session, loja: Shop) -> list[tuple[int, str, int]]:
    """Produtos que aparecem nas avaliações mas não têm informações: (item_id, nome, nº de avaliações)."""
    cadastrados = select(ProductInfo.item_id).where(ProductInfo.shop_id == loja.id)
    return [
        (item_id, nome, total)
        for item_id, nome, total in db.execute(
            select(Review.item_id, func.max(Review.produto_nome), func.count())
            .where(Review.shop_id == loja.id, Review.item_id.not_in(cadastrados))
            .group_by(Review.item_id)
            .order_by(func.count().desc(), Review.item_id)
        ).all()
    ]


def _ctx_produtos(db: Session, loja: Shop, editando: ProductInfo | None = None, form: dict | None = None,
                  erros: dict | None = None, aviso: str = "", aberto: bool = False) -> dict:
    produtos = db.scalars(
        select(ProductInfo).where(ProductInfo.shop_id == loja.id).order_by(ProductInfo.nome)
    ).all()
    if form is None:
        form = {c: getattr(editando, c) for c, *_ in CAMPOS_PRODUTO} if editando else \
            {c: "" for c, *_ in CAMPOS_PRODUTO}
        form["item_id"] = str(editando.item_id) if editando else ""
    return _contexto(
        db, loja, "personalizacao",
        aba="produtos", abas=ABAS, produtos=produtos, sem_cadastro=_produtos_sem_cadastro(db, loja),
        campos_produto=CAMPOS_PRODUTO, editando=editando, form=form, erros=erros or {},
        aviso=aviso, aberto=aberto or editando is not None or bool(erros),
    )


def _aba_produtos(request: Request, db: Session, loja: Shop, status_code: int = 200, **kw):
    return templates.TemplateResponse(
        request, "partials/aba_produtos.html", _ctx_produtos(db, loja, **kw), status_code=status_code
    )


def _produto_da_loja(db: Session, loja: Shop | None, produto_id: int) -> ProductInfo:
    produto = db.get(ProductInfo, produto_id)
    if loja is None or produto is None or produto.shop_id != loja.id:
        raise HTTPException(status_code=404, detail="Produto não encontrado.")
    return produto


@router.get("/produtos")
def produtos(request: Request, editar: int | None = None, item_id: str = "", nome: str = "",
             db: Session = Depends(get_db), loja: Shop | None = Depends(loja_atual)):
    if loja is None:
        return templates.TemplateResponse(request, "personalizacao.html", _contexto(db, None, "personalizacao"))
    editando = _produto_da_loja(db, loja, editar) if editar else None
    form = None
    if item_id and not editando:  # "Cadastrar" a partir de um produto sem informações
        form = {c: "" for c, *_ in CAMPOS_PRODUTO} | {"item_id": item_id, "nome": nome}
    ctx = _ctx_produtos(db, loja, editando=editando, form=form, aberto=bool(item_id))
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "partials/aba_produtos.html", ctx)
    return templates.TemplateResponse(request, "personalizacao.html", ctx)


def _ler_form_produto(dados: dict) -> tuple[dict, dict]:
    form = {c: (dados.get(c) or "").strip() for c, *_ in CAMPOS_PRODUTO}
    form["item_id"] = (dados.get("item_id") or "").strip()
    erros = {}
    if not form["item_id"].isdigit() or int(form["item_id"]) <= 0:
        erros["item_id"] = "Use o número do anúncio na Shopee (só dígitos)."
    if not form["nome"]:
        erros["nome"] = "Preencha o nome."
    for c, *_ in CAMPOS_PRODUTO:
        if len(form[c]) > MAX_CAMPO_PRODUTO:
            erros[c] = f"Use no máximo {MAX_CAMPO_PRODUTO} caracteres."
    return form, erros


async def _dados_form(request: Request) -> dict:
    return dict(await request.form())


@router.post("/produtos")
def salvar_produto(
    request: Request,
    dados: dict = Depends(_dados_form),
    db: Session = Depends(get_db),
    loja: Shop | None = Depends(loja_atual),
):
    if loja is None:
        raise HTTPException(status_code=404)
    produto_id = dados.get("produto_id")
    editando = _produto_da_loja(db, loja, int(produto_id)) if produto_id else None
    form, erros = _ler_form_produto(dados)
    if not erros:
        outro = db.scalar(select(ProductInfo).where(
            ProductInfo.shop_id == loja.id, ProductInfo.item_id == int(form["item_id"])))
        if outro and outro is not editando:
            erros["item_id"] = f"Este número já está cadastrado ({outro.nome})."
    if erros:
        return _aba_produtos(request, db, loja, status_code=422, editando=editando, form=form, erros=erros)

    produto = editando or ProductInfo(shop_id=loja.id)
    produto.item_id = int(form["item_id"])
    for c, *_ in CAMPOS_PRODUTO:
        setattr(produto, c, form[c])
    if editando is None:
        db.add(produto)
    db.add(AuditLog(shop_id=loja.id, acao="produto_alterado" if editando else "produto_criado",
                    autor=AUTOR_PAINEL, detalhes={"item_id": produto.item_id}))
    db.commit()
    return _aba_produtos(request, db, loja, aviso=f"{produto.nome}: informações salvas.")


@router.post("/produtos/{produto_id}/excluir")
def excluir_produto(produto_id: int, request: Request, db: Session = Depends(get_db),
                    loja: Shop | None = Depends(loja_atual)):
    produto = _produto_da_loja(db, loja, produto_id)
    db.delete(produto)
    db.add(AuditLog(shop_id=loja.id, acao="produto_excluido", autor=AUTOR_PAINEL,
                    detalhes={"item_id": produto.item_id}))
    db.commit()
    return _aba_produtos(request, db, loja, aviso=f"{produto.nome} excluído.")
