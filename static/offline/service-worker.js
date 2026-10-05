const CACHE = 'offline-pilot-shell-v4';
const ASSETS = ['/offline/', '/static/offline/core.js', '/static/offline/app.js', '/static/offline/pilot.css'];
self.addEventListener('install', event => {
    event.waitUntil((async () => {
        const cache = await caches.open(CACHE);
        for (const url of ASSETS) {
            const response = await fetch(url, {cache: 'reload'});
            if (!response.ok) throw new Error('Arquivo offline indisponível: ' + url);
            await cache.put(url, response);
        }
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
    if (event.request.method !== 'GET' || url.origin !== self.location.origin || !ASSETS.includes(url.pathname)) return;
    event.respondWith((async () => {
        const cached = await caches.match(url.pathname, {cacheName: CACHE});
        return cached || fetch(event.request);
    })());
});
