# Responde Avaliações — especificação do projeto

## O que é
Sistema que responde automaticamente avaliações de lojas da Shopee usando IA.
Ele busca as avaliações sem resposta, analisa texto, estrelas, fotos e vídeo com a API do Claude,
classifica cada uma e gera uma resposta no tom da loja. Elogios podem ser publicados
automaticamente; reclamações vão para uma fila de aprovação.

Começa sendo usado em uma loja (a do dono do projeto), mas deve nascer **multi-loja**:
no futuro, outros vendedores vão conectar as próprias lojas.

## Stack
- Python 3.11+
- FastAPI (API + painel web simples com Jinja2 + HTMX)
- SQLAlchemy + SQLite no MVP (manter compatível com Postgres)
- SDK oficial `anthropic` para a IA
- APScheduler para o robô periódico
- `httpx` para chamadas HTTP
- ffmpeg para extrair quadros de vídeos
- pytest para testes
- Configuração via `.env` (python-dotenv / pydantic-settings)

## Estrutura
```
app/
  config.py            # lê .env
  db/models.py         # Shop, Review, ReplyDraft, ShopSettings, ReplyExample, ProductInfo
  shopee/
    client.py          # cliente real da Shopee Open Platform v2 (assinatura HMAC-SHA256, OAuth, tokens)
    mock_client.py     # cliente falso com avaliações de exemplo (usado até a API ser liberada)
    base.py            # interface comum aos dois clientes
  media/
    downloader.py      # baixa fotos/vídeos da avaliação
    frames.py          # extrai 3–4 quadros de cada vídeo com ffmpeg
  ai/
    prompts.py         # prompt de sistema montado a partir das regras da loja
    analyzer.py        # chama o Claude e devolve análise estruturada (JSON)
  worker.py            # ciclo: buscar -> analisar -> decidir -> publicar ou enfileirar
  web/
    routes.py          # painel: fila de aprovação, histórico, configurações da loja
    templates/
tests/
data/mock_reviews.json # avaliações de exemplo (boas, ruins, com foto, com vídeo)
```

## Fluxo do robô (worker)
1. Para cada loja ativa, buscar avaliações sem resposta (Shopee: `get_comment`).
2. Ignorar avaliações já processadas (idempotência pelo id do comentário + shop_id).
3. Baixar mídia; de vídeos, extrair quadros.
4. Enviar ao Claude: texto, estrelas, nome do produto, imagens e regras da loja.
5. Receber JSON validado (pydantic):
   ```json
   {
     "sentimento": "positivo | neutro | negativo",
     "categoria": "elogio | defeito | quebrado_no_envio | atraso | produto_diferente | duvida | outro",
     "gravidade": 1,
     "precisa_revisao": false,
     "resposta": "texto da resposta",
     "motivo": "explicação curta da classificação"
   }
   ```
6. Decidir:
   - Chave **"Respostas automáticas"** da loja DESLIGADA → tudo vai para a fila de aprovação.
   - Chave LIGADA e `positivo` → publicar (Shopee: `reply_comment`).
   - qualquer outro caso → salvar como rascunho na fila de aprovação.
7. Registrar tudo (o que foi publicado, quando, por quem: robô ou humano).

## Regras das respostas (vão no prompt)
- Curtas (1 a 3 frases), naturais, em português do Brasil, sem parecer texto de robô.
- Sem emojis em excesso, sem frases genéricas de "agradecemos imensamente".
- Mencionar algo específico da avaliação (o produto, a foto, o problema citado).
- Em reclamação: reconhecer o problema, pedir desculpas uma vez e indicar o próximo passo
  conforme as regras da loja (ex.: "chama a gente no chat que resolvemos").
- Nunca prometer reembolso, reenvio ou prazo que não esteja nas regras da loja.
- Nunca culpar o cliente nem discutir.
- Conhecimento de impressão 3D: linhas de camada leves são normais do processo;
  peças quebradas, deformadas ou faltando partes são defeito.

