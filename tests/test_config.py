from app.config import RAIZ_PROJETO, Settings

VARIAVEIS = [
    "ANTHROPIC_API_KEY",
    "CLAUDE_MODEL",
    "CLAUDE_MODEL_DIFICIL",
    "USE_BATCH_API",
    "LIMITE_AVALIACOES_MES",
    "SHOPEE_ENV",
    "SHOPEE_PARTNER_ID",
    "SHOPEE_PARTNER_KEY",
    "SHOPEE_REDIRECT_URL",
    "USE_MOCK_SHOPEE",
    "DATABASE_URL",
]


def _limpar_ambiente(monkeypatch):
    for nome in VARIAVEIS:
        monkeypatch.delenv(nome, raising=False)


def test_valores_padrao(monkeypatch):
    _limpar_ambiente(monkeypatch)
    s = Settings(_env_file=None)
    assert s.claude_model == "claude-haiku-5-5"
    assert s.claude_model_dificil == ""
    assert s.use_batch_api is True
    assert s.limite_avaliacoes_mes == 500
    assert s.shopee_env == "sandbox"
    assert s.use_mock_shopee is True
    assert s.database_url == "sqlite:///./data/app.db"


def test_variaveis_de_ambiente_sobrescrevem(monkeypatch):
    _limpar_ambiente(monkeypatch)
    monkeypatch.setenv("CLAUDE_MODEL", "outro-modelo")
    monkeypatch.setenv("USE_MOCK_SHOPEE", "false")
    monkeypatch.setenv("LIMITE_AVALIACOES_MES", "50")
    s = Settings(_env_file=None)
    assert s.claude_model == "outro-modelo"
    assert s.use_mock_shopee is False
    assert s.limite_avaliacoes_mes == 50


def test_env_example_e_valido(monkeypatch):
    """O .env.example copiado como .env precisa carregar sem erro."""
    _limpar_ambiente(monkeypatch)
    s = Settings(_env_file=RAIZ_PROJETO / ".env.example")
    assert s.shopee_env == "sandbox"  # comentário na mesma linha é ignorado
    assert s.claude_model == "claude-haiku-5-5"
    assert s.use_mock_shopee is True


def test_env_example_lista_todas_as_variaveis():
    linhas = (RAIZ_PROJETO / ".env.example").read_text(encoding="utf-8").splitlines()
    nomes = {linha.split("=", 1)[0] for linha in linhas if "=" in linha}
    assert nomes == set(VARIAVEIS)
