# Títulos automáticos e organização das conferências

Preparação isolada em `.worktrees/inventario-titulos-d66c1d9`, baseada em
`d66c1d9becdf773380b72453f4f47f466564c114`. Commit e push autorizados após
aprovação das prévias; deploy permanece manual, pelo proprietário.

## Comportamento

- Novas conferências não pedem título. O serviço ignora títulos fornecidos pelo
  cliente e usa o ID efetivamente gerado no banco, sem calcular um próximo número.
- Fornecedor: nome do registro vinculado à seleção ativa. Categoria: categoria
  efetivamente filtrada. Manual: “Produtos selecionados”.
- Seleções inválidas ou vazias continuam bloqueadas. O limite existente de 160
  caracteres é validado; excedê-lo reverte integralmente a criação, sem truncar
  o nome nem deixar conferências parciais. Não foi alterado o schema.
- Títulos históricos permanecem preservados, sem atualização de registros antigos.
- A lista destaca conferências em andamento e separa as finalizadas em histórico.
  Ambas as seções ordenam por criação decrescente, com desempate pelo ID.
- Número, título, situação e datas reais vêm do banco. Datas são exibidas em
  `dd/mm/aaaa HH:mm:ss`, com `America/Belem` e localização brasileira explícitas.
  A apresentação não modifica timestamps nem o fuso de outras telas.
- Permissões, seleção de produtos, snapshots, contagem, revisão, ajustes e auditoria
  permanecem nos serviços existentes. A listagem continua somente leitura.

## Validação

PostgreSQL 17 descartável com identidade verificada, Chrome com perfis temporários,
usuários e dados fictícios. Nenhuma migration foi executada; clusters encerrados.

Regressão de 27 testes: **26 aprovados e uma falha preexistente**, em 15,598 s.
Foram testados os três títulos, POST sem título, rejeição de título externo,
seleção inválida, rollback, datas de Belém mesmo com fuso global diferente,
preservação de títulos antigos, ordem das seções, consulta sem escrita,
conversões/seleção existentes, permissões e saldos aplicados históricos.

A falha é `NavegacaoResumoBrowserTests.test_produtos_administracao_e_atalhos_abrem_mesma_pagina`,
na asserção da ausência do atalho de conferência em Vendas após remover cookies,
linha 158. O mesmo teste foi executado separadamente na base `d66c1d9`, sem esta
melhoria, e falhou na mesma asserção (5,265 s). Não foi alterado o fluxo de Vendas
para contornar esse teste. Não se declara aprovação integral dessa regressão.

Os seis testes específicos da melhoria foram executados novamente após ajustar
apenas as datas fictícias da prévia. Capturas desktop (1366) e mobile (390) em
`artifacts/titulos-conferencias/`. Artefatos são locais e não fazem parte do código.

Na preparação não houve commit ou push. Não houve acesso ao Neon, alterações de
estoque ou conferências reais, novas migrations, habilitação de ajustes ou deploy.

## Revisão visual final

Cabeçalho alterado para “Conferências em andamento”. Cada cartão mostra o título
uma única vez, em destaque como link, seguido de situação e datas. A seção
“Histórico de conferências finalizadas” permanece preservada.

Pesquisa, filtros e paginação da listagem de conferências **não estão
implementados**. Existem apenas a busca de produtos e os critérios de seleção
no formulário de criação. Essa ausência foi informada ao proprietário; tais
recursos não foram acrescentados nesta revisão visual.

Revalidação: **6 testes específicos aprovados em 3,409 segundos**, PostgreSQL
descartável e Chrome desktop/mobile. Verificados cabeçalhos, título único como
link, situação e datas abaixo do título, histórico e ausência de overflow.
Cluster `inventario-confiabilidade-pg-w7q7c0q2` encerrado, zero falhas e nenhuma
migration executada. Novas capturas em `artifacts/titulos-conferencias/revisao-final/`.

## Verificação final para commit e push

Branch remota `backup/wip-pedidos-base-2026-06-04` conferida em
`d66c1d9becdf773380b72453f4f47f466564c114`, sem avanço desde a base desta worktree.
Verificação final: **26 testes aprovados em 9,390 segundos** no PostgreSQL
descartável e Chrome. Abrange os seis testes específicos, seleção/conversão,
permissões e resumo, saldos históricos aplicados e prévias desktop/mobile.
O teste de navegação de Vendas com falha preexistente descrito acima não foi
incluído nessa rodada; sua falha anterior continua registrada, sem alteração.
Cluster `inventario-confiabilidade-pg-2z5vr8yx` encerrado, zero falhas e nenhuma
migration executada. `git diff --check` sem erros.

Somente serviço de criação, view da listagem, dois templates, testes específicos
e este relatório pertencem ao commit. Capturas, logs e backups ficam fora.
As capturas do teste usam pastas únicas para não sobrescrever arquivos abertos.
Sem alterações em outras tarefas, Neon, estoques ou conferências reais. Sem deploy.
