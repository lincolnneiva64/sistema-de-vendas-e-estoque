# Fase 2 — auditoria e MVP de observações

## Fluxo auditado

Rotas sob `/locacoes/`:

| Rota | Comportamento atual | Offline no MVP |
| --- | --- | --- |
| `checklist-operacional/` | Agenda de entrega/recolhimento, responsável, links para conferência e formulário de exceção | Leitura preparada e nova observação por tarefa |
| `tarefas-operacionais/<pk>/conferencia-entrega/` | GET prepara formulário; POST chama `ConferenciaEntregaLocacao.registrar` | Leitura preparada e nova observação |
| `tarefas-operacionais/<pk>/conferencia-recolhimento/` | GET prepara formulário; POST chama `ConferenciaRecolhimentoLocacao.registrar` | Leitura preparada e nova observação |
| `conferencias-entrega/<pk>/checklist-entrega/` | Comprovante após conferência; copiar/compartilhar/WhatsApp | Fora do MVP |
| `conferencias-recolhimento/<pk>/checklist-recolhimento/` | Comprovante; POST/fetch confirma envio ao funcionário, grava `checklist_recolhimento_enviado` | Fora do MVP |
| `tarefas-operacionais/<pk>/confirmar/` | Entrega/recolhimento redirecionam para conferência detalhada | Online |
| `tarefas-operacionais/<pk>/nao-possivel/` | POST altera tarefa para `nao_possivel`, tentativa/motivo/responsável e evento | Online |
| `tarefas-operacionais/<pk>/resolver/` | Encaminha para tratamento administrativo | Online |

Models: `Locacao`, `ItemLocacao`, `TarefaOperacionalLocacao`, `ConferenciaEntregaLocacao`, `ConferenciaRecolhimentoLocacao`, `EventoLocacao`, `MovimentoEstoqueLocacao`; protocolo existente em `OperacaoSincronizacao`.

Entrega registra recebedor/relação, observação, materiais previstos/entregues/acumulados/pendentes. Pode registrar saída (`saiu_para_entrega`), reagendar tarefa parcial ou confirmar tarefa/locação entregue. Eventos `checklist_entrega_parcial`, `checklist_entrega_completa`, `entregue` e `saiu_para_entrega`. Não é uma marcação neutra: afeta disponibilidade e estado físico.

Recolhimento registra quantidades boas/quebradas/perdidas/descartadas e pendentes, observação e responsável. Avarias geram movimentos de material; parcial reagenda e mantém `pendente_devolucao`; completo confirma tarefa e muda locação para `devolvida` ou `devolvida_com_avaria`. Eventos `checklist_recolhimento_parcial` e `checklist_recolhimento_completo`. Não há campo de upload de fotos nessas conferências.

Observações dos formulários antigos fazem parte da confirmação física, portanto não são reaproveitadas isoladamente. Nenhuma marcação/conferência/quantidade/confirmação/envio/exceção foi liberada offline. As chamadas HTML/fetch existentes não são replayadas.

## Implementação

O menor comando seguro já existe na Fase 1: `observacao_operacional`, versão 1. Anexa um evento `observacao_offline` à locação, com tarefa e autor. Mantém UUID, hash, identidade, sequência, tentativas, erros e recibo. Não substitui textos existentes nem altera tarefa, conferência, estoque ou financeiro. Anexos independentes de dois dispositivos não sobrescrevem uns aos outros.

Nas três telas operacionais, a observação adicional é sempre salva primeiro no IndexedDB, inclusive online, e enviada somente pelo mecanismo global manual. Confirmações online existentes mantêm seus POSTs. Recuperação local abre o mesmo URL; os formulários antigos ficam bloqueados até reabrir a tela online com estado e CSRF atualizados.

Preparação automática exige `offline.registrar_observacao`. O snapshot por tarefa usa o mesmo endpoint com filtro `?task=` para não depender das primeiras 200 tarefas da agenda. A agenda mantém o limite existente de 200; tarefas fora do snapshot não oferecem observação offline.

O Service Worker guarda apenas assets estáticos e entrega uma shell para navegações operacionais que falham por falta de rede. O conteúdo renderizado preparado fica no IndexedDB, associado a usuário/ambiente/URL exato. Scripts e tokens CSRF são removidos da cópia. O carregador verifica a identidade preparada antes de mostrar o conteúdo. O snapshot contém dados pessoais de operação; use dispositivo/perfil de navegador individual, como na Fase 1.

Sem novo serviço de domínio: `offline.services.process_operation` já contém a transação idempotente e a validação necessária. Status de tarefa alterado, tarefa ausente ou locação encerrada/cancelada produzem `conflito`, preservam payload e não criam evento parcial. Não há last-write-wins. Revisão administrativa dos conflitos continua pendente de uma interface específica; registros locais e backend permanecem disponíveis.

## Limites de validação

Validação executada com `offline.test_settings`, banco isolado: 16 testes de backend aprovados e 1 concorrência PostgreSQL ignorado; 10 testes Chrome aprovados, incluindo refresh, reinício completo do navegador com mesmo perfil, UUID preservado, 15 minutos e sincronização manual. Os testes compartilhados existentes cobrem resposta perdida, recibo definitivo, conflito, sessão, oscilação e exclusão mútua entre abas. Mobile foi emulado em Chrome (320/390 px), sem aparelho físico. `git diff --check` passou.

Suíte operacional de locações: 45 PASS e 4 casos reprovados no código ATUAL (3 FAIL de asserção e 1 ERROR ao ler JSON). Os mesmos quatro casos também foram reproduzidos com os quatro templates originais de HEAD em um diretório temporário, sem modificar os templates do workspace. A Fase 2 não corrigiu esses casos nem introduziu suas falhas: expectativa de JSON no POST AJAX de entrega; redirecionamento de recolhimento completo; texto de checklist compartilhável; WhatsApp na listagem. Não foram corrigidos neste escopo. Também existe o aviso anterior de namespace `estoque` duplicado.

Arquivos deste trabalho: quatro templates existentes (`checklist_operacional.html`, `conferencia_entrega.html`, `conferencia_recolhimento.html`, `includes/checklist_grupo.html`), novo `includes/offline_checklist.html`, `offline/views.py`, `offline/tests.py`, `offline/tests_browser.py`, `static/offline/service-worker.js`, novos `static/offline/checklist.js`, `checklist-restore.js`, `checklist.html`, e este documento. A alteração já existente em `estoque/templates/estoque/separacao_vendas_fila.html` foi preservada.

A cópia local é a última página preparada, não uma agenda completa nem uma autorização nova. URLs com query string distinta devem ser preparados separadamente. Reabrir online atualiza estado; sessão expirada impede sincronização, preservando operações. Navegadores sem Web Locks não sincronizam. IndexedDB depende de quota, persistência concedida e conservação dos dados do site; limpeza do perfil ou armazenamento perdido não pode ser recuperado automaticamente. Queda real de energia e aparelho mobile físico exigem homologação de campo. Não houve commit/push nem mudança de regras comerciais.
