# Atendimento automatico das pendencias de vendas

A revisao da nota pode ser atendida ao salvar pela edicao unificada ou pelos
fluxos existentes que recalculam a separacao. Nao depende de `ajuste_separacao_id`.

Tipos identificados e criterios:

- Ajuste da nota por peso registrado: exige quantidade positiva exatamente igual
  ao peso fisico conferido, mesma unidade e produto do snapshot.
- Alteracao apos separacao (inclusao, quantidade, remocao): exige correspondencia
  completa entre itens atuais e checklist conferido. Incluir o produto na nota
  sem sua conferencia fisica nao atende a pendencia.
- Separacao: pendente, nao encontrado e quantidade insuficiente continuam
  exigindo conferencia. Uma edicao comercial nao comprova separacao fisica.
- Observacoes livres: conservadas para atendimento manual.
- Pendencias de entrega (`EntregaChecklistItem`) e saldo de pedido
  (`PendenciaPedidoEncerrada`): preservam os fluxos existentes; editar uma nota
  nao comprova entrega nem autoriza encerrar saldo de pedido.

O atendimento usa o evento existente `alteracao_separacao_atendida`, com texto
“Atendida automaticamente”, ator, canal sistema e data. A revisao e o evento sao
gravados na mesma transacao, com bloqueio da separacao; os snapshots sao mantidos.
Salvar novamente nao produz outro atendimento sem nova pendencia. A fila usa os
campos e eventos existentes ao ser consultada novamente. Nao ha polling novo.

Validacao exclusivamente em SQLite temporario, sem acessar dados operacionais:

```powershell
venv\Scripts\python.exe manage.py test estoque.tests_pendencias_automaticas estoque.tests.SeparacaoVendaFase1Tests --settings=sistema.settings_test_compras --noinput
```
