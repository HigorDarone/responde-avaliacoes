# Responde Avaliações

Sistema que responde automaticamente avaliações de lojas da Shopee usando IA (Claude).
Busca as avaliações sem resposta, analisa texto, estrelas, fotos e vídeo, classifica e gera
uma resposta no tom da loja. Elogios podem ser publicados sozinhos (se a chave
"Respostas automáticas" estiver ligada); reclamações vão para uma fila de aprovação.

A especificação completa está em [CLAUDE.md](CLAUDE.md).

## Estado atual

- [x] Passo 1 — estrutura, configuração, modelos do banco, `.env.example`
- [x] Passo 2 — cliente falso da Shopee (`app/shopee/mock_client.py`) + `data/mock_reviews.json`
- [x] Passo 3 — análise com o Claude (`app/ai/`)
- [ ] Passo 4 — robô (worker) com a regra de decisão
- [ ] Passo 5 — painel web
- [ ] Passo 6 — cliente real da Shopee
- [ ] Passo 7 — quadros de vídeo

## Requisitos

- Python 3.11 ou mais novo
- (a partir do passo 7) ffmpeg

## Instalação (Windows / PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
```

No Linux/macOS: `source .venv/bin/activate` e `cp .env.example .env`.

Depois edite o `.env` (a chave `ANTHROPIC_API_KEY` só é necessária a partir do passo 3).
**Nunca** commite o `.env`.

## Executar

Rode os comandos a partir da pasta do projeto, com o ambiente ativado:

```powershell
python -m app
```

Cria o banco (`data/app.db`), cadastra a loja de exemplo do mock e lista as avaliações
sem resposta.

```powershell
python -m app analisar
```

Analisa cada avaliação do mock com o Claude (modo imediato) e mostra classificação,
resposta sugerida e custo estimado. Precisa de `ANTHROPIC_API_KEY` no `.env`.
Não salva nem publica nada — isso vem no passo 4 (robô).

## Testes

```powershell
pytest
```

## Estrutura

```
app/
  config.py          # lê o .env
  __main__.py        # python -m app
  db/
    base.py          # conexão (SQLite no MVP, compatível com Postgres)
    models.py        # Shop, ShopSettings, Review, ReplyDraft, ReplyExample, ProductInfo, AuditLog
    repo.py          # criar_loja etc.
  ai/
    prompts.py       # prompt: regras gerais + regras da loja (com cache) + exemplos + produto
    analyzer.py      # chama o Claude (imediato ou Batch API), valida o JSON, resposta pronta 5★
    contexto.py      # carrega regras, exemplos e produto da loja do banco
    exemplos.py      # escolhe os exemplos mais parecidos (TF-IDF simples)
    custos.py        # tokens e custo estimado
  shopee/
    base.py          # interface comum (ShopeeClient, ShopeeComment)
    mock_client.py   # cliente falso, lê data/mock_reviews.json
data/mock_reviews.json
tests/
```
