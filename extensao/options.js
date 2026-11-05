const PADRAO = { painel: "http://localhost:8000", chave: "", ativo: true, intervalo: 30 };
const campo = (id) => document.getElementById(id);

chrome.storage.sync.get(PADRAO).then((cfg) => {
  campo("painel").value = cfg.painel;
  campo("chave").value = cfg.chave;
  campo("intervalo").value = cfg.intervalo;
  campo("ativo").checked = cfg.ativo;
});

campo("salvar").addEventListener("click", async () => {
  const painel = campo("painel").value.trim().replace(/\/$/, "");
  let origem;
  try {
    origem = new URL(painel).origin;
  } catch {
    campo("status").textContent = "Endereço do painel inválido.";
    return;
  }
  // Painel fora do localhost: pede permissão só para esse endereço.
  if (!/^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(origem)) {
    const ok = await chrome.permissions.request({ origins: [origem + "/*"] });
    if (!ok) {
      campo("status").textContent = "Sem permissão para acessar esse endereço.";
      return;
    }
  }
  await chrome.storage.sync.set({
    painel,
    chave: campo("chave").value.trim(),
    intervalo: Math.max(10, Number(campo("intervalo").value) || 30),
    ativo: campo("ativo").checked,
  });
  await chrome.runtime.sendMessage({ tipo: "reagendar" });
  campo("status").textContent = "Salvo ✓";
});
