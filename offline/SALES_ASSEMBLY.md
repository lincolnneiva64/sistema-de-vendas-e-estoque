# Venda Offline — Etapa 2.2

## Auditoria feita antes da implementação

A rota `/vendas/` usa `estoque.views.vendas` e o template
`estoque/templates/estoque/vendas_layout_teste.html`. `vendas.html` não é a tela ativa.

| Parte | Funcionamento anterior | Risco sem rede / fallback implementado |
| --- | --- | --- |
| HTML inicial | Produtos ativos, preços, custos de referência, unidades, fator, estoque, flags, operadores e tipos. Também há dados de pedidos/edição e painéis financeiros/operacionais. | O HTML autenticado não entra no cache do worker. O shell é renderizado sem request, context processors ou dados comerciais. |
| Cliente | Autocomplete em `/clientes/autocomplete/`, com debounce, cache em memória, seleção por clique/teclado/toque e carregamento de detalhe financeiro. | `obterClientesSnapshot()`, busca por nome/apelido/telefone, aliases `prazo` e `whatsapp` derivados exclusivamente do catálogo. Detalhes financeiros/histórico não são carregados offline. |
| Produto | Busca local sobre opções do select oculto `produto`. | `obterProdutosSnapshot()` repõe essas opções; a busca local permite nome/código, sem acentos. |
| Preço/unidade/conversão | A opção contém atributos `data-preco`, `data-preco2`, `data-unidade1/2`, `data-fator` e `data-fracionado`; seleção e troca de unidade preenchem os campos. | Mesmos atributos e mesmos cálculos, preenchidos com o snapshot. Sem preço inventado ou consulta externa. |
| Estoque | Atributos no HTML, badges e preview; consulta/conferência por endpoint; bloqueios de saldo zero/insuficiente. | Texto “Estoque de referência”; conferência bloqueada; referência zero/insuficiente não impede montagem. Não há reserva nem baixa. |
| Operador | Funcionários ativos habilitados no HTML; valor do select é o nome. | Mesmos valores de interface, provenientes dos operadores do snapshot. Não representa autenticação/autorização oficial. |
| Tipo | `À vista`, `A prazo` e `consumo_proprio`, definidos no HTML. | Opções exclusivamente das formas do snapshot; prazo local continua sugerindo vencimento/tipo. |
| Montagem | Adição, edição, remoção, subtotal e total no DOM; conclusão via POST. | Reutiliza o motor da tela. Itens continuam apenas na memória/DOM. Gravação bloqueada no botão, na captura de ações e no próprio método de gravação. |
| Ações auxiliares | Financeiro, estoque, separação, revisão de preços, visitas e locações dependem do servidor. | Painéis de ações oficiais ocultados/bloqueados no modo local; formulários não são enviados. Atalhos para outras páginas não passam a funcionar offline. |

O worker anterior (v17) guardava apenas o shell do piloto/checklists e módulos comerciais;
não atendia navegação offline de `/vendas/`.

## Arquitetura e comportamento

- `static/offline/sales.js` faz a ponte do catálogo validado para os controles existentes.
  Usa `Repository/openDB`, sem banco, store, schema ou migration novos.
- `/offline/vendas-shell/` é uma estrutura neutra baseada no mesmo template, renderizada
  **sem request**. Só recebe o ambiente e valores nulos para cliente/pedido/edição.
  Não contém identidade de usuário, catálogo, CSRF ou resumos financeiros.
- Worker v18: navegação `/vendas/` tenta servidor com `cache: no-store`, timeout de 5s
  e fallback para o shell. Somente shell e assets explicitamente permitidos são cacheados.
  HTML autenticado de vendas e respostas das APIs não são armazenados pelo worker.
- A preparação manual aguarda ativação dos arquivos do worker; falhas ficam indicadas no Status.
- `metadata/sales-identity` conserva o último escopo apresentado online. Páginas com indicador
  atualizam o escopo; resposta de sessão explicitamente não autenticada o invalida.
  BroadcastChannel/foco invalidam uma tela aberta se o usuário/ambiente mudar.
  O shell não escolhe automaticamente qualquer snapshot: compara ambiente e escopo,
  e as APIs comerciais já validam usuário, ambiente, schema e dispositivo.
- Online, controles, servidor e conclusão existentes continuam como fonte principal.
  O fallback entra ao perder conexão, após perda confirmada no indicador ou quando uma
  busca de cliente falha por rede (inclusive com `navigator.onLine` ainda verdadeiro).
