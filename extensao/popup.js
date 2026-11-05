const ultima = document.getElementById("ultima");
const botao = document.getElementById("rodar");

function mostrar(registro) {
  if (!registro) return;
  ultima.textContent = `${registro.quando}: ${registro.mensagem}`;
  ultima.className = registro.ok ? "" : "erro";
}

document.getElementById("versao").textContent = "v" + chrome.runtime.getManifest().version;

chrome.storage.local.get("ultimaPassada").then(({ ultimaPassada }) => mostrar(ultimaPassada));

botao.addEventListener("click", async () => {
  botao.disabled = true;
  botao.textContent = "Rodando… (pode levar 1–2 min)";
  const registro = await chrome.runtime.sendMessage({ tipo: "rodarAgora" });
  mostrar(registro);
  botao.disabled = false;
  botao.textContent = "Rodar agora";
});
