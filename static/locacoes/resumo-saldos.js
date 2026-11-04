(function () {
  const dialog = document.getElementById('modal-recebimento');
  if (!dialog) return;
  const content = document.getElementById('conteudo-recebimento');
  const status = document.getElementById('status-saldos');
  const modalStatus = document.getElementById('status-recebimento');
  let paying = false;
  let origin;
  let requestId = 0;
  let modalRequest = 0;
  const headers = {'X-Requested-With': 'XMLHttpRequest'};
  function url(fragment, category, page) {
    const target = new URL(location.href);
    target.searchParams.set(fragment, '1');
    target.searchParams.set('saldo_categoria', category);
    target.searchParams.set('saldo_pagina', page);
    return target;
  }
  function replace(selector, html) {
    const documentFragment = new DOMParser().parseFromString(html, 'text/html');
    document.querySelector(selector).replaceWith(documentFragment.querySelector(selector));
  }
  async function read(response) {
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.erro || data.mensagem || 'Não foi possível concluir. Verifique sua conexão e tente novamente.');
    }
    return response;
  }
  async function refresh(category, page, full = false) {
    const id = ++requestId;
    status.textContent = 'Atualizando saldos…';
    const response = await read(await fetch(url(full ? 'fragmento_financeiro' : 'fragmento_saldos', category, page), {headers, cache: 'no-store'}));
    const data = await response.json();
    if (id !== requestId) return;
    replace('#saldos-consulta', full ? data.saldos : data.html);
    if (full) replace('#resumo-valores', data.periodo);
    status.textContent = '';
  }
  function refreshError(message) {
    status.textContent = message + ' ';
    const retry = document.createElement('button');
    retry.type = 'button'; retry.dataset.atualizarFinanceiro = ''; retry.textContent = 'Atualizar saldos';
    status.appendChild(retry);
  }
  async function history(details, page = 1) {
    if (!details || details.dataset.loading === 'true') return;
    const target = new URL(details.dataset.historicoUrl, location.href);
    target.searchParams.set('pagina', page);
    const container = details.querySelector('[data-conteudo-historico]');
    details.dataset.loading = 'true';
    container.textContent = 'Carregando pagamentos…';
    try {
      const response = await read(await fetch(target, {headers, cache: 'no-store'}));
      if (!details.isConnected || !dialog.open) return;
      container.innerHTML = await response.text();
      details.dataset.loaded = 'true';
    } catch (error) {
      if (details.isConnected) container.textContent = error.message + ' Feche e abra o histórico para tentar novamente.';
    } finally { details.dataset.loading = 'false'; }
  }
  document.addEventListener('toggle', event => {
    const details = event.target;
    if (details.matches?.('details[data-historico-url]') && details.open && details.dataset.loaded !== 'true') history(details);
  }, true);
  document.addEventListener('click', async function (event) {
    const button = event.target.closest('button');
    if (!button) return;
    const panel = document.getElementById('saldos-consulta');
    if (button.hasAttribute('data-historico-pagina')) {
      history(button.closest('details'), button.dataset.historicoPagina);
    } else if (button.hasAttribute('data-fechar-recebimento')) {
      if (!paying) dialog.close();
    } else if (button.hasAttribute('data-saldo-categoria') || button.hasAttribute('data-saldo-pagina') || button.hasAttribute('data-atualizar-financeiro')) {
      try {
        const category = button.dataset.saldoCategoria || panel.dataset.categoria;
        const page = button.hasAttribute('data-saldo-categoria') ? 1 : button.dataset.saldoPagina || panel.dataset.pagina;
        await refresh(category, page, button.hasAttribute('data-atualizar-financeiro'));
        document.querySelector('[data-saldo-categoria="' + category + '"]')?.focus({preventScroll: true});
      } catch (error) { refreshError(error.message); }
    } else if (button.dataset.receberLocacao) {
      const id = ++modalRequest;
      origin = button;
      status.textContent = '';
      dialog.showModal();
      content.textContent = 'Carregando recebimento…'; modalStatus.textContent = '';
      try {
        const response = await read(await fetch(button.dataset.receberLocacao, {headers, cache: 'no-store'}));
        if (id !== modalRequest || !dialog.open) return;
        content.innerHTML = await response.text();
        content.querySelector('[name=valor]')?.focus();
        content.querySelector('[name=valor]')?.select();
      } catch (error) { if (id === modalRequest && dialog.open) content.textContent = error.message; }
    }
  });
  dialog.addEventListener('cancel', event => { if (paying) event.preventDefault(); });
  dialog.addEventListener('close', () => { ++modalRequest; origin?.focus({preventScroll: true}); });
  document.addEventListener('submit', async function (event) {
    const form = event.target;
    if (form.id !== 'form-recebimento-resumo') return;
    event.preventDefault();
    if (paying) return;
    paying = true;
    const submit = form.querySelector('[type=submit]');
    submit.disabled = true; modalStatus.textContent = 'Confirmando recebimento…';
    try {
      // Keep this form's operation token for retries, including lost responses.
      const csrfName = (form.dataset.csrfCookie || 'csrftoken') + '=';
      const csrf = document.cookie.split('; ').find(value => value.startsWith(csrfName));
      const response = await fetch(form.action, {method: 'POST', headers: {...headers,
        'X-CSRFToken': csrf ? decodeURIComponent(csrf.slice(csrfName.length)) : form.querySelector('[name=csrfmiddlewaretoken]').value},
        credentials: 'same-origin', body: new FormData(form)});
      if (response.status === 422) {
        content.innerHTML = await response.text();
        modalStatus.textContent = 'Revise o pagamento antes de confirmar.';
        const invalid = content.querySelector('[aria-invalid="true"]') || content.querySelector('[name=valor]');
        invalid?.parentElement.scrollIntoView({block: 'center'});
        invalid?.focus({preventScroll: true});
        return;
      }
      await read(response);
      const result = await response.json();
      if (!result.ok) throw new Error('O servidor não confirmou o pagamento. Tente novamente neste mesmo formulário.');
      dialog.close();
      const panel = document.getElementById('saldos-consulta');
      try {
        await refresh(panel.dataset.categoria, panel.dataset.pagina, true);
        status.textContent = 'Recebimento confirmado. Saldos atualizados. ';
        const receipt = document.createElement('a');
        receipt.href = result.recibo_url; receipt.target = '_blank'; receipt.rel = 'noopener'; receipt.textContent = 'Ver recibo';
        status.appendChild(receipt);
        document.querySelector('[data-saldo-categoria="' + panel.dataset.categoria + '"]')?.focus({preventScroll: true});
      } catch (error) { refreshError('Recebimento confirmado. Não foi possível atualizar a consulta; não repita o pagamento.'); }
    } catch (error) {
      modalStatus.textContent = error.message + ' Se necessário, tente novamente neste mesmo formulário para evitar duplicidade.';
    } finally {
      paying = false;
      if (submit.isConnected) submit.disabled = false;
    }
  });
})();
