# Venda Offline — Etapa 2.3

Base publicada: `1b8c324` (Etapa 2.2). Esta etapa persiste montagem local;
não cria comando comercial, fila, venda offline, estoque ou financeiro.

## Auditoria anterior à implementação

A tela ativa continua sendo `estoque/templates/estoque/vendas_layout_teste.html`.

| Informação | Fonte real na tela | Persistência escolhida |
| --- | --- | --- |
| Cliente | `clienteSelecionado` em memória, `clienteId`, `clienteBusca`, flag de seleção válida | ID, nome de referência e prazo; nenhum resumo financeiro |
| Itens | Linhas de `tabelaProdutos`; `coletarItensVenda()` lê datasets/células | ID/nome do produto, quantidade, unidade, preço, unidades/conversão e subtotal de referência |
| Quantidade/preço/subtotal | `data-quantidade`, `data-preco-unitario`, `data-valor-total` | Quantidade/preço em strings decimais; subtotal reconstruído a partir deles |
| Unidade/conversão | Opções de `produto`, unidades 1/2, fracionamento e fator | Referência guardada na linha e no rascunho; não acompanha atualização silenciosa de catálogo |
| Operador | `operadorVenda`, cujo valor existente é o nome | Nome, mantendo contrato atual da interface/servidor |
| Tipo | `tipoVenda`: À vista, A prazo, consumo_proprio | Valor do select; nenhuma ação financeira |
| Datas | `dataVenda`, `vencimentoVenda`; prazo do cliente sugere vencimento | Strings de data; restauração preserva escolha manual |
| Total | `totalGeral`, calculado sobre os subtotais | `total_referencia`, recalculável visualmente; sem autoridade oficial |
| Linha ainda em edição | Produto selecionado, quantidade, unidade, preço e `linhaSelecionada` | `lancamento` opcional com referência do produto e índice da linha em edição |
| Observações | Não existe campo de observações nesse fluxo de montagem | Nenhum campo inventado |
| Origem oficial | `vendaEdicaoVenda`, `pedidoImportadoVenda`, cliente inicial explícito por URL | Contextos explícitos não recebem nem sobrescrevem o rascunho da nova venda comum |

Adição/edição/remoção já são locais; a confirmação oficial usa
`gravarVendaAtual()`, POST `/vendas/gravar/`, e exige resposta HTTP válida com
`sucesso`. A limpeza existente ocorre depois dessa resposta.

## Arquitetura, store e chave

O banco continua `vendas-offline-pilot`, versão **1**, com os mesmos stores
`metadata`, `snapshots`, `operations` e `history`. `core.js` não foi alterado.
Não existe migração de IndexedDB, store novo ou banco paralelo.

Um rascunho por escopo fica no store **metadata**:

```text
rascunho_venda:[environment_id,actor_id,device_id]
```

O sufixo é serializado com `JSON.stringify()`; exemplos acima são conceituais.
A revisão fica na mesma chave acrescida de `:revision`. Descartar apaga o registro
comercial fisicamente e mantém apenas esse contador, impedindo ressurreição por aba antiga.

O documento contém `tipo=rascunho_venda`, `schema_version=1`, UUID `draft_id`,
`criado_em`, `atualizado_em`, escopo, `revision`, cliente, operador, tipo, datas,
itens, `lancamento` opcional e `total_referencia`. O UUID e a data de criação
continuam os mesmos ao editar o mesmo rascunho.

O dispositivo existente é reutilizado por leitura de `metadata/device`.
Quando necessário, cria-se somente a identidade local com sequência zero.
Nunca se chama `Repository.identity()` (que reserva sequence), `create()` ou qualquer API de fila.

Projeção explícita antes de escrever/ler exclui CPF/CNPJ, endereço, telefone,
limite, saldos, crédito, Pix, cobranças, histórico, custo, CSRF, IDs de venda/pedido,
referências de movimento financeiro e campos desconhecidos. HTML nunca é persistido.

## Salvamento, restauração e descarte

- Mudanças de cliente, cabeçalho e tabela disparam salvamento automático.
  Digitação relevante tem debounce de 250ms; alterações de item/select usam 0ms.
  A comparação do conteúdo evita regravar só por foco, cor de seleção ou mutação equivalente.
- Quantidade/unidade/preço ainda em edição também sobrevivem à recarga.
  Isso não confirma a linha: os totais seguem os itens já adicionados até o usuário atualizar.
- `visibilitychange`/`pagehide` solicitam flush; o teste de F5 imediato não espera debounce
  nem chama flush manualmente. O aviso diferencia salvamento em andamento e salvo.
- A restauração funciona online e no shell offline, sem modal bloqueante.
  Mostra “Rascunho local restaurado.” e data/hora da última alteração.
- Sem rascunho, a montagem abre normalmente. Sem identidade autenticada, há orientação
  para autenticar; o fluxo online existente continua disponível.
- `Descartar rascunho` usa a confirmação já existente. Cancelar preserva tela e registro.
  Confirmar apaga somente o rascunho daquele escopo e limpa a montagem visual.