## Regras do projeto (obrigatórias)
- **Nunca** publicar resposta a avaliação negativa ou neutra sem aprovação humana no MVP.
- **Nunca** commitar `.env`, chaves ou tokens. Criar `.env.example` com os nomes das variáveis.
- Tudo que é por loja leva `shop_id` (multi-loja desde o início).
- Começar usando `mock_client.py`; o cliente real entra quando houver credenciais.
- Antes de implementar o cliente real, conferir na documentação oficial (open.shopee.com):
  endpoints de avaliação, se a API retorna fotos/vídeos, limite de caracteres da resposta
  e se a resposta pode ser editada depois.
- Modelo do Claude configurável por variável de ambiente (`CLAUDE_MODEL`); conferir os nomes
  de modelo atuais em docs.claude.com.
- Escrever testes para: analisador (com resposta do Claude simulada), regra de decisão,
  idempotência e assinatura da Shopee.

## Custos da IA (padrão do projeto)
- Modelo padrão: Claude Haiku 5.5 (`claude-haiku-5-5`), o mais barato. Modelo maior só se
  configurado em `CLAUDE_MODEL_DIFICIL` para casos marcados como difíceis.
- Usar **prompt caching** no prompt de sistema e nas regras da loja (parte que se repete).
- Processamento não urgente pela **Batch API** (50% mais barato); manter também o modo
  imediato para testes.
- Avaliação 5 estrelas sem texto e sem mídia: resposta pronta (sorteada entre modelos da loja),
  sem chamar a IA.
- Reduzir imagens antes de enviar (lado maior ~1000px) e no máximo 4 quadros por vídeo.
- Registrar tokens de entrada/saída e custo estimado de cada avaliação no banco.
- Limite de avaliações por loja por mês (configurável), pensando nos planos futuros.

## Chave "Respostas automáticas"
- Configuração por loja (`ShopSettings.auto_publicar`), **desligada por padrão** em toda loja nova.
- Desligada: o robô continua buscando e analisando, mas nada é publicado sozinho; tudo cai na fila.
- Ligada: só avaliações positivas são publicadas sozinhas; negativas/neutras continuam na fila.
- Ao ligar, NÃO publicar em massa o que já está na fila; vale só para avaliações novas.
- Interruptor bem visível no topo do painel e da fila (ex.: "Respostas automáticas: Desligadas"),
  com confirmação ao ligar. Desligar é imediato, sem confirmação.
- Registrar no histórico quem ligou/desligou e quando.
- Testes: com a chave desligada, nenhuma chamada a `reply_comment` pode acontecer.

## Personalização das respostas (parte central do produto)
Não há fine-tuning: a personalização é montada no prompt a cada avaliação. Tudo é por loja (`shop_id`).

**1. Regras da loja** (`ShopSettings`)
- Tom (descontraído / neutro / formal), assinatura, uso de emoji (nunca / pouco / à vontade),
  tamanho da resposta (curta / média).
- O que oferecer por categoria (texto livre por categoria: defeito, quebrado_no_envio, atraso,
  produto_diferente, duvida).
- Lista "nunca dizer / nunca prometer" e lista de palavras proibidas.

**2. Exemplos de respostas** (modelo `ReplyExample`)
- Campos: shop_id, categoria, sentimento, texto da avaliação, resposta ideal, origem
  (manual | edicao | marcada_otima), ativo, criado_em.
- A cada avaliação, escolher os 3–4 exemplos mais parecidos (mesma categoria/sentimento primeiro;
  depois similaridade de texto simples, ex. TF-IDF; sem serviço pago de embeddings no MVP).
- Exemplos vão no prompt como pares "avaliação → resposta ideal".

**3. Aprendizado com as edições**
- Ao aprovar uma resposta editada, salvar versão da IA + versão final no `ReplyDraft`
  e criar automaticamente um `ReplyExample` (origem = edicao).
