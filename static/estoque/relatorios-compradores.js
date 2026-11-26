document.addEventListener("click", async function (evento) {
  const botao = evento.target.closest("[data-comprador-expandir]");
  const paginacao = evento.target.closest("[data-comprador-pagina]");
  if (!botao && !paginacao) return;

  if (paginacao) evento.preventDefault();

  const linha = (botao || paginacao).closest("tr");
  const linhaDetalhe = botao
    ? linha.nextElementSibling
    : linha.closest(".comprador-notas-linha");
  const conteudo = linhaDetalhe?.querySelector("[data-comprador-conteudo]");
  const controle = botao || linhaDetalhe?.previousElementSibling
    ?.querySelector("[data-comprador-expandir]");

  if (!linhaDetalhe || !conteudo || !controle) return;

  if (botao && !linhaDetalhe.hidden) {
    linhaDetalhe.hidden = true;
    controle.setAttribute("aria-expanded", "false");
    controle.querySelector("[data-seta]").textContent = "?";
    return;
  }

  if (controle.dataset.carregando === "1") return;

  const url = paginacao ? paginacao.href : controle.dataset.url;
  if (!url) return;

  linhaDetalhe.hidden = false;
  controle.setAttribute("aria-expanded", "true");
  controle.querySelector("[data-seta]").textContent = "?";

  if (!paginacao && conteudo.dataset.carregado === "1") return;

  controle.dataset.carregando = "1";
  conteudo.textContent = "Carregando notas...";

  try {
    const resposta = await fetch(url, {
      credentials: "same-origin",
      headers: {"X-Requested-With": "XMLHttpRequest"}
    });
    if (!resposta.ok) throw new Error("HTTP " + resposta.status);
    const html = await resposta.text();
    const documento = new DOMParser().parseFromString(html, "text/html");

    if (!documento.querySelector(".relatorios-tabela")) {
      throw new Error("Resposta inesperada");
    }

    conteudo.replaceChildren(...Array.from(documento.body.childNodes));
    conteudo.dataset.carregado = "1";
  } catch (erro) {
    conteudo.textContent = "Nao foi possivel carregar as notas. Clique novamente.";
    conteudo.dataset.carregado = "0";
  } finally {
    delete controle.dataset.carregando;
  }
});