- Perder conexão mantém cabeçalho, preços e itens atuais, inclusive quando não há catálogo.
  Sem catálogo, permite continuar editando as linhas existentes e bloqueia novos produtos.
- Catálogo novo não substitui preços/conversões dos itens restaurados. Produto ausente é
  mantido com “Produto fora do catálogo — revisar” e opção local de referência para edição.
  A conclusão online exige revisar/remover essa linha; a conclusão offline continua bloqueada.
  Alterar explicitamente uma linha pode usar as referências do catálogo atual.

Datas/tipo de venda não são substituídos pela simples mudança de conectividade.
Online, o resumo financeiro é consultado novamente no servidor e mantido só em memória,
sem alterar o prazo/tipo/itens restaurados ou acrescentar esses dados ao rascunho.
Uma montagem restaurada continua sendo referência: não concede crédito,
disponibilidade de estoque, preço oficial ou autorização comercial.

## Atomicidade, identidade e multiaba

Leitura de dispositivo, identidade atual, revisão e registro ocorre na mesma transação.
Gravação substitui documento completo e contador numa única transação `readwrite`.
Abort, quota, validação ou erro preservam o último registro válido.

Chave, conteúdo e identidade atual devem coincidir em usuário, ambiente e dispositivo.
Troca de identidade limpa a montagem visual e bloqueia salvamento no escopo antigo,
sem apagar seu rascunho. O shell também compara ambiente com a identidade online conhecida.

Cada aba conserva a revisão que carregou. A gravação/descarte compara essa revisão
dentro da transação (compare-and-swap). BroadcastChannel apenas antecipa o aviso;
a proteção de gravação não depende dele. Aba antiga é avisada e deve reabrir a tela;
não substitui a versão nova nem a ressuscita após descarte.
Antes da conclusão online, a revisão é lida novamente, inclusive se não houve edição local.
Não há colaboração, merge, reconciliação ou conflito de sincronização comercial.

## Conclusão online e Service Worker

O fluxo atual de POST foi mantido. O rascunho permanece durante requisição pendente,
erro HTTP, `sucesso=false`, JSON inválido ou falha de comunicação.
Depois de `resposta.ok`, `sucesso=true` e `venda_id` confirmado, apaga-se o rascunho
correspondente com a mesma proteção de revisão.
Alterações posteriores ao envio ou feitas em outra aba são preservadas;
falha na limpeza local não muda o resultado oficial confirmado no servidor.

Correção do caso de falha após sucesso: antes do POST real, reserva-se em `localStorage`
um marcador mínimo ligado ao escopo, UUID e revisão do rascunho. Ao receber HTTP válido,
`sucesso=true` e ID válido, ele passa a `concluido` com o ID oficial, antes da limpeza IDB.
O marcador não contém a montagem, não é fila e não reserva sequence. Se o IndexedDB
falhar, a leitura/recarga ignora aquela revisão concluída. Se nem a atualização do
marcador for possível, a reserva anterior impede restauração automática e orienta
conferir a venda online. Erros sem confirmação liberam a reserva criada pelo próprio envio.
Exceção local depois da confirmação mantém a UI em sucesso; a mesma montagem não pode
ser enviada novamente. Uma nova montagem vazia pode iniciar outro rascunho com novo UUID.
O teste específico injeta falha IDB e exceção inesperada na limpeza, confere uma única
Venda/ItemVenda, bloqueio do botão, recarga sem pendência, fila vazia e sequence inalterada.

Worker **v19** só acrescenta os dois módulos estáticos de rascunho e atualiza o shell neutro.
Nenhum documento de rascunho entra no Cache Storage. A tela autenticada/API continuam
fora do cache compartilhado; shell não contém dados de usuário.
Perfis com worker anterior precisam passar online para receber os arquivos desta etapa.

## Arquivos desta etapa

Modificados:

- `estoque/templates/estoque/vendas_layout_teste.html` — ponte de captura/restauração,
  eventos e limpeza após sucesso online.
- `static/offline/sales.js` — preserva a montagem na transição offline e atualiza o link de retorno online.
- `static/offline/service-worker.js` — v19 e assets explícitos.
- `offline/tests_browser.py` — expectativa de cache v19.
- `offline/tests_sales_browser.py` — reset de sequences dos fixtures para evitar IDs de clientes
  que a tela preexistente classifica como consumo próprio.

Novos:

- `static/offline/sales-drafts.js` — contrato, projeção, transações e revisão.
- `static/offline/sales-draft-ui.js` — autosave, restauração, aviso, descarte e integração online.
- `offline/tests_sales_drafts_browser.py` — cenários de persistência/segurança em Chrome real.
- `offline/SALES_DRAFTS.md` — auditoria e entrega.

`separacao_vendas_fila.html` já estava modificado antes desta etapa e foi preservado.
Os `.bak`, `.txt` antigos, `offline/diagnostics/` e demais arquivos locais preexistentes
não foram editados. Não houve staging, commit ou push.

## Validação

