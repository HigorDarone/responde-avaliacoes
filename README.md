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
- [x] Passo 4 — robô (worker) com a regra de decisão (`app/worker.py`)
- [ ] Passo 5 — painel web (5a, 5b, 5c e 5c2 feitos: layout, fila com aprovar/editar/descartar,
      interruptor, Personalização › Regras, Exemplos, Produtos, aprendizado pelas edições)
- [x] Passo 6 — cliente real da Shopee (assinatura, OAuth, tokens, avaliações, produtos) —
      falta testar com uma loja de verdade
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
Não salva nem publica nada.

```powershell
python -m app robo --uma-vez     # um ciclo e sai
python -m app robo               # um ciclo agora e depois a cada 30 min (Ctrl+C para parar)
python -m app robo --intervalo 10
```

O robô busca as avaliações sem resposta, analisa, e decide:
- chave "Respostas automáticas" **desligada** (padrão) → tudo vai para a fila de aprovação;
- **ligada** → só avaliações positivas, sem pedido de revisão, são publicadas sozinhas;
  negativas e neutras sempre vão para a fila.

Com `USE_BATCH_API=true` as análises vão pela Batch API (50% mais barata): o resultado
chega em um ciclo seguinte (normalmente em menos de 1 hora). Para ver o resultado na hora,
use `USE_BATCH_API=false` no `.env`. O robô respeita `LIMITE_AVALIACOES_MES` por loja;
o que passar do limite fica esperando o mês seguinte.

```powershell
python -m app web                # painel em http://localhost:8000
```

Fila de aprovação e interruptor "Respostas automáticas" (ligar pede confirmação;
desligar é imediato; tudo fica registrado no histórico). Em cada card:
**Aprovar e publicar** (publica o texto como está no campo), **Editar** (abre o texto;
Salvar guarda sem publicar) e **Descartar** (tira da fila, com Desfazer). A versão da IA e
a versão final ficam guardadas no rascunho.

Em **Personalização › Regras** ficam o tom, o tamanho, o emoji, a assinatura, o que oferecer
em cada tipo de reclamação, o que nunca dizer, as palavras proibidas e as respostas prontas
para 5★ sem comentário. A prévia ao lado mostra exatamente o texto que vai no prompt.

Em **Personalização › Exemplos** ficam os pares "avaliação → resposta ideal" que a IA imita
(3–4 mais parecidos por análise; só os 30 ativos mais recentes de cada categoria). Eles
chegam sozinhos: resposta **editada e publicada** vira exemplo, e o botão **"Marcar como
ótima"** na fila também cria um. Em **Produtos** ficam material, tamanho, o que é normal da
impressão 3D e cuidados de cada anúncio; a tela lista os produtos das avaliações que ainda
não têm informações. Estilo em `referencias/estilo.md`.

## Conectar a Shopee de verdade

1. No `.env`: `USE_MOCK_SHOPEE=false`, `SHOPEE_ENV=live` (ou `sandbox`), `SHOPEE_REGIAO=br`,
   `SHOPEE_PARTNER_ID` e `SHOPEE_PARTNER_KEY` (Console da Open Platform › detalhes do app).
2. No Console, o **Redirect URL Domain** do app precisa aceitar o endereço de
   `SHOPEE_REDIRECT_URL` (padrão `http://localhost:8000/shopee/callback`).
3. `python -m app web` › **Conexão Shopee** › **Conectar loja Shopee** › entre com a conta da
   loja na página da Shopee e autorize.
4. Os tokens ficam na tabela de lojas e são renovados sozinhos (access_token vale 4 h,
   refresh_token 30 dias; a autorização dura até 365 dias).

Limites da API (documentação oficial): resposta de 1 a 500 caracteres e **não pode ser
editada** depois de publicada. Em **Personalização › Produtos**, "Importar da Shopee" traz
nome, material e descrição dos anúncios sem apagar o que você escreveu.

> Colunas novas e opcionais são criadas sozinhas ao iniciar (sem perder dados).
> Mudanças maiores no banco vão pedir migrações (Alembic).

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
  worker.py          # robô: buscar -> analisar -> decidir -> publicar ou enfileirar
  web/
    main.py          # cria o app FastAPI
    routes.py        # fila de aprovação, interruptor
    personalizacao.py # tela Personalização (abas Regras, Exemplos, Produtos)
    templates/       # Jinja2 + HTMX
    static/app.css   # estilo (tokens de referencias/estilo.md)
referencias/         # referência visual do painel
  shopee/
    base.py          # interface comum (ShopeeClient, ShopeeComment)
    mock_client.py   # cliente falso, lê data/mock_reviews.json
data/mock_reviews.json
tests/
```
