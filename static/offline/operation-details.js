import {commercialConflict} from '/offline/assets/2-8f/sales-revisions.js';
// Opening details remains read-only. Review is an explicit separate navigation.
export function operationDetails(operation, scope, revisionView = null) {
    if (!operation || operation.actor_id !== scope.actor_id || operation.environment_id !== scope.environment_id
        || !['pendente', 'enviando', 'conflito', 'resultado_desconhecido', 'erro'].includes(operation.status)) return null;
    const details = document.createElement('details');
    details.className = 'offline-operation-details';
    details.dataset.operationId = operation.operation_id;
    const summary = document.createElement('summary');
    summary.textContent = 'Ver diagnóstico da operação';
    details.append(summary);
    const payload = operation.payload || {};
    const originalCustomer = operation.original_labels?.cliente;
    const fields = {
        UUID:operation.operation_id, Status:operation.status, Tentativas:operation.attempts,
        'Data/hora original':operation.created_at, Tipo:operation.type,
        'Cliente original (ID)':payload.cliente_id ?? 'Não informado',
        'Nome original do cliente':originalCustomer && originalCustomer.id === payload.cliente_id ? originalCustomer.nome : 'Nome não preservado na operação',
        'Data da venda':payload.data_venda, Vencimento:payload.data_vencimento,
        'Condição/pagamento':payload.tipo_pagamento, Operador:payload.operador,
        Hash:operation.payload_hash, Sequência:operation.sequence,
        'Snapshot de referência disponível na conclusão':operation.reference_snapshot?.prepared_at || 'Data não registrada nesta operação',
        'Mensagem/erro':operation.last_error || operation.server_result?.erro || '',
        'Operação substituta':revisionView?.replacementId || 'Não registrada',
        Diagnóstico:operation.diagnostic_code || '',
    };
    const list = document.createElement('dl');
    for (const [label, value] of Object.entries(fields)) {
        const term = document.createElement('dt'), text = document.createElement('dd');
        term.textContent = label; text.textContent = value == null ? 'Não informado' : String(value);
        text.style.overflowWrap = 'anywhere'; list.append(term, text);
    }
    details.append(list);
    if (operation.payload?.revisao) {
        const origin = document.createElement('p');
        origin.textContent = 'Revisão da operação: ' + operation.payload.revisao.original_operation_id;
        details.append(origin);
    }
    if (revisionView?.recordId) {
        const text = document.createElement('p'), link = document.createElement('a');
        text.textContent = revisionView.originalConfirmed ? 'Original confirmada oficialmente. Revisão bloqueada.'
            : `Conflito resolvido pela venda nº ${revisionView.recordId}. Venda corrigida confirmada. Se você enviou ao cliente informações da versão anterior, envie novamente a nota correta.`;
        link.href = `/vendas/${revisionView.recordId}/`; link.textContent = 'Ver venda oficial';
        details.append(text,link);
    } else if (revisionView?.replacementId) {
        const text = document.createElement('p');text.textContent = 'Revisão já concluída: ' + revisionView.replacementId;
        details.append(text);
    } else if (commercialConflict(operation)) {
        const link = document.createElement('a');link.href = '/offline/revisao/?operation_id=' + encodeURIComponent(operation.operation_id);
        link.textContent = 'Revisar venda';link.dataset.reviewSale = operation.operation_id;details.append(link);
    }
    const items = document.createElement('ol');
    for (const [index, item] of (payload.itens || []).entries()) {
        const row = document.createElement('li');
        const original = operation.original_labels?.produtos?.[index];
        const name = original && original.id === item.produto_id ? original.nome : 'Nome não preservado';
        row.textContent = `Produto original (ID): ${item.produto_id} · ${name} · Quantidade: ${item.quantidade} · Unidade: ${item.unidade} · Preço original: ${item.preco_unitario}`;
        items.append(row);
    }
    details.append(items);
    for (const [label, value] of [['Receipt', operation.server_result], ['Resposta de diagnóstico', operation.diagnostic_response]]) {
        if (!value) continue;
        const title = document.createElement('p'), body = document.createElement('pre');
        title.textContent = label; body.textContent = JSON.stringify(value, null, 2);
        body.style.cssText = 'white-space:pre-wrap;overflow-wrap:anywhere'; details.append(title, body);
    }
    return details;
}
