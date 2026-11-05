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
  let newReceiptDraft = null;
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
    document.querySelector('#saldos-consulta h2').insertAdjacentElement('afterend', status);
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
  function showMode(mode) {
    const selecting = mode === 'corrigir';
    const form = content.querySelector('#form-recebimento-resumo');
    const selection = content.querySelector('[data-selecao-pagamentos]');
    if (!form || !selection) return;
    form.hidden = selecting;
    selection.hidden = !selecting;
    const back = content.querySelector('[data-voltar-formulario]');
    if (back) back.hidden = selecting;
    content.querySelector('#titulo-recebimento').textContent = selecting ? 'Corrigir pagamento' : 'Receber / Quitar';
    modalStatus.textContent = '';
    dialog.scrollTop = 0;
    if (selecting && selection.dataset.loaded !== 'true') history(selection);
  }
  async function loadForm(target, button, editing = false) {
    const id = ++modalRequest;
    const alreadyOpen = dialog.open;
    if (!alreadyOpen) {
      origin = button;
      newReceiptDraft = null;
      status.textContent = '';
      dialog.showModal();
      content.textContent = 'Carregando recebimento…';
    }
    modalStatus.textContent = alreadyOpen ? 'Carregando recebimento…' : '';
    try {
      const response = await read(await fetch(target, {headers, cache: 'no-store'}));
      const html = await response.text();
      if (id !== modalRequest || !dialog.open) return;
      const currentForm = content.querySelector('#form-recebimento-resumo');
      if (editing && currentForm && !currentForm.hasAttribute('data-correcao-pagamento') && !newReceiptDraft) {
        // Preserve typed values and the original operation token only in memory,
        // until this modal closes. Switching mode must not create a new receipt.
        newReceiptDraft = document.createDocumentFragment();
        while (content.firstChild) newReceiptDraft.appendChild(content.firstChild);
      }
      content.innerHTML = html;
      modalStatus.textContent = '';
      dialog.scrollTop = 0;
      content.querySelector('[name=valor]')?.focus({preventScroll: true});
      content.querySelector('[name=valor]')?.select();
      if (button.hasAttribute('data-abrir-historico')) showMode('corrigir');
    } catch (error) {
      if (id === modalRequest && dialog.open) {
        if (alreadyOpen) modalStatus.textContent = error.message;
        else content.textContent = error.message;
      }
    }
  }
  document.addEventListener('toggle', event => {
    const details = event.target;
    if (details.matches?.('details[data-historico-url]') && details.open && details.dataset.loaded !== 'true') history(details);
  }, true);
  document.addEventListener('click', async function (event) {
    const button = event.target.closest('button');
    if (!button) return;
    const panel = document.getElementById('saldos-consulta');
    if (button.hasAttribute('data-modo-recebimento')) {
      if (paying) return;
      ++modalRequest;
      if (button.dataset.modoRecebimento === 'novo') {
        if (newReceiptDraft) {
          content.replaceChildren(newReceiptDraft);
          newReceiptDraft = null;
        } else if (content.querySelector('[data-correcao-pagamento]')) {
          await loadForm(button.dataset.novoRecebimentoUrl, button);
        }
      }
      showMode(button.dataset.modoRecebimento);
    } else if (button.hasAttribute('data-historico-pagina')) {
      history(button.closest('[data-historico-url]'), button.dataset.historicoPagina);
    } else if (button.hasAttribute('data-fechar-recebimento')) {
      if (!paying) dialog.close();
    } else if (button.hasAttribute('data-saldo-categoria') || button.hasAttribute('data-saldo-pagina') || button.hasAttribute('data-atualizar-financeiro')) {
      try {
        const category = button.dataset.saldoCategoria || panel.dataset.categoria;
        const page = button.hasAttribute('data-saldo-categoria') ? 1 : button.dataset.saldoPagina || panel.dataset.pagina;
        await refresh(category, page, button.hasAttribute('data-atualizar-financeiro'));
        document.querySelector('[data-saldo-categoria="' + category + '"]')?.focus({preventScroll: true});
      } catch (error) { refreshError(error.message); }
    } else if (button.dataset.receberLocacao || button.dataset.editarPagamento) {
      if (paying) return;
      await loadForm(button.dataset.editarPagamento || button.dataset.receberLocacao, button, !!button.dataset.editarPagamento);
    }
  });
  dialog.addEventListener('cancel', event => { if (paying) event.preventDefault(); });
  dialog.addEventListener('close', () => { ++modalRequest; newReceiptDraft = null; origin?.focus({preventScroll: true}); });
  document.addEventListener('submit', async function (event) {
    const form = event.target;
    if (form.id !== 'form-recebimento-resumo') return;
    event.preventDefault();
    if (paying) return;
    paying = true;
    const correcting = form.hasAttribute('data-correcao-pagamento');
    const submit = form.querySelector('[type=submit]');
    submit.disabled = true; modalStatus.textContent = correcting ? 'Salvando correção…' : 'Confirmando recebimento…';
    try {
      // Keep this form's operation token for retries, including lost responses.
      const csrfName = (form.dataset.csrfCookie || 'csrftoken') + '=';
      const csrf = document.cookie.split('; ').find(value => value.startsWith(csrfName));
      const response = await fetch(form.action, {method: 'POST', headers: {...headers,
        'X-CSRFToken': csrf ? decodeURIComponent(csrf.slice(csrfName.length)) : form.querySelector('[name=csrfmiddlewaretoken]').value},
        credentials: 'same-origin', body: new FormData(form)});
      if (response.status === 422) {
        content.innerHTML = await response.text();
        modalStatus.textContent = correcting ? 'Revise a correção antes de salvar.' : 'Revise o pagamento antes de confirmar.';
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
        status.textContent = correcting ? 'Correção confirmada. Saldos atualizados. ' : 'Recebimento confirmado. Saldos atualizados. ';
        const receipt = document.createElement('a');
        receipt.href = result.recibo_url; receipt.target = '_blank'; receipt.rel = 'noopener'; receipt.textContent = 'Ver recibo';
        status.appendChild(receipt);
        const currentPanel = document.getElementById('saldos-consulta');
        document.querySelector('[data-saldo-categoria="' + currentPanel.dataset.categoria + '"]')?.focus({preventScroll: true});
      } catch (error) { refreshError(correcting ? 'Correção confirmada. Não foi possível atualizar a consulta; atualize os saldos.' : 'Recebimento confirmado. Não foi possível atualizar a consulta; não repita o pagamento.'); }
      if (correcting) {
        const currentPanel = document.getElementById('saldos-consulta');
        currentPanel.querySelector('h2').insertAdjacentElement('afterend', status);
        currentPanel.scrollIntoView({block: 'start', behavior: 'auto'});
      }
    } catch (error) {
      modalStatus.textContent = error.message + ' Se necessário, tente novamente neste mesmo formulário para evitar duplicidade.';
    } finally {
      paying = false;
      if (submit.isConnected) submit.disabled = false;
    }
  });
})();
