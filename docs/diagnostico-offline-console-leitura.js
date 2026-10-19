// Diagnóstico de leitura. Não abre IndexedDB, não sincroniza e não altera armazenamento.
(async () => {
    const indicator = document.getElementById('offline-global');
    const result = {
        navigatorOnline: navigator.onLine,
        connection: indicator?.dataset.connection ?? null,
        indicatorState: indicator?.dataset.state ?? null,
        authenticatedPageIdentity: !!indicator?.dataset.actor,
        localSalesActive: window.salesOffline?.active ?? null,
        cachedShell: window.salesOffline?.shell ?? null,
        assemblyProtected: window.salesOffline?.completed ?? null,
        identityBlocked: window.salesOffline?.identityBlocked ?? null,
        draftReady: window.salesDraftUI?.ready ?? null,
        draftBlocked: window.salesDraftUI?.blocked ?? null,
        recoveryBlock: window.salesDraftUI?.lastOnlineRecoveryBlock ?? null,
        hasLocalFinalization: !!window.salesDraftUI?.finalization,
        worker: navigator.serviceWorker?.controller
            ? new URL(navigator.serviceWorker.controller.scriptURL).pathname : null,
        scripts: [...document.scripts].map(s => s.src ? new URL(s.src).pathname : '')
            .filter(p => p.startsWith('/offline/assets/')),
        endpoints: {},
    };
    for (const path of ['/api/offline/health/', '/api/offline/session/']) {
        try {
            const response = await fetch(path, {
                method: 'GET', credentials: 'same-origin', cache: 'no-store',
                redirect: 'manual', signal: AbortSignal.timeout(15000),
            });
            const entry = {status: response.status, type: response.type};
            if (response.headers.get('Content-Type')?.includes('application/json')) {
                const data = await response.json();
                if (path.includes('/health/')) {
                    entry.ok = data.ok === true;
                    entry.environmentMatches = data.environment === indicator?.dataset.environment;
                    entry.protocolVersion = data.protocol_version;
                } else {
                    entry.authenticated = data.authenticated === true;
                    entry.canPrepare = data.can_prepare === true;
                }
            }
            result.endpoints[path] = entry;
        } catch (error) { result.endpoints[path] = {error: error.name}; }
    }
    console.log(JSON.stringify(result, null, 2));
})();
