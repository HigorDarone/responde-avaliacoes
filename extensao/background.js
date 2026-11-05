// Agenda as passadas e conversa com o painel. Cada passada:
// 1) abre/atualiza uma aba do Seller Center (em segundo plano) e lê as avaliações "Para Responder";
// 2) manda para o painel, que analisa com a IA e decide (elogio + chave ligada = publicar);
// 3) busca as respostas aprovadas e publica uma por vez, devagar, confirmando cada uma.

// Já abre filtrado em "Para Responder", com todas as estrelas.
const URL_AVALIACOES =
  "https://seller.shopee.com.br/portal/settings/shop/rating?pageNumber=1&fromPageNumber=1&cursor=0" +
  "&pageSize=20&replied=TO_REPLY&ratingStar=ALL&ratingStar=5&ratingStar=4&ratingStar=3&ratingStar=2&ratingStar=1";
const PADRAO = { painel: "http://localhost:8000", chave: "", ativo: true, intervalo: 30 };
const PAUSA_ENTRE_ENVIOS_MS = 12000;
const ESPERA_ANALISE_MS = 45000;

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
  if (msg.tipo === "rodarAgora") { ciclo().then(responder); return true; }
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

async function abaDoSellerCenter() {
  const { abaId } = await chrome.storage.local.get("abaId");
  let tab = null;
  if (abaId) tab = await chrome.tabs.get(abaId).catch(() => null);
  if (tab) {
    await chrome.tabs.update(tab.id, { url: URL_AVALIACOES });
  } else {
    tab = await chrome.tabs.create({ url: URL_AVALIACOES, active: false, pinned: true });
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

async function ciclo() {
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

    const lidas = await mandar(tab.id, { tipo: "coletar" });
    if (!lidas || lidas.erro) return registrar(lidas ? lidas.erro : "Não consegui ler a página.", false);
    const recebidas = await api("/api/extensao/avaliacoes", {
      method: "POST",
      body: JSON.stringify({ avaliacoes: lidas.avaliacoes }),
    });
    if (recebidas.novas) await dormir(ESPERA_ANALISE_MS); // tempo para a IA analisar as novas

    const { envios } = await api("/api/extensao/envios");
    let publicadas = 0;
    let falhas = 0;
    for (const envio of envios) {
      const r = (await mandar(tab.id, { tipo: "responder", envio })) || { ok: false, erro: "Sem resposta da página." };
      await api(`/api/extensao/envios/${envio.id}`, {
        method: "POST",
        body: JSON.stringify({ ok: !!r.ok, erro: r.erro || "" }),
      });
      r.ok ? publicadas++ : falhas++;
      await dormir(PAUSA_ENTRE_ENVIOS_MS);
    }
    return registrar(
      `${lidas.avaliacoes.length} lidas, ${recebidas.novas} novas, ${publicadas} publicadas` +
        (falhas ? `, ${falhas} com falha (veja o painel)` : ""),
      falhas === 0
    );
  } catch (e) {
    return registrar(`Erro: ${e.message}`, false);
  } finally {
    rodando = false;
  }
}
