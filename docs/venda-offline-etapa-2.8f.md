# Etapa 2.8F — várias vendas consecutivas offline

Base: `7128a9b` (2.8E). Sem commit, push, deploy ou acesso ao banco de produção.

## Causa comprovada

`finalizeDraftOffline` grava atomicamente a operação e substitui o rascunho pelo
marcador `venda_concluida_offline`. `completed` bloqueia a montagem. Até a 2.8E,
`refreshCompletion` liberava esse marcador somente após confirmação oficial da
original ou de sua revisão. Faltava uma ação para criar uma montagem independente
enquanto a operação anterior permanecia na fila.

## Solução

- `sales-drafts.js`: registra `conclusion_mode` no marcador e `transport` na
  operação na transação inicial. O envio online informa explicitamente `online`,
  inclusive antes do primeiro POST; uma interrupção nesse intervalo não autoriza
  tratar a operação como conclusão offline.
- `startNextOfflineDraft`: verifica hash, UUID/aggregate, identidade, revisão,
  origem offline, estado pendente, zero tentativas e ausência de recibo. Reconfere
  a operação na transação de escrita. Remove somente o marcador da montagem e
  incrementa a revisão, sem alterar operações, payloads, hashes ou históricos.
- A montagem seguinte recebe um novo `draft_id` na primeira gravação; a conclusão
  o promove ao seu próprio UUID e usa a sequência existente do dispositivo.
- `sales-draft-ui.js`: mensagem “Venda salva neste dispositivo. Aguardando
  sincronização.”, identificação local, quantidade aguardando sincronização ou
  resolução e ação explícita **Nova venda**. A tela só é liberada após a transação
  bem-sucedida. F5 e reabertura recuperam o estado persistido.
- BroadcastChannel comunica a liberação. A outra aba só adota uma montagem ainda
  vazia; se já existe uma versão mais recente, deve reabrir a tela. A revisão impede
  sobrescrita e dupla conclusão da mesma montagem.
- `ambiguousSales`: envio em andamento, resultado desconhecido ou envio online
  não encerrado bloqueiam novas montagens. Gravação, conclusão, descarte e
  liberações verificam a fila na transação. Uma montagem em edição é preservada
  durante a ambiguidade e liberada após resolução oficial.
- `last-local` mantém uma referência de apresentação após F5/reabertura. A sequência
  compara essa referência com a oficial, para não apresentar uma venda antiga.
- `sales.js`: permite o botão Nova venda no bloqueio dos controles; mantém a
  proteção das ações oficiais durante o funcionamento offline.

Nenhum serviço comercial, regra de estoque/financeiro, modelo, migration,
algoritmo de sincronização ou política de estabilidade foi alterado.
A fila continua sendo enviada por ação manual, em sequência, após 900000 ms.
Conflitos individuais não eliminam as demais operações.

## Assets

Namespace ativo `/offline/assets/2-8f/` e Service Worker
`offline-pilot-shell-v28-2-8f`. Aliases anteriores continuam disponíveis.
Nos demais módulos compartilhados, templates e testes, somente referências de
versão foram atualizadas; não há mudança funcional em compras, contas ou locações.

## Testes isolados

`offline/tests_sales_consecutive_browser.py` usa servidor Django real, SQLite
privado em TEMP e perfis Chrome temporários. Não usa Neon nem dados operacionais.
A confirmação é verificada nos registros do banco de testes.

- A/B/C: três UUIDs e sequências distintos, três operações preservadas, queda real
  de rede via CDP, Service Worker, F5 e fechamento/reabertura.
- Retorno de conexão sem envio automático, bloqueio antes de 15 minutos e envio
  manual na ordem. Três vendas/itens, seis movimentos financeiros e estoques finais
  conferidos; nenhum efeito oficial é criado pela conclusão local.
- Conflito de estoque em B, mantendo A/C confirmadas e B preservada.
- Duas abas disputando liberação/conclusão; ação Nova venda pela interface e
  proteção da montagem mais recente.
- Aborto real da transação IndexedDB durante liberação e conclusão: dados
  preservados e nova tentativa com o mesmo UUID.
- Resultado desconhecido de operação separada da montagem bloqueia edição,
  conclusão e descarte após F5; reconciliação manual preserva a montagem.
- Resposta online perdida após commit: Nova venda recusada, UUID preservado após
  F5 e confirmação recuperada com uma única venda e efeitos oficiais únicos.
- Regressão das proteções online da 2.8D e das cadeias de revisão da 2.8E.

