# Correções após teste real offline 2.8F

Base: `bc353e7`, branch `backup/wip-pedidos-base-2026-06-04`.
Sem commit, push, deploy, migrations ou acesso ao Neon. Nenhuma venda real,
operação ou dado do IndexedDB do notebook foi modificado ou reenviado.

## Contexto comprovado e limites da auditoria

O usuário conferiu oficialmente a venda #744: Skol Lata 12/350ml, 1 DZ por
R$ 46,00, e Skol Lata 15/269ml, 1 PCT por R$ 55,00, total R$ 101,00.
Não há divergência comprovada de valor. A venda #745 e a operação em conflito
`97ff91bd...` permanecem preservadas.

A inicialização já usava componentes de data local, e a restauração copia
`draft.data_venda` sem conversão UTC. Reutilização de um rascunho antigo ou início
antes da virada do dia são possibilidades para 08/10/2026, não causas comprovadas
da #744. Sem o rascunho/horário original do notebook, não se reconstrói essa origem.
Nenhuma correção retroativa da data oficial foi feita.

## Causas comprovadas no código

- `validarEstoqueItemVendaAtual` dispensava a comparação de saldo no modo offline.
  `finalizeDraftOffline` não verificava estoque cumulativo.
- O snapshot não era abatido pelas operações locais. Cada nova montagem podia
  comparar quantidades com o mesmo saldo de referência.
- `completed` e a proteção de cliques bloqueavam amplamente controles de
  `#layout-vendas`, atingindo Consultar Vendas e controles auxiliares. O marcador
  da montagem é compartilhado pelas abas do mesmo usuário/ambiente/dispositivo.
- `commercial-ui.js` inseria uma mensagem estática antiga de conclusão indisponível.
- A mensagem de restauração não explicitava a data da venda, e uma montagem nova
  ainda não persistida podia manter a data inicial da página após a virada do dia.

## Correções

### Data e restauração

`salesDraftBridge.ensureNewDate` usa ano, mês e dia locais antes da primeira
persistência de uma montagem nova. Respeita a escolha explícita de data e não é
aplicado sobre o rascunho restaurado. Se um vencimento ainda corresponde ao prazo
automático e a data nova muda, recalcula somente esse vencimento automático;
não muda pagamento nem vencimento escolhido explicitamente.

Nova venda limpa a montagem e usa data local atual. Restauração preserva a data
salva e a apresenta num aviso separado, que não é sobrescrito pelas atualizações
de confirmação da operação anterior. Payloads existentes não são reescritos.

### Estoque local

`sales-stock.js` calcula uma projeção em memória, sem modificar snapshot,
operações, estoque oficial ou financeiro. Confere unidade, fator, fracionamento
e saldo antes da inclusão e antes da conclusão offline. Usa aritmética decimal
na precisão 0,001 e arredondamento half-even, comparada às regras Python existentes.

Soma os itens da montagem e o consumo das operações locais. Uma cadeia de revisão
usa a operação substituta, sem somar novamente os pais. Confirmações ainda não
incorporadas ao catálogo não devolvem ficticiamente saldo local.

Na preparação MANUAL do catálogo, um metadado separado registra os UUIDs já
confirmados antes da consulta de estoque. Assim, uma preparação posterior não
desconta duas vezes efeitos já incluídos no saldo oficial. Para catálogos antigos,
confirmações vinculadas ao mesmo snapshot continuam descontadas. Se uma confirmação
antiga não tem referência suficiente para decidir se está incorporada, exige nova
preparação manual em vez de inventar um saldo.

A conclusão relê snapshot, referência e operações na transação e recusa alteração
concorrente. A operação só é criada após essa verificação. A conclusão aguarda a
atualização do painel/contador. O servidor mantém sua validação definitiva,
transacional e idempotente. A janela de 15 minutos e o envio manual não mudam.

### Conflito, navegação e mensagens

A montagem continua protegida diante de conflito, envio ou resultado desconhecido.
Consultar Vendas, navegação de leitura, diagnóstico, revisão e fechamento do aviso
de operador permanecem acessíveis. Não há liberação automática da operação em
conflito nem remoção de histórico. O fechamento do modal evita focar um operador
desabilitado na montagem protegida.

A mensagem identifica a origem da venda e aponta a conexão atual no painel Status.
Conflito oferece “Revisar venda”; não sugere reenviar a original. O aviso antigo
foi substituído por orientação de salvamento local e revalidação manual.

### Assets

Namespace novo somente para os módulos alterados: `/offline/assets/2-8f-fix/`.
Módulos inalterados continuam em `2-8f`, incluindo o mesmo `core.js` compartilhado.
O cache passa a `offline-pilot-shell-v29-2-8f-fix` e contém ambos os conjuntos
necessários. Não houve troca de versões em compras, contas ou locações.
Após publicação autorizada, reabrir a tela para carregar os novos módulos;
não limpar os dados do site.

## Cancelamento de consumo próprio — somente auditoria

