import {Repository, openDB} from '/offline/assets/2-8d/core.js';
const repo = new Repository(await openDB());
const identity = await repo.get('metadata', 'checklist-identity');
const saved = await repo.get('snapshots', 'checklist:' + location.pathname + location.search);
if (!saved || saved.actor !== identity?.actor || saved.environment !== identity?.environment) {
    document.getElementById('local-message').textContent = 'Este checklist não foi preparado neste dispositivo. Abra esta tela online primeiro.';
} else {
    document.body.innerHTML = saved.html;
    document.head.insertAdjacentHTML('beforeend', saved.styles);
    const css = document.createElement('link'); css.rel = 'stylesheet'; css.href = '/static/offline/indicator.css'; document.head.append(css);
    document.body.dataset.localChecklist = 'true';
    await import('/offline/assets/2-8d/app.js');
    await import('/offline/assets/2-8d/checklist.js');
}
