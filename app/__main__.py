"""Linha de comando.

    python -m app            cria o banco e lista as avaliações sem resposta
    python -m app analisar   analisa cada avaliação com o Claude (modo imediato) e mostra
                             a resposta sugerida. Não salva nem publica nada.
"""

import sys

from app.config import get_settings
from app.db.base import get_session, init_db
from app.db.repo import buscar_loja_por_shopee_id, criar_loja
from app.shopee import get_shopee_client
from app.shopee.mock_client import MockShopeeClient


def main(argv: list[str]) -> None:
    analisar = argv[:1] == ["analisar"]
    settings = get_settings()
    init_db()
    client = get_shopee_client(settings)
    print(f"Banco: {settings.database_url}")
    print(f"Shopee: {'mock' if settings.use_mock_shopee else settings.shopee_env}")
    if analisar:
        print(f"Modelo: {settings.claude_model}")

    with get_session() as session:
        # No modo mock, cria no banco as lojas de exemplo se ainda não existirem.
        if isinstance(client, MockShopeeClient):
            for shopee_id in client.lojas():
                if buscar_loja_por_shopee_id(session, shopee_id) is None:
                    criar_loja(session, f"Loja mock {shopee_id}", shopee_id)
            session.commit()

        for shopee_id in client.lojas():
            loja = buscar_loja_por_shopee_id(session, shopee_id)
            chave = "Ligadas" if loja.settings.auto_publicar else "Desligadas"
            comentarios = client.get_unreplied_comments(shopee_id)
            print(f"\n{loja.nome} — Respostas automáticas: {chave}")
            print(f"{len(comentarios)} avaliações sem resposta:\n")
            if analisar:
                _analisar(session, loja.id, comentarios)
                continue
            for c in comentarios:
                print(f"  {_cabecalho(c)}")
                print(f"        {c.texto or '(sem texto)'}")


def _cabecalho(c) -> str:
    midia = f" [{', '.join(m.tipo for m in c.midias)}]" if c.midias else ""
    return f"{'★' * c.estrelas:<5} {c.produto_nome}{midia}"


def _analisar(session, shop_id: int, comentarios) -> None:
    import anthropic

    from app.ai.analyzer import AnaliseError, Analisador
    from app.ai.contexto import carregar_contexto

    analisador = Analisador()
    custo_total = 0.0
    for c in comentarios:
        print(f"  {_cabecalho(c)}")
        print(f"    Avaliação: {c.texto or '(sem texto)'}")
        try:
            r = analisador.analisar(c, carregar_contexto(session, shop_id, c))
        except anthropic.AuthenticationError:
            sys.exit("\nChave da API inválida ou ausente. Preencha ANTHROPIC_API_KEY no .env.")
        except (AnaliseError, anthropic.APIError) as e:
            print(f"    ERRO: {e}\n")
            continue
        a = r.analise
        revisao = "  ⚠ precisa de você" if a.precisa_revisao else ""
        print(f"    → {a.sentimento} · {a.categoria} · gravidade {a.gravidade}{revisao}")
        print(f"    Resposta: {a.resposta}")
        print(f"    Motivo: {a.motivo}")
        print(f"    ({r.origem}; {r.uso.tokens_entrada_total} tokens entrada, "
              f"{r.uso.tokens_saida} saída; US$ {r.uso.custo_usd:.5f})\n")
        custo_total += r.uso.custo_usd
    print(f"Custo estimado total: US$ {custo_total:.4f}")


if __name__ == "__main__":
    main(sys.argv[1:])
