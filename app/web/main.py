from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.web import extensao, personalizacao, shopee
from app.web.routes import router


def create_app() -> FastAPI:
    app = FastAPI(title="Responde Avaliações", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.include_router(router)
    app.include_router(personalizacao.router)
    app.include_router(shopee.router)
    app.include_router(extensao.router)
    return app
