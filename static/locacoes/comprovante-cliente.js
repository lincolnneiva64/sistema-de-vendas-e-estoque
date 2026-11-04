/* The fragment is never sent in HTTP URLs, access logs or Referer headers. */
(async function () {
  const status = document.getElementById('acesso-status');
  const token = location.hash.slice(1);
  if (!/^[A-Za-z0-9_-]{43}$/.test(token)) {
    status.textContent = 'Link indisponível ou vencido. Peça um novo comprovante ao depósito.';
    return;
  }
  try {
    const response = await fetch(location.pathname, {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: {'X-CSRFToken': document.querySelector('[name=csrfmiddlewaretoken]').value},
      body: new URLSearchParams({token})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.erro);
    const documentReceipt = new DOMParser().parseFromString(data.html, 'text/html');
    document.title = documentReceipt.title;
    documentReceipt.querySelectorAll('style').forEach(style => document.head.appendChild(style));
    document.querySelector('main').replaceWith(documentReceipt.querySelector('main'));
  } catch (error) {
    status.textContent = error.message || 'Não foi possível abrir o comprovante. Tente novamente.';
  }
})();