Rodada dirigida final: **12 testes aprovados, zero falhas e zero ignorados**,
em 94,827 s. Inclui os sete cenários da 2.8F e cinco verificações de regressão.

A primeira regressão completa executou 166 testes: 157 aprovados, cinco falhas e
quatro ignorados. Duas expectativas de cache ainda usavam v27; um teste esperava
a mensagem anterior; a simulação de quota só interceptava transações com um único
store. Referências e simulação foram ajustadas, mantendo “Sem número oficial” na
mensagem. O teste de consulta tardia teve timeout nessa rodada e passou na rodada
dirigida sem alteração na lógica de revisão.

Regressão completa final: **166 testes em 544,542 s; 162 aprovados, zero falhas e
quatro ignorados por exigirem PostgreSQL isolado**. Inclui os sete cenários novos,
a recuperação de resposta offline perdida após commit com uma montagem seguinte
preservada e todas as referências finais de assets/cache. A consulta tardia passou
também na rodada completa final, sem alteração na lógica de revisão.

Os quatro testes PostgreSQL existentes não foram reexecutados. Os serviços,
modelos e regras de concorrência do servidor permanecem inalterados nesta etapa.

`git diff --check`: aprovado. Aviso preexistente de namespace `estoque` duplicado
durante os testes; não houve falha funcional associada nesta rodada final.

Logs preservados em TEMP:

- `offline-28f-regression-4936d303-261f-4f7b-963c-5e4b7f8f7cba.log` (rodada inicial).
- `offline-28f-directed-629e1197-be9e-4808-885d-79e5c154ac08.log` (12 dirigidos).
- `offline-28f-final-regression-da2ac104-967f-4fad-b230-8ac614c3c93f.log` (166 finais).

Comando usado, com a variável de banco adicional vazia apenas no processo de teste:

```powershell
$env:OFFLINE_TEST_DATABASE_URL = ''
.\venv\Scripts\python.exe -B manage.py test offline --settings=offline.test_settings --noinput --verbosity=2
```

## Limites e riscos remanescentes

- Conclusão local não reserva estoque no servidor. O catálogo continua sendo
  referência; várias vendas podem exceder o saldo e gerar conflitos no envio.
  O servidor mantém a autoridade sobre estoque, preços e efeitos financeiros.
- Resultado desconhecido bloqueia novas conclusões até reconciliação. Não há
  sincronização automática nem redução da janela de estabilidade.
- Marcadores anteriores sem origem offline explícita não recebem liberação local.
  Devem ser resolvidos pelo fluxo de confirmação/revisão existente. Presumir a
  origem poderia liberar um envio online interrompido.
- O rascunho continua compartilhado por operador/ambiente/dispositivo. Duas abas
  não passam a editar duas montagens independentes simultaneamente.
- Não implementa atualização automática de catálogo ou regras comerciais novas.

## Manifesto para staging seletivo

Somente os 31 arquivos abaixo integram a 2.8F. Excluir
`estoque/templates/estoque/separacao_vendas_fila.html` e todos os arquivos locais
preexistentes e não rastreados.

- `docs/venda-offline-etapa-2.8f.md`
- `estoque/templates/estoque/base.html`
- `estoque/templates/estoque/vendas_layout_teste.html`
- `locacoes/templates/locacoes/includes/offline_checklist.html`
- `offline/templates/offline/sale_revision.html`
- `offline/tests.py`
- `offline/tests_browser.py`
- `offline/tests_commercial.py`
- `offline/tests_commercial_browser.py`
- `offline/tests_operation_lookup.py`
- `offline/tests_sale_release_browser.py`
- `offline/tests_sale_revisions_browser.py`
- `offline/tests_sale_sync_browser.py`
- `offline/tests_sales_browser.py`
- `offline/tests_sales_connectivity_browser.py`
- `offline/tests_sales_consecutive_browser.py`
- `offline/tests_sales_drafts_browser.py`
- `offline/urls.py`
- `static/offline/app.js`
- `static/offline/checklist-restore.js`
- `static/offline/checklist.html`
- `static/offline/checklist.js`
- `static/offline/commercial-ui.js`
- `static/offline/operation-details.js`
- `static/offline/pilot.html`
- `static/offline/sales-draft-ui.js`
- `static/offline/sales-drafts.js`
- `static/offline/sales-revision-ui.js`
- `static/offline/sales-revisions.js`
- `static/offline/sales.js`
- `static/offline/service-worker.js`
