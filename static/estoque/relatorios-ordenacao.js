/* Ordenação global feita pelo servidor; aprimoramento progressivo sem bibliotecas. */
(() => {
  'use strict';
  if (!document.getElementById('investigacao-resultados')) return;
  let pending;
  async function navigate(url, column, push) {
    pending?.abort();
    const controller = new AbortController();
    pending = controller;
    const region = document.getElementById('investigacao-resultados');
    const status = document.getElementById('ordenacao-status');
    const expanded = new Set([...region.querySelectorAll('tr[data-registro]')]
      .filter(row => row.querySelector('details[open]')).map(row => row.dataset.registro));
    region.setAttribute('aria-busy', 'true');
    status.textContent = 'Atualizando resultados…';
    try {
      const response = await fetch(url, {signal: controller.signal, credentials: 'same-origin'});
      if (!response.ok) throw new Error('Resposta indisponível');
      const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
      const next = doc.getElementById('investigacao-resultados');
      if (controller.signal.aborted) return;
      if (!next) throw new Error('Resultados indisponíveis');
      for (const row of next.querySelectorAll('tr[data-registro]')) {
        if (expanded.has(row.dataset.registro) && row.querySelector('details')) row.querySelector('details').open = true;
      }
      region.replaceWith(next);
      const params = new URL(url, location.href).searchParams;
      const form = document.getElementById('relatorios-filtros-vendas');
      for (const name of ['ordenar_por', 'ordem_coluna', 'direcao']) {
        form.querySelectorAll(`input[name="${name}"]`).forEach(input => input.remove());
        if (params.has(name)) {
          const input = document.createElement('input');
          input.type = 'hidden'; input.name = name; input.value = params.get(name); form.append(input);
        }
      }
      if (push) history.pushState({investigacaoOrdenacao: true}, '', url);
      const focus = column ? next.querySelector(`[data-coluna="${column}"]`) : next.querySelector('[aria-current]') || next.querySelector('[role="region"]');
      focus?.focus({preventScroll: true});
      status.textContent = 'Resultados atualizados.';
    } catch (error) {
      if (error.name !== 'AbortError') location.assign(url);
    } finally {
      if (pending === controller) document.getElementById('investigacao-resultados')?.removeAttribute('aria-busy');
    }
  }
  document.addEventListener('click', event => {
    const link = event.target.closest('a[data-ordenacao-link]');
    if (!link || !link.closest('#investigacao-resultados') || event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigate(link.href, link.dataset.coluna, true);
  });
  window.addEventListener('popstate', () => navigate(location.href, null, false));
})();
