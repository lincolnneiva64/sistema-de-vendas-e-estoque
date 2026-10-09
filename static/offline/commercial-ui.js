import {openDB, Repository} from '/offline/assets/2-8f/core.js';
import {carregarSnapshotComercial, atualizarSnapshotComercial} from '/offline/assets/2-8f/commercial.js';

const panel = document.querySelector('#offline-global .offline-panel');
const indicator = document.getElementById('offline-global');
if (panel && indicator.dataset.actor) {
    const scope = {actor_id: indicator.dataset.actor, environment_id: indicator.dataset.environment};
    const box = document.createElement('div');
    box.className = 'offline-commercial';
    box.innerHTML = '<button type="button">Preparar dados de vendas</button><p role="status" aria-live="polite"></p><small>Referência local para montagem. Conclusão offline ainda não disponível.</small>';
    panel.append(box);
    const button = box.querySelector('button'), status = box.querySelector('[role="status"]');
    let repo, busy = false;
    function describe(snapshot) {
        status.textContent = snapshot
            ? `Catálogo salvo: ${snapshot.contagens.clientes} clientes e ${snapshot.contagens.produtos} produtos. Atualizado em ${new Date(snapshot.prepared_at).toLocaleString('pt-BR')}.`
            : 'Nenhum catálogo comercial salvo para este usuário.';
    }
    async function initialize() {
        repo = new Repository(await openDB());
        try { describe(await carregarSnapshotComercial(repo, scope)); }
        catch (_) { status.textContent = 'Catálogo local incompatível. Prepare os dados novamente online.'; }
    }
    const ready = initialize().catch(error => { status.textContent = error.message; throw error; });
    // Keep initialization failures visible without an unhandled rejection.
    ready.catch(() => {});
    button.addEventListener('click', async () => {
        if (busy) return;
        busy = true; button.disabled = true; button.textContent = 'Preparando dados...';
        try {
            await ready;
            describe(await atualizarSnapshotComercial(repo, scope));
            if ('serviceWorker' in navigator) {
                try {
                    const registration = await navigator.serviceWorker.register('/service-worker.js', {scope: '/'});
                    const worker = registration.installing || registration.waiting;
                    if (worker && worker.state !== 'activated') await new Promise((resolve, reject) => {
                        const timer = setTimeout(() => finish(new Error('Preparação dos arquivos demorou demais.')), 15000);
                        function finish(error) {
                            clearTimeout(timer); worker.removeEventListener('statechange', changed);
                            error ? reject(error) : resolve();
                        }
                        function changed() {
                            if (worker.state === 'activated') finish();
                            else if (worker.state === 'redundant') finish(new Error('Arquivos offline indisponíveis.'));
                        }
                        worker.addEventListener('statechange', changed); changed();
                    });
                }
                catch (_) { status.textContent += ' Catálogo salvo; preparação dos arquivos offline não foi concluída.'; }
            }
        } catch (error) { status.textContent = error.name === 'AbortError' ? 'Atualização demorou demais. Catálogo anterior preservado.' : error.message; }
        finally { busy = false; button.disabled = false; button.textContent = 'Preparar dados de vendas'; }
    });
}
