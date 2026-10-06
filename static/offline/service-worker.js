const CACHE = 'offline-pilot-shell-v18';
const ASSETS = ['/offline/', '/static/offline/core.js', '/static/offline/app.js', '/static/offline/pilot.css', '/static/offline/indicator.css', '/static/offline/presentation.js', '/static/offline/checklist.html', '/static/offline/checklist.js', '/static/offline/checklist-restore.js', '/static/offline/commercial.js', '/static/offline/commercial-ui.js'];
ASSETS.push('/offline/vendas-shell/', '/static/offline/sales.js');
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
    if (event.request.method === 'GET' && url.origin === self.location.origin
        && event.request.mode === 'navigate' && url.pathname === '/vendas/') {
        event.respondWith((async () => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 5000);
            try { return await fetch(event.request, {signal: controller.signal, cache: 'no-store'}); }
            catch { return (await caches.match('/offline/vendas-shell/', {cacheName: CACHE})) || Response.error(); }
            finally { clearTimeout(timer); }
        })());
        return;
    }
    if (event.request.method === 'GET' && url.origin === self.location.origin && event.request.mode === 'navigate'
        && (/^\/locacoes\/(checklist-operacional\/|tarefas-operacionais\/\d+\/conferencia-(entrega|recolhimento)\/)$/.test(url.pathname) || /^\/entregas\/\d+\/checklist\/$/.test(url.pathname))) {
        event.respondWith((async () => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 5000);
            try { return await fetch(event.request, {signal: controller.signal}); }
            catch { return (await caches.match('/static/offline/checklist.html', {cacheName: CACHE})) || Response.error(); }
            finally { clearTimeout(timer); }
        })());
        return;
    }
    if (event.request.method !== 'GET' || url.origin !== self.location.origin || !ASSETS.includes(url.pathname)) return;
    event.respondWith((async () => {
        const cached = await caches.match(url.pathname, {cacheName: CACHE});
        return cached || fetch(event.request);
    })());
});