- Botão "Marcar como ótima" na fila e no histórico → vira exemplo (origem = marcada_otima).
- Exemplos podem ser desativados ou excluídos; limite de exemplos ativos por categoria
  (ex.: 30 mais recentes) para o prompt não crescer sem fim.

**4. Informações dos produtos** (modelo `ProductInfo`)
- Por produto (item_id da Shopee): nome, material, tamanho, o que é normal da impressão 3D,
  cuidados de uso, observações. Entra no prompt só o produto da avaliação.

**5. Segurança das respostas**
- Se faltar informação (produto sem descrição, pedido de troca/reembolso, assunto fora das regras),
  a IA não inventa: `precisa_revisao = true` e motivo "precisa de você"; vai para a fila mesmo
  com respostas automáticas ligadas.

**6. Medir a qualidade**
- Taxa de "aprovadas sem edição" por semana e por categoria, mostrada no painel e na tela de
  personalização, com uma dica: "Quando passar de ~90%, considere ligar as respostas automáticas".

**7. Testar antes de salvar**
- Área "Testar": colar uma avaliação de exemplo (e opcionalmente uma foto) e ver a resposta
  gerada com as regras/exemplos atuais, sem publicar nada.

**Tela "Personalização"** (abas): Regras · Exemplos · Produtos · Testar.

## Interface (front-end)
- FastAPI + Jinja2 + HTMX, responsiva (funcionar bem no celular).
- Telas: login/cadastro, conexão Shopee, painel (números do dia, motivos de reclamação,
  produtos com mais problemas), fila de aprovação, histórico, regras da loja.
- Fila de aprovação: card por avaliação com produto, estrelas, texto, miniaturas de foto/vídeo,
  etiqueta da categoria, resposta sugerida editável e botões Aprovar e publicar / Editar / Descartar.
- **Referências visuais**: o dono do projeto vai colocar imagens e links na pasta `referencias/`.
  Antes de começar o passo 5, olhar essa pasta e seguir o estilo dela. Se estiver vazia, perguntar.
- **Construir por etapas** (ver passo 5). Ao terminar cada etapa: parar, explicar como abrir a
  tela no navegador e esperar aprovação antes de seguir para a próxima.

## Variáveis de ambiente (.env.example)
```
ANTHROPIC_API_KEY=
CLAUDE_MODEL=claude-haiku-5-5
CLAUDE_MODEL_DIFICIL=
USE_BATCH_API=true
LIMITE_AVALIACOES_MES=500
SHOPEE_ENV=sandbox          # sandbox | live
SHOPEE_PARTNER_ID=
SHOPEE_PARTNER_KEY=
SHOPEE_REDIRECT_URL=http://localhost:8000/shopee/callback
USE_MOCK_SHOPEE=true
DATABASE_URL=sqlite:///./data/app.db
```

## Ordem de construção
1. Estrutura do projeto, config, modelos do banco, `.env.example`, README.
2. `mock_client.py` + `data/mock_reviews.json` com ~10 avaliações variadas.
3. `ai/analyzer.py` + prompts (regras, exemplos e produto da loja), testado com avaliações mock.
4. `worker.py` com a regra de decisão.
5. Painel web, **por etapas** (parar e esperar aprovação ao fim de cada uma):
   - 5a. Layout base (menu, cores, fontes) seguindo `referencias/` + tela da fila de aprovação
     + interruptor "Respostas automáticas".
   - 5b. Ações da fila: aprovar e publicar, editar resposta, descartar (com HTMX).
   - 5c. Tela de Personalização: aba Regras.
   - 5c2. Abas Exemplos e Produtos + botão "Marcar como ótima" + aprendizado pelas edições.
   - 5c3. Aba Testar.
   - 5d. Histórico.
   - 5e. Painel com números e gráficos (incluindo taxa de aprovadas sem edição).
   - 5f. Login/cadastro e tela de conexão Shopee.
6. Cliente real da Shopee (OAuth, assinatura, tokens com renovação) — quando houver credenciais.
7. Extração de quadros de vídeo.
