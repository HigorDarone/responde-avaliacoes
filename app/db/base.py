"""Conexão com o banco. Funciona com SQLite (MVP) e Postgres."""

from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    if not url.startswith("sqlite"):
        return create_engine(url)

    database = make_url(url).database
    em_memoria = not database or database == ":memory:"
    if not em_memoria:
        Path(database).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        url,
        connect_args={"check_same_thread": False},
        # Em memória, todas as threads (ex.: servidor web nos testes) precisam do mesmo banco.
        poolclass=StaticPool if em_memoria else None,
    )

    # SQLite só respeita chaves estrangeiras com este PRAGMA.
    @event.listens_for(engine, "connect")
    def _ativar_fk(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


@lru_cache
def get_engine() -> Engine:
    return make_engine(get_settings().database_url)


def get_session() -> Session:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)()


def init_db(engine: Engine | None = None) -> None:
    # Importa os modelos para registrá-los no metadata.
    from app.db import models  # noqa: F401

    engine = engine or get_engine()
    Base.metadata.create_all(engine)
    _adicionar_colunas_novas(engine)


def _adicionar_colunas_novas(engine: Engine) -> None:
    """Migração simples para mudanças só de acréscimo: cria colunas novas (opcionais)
    em tabelas que já existem, sem perder dados. Mudanças maiores pedem Alembic.
    """
    inspetor = inspect(engine)
    with engine.begin() as conn:
        for tabela in Base.metadata.sorted_tables:
            existentes = {c["name"] for c in inspetor.get_columns(tabela.name)}
            for coluna in tabela.columns:
                if coluna.name in existentes:
                    continue
                if not coluna.nullable:
                    raise RuntimeError(
                        f"Coluna obrigatória nova {tabela.name}.{coluna.name}: crie uma migração."
                    )
                tipo = coluna.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE {tabela.name} ADD COLUMN "{coluna.name}" {tipo}'))
