"""`python -m app`: cria o banco e mostra as avaliações sem resposta do cliente atual.

Por enquanto (passos 1 e 2) só lista; a análise e a publicação vêm nos próximos passos.
"""

from app.config import get_settings
from app.db.base import get_session, init_db
from app.db.repo import buscar_loja_por_shopee_id, criar_loja
from app.shopee import get_shopee_client
from app.shopee.mock_client import MockShopeeClient


def main() -> None:
    settings = get_settings()
    init_db()
    client = get_shopee_client(settings)
    print(f"Banco: {settings.database_url}")
    print(f"Shopee: {'mock' if settings.use_mock_shopee else settings.shopee_env}")

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
            for c in comentarios:
                midia = f" [{', '.join(m.tipo for m in c.midias)}]" if c.midias else ""
                texto = c.texto or "(sem texto)"
                print(f"  {'★' * c.estrelas:<5} {c.produto_nome}{midia}")
                print(f"        {texto}")


if __name__ == "__main__":
    main()
