// Agenda as passadas e conversa com o painel. Cada passada:
// 1) abre/atualiza uma aba do Seller Center (em segundo plano) e lê as avaliações "Para Responder";
// 2) manda para o painel, que analisa com a IA e decide (elogio + chave ligada = publicar);
// 3) busca as respostas aprovadas e publica uma por vez, devagar, confirmando cada uma.

// Já abre filtrado em "Para Responder", com todas as estrelas.
const URL_AVALIACOES =
  "https://seller.shopee.com.br/portal/settings/shop/rating?pageNumber=1&fromPageNumber=1&cursor=0" +
  "&pageSize=20&replied=TO_REPLY&ratingStar=ALL&ratingStar=5&ratingStar=4&ratingStar=3&ratingStar=2&ratingStar=1";
// Já respondidas, só 1 a 4 estrelas (para o relatório "O que melhorar").
const URL_HISTORICO =
  "https://seller.shopee.com.br/portal/settings/shop/rating?pageNumber=1&fromPageNumber=1&cursor=0" +
  "&pageSize=20&replied=REPLIED&ratingStar=4&ratingStar=3&ratingStar=2&ratingStar=1";
const INTERVALO_HISTORICO_MS = 12 * 60 * 60 * 1000;
const PADRAO ={ painel: "http://localhost:8000", chave: "", ativo: true, intervalo: 30 };
const PAUSA_ENTRE_ENVIOS_MS = 12000;
const ESPERA_ANALISE_MS = 45000;
const PAUSA_ENTRE_PAGINAS_MS = 1500; // + até 1,5 s aleatório, no ritmo de uma pessoa
const MAX_PAGINAS = 50; // até 1.000 avaliações por passada

const dormir = (ms) => new Promise((r) => setTimeout(r, ms));
let rodando = false;

async function config() {
  return chrome.storage.sync.get(PADRAO);
}

async function agendar() {
  const { intervalo } = await config();
  await chrome.alarms.clear("ciclo");
  chrome.alarms.create("ciclo", { periodInMinutes: Math.max(10, Number(intervalo) || 30) });
}

chrome.runtime.onInstalled.addListener(agendar);
chrome.runtime.onStartup.addListener(agendar);
chrome.alarms.onAlarm.addListener((a) => { if (a.name === "ciclo") ciclo(); });

chrome.runtime.onMessage.addListener((msg, _remetente, responder) => {
  // "Rodar agora" também confere o histórico (rápido: para na 1ª página sem novidade).
  if (msg.tipo === "rodarAgora") { ciclo(true).then(responder); return true; }
  if (msg.tipo === "reagendar") { agendar().then(() => responder({ ok: true })); return true; }
  return false;
});

async function registrar(mensagem, ok = true) {
  const registro = { quando: new Date().toLocaleString("pt-BR"), mensagem, ok };
  await chrome.storage.local.set({ ultimaPassada: registro });
  chrome.action.setBadgeText({ text: ok ? "" : "!" });
  chrome.action.setBadgeBackgroundColor({ color: "#fa6838" });
  return registro;
}

function clienteDoPainel(cfg) {
  return async (caminho, opcoes = {}) => {
    const r = await fetch(cfg.painel.replace(/\/$/, "") + caminho, {
      ...opcoes,
      headers: { "Content-Type": "application/json", "X-Chave-Extensao": cfg.chave },
    });
    if (!r.ok) throw new Error(`Painel respondeu ${r.status}: ${(await r.text()).slice(0, 200)}`);
    return r.json();
  };
}

async function esperarCarregar(tabId) {
  for (let i = 0; i < 60; i++) {
    const tab = await chrome.tabs.get(tabId);
    if (tab.status === "complete") return tab;
    await dormir(500);
  }
  return chrome.tabs.get(tabId);
}

async function abaDoSellerCenter(url = URL_AVALIACOES) {
  const { abaId } = await chrome.storage.local.get("abaId");
  let tab = null;
  if (abaId) tab = await chrome.tabs.get(abaId).catch(() => null);
  if (tab) {
    await chrome.tabs.update(tab.id, { url });
  } else {
    tab = await chrome.tabs.create({ url, active: false, pinned: true });
    await chrome.storage.local.set({ abaId: tab.id });
  }
  tab = await esperarCarregar(tab.id);
  await dormir(3000); // a lista é montada depois do carregamento
  return tab;
}

async function mandar(tabId, mensagem) {
  try {
    return await chrome.tabs.sendMessage(tabId, mensagem);
  } catch {
    // O script da página ainda não estava lá: injeta e tenta de novo.
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
    await dormir(500);
    return chrome.tabs.sendMessage(tabId, mensagem);
  }
}