Os testes de navegador usam Chrome real, perfil temporário e banco SQLite isolado.
Cobrem recarga, fechamento real da aba, reinício do Chrome, montagem online/offline,
linha em edição, F5 imediato, ausência/alteração de catálogo, remoção, troca de cliente,
operador, tipo, vencimento, descarte confirmado/cancelado, quota/abort, projeção sem
campos sensíveis, troca real de usuário/ambiente/device, duas abas e revisão após descarte.
Também mantêm a resposta online pendente, simulam erro e depois concluem pelo servidor real.

Apenas salvar/restaurar/descartar compara Produto, Venda, ItemVenda, ContaReceber,
MovimentoFinanceiro e OperacaoSincronizacao antes/depois: zero mutações oficiais.
`operations` permanece vazio e a sequence local não avança.
O teste separado de conclusão online cria uma venda oficial somente após ação explícita.
Desktop (1280px) e mobile (390px) estão incluídos.

Resultado da rodada ampla: **43 testes, 42 aprovados, 1 ignorado**, em 36,904s.
O teste ignorado exige PostgreSQL isolado; a configuração usada foi `offline.test_settings` (SQLite).
Após os ajustes finais, os **7 testes de navegador de vendas/snapshot/rascunho** passaram novamente.
O aviso preexistente `urls.W005` (namespace estoque duplicado) continua presente.

Comando da rodada ampla:

```powershell
.\venv\Scripts\python.exe manage.py test offline.tests_sales_drafts_browser offline.tests_sales_browser offline.tests_commercial offline.tests_commercial_browser offline.tests offline.test_checklist_history estoque.tests.PixRecebidoTests.test_gravar_venda_baixa_estoque estoque.tests.PixRecebidoTests.test_gravar_venda_bloqueia_estoque_insuficiente estoque.tests.PixRecebidoTests.test_gravar_venda_unidade_fracionada_usa_preco_compra_fracionado offline.tests_browser.OfflineBrowserTests.test_sales_and_offline_health_200_session_denied_without_snapshot offline.tests_browser.OfflineBrowserTests.test_login_return_session_and_prepare offline.tests_browser.OfflineBrowserTests.test_rental_checklist_local_note_refresh_and_manual_sync estoque.tests.FechamentoCompraFinanceiroTests.test_vendas_template_valida_estoque_antes_de_gravar_venda --settings=offline.test_settings --noinput
```

`git diff --check`: exit code 0, sem erros de whitespace (somente avisos LF/CRLF).

`git status --short` ao final (inclui os artefatos anteriores preservados):

```text
 M estoque/templates/estoque/separacao_vendas_fila.html
 M estoque/templates/estoque/vendas_layout_teste.html
 M offline/tests_browser.py
 M offline/tests_sales_browser.py
 M static/offline/sales.js
 M static/offline/service-worker.js
?? diff_abas.txt
?? diff_compra_atual.txt
?? diff_compra_mobile.txt
?? diff_pagamentos.txt
?? estoque/models.py.bak-cartoes
?? estoque/models.py.bak-conferente-rota
?? estoque/templates/estoque/contas_pagar_pagamentos.html.bak-filtros-periodo
?? estoque/templates/estoque/despesas_diarias.html.bak-rota
?? estoque/templates/estoque/entrega_checklist.html.bak-mobile
?? estoque/templates/estoque/receber_cliente.html.bak-mobile-dividas-calculadora
?? estoque/templates/estoque/receber_cliente.html.bak-mobile-limpeza
?? estoque/templates/estoque/receber_cliente.html.bak-mobile-operacional
?? estoque/templates/estoque/receber_cliente_recebimentos_rota.html.bak-abas
?? estoque/templates/estoque/receber_cliente_recebimentos_rota.html.bak-whatsapp-conferencia
?? estoque/templates/estoque/venda_detalhe.html.bak-mobile-operacional
?? estoque/views.py.bak-conferente-rota
?? estoque/views.py.bak-dividas-mobile
?? estoque/views.py.bak-rota
?? estoque/views.py.bak-whatsapp-conferencia
?? fase2-auditoria-anteriores.txt
?? fase2-auditoria-backend.txt
?? fase2-auditoria-browser.txt
?? offline/SALES_DRAFTS.md
?? offline/diagnostics/
?? offline/tests_sales_drafts_browser.py
?? revisao_final.txt
?? static/offline/sales-draft-ui.js
?? static/offline/sales-drafts.js
?? trecho_motor_recebimento.txt
?? trecho_recebimento.txt
```

## Riscos e pendências para Etapa 2.4

O conteúdo depende da disponibilidade/retenção do IndexedDB no perfil do navegador;
remover os dados do site remove também o rascunho. Escrita que falha mantém a última
versão confirmada e exibe erro. Não há garantia de salvar uma transação interrompida
por encerramento abrupto do processo; o indicador informa quando a gravação terminou.

Esta entrega atende a nova venda comum em `/vendas/`. Edição de venda oficial,
importação de pedido e contextos explícitos de cliente não sobrescrevem esse rascunho;
uma futura extensão precisará de contrato de origem separado.

Continuam pendentes conclusão offline autorizada, contrato/idempotência de `criar_venda`,
fila, sincronização, revalidação de preço/conversão/cliente/operador/estoque, financeiro,
consumo próprio, pedido e reconciliação/conflitos. Nada disso foi implementado aqui.