- Depois de entrar no modo local, a montagem permanece protegida mesmo se a conexão voltar.
  O aviso oferece reabrir `/vendas/` online. Reabrir/recarregar perde a montagem local;
  não há tentativa de concluir automaticamente dados locais sem revalidação.
- O aviso mostra data/hora da preparação e informa futura revalidação de preço/estoque.
  Catálogo antigo não é bloqueado por idade. Catálogo ausente/inválido/de outro escopo
  mostra orientação para `Status → Preparar dados de vendas` e impede adicionar produtos.
- Não há persistência de carrinho, fila `criar_venda`, sincronização comercial, IDs oficiais,
  eventos, ContaReceber, MovimentoFinanceiro, consumo financeiro ou atualização de pedido.
  Pedido/edição solicitado por query string não é importado pelo shell neutro offline.

## Arquivos desta entrega

Modificados:

- `estoque/templates/estoque/vendas_layout_teste.html`
- `offline/urls.py`
- `offline/views.py`
- `offline/tests_browser.py` (expectativa de versão do cache)
- `static/offline/app.js` (escopo local e invalidação por sessão)
- `static/offline/commercial-ui.js` (texto e espera dos arquivos do worker)
- `static/offline/service-worker.js`

Novos:

- `static/offline/sales.js`
- `offline/tests_sales_browser.py`
- `offline/SALES_ASSEMBLY.md`

`estoque/templates/estoque/separacao_vendas_fila.html` já estava modificado antes desta
etapa e foi preservado. Os `.txt`, `.bak` e `offline/diagnostics/` preexistentes não foram editados.
Não houve staging, commit ou push.

## Testes

Teste real de Chrome com perfil temporário e SQLite isolado:

- Preparação manual online e abertura de documento novo sem rede, com o worker também offline.
- Falha de fetch de cliente enquanto `navigator.onLine` continua verdadeiro.
- Busca de cliente por apelido, seleção, prazo e forma de venda.
- Busca de produto por código, seleção, preço integral/fracionado, unidade e fator.
- Estoque zero de referência, montagem de quantidade superior ao saldo, subtotal/total,
  alteração de quantidade e remoção por teclado/confirmação.
- Operador/tipos locais, bloqueio de ambos os botões de fechamento, desktop 1280px/mobile 390px.
- Catálogo antigo (ano 2000) continua disponível; catálogo ausente e outros escopos são recusados.
- Shell sem nomes comerciais/usuário/token; APIs e HTML de vendas fora do cache compartilhado.
- Comparação integral antes/depois de Produto, Venda, ItemVenda, ContaReceber,
  MovimentoFinanceiro e OperacaoSincronizacao. Zero alterações oficiais, fila vazia,
  sequência do dispositivo sem incremento.

Também foram executadas suites do piloto/snapshot/checklist e regressões da conclusão online
(venda baixa estoque; estoque insuficiente bloqueia; preço fracionado respeita custo).
Regressões de navegador cobrem sessão negada com health disponível, login/preparação
e checklist local com sincronização manual. Um teste de concorrência depende de PostgreSQL
isolado e é ignorado pela configuração SQLite. Há o aviso preexistente de namespace `estoque` duplicado.

Rodada final: **38 testes, 37 aprovados e 1 ignorado**, em 22,991s.
`git diff --check`: exit code 0, sem erros de whitespace; apenas avisos de conversão LF/CRLF.

`git status --short` ao final (inclui os artefatos preexistentes preservados):

```text
 M estoque/templates/estoque/separacao_vendas_fila.html
 M estoque/templates/estoque/vendas_layout_teste.html
 M offline/tests_browser.py
 M offline/urls.py
 M offline/views.py
 M static/offline/app.js
 M static/offline/commercial-ui.js
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
?? offline/SALES_ASSEMBLY.md
?? offline/diagnostics/
?? offline/tests_sales_browser.py
?? revisao_final.txt
?? static/offline/sales.js
?? trecho_motor_recebimento.txt
?? trecho_recebimento.txt
```

## Pendências para a Etapa 2.3

Definir a persistência de rascunhos e o contrato/autorização da futura conclusão offline,
idempotência/fila/sincronização, revalidação de dados comerciais e estoque,
tratamento financeiro/consumo próprio, integração com pedido e reconciliação/conflitos.
Nenhuma dessas operações foi implementada na Etapa 2.2.
