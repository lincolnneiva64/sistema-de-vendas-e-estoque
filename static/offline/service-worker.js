// Refresh modules inherited from upstream, including independent sales drafts.
const CACHE = 'offline-pilot-shell-v43-horarios-belem';
const ASSETS = ['/offline/', '/static/offline/core.js', '/static/offline/app.js', '/static/offline/pilot.css', '/static/offline/indicator.css', '/static/offline/presentation.js', '/static/offline/checklist.html', '/static/offline/checklist.js', '/static/offline/checklist-restore.js', '/static/offline/commercial.js', '/static/offline/commercial-ui.js'];
ASSETS.push('/offline/assets/2-8h-pickup/checklist.js');
ASSETS.push('/offline/revisao/', '/offline/vendas-shell/', '/static/offline/sales.js', '/static/offline/sales-drafts.js', '/static/offline/sales-draft-ui.js');
ASSETS.push(...['app.js', 'core.js', 'presentation.js', 'sales.js', 'sales-drafts.js',
    'sales-draft-ui.js', 'operation-details.js', 'sales-revisions.js', 'sales-revision-ui.js', 'commercial.js', 'commercial-ui.js',
    'checklist.js', 'checklist-restore.js'].map(file => '/offline/assets/2-8f/' + file));
ASSETS.push(...['app.js', 'operation-details.js', 'sales-draft-ui.js', 'sales-drafts.js', 'sales-revision-ui.js', 'sales-revisions.js', 'sales-stock.js', 'sales-closures.js'].map(file => '/offline/assets/2-8g-close/' + file));
ASSETS.push(...['sales.js','sales-drafts.js','sales-draft-ui.js','sales-stock.js','commercial.js','commercial-ui.js']
    .map(file => '/offline/assets/2-8f-fix/' + file));
ASSETS.push('/offline/assets/2-8g-sidebar/sales.js', '/offline/assets/2-8g-sidebar/sales-draft-ui.js');
ASSETS.push('/offline/assets/2-8g-rental-actions/sales.js', '/offline/assets/2-8g-rental-actions/sales-draft-ui.js');
ASSETS.push('/offline/assets/2-8g-auth-link/sales.js', '/offline/assets/2-8g-auth-link/sales-draft-ui.js');
ASSETS.push(...['app.js', 'sales.js', 'sales-draft-ui.js'].map(file => '/offline/assets/2-8i-local-auth/' + file));
ASSETS.push(...['app.js','core.js','sales.js','sales-draft-ui.js'].map(file => '/offline/assets/local-controlled-v1/' + file));
ASSETS.push('/offline/assets/2-8h-inventario/sales.js');
// New path bypasses workers that cached the previous sales module; no storage reset.
ASSETS.push('/offline/assets/connection-recovery-v1/sales.js', '/offline/assets/connection-recovery-v1/sales-draft-ui.js');
ASSETS.push(...['sales-draft-ui.js', 'sales-drafts.js', 'sales-stock.js'].map(file => '/offline/assets/sack-conversion-v1/' + file));
self.addEventListener('install', event => {
    event.waitUntil((async () => {
        const cache = await caches.open(CACHE);
        for (const url of ASSETS) {
            const response = await fetch(url, {cache: 'reload'});
            if (!response.ok) throw new Error('Arquivo offline indisponível: ' + url);
            await cache.put(url, response);
        }
        // Activate the refreshed shell without forcing a reload during local work.
        await self.skipWaiting();
    })());
});
self.addEventListener('activate', event => {
    event.waitUntil((async () => {
        for (const key of await caches.keys()) if (key.startsWith('offline-pilot-shell-') && key !== CACHE) await caches.delete(key);
        await self.clients.claim();
    })());
});
self.addEventListener('fetch', event => {
    const url = new URL(event.request.url);
    if (event.request.method === 'GET' && url.origin === self.location.origin && event.request.mode === 'navigate' && url.pathname === '/offline/revisao/') {
        event.respondWith(fetch(event.request, {cache:'no-store'}).catch(() => caches.match('/offline/revisao/', {cacheName:CACHE})));
        return;
    }
    if (event.request.method === 'GET' && url.origin === self.location.origin
        && event.request.mode === 'navigate' && url.pathname === '/vendas/') {
        event.respondWith((async () => {
            try { return await fetch(event.request, {cache: 'no-store'}); }
            catch { return (await caches.match('/offline/vendas-shell/', {cacheName: CACHE})) || Response.error(); }
        })());
        return;
    }
    if (event.request.method === 'GET' && url.origin === self.location.origin && event.request.mode === 'navigate'
        && (/^\/locacoes\/(checklist-operacional\/|tarefas-operacionais\/\d+\/conferencia-(entrega|recolhimento)\/)$/.test(url.pathname) || /^\/entregas\/\d+\/checklist\/$/.test(url.pathname))) {
        event.respondWith((async () => {
            // A slow online response is not evidence of offline operation.
            // Only a network failure may replace the requested page with the shell.
            try { return await fetch(event.request, {cache: 'no-store'}); }
            catch { return (await caches.match('/static/offline/checklist.html', {cacheName: CACHE})) || Response.error(); }
        })());
        return;
    }
    if (event.request.method !== 'GET' || url.origin !== self.location.origin || !ASSETS.includes(url.pathname)) return;
    event.respondWith((async () => {
        const cached = await caches.match(url.pathname, {cacheName: CACHE});
        return cached || fetch(event.request);
    })());
});
