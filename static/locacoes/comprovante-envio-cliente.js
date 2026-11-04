(function () {
  const form = document.getElementById('envio-comprovante-cliente');
  if (!form) return;
  const button = form.querySelector('button');
  const status = document.getElementById('envio-cliente-status');
  const fallback = document.getElementById('envio-cliente-whatsapp');
  const revoke = document.getElementById('revogar-comprovante-cliente');
  let busy = false;
  function csrfHeaders() {
    const cookie = document.cookie.split('; ').find(value => value.startsWith('csrftoken='));
    return {'X-CSRFToken': cookie ? decodeURIComponent(cookie.slice(10)) : form.querySelector('[name=csrfmiddlewaretoken]').value};
  }
  revoke?.addEventListener('click', async function () {
    if (busy) return;
    busy = true;
    button.disabled = true;
    revoke.disabled = true;
    try {
      const body = new FormData(form);
      body.set('acao', 'revogar');
      const response = await fetch(form.action, {method: 'POST', credentials: 'same-origin', cache: 'no-store', headers: csrfHeaders(), body});
      if (!response.ok) throw new Error('Não foi possível revogar o link. Tente novamente.');
      fallback.hidden = true;
      fallback.removeAttribute('href');
      status.textContent = 'Link revogado. O comprovante e seu histórico foram preservados.';
      revoke.hidden = true;
    } catch (error) {
      status.textContent = error.message;
      revoke.disabled = false;
    } finally {
      busy = false;
      button.disabled = form.dataset.semWhatsapp === 'true';
    }
  });
  form.addEventListener('submit', async function (event) {
    event.preventDefault();
    if (button.disabled || busy) return;
    busy = true;
    button.disabled = true;
    fallback.hidden = true;
    const tab = window.open('about:blank', '_blank');
    if (tab) tab.opener = null;
    try {
      const response = await fetch(form.action, {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: csrfHeaders(),
        body: new FormData(form)
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.whatsapp_url) throw new Error(data.erro || 'Não foi possível preparar o comprovante. Entre com sua conta e tente novamente.');
      fallback.href = data.whatsapp_url;
      fallback.hidden = false;
      if (revoke) { revoke.disabled = false; revoke.hidden = false; }
      if (tab) tab.location.replace(data.whatsapp_url);
      status.textContent = 'Mensagem preparada. Confirme o envio no WhatsApp. O link vale por 30 dias; um novo envio invalida o anterior.';
    } catch (error) {
      if (tab) tab.close();
      status.textContent = error.message;
    } finally {
      busy = false;
      button.disabled = false;
    }
  });
  button.disabled = form.dataset.semWhatsapp === 'true';
})();
