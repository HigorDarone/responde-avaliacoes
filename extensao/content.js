// Roda dentro da página "Gerenciar Avaliações" do Seller Center.
// Só faz o que uma pessoa faria na tela: ler as avaliações e preencher/enviar respostas.
// Não usa senha, não chama rotas internas da Shopee e não contorna nenhuma proteção.

(() => {
  if (window.__respondeAvaliacoes) return;
  window.__respondeAvaliacoes = true;

  const PLACEHOLDER_RESPOSTA = "Insira sua resposta.";
  const DATA = /\d{2}\/\d{2}\/\d{4} \d{2}:\d{2}/;
  const dormir = (ms) => new Promise((r) => setTimeout(r, ms));

  async function esperar(condicao, limiteMs = 10000, passoMs = 250) {
    const fim = Date.now() + limiteMs;
    while (Date.now() < fim) {
      const valor = condicao();
      if (valor) return valor;
      await dormir(passoMs);
    }
    return null;
  }

  // --- Leitura dos cartões de avaliação ---

  function cartoes() {
    const marcas = [...document.querySelectorAll("span, div")].filter(
      (e) => e.childElementCount <= 3 && /^ID do pedido/.test(e.textContent.trim())
    );
    const unicos = new Set(marcas.map((m) => m.closest("div.rounded.border-solid")).filter(Boolean));
    return [...unicos];
  }

  function colunas(cartao) {
    const linha = cartao.querySelector("div.divide-x");
    return linha ? [...linha.children] : [];
  }

  function pedidoDe(cartao) {
    const m = cartao.innerText.match(/ID do pedido\s*([A-Z0-9]+)/);
    return m ? m[1] : "";
  }

  function produtoDe(cartao) {
    const col = colunas(cartao)[0];
    if (!col) return { produto: "", variacao: "" };
    const texto = col.innerText.trim();
    const i = texto.indexOf("Variação:");
    const produto = (i >= 0 ? texto.slice(0, i) : texto).replace(/\s+/g, " ").trim();
    const variacao = i >= 0 ? texto.slice(i + "Variação:".length).replace(/\s+/g, " ").trim() : "";
    return { produto, variacao };
  }

  function dourada(svg) {
    return /255,\s*191,\s*0/.test(getComputedStyle(svg).color);
  }

  // As estrelas vêm em duas camadas (cinza por baixo, dourada por cima, cortada na nota).
  // Conta as douradas que ficam realmente visíveis.
  function estrelasDe(col) {
    const svgs = [...col.querySelectorAll("svg")];
    const douradas = svgs.filter(dourada);
    if (douradas.length === svgs.length) return Math.min(5, Math.max(1, douradas.length));
    let visiveis = 0;
    for (const svg of douradas) {
      const r = svg.getBoundingClientRect();
      if (r.width < 1) continue;
      let visivel = true;
      for (let p = svg.parentElement; p && p !== col; p = p.parentElement) {
        if (getComputedStyle(p).overflow === "visible") continue;
        const pr = p.getBoundingClientRect();
        if (pr.width < 1 || pr.right < r.left + r.width / 2) { visivel = false; break; }
      }
      if (visivel) visiveis++;
    }
    return Math.min(5, Math.max(1, visiveis));
  }

  function lerCartao(cartao) {
    const [, col] = colunas(cartao);
    if (!col) return null;
    const { produto, variacao } = produtoDe(cartao);
    const bruto = col.innerText;
    const data = (bruto.match(DATA) || [""])[0];
    const midias = [
      ...[...col.querySelectorAll("img")].map((i) => ({ tipo: "foto", url: i.currentSrc || i.src })),
      ...[...col.querySelectorAll("video")].map((v) => ({ tipo: "video", url: v.currentSrc || v.src })),
    ].filter((m) => /^https?:/.test(m.url));
    return {
      pedido: pedidoDe(cartao),
      produto,
      variacao,
      estrelas: estrelasDe(col),
      texto: bruto.replace(DATA, "").trim(),
      midias,
      data,
    };
  }

  function botaoResponder(cartao) {
    return [...cartao.querySelectorAll("button")].find((b) => b.innerText.trim() === "Responder");
  }

  async function filtrarParaResponder() {
    const filtro = [...document.querySelectorAll("button, div, span")].find(
      (e) => e.childElementCount <= 2 && /^Para Responder/.test(e.textContent.trim())
    );
    if (filtro) {
      filtro.click();
      await dormir(2500);
    }
  }

  async function coletar() {
    if (!location.hostname.startsWith("seller.")) return { erro: "Faça login no Seller Center." };
    await esperar(() => cartoes().length || document.body.innerText.includes("Lista de Avaliação"), 15000);
    await filtrarParaResponder();
    await esperar(() => cartoes().length, 8000);
    const lidas = [];
    for (const cartao of cartoes()) {
      if (!botaoResponder(cartao)) continue; // já respondida
      cartao.scrollIntoView({ block: "center" });
      await dormir(150);
      const a = lerCartao(cartao);
      if (a && a.pedido && a.produto) lidas.push(a);
    }
    window.scrollTo(0, 0);
    return { avaliacoes: lidas };
  }

  // --- Publicação de uma resposta aprovada ---

  function mesmoItem(cartao, envio) {
    if (pedidoDe(cartao) !== envio.pedido) return false;
    const { produto } = produtoDe(cartao);
    return produto.slice(0, 60) === (envio.produto || "").slice(0, 60);
  }

  function mensagemVisivel() {
    const avisos = [...document.querySelectorAll('[class*="toast"], [class*="message"], [role="alert"]')]
      .map((e) => e.innerText.trim())
      .filter(Boolean);
    return avisos.join(" | ").slice(0, 300);
  }

  async function responder(envio) {
    const cartao = cartoes().find((c) => mesmoItem(c, envio));
    if (!cartao) return { ok: false, erro: "Avaliação não encontrada na página." };
    const botao = botaoResponder(cartao);
    if (!botao) return { ok: true, nota: "Já estava respondida." };
    if (!envio.texto || envio.texto.length > 500) return { ok: false, erro: "Texto vazio ou com mais de 500 caracteres." };

    cartao.scrollIntoView({ block: "center" });
    await dormir(300);
    botao.click();

    const caixa = await esperar(() => document.querySelector(`textarea[placeholder="${PLACEHOLDER_RESPOSTA}"]`), 8000);
    if (!caixa) return { ok: false, erro: "A caixa de resposta não abriu." };

    // Caixa controlada pelo React: precisa do setter nativo + evento "input".
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
    caixa.focus();
    setter.call(caixa, envio.texto);
    caixa.dispatchEvent(new Event("input", { bubbles: true }));
    await dormir(600);
    if (caixa.value !== envio.texto) return { ok: false, erro: "Não consegui preencher a caixa de resposta." };

    const form = caixa.closest("form");
    const enviar = form && ([...form.querySelectorAll("button")].find((b) => b.innerText.trim() === "Enviar")
      || form.querySelector('button[type="submit"]'));
    if (!enviar || enviar.disabled) return { ok: false, erro: "Botão Enviar indisponível." };
    enviar.click();

    const fechou = await esperar(() => !document.body.contains(caixa) || caixa.offsetParent === null, 15000);
    if (!fechou) {
      return { ok: false, erro: mensagemVisivel() || "A caixa não fechou depois de enviar." };
    }
    await dormir(2000);
    const ainda = cartoes().find((c) => mesmoItem(c, envio));
    if (ainda && botaoResponder(ainda)) {
      return { ok: false, erro: mensagemVisivel() || "A Shopee não confirmou o envio." };
    }
    return { ok: true };
  }

  chrome.runtime.onMessage.addListener((msg, _remetente, responderMsg) => {
    const tarefas = { coletar: () => coletar(), responder: () => responder(msg.envio), ping: async () => ({ ok: true }) };
    const tarefa = tarefas[msg.tipo];
    if (!tarefa) return false;
    tarefa().then(responderMsg).catch((e) => responderMsg({ ok: false, erro: String(e && e.message || e) }));
    return true; // resposta assíncrona
  });
})();
