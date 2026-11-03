"""Linha de comando.

    python -m app                    cria o banco e lista as avaliações sem resposta
    python -m app analisar           analisa cada avaliação com o Claude (modo imediato) e
                                     mostra a resposta sugerida. Não salva nem publica nada.
    python -m app robo               liga o robô: um ciclo agora e depois a cada 30 minutos
    python -m app robo --uma-vez     roda um único ciclo e sai
    python -m app robo --intervalo 10
    python -m app web                abre o painel em http://localhost:8000
"""

import argparse
import logging

from app.config import get_settings
from app.db.base import get_session, init_db
from app.db.repo import consulta_lojas_operaveis, garantir_lojas_mock
from app.shopee import get_shopee_client
from app.shopee.mock_client import MockShopeeClient


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app")
    sub = parser.add_subparsers(dest="comando")
    sub.add_parser("analisar")
    robo = sub.add_parser("robo")
    robo.add_argument("--uma-vez", action="store_true", help="roda um ciclo e sai")
    robo.add_argument("--intervalo", type=int, default=30, help="minutos entre ciclos")
    web = sub.add_parser("web")
    web.add_argument("--porta", type=int, default=8000)
    args = parser.parse_args(argv)

    if args.comando == "web":
        _web(args.porta)
        return

    settings = get_settings()
    init_db()
    client = get_shopee_client(settings)
    print(f"Banco: {settings.database_url}")
    print(f"Shopee: {'mock' if settings.use_mock_shopee else settings.shopee_env}")

    if isinstance(client, MockShopeeClient):
        with get_session() as session:
            garantir_lojas_mock(session, client)

    if args.comando == "robo":
        _robo(client, args.uma_vez, args.intervalo)
    else:
        _listar(client, analisar=args.comando == "analisar")


def _web(porta: int) -> None:
    import uvicorn

    from app.web.main import create_app

    init_db()
    print(f"Painel em http://localhost:{porta}  (Ctrl+C para parar)")
    uvicorn.run(create_app(), host="127.0.0.1", port=porta)


def _robo(client, uma_vez: bool, intervalo: int) -> None:
    from app.ai.analyzer import Analisador
    from app.worker import Robo

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)  # uma linha por chamada é ruído
    settings = get_settings()
    robo = Robo(client, Analisador(), settings)
    print(f"Modelo: {settings.claude_model} · modo {'lote (Batch API)' if robo.modo_lote else 'imediato'}")

    def ciclo() -> None:
        with get_session() as session:
            print(f"Ciclo concluído: {robo.executar_ciclo(session)}")

    if uma_vez:
        ciclo()
        return

    from apscheduler.schedulers.blocking import BlockingScheduler

    agenda = BlockingScheduler()
    agenda.add_job(ciclo, "interval", minutes=intervalo, max_instances=1, coalesce=True)
    print(f"Robô ligado: um ciclo agora e depois a cada {intervalo} min. Ctrl+C para parar.")
    ciclo()
    try:
        agenda.start()
    except (KeyboardInterrupt, SystemExit):
        print("Robô desligado.")


def _listar(client, analisar: bool) -> None:
    settings = get_settings()
    if analisar:
        print(f"Modelo: {settings.claude_model}")
    with get_session() as session:
        lojas = session.scalars(consulta_lojas_operaveis(settings.use_mock_shopee)).all()
        if not lojas:
            print("\nNenhuma loja conectada. Abra o painel (python -m app web) > Conexão Shopee.")
        for loja in lojas:
            chave = "Ligadas" if loja.settings.auto_publicar else "Desligadas"
            comentarios = client.get_unreplied_comments(loja.shopee_shop_id)
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
            raise SystemExit("\nChave da API inválida ou ausente. Preencha ANTHROPIC_API_KEY no .env.")
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
    main()