Nenhuma rotina de cancelamento foi executada.

No caminho `estoque/views.py::venda_cancelar`, dentro da transação:

- `_devolver_estoque_cancelamento_venda` devolve as quantidades dos itens ainda
  vinculados a produtos e marca `estoque_devolvido_cancelamento`, impedindo repetição.
- Venda é marcada cancelada; `_sincronizar_despesas_consumo_proprio` zera as despesas
  automáticas existentes, preservando seus IDs e vínculo com a venda.
- Entrega e separação vinculadas são marcadas canceladas; itens da venda e eventos
  anteriores permanecem. É registrado um novo evento de cancelamento.
- Conta a receber existente é cancelada/zerada pelo fluxo aplicável; recebimentos
  históricos são preservados. Estorno de caixa/banco trata pagamento imediato,
  não cria um estorno fictício para consumo próprio.
- O recibo da operação offline não é reaberto; a identidade histórica permanece.

**Ressalva concreta:** a devolução converte a quantidade pela unidade/fator ATUAIS
do produto, não diretamente por `estoque_movimentado` do item histórico. Se fator
ou unidades mudaram após a venda, conferir os snapshots de movimentação antes de
cancelar. Itens sem produto vinculado são ignorados pela devolução. Sem leitura dos
cadastros/vínculos reais, não se afirma que todos esses pré-requisitos estão
satisfeitos especificamente nas vendas #744 e #745. O serviço não foi alterado.

## Validação isolada

`offline/tests_sales_post_trial_browser.py`: seis testes de navegador/servidor real:

- Data local em São Paulo perto da mudança de dia UTC, rascunho antigo restaurado.
- Duas montagens com datas distintas e duas vendas/itens/contas oficiais únicos.
- Saldo principal/secundário, consumo acumulado e impossibilidade de contornar a
  validação chamando diretamente a conclusão.
- Paridade de unidade/fracionamento/fator com as funções comerciais existentes.
- Conflito real preservado após F5 e em duas abas; consulta e OK do modal acessíveis.
- Confirmação não devolve saldo fictício; preparação manual não desconta duas vezes.

SQLite privado e perfis Chrome temporários. Nenhum banco de produção foi utilizado.
Testes direcionados iniciais: seis aprovados, sem falhas/ignorados, em 15,850 s.
Regressão proporcional final: **78 aprovados, zero falhas e zero ignorados**, em
308,389 s. Inclui vendas online/offline, respostas perdidas, idempotência, efeitos
comerciais únicos, rascunhos, revisões, catálogo, consulta e cache atualizado.
Conferência dirigida da versão final: **seis aprovados, zero falhas e zero
ignorados**, em 16,608 s; inclui as verificações adicionais de vencimento automático.
Os seis cenários novos fazem parte da seleção de 78; não são 84 testes distintos.

`git diff --check`: aprovado. Apenas aviso preexistente de namespace Django
`estoque` duplicado. Não houve teste PostgreSQL nesta seleção nem nova instalação.
Logs da regressão em TEMP: `offline-post-trial-regression-fd328601-e846-43f4-82f8-b791b662c920.log`
(rodada inicial) e `offline-post-trial-final-3fa4fdb1-ae57-4148-804c-7dfff08a6fa9.log` (final).

A rodada inicial de 78 testes apontou três ajustes: referência antiga de asset no
teste, cenário que permitia inclusão com saldo zero e leitura do contador antes da
atualização. O teste de saldo agora verifica bloqueio acima do saldo e mantém a
montagem com quantidade válida; a conclusão aguarda a apresentação atualizada.

## Riscos restantes

- Catálogo continua sendo referência. Mudanças externas podem causar conflito
  na sincronização; a projeção não é reserva oficial.
- Metadado ausente/inconclusivo pode exigir preparação manual. Operações e
  snapshots preservados não são apagados para contornar o bloqueio.
- O conflito real não foi revisado nem sincronizado nesta intervenção.
- A causa histórica exata da data da #744 depende do registro original.
- Cancelamento com mudança posterior de conversão exige conferência específica.

## Manifesto de staging seletivo

Somente os 16 arquivos abaixo. Excluir `separacao_vendas_fila.html`, backups,
temporários, `.worktrees/` e todos os arquivos locais preexistentes.

- `docs/venda-offline-pos-teste-2.8f.md`
- `estoque/templates/estoque/vendas_layout_teste.html`
- `offline/tests_browser.py`
- `offline/tests_commercial.py`
- `offline/tests_sale_sync_browser.py`
- `offline/tests_sales_browser.py`
- `offline/tests_sales_post_trial_browser.py`
- `offline/urls.py`
- `offline/views.py`
- `static/offline/commercial-ui.js`
- `static/offline/commercial.js`
- `static/offline/sales-draft-ui.js`
- `static/offline/sales-drafts.js`
- `static/offline/sales-stock.js`
- `static/offline/sales.js`
- `static/offline/service-worker.js`