// Lê página por página (20 por vez), mandando cada uma ao painel assim que lê.
// pararSemNovas: para na primeira página sem nada novo (a lista vem das mais recentes).
async function lerPaginas(api, tabId, rota, { historico = false, pararSemNovas = false } = {}) {
  const r = { lidas: 0, novas: 0, paginas: 0, erro: "" };
  for (;;) {
    const pagina = await mandar(tabId, { tipo: "coletar", historico });
    if (!pagina || pagina.erro) {
      if (r.paginas === 0) r.erro = pagina ? pagina.erro : "Não consegui ler a página.";
      return r; // se já leu algumas, segue com o que tem
    }
    r.paginas++;
    let novasDaPagina = 0;
    if (pagina.avaliacoes.length) {
      const resp = await api(rota, { method: "POST", body: JSON.stringify({ avaliacoes: pagina.avaliacoes }) });
      r.lidas += resp.recebidas;
      r.novas += resp.novas;
      novasDaPagina = resp.novas;
    }
    if (!pagina.temProxima || r.paginas >= MAX_PAGINAS) return r;
    if (pararSemNovas && novasDaPagina === 0) return r;
    await dormir(PAUSA_ENTRE_PAGINAS_MS + Math.random() * 1500);
    const prox = await mandar(tabId, { tipo: "proxima" });
    if (!prox || !prox.ok) return r;
  }
}

// Avaliações de 1 a 4 estrelas já respondidas: entram só no relatório, nunca na fila.
// A primeira vez lê tudo; depois, no máximo uma vez a cada 12 h, só até achar uma página sem novidade.
async function lerHistorico(api, forcar) {
  const { historicoEm = 0, historicoCompleto = false } = await chrome.storage.local.get(["historicoEm", "historicoCompleto"]);
  if (!forcar && Date.now() - historicoEm < INTERVALO_HISTORICO_MS) return null;
  const tab = await abaDoSellerCenter(URL_HISTORICO);
  const r = await lerPaginas(api, tab.id, "/api/extensao/historico", {
    historico: true,
    pararSemNovas: historicoCompleto,
  });
  if (!r.erro) await chrome.storage.local.set({ historicoEm: Date.now(), historicoCompleto: true });
  return r;
}

async function ciclo(forcarHistorico = false) {
  if (rodando) return { ok: false, mensagem: "Já está rodando." };
  rodando = true;
  try {
    const cfg = await config();
    if (!cfg.chave) return registrar("Falta a chave da extensão (abra as opções).", false);
    if (!cfg.ativo) return registrar("Extensão pausada nas opções.", false);
    const api = clienteDoPainel(cfg);
    await api("/api/extensao/status");

    const tab = await abaDoSellerCenter();
    if (!tab.url || !tab.url.startsWith("https://seller.shopee.com.br/")) {
      return registrar("Faça login no Seller Center (a aba fixada da extensão abriu a tela de login).", false);
    }

    const lidas = await lerPaginas(api, tab.id, "/api/extensao/avaliacoes");
    if (lidas.erro) return registrar(lidas.erro, false);
    if (lidas.novas) await dormir(ESPERA_ANALISE_MS); // tempo para a IA analisar as novas

    const { envios } = await api("/api/extensao/envios");
    let publicadas = 0;
    let falhas = 0;
    for (const envio of envios) {
      const r = (await mandar(tab.id, { tipo: "responder", envio, maxPaginas: MAX_PAGINAS }))
        || { ok: false, erro: "Sem resposta da página." };
      await api(`/api/extensao/envios/${envio.id}`, {
        method: "POST",
        body: JSON.stringify({ ok: !!r.ok, erro: r.erro || "" }),
      });
      r.ok ? publicadas++ : falhas++;
      await dormir(PAUSA_ENTRE_ENVIOS_MS);
    }

    let historico = "";
    try {
      const h = await lerHistorico(api, forcarHistorico);
      if (h) historico = h.erro ? ` Histórico: ${h.erro}` : ` Histórico (já respondidas 1–4★): ${h.novas} novas.`;
    } catch (e) {
      historico = ` Histórico: erro ${e.message}`;
    }
    return registrar(
      `${lidas.lidas} lidas em ${lidas.paginas} página(s), ${lidas.novas} novas, ${publicadas} publicadas` +
        (falhas ? `, ${falhas} com falha (veja o painel)` : "") + "." + historico,
      falhas === 0
    );
  } catch (e) {
    return registrar(`Erro: ${e.message}`, false);
  } finally {
    rodando = false;
  }
}
