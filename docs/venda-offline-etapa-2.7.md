# Venda Offline — etapa 2.7

## Auditoria anterior às alterações

Base `2b5a620`. `app.js:synchronize` só é chamado pelos botões manual/global,
abre confirmação e usa Web Locks offline-pilot-sync. runSynchronization exige
snapshot pilot, sessão/permissão, ator/ambiente compatíveis e Stability.ready.
Esse snapshot de tarefas é uma dependência desnecessária para vendas preparadas
apenas com catálogo comercial. Não existe whitelist de tipos no envio.

Seleção: operations do actor/environment, status pendente/erro/
resultado_desconhecido, ordenadas por sequence. Cada envio grava enviando e
incrementa attempts antes de POST /api/offline/observations/. commandOf copia
os campos imutáveis existentes e envia o payload_hash persistido. Nenhum UUID,
sequence, hash, timestamp ou payload é recalculado no retry.

Antes dos POSTs, GET snapshot autentica ator/ambiente e obtém CSRF em memória,
sem substituir snapshot preparado. HTTP 200/409 passam por validReceipt e
Repository.record; 400/413 tornam erro com revisão técnica; 401/403 tornam erro,
pedem autenticação e param lote; demais status, timeout, fetch rejeitado, JSON
inválido ou receipt incompatível tornam resultado_desconhecido e interrompem lote.
Não são conflitos comerciais. probe/health mantém sua política existente.
Conflito válido é persistido e o lote continua para o próximo item.

Repository.record usa uma transação operations/history e preserva comando,
server_result, status e synchronized_at no histórico. Confirmadas permanecem
armazenadas, mas saem da contagem/seleção. Conflitos permanecem visíveis e não
entram no envio comum. recover converte enviando em resultado_desconhecido sob
o lock ao reabrir. Attempts é preservado; estado incerto reenvia o mesmo UUID.

validReceipt já verifica UUID/hash, confirmação com PK positiva inteira e data
parseável; conflito é permissivo demais para o novo receipt completo de venda.
Ainda não confere coerência status HTTP/status do receipt. Um erro de render após
record pode sobrescrever receipt já persistido no catch; será protegido.

POLICY.window=900000, interval=30000, maxGap=60000, timeout=5000. Observações
reais de health sustentam estabilidade; falhas de comunicação reiniciam contagem.
online/pageshow/F5 só verificam saúde/sessão; não chamam synchronize.
BroadcastChannel offline-pilot publica changed/escopo/progresso. Evento
offline-operations-changed atualiza indicador após conclusão local. sales-drafts
avisa conclusão entre abas; o tombstone em metadata aponta para operation_id.
A UI atual não resolve operations.server_result: sempre bloqueia formulário e
mostra aguardando, sem número oficial. Será corrigida por leitura da operação.

## Decisão

Um único sincronizador. Usar escopo preparado de vendas/global para operações
criar_venda mesmo sem snapshot pilot; manter checagem de sessão e identidade
online antes do POST. Não alterar política de estabilidade, recuperação, ordem,
comando, snapshots comerciais, backend ou efeitos oficiais.
Receipt completo de venda será validado antes de record e de exibir PK oficial.
Tombstone continua intacto; operations/history guardam vínculo oficial.
Mesmo evento local e BroadcastChannel atualizam UI por leitura de IndexedDB.
Confirmação permite link conhecido /vendas/PK/; conflito é texto seguro e bloqueado.
Sem edição/correção de conflito ou nova operação automática. SW v20→v21 apenas
para entregar JS/shell atualizados, sem mudança de estratégia/cache autenticado.

## Integração final

Não há outro motor/fila/endpoint. runSynchronization usa o scope do indicador
atual; no shell público sem actor no HTML pode usar sales-identity do mesmo
ambiente. A existência de criar_venda desse scope autoriza o preparo local de
vendas como origem da fila, sem exigir um snapshot de tarefas. Operações de venda
confirmadas/conflitantes também mantêm esse contexto preparado, para não bloquear
observações remanescentes. Elas não entram na seleção de envio por isso.
Observações mantêm o caminho de snapshot pilot já preparado.

Antes do lote, checkSession verifica sessão/permissão; GET snapshot fornece
identidade atual e CSRF em memória. Actor, environment e protocol_version devem
coincidir. Divergência interrompe sem POST nem incremento de attempts. Não se
adota a identidade recebida para reescrever comandos de outra pessoa/ambiente.
O fallback exige sales-identity existente e indicador compatível; ausência de
dados no piloto continua usando anonymous, sem acessar objeto inexistente.

Seleção/ordem/lock/modal continuam os mesmos: pendente, erro e
resultado_desconhecido, por actor/environment e sequence crescente. enviando,
confirmada e conflito não são selecionados. O usuário precisa clicar no botão
existente e confirmar no diálogo, com Stability.ready e Web Locks disponíveis.
POLICY.window continua exatamente 900000. Não há chamada de synchronize em
online, health, carregamento, F5, leitura do tombstone ou atualização de receipt.

O POST usa commandOf da operação persistida + payload_hash persistido. Comparação
com a base confirmou commandOf/canonical/POLICY iguais. Não há hash novo, UUID,
sequence, timestamp, ajuste de preço, catálogo ou novo draft no envio/retry.

## Receipt e estados

validReceipt valida identidade/hash tipados e correspondentes à operação; hash
local deve ser hexadecimal SHA-256. Status HTTP deve corresponder ao resultado:
200→confirmada, 409→conflito. Confirmação exige record_id positivo inteiro seguro
JS e completed_at parseável. Para criar_venda, completed_at também precisa ser
timestamp ISO com timezone; conflito exige record_id null e erro string não vazia.
O contrato dos receipts legítimos de observação permanece compatível.
Repository.record revalida antes de escrever, numa única transação
operations/history. Não confia apenas no status HTTP nem exibe número inválido.

| Resposta | Persistência local e comportamento |
| --- | --- |
| 200, receipt confirmado válido | confirmada, server_result integral, last_error limpo, cópia em history, sai da contagem |
| 409, receipt de conflito válido | conflito, server_result integral e mensagem em last_error/history; continua visível; não é reenviado no fluxo comum; lote continua |
| 400/413 | erro, mensagem de formato/revisão técnica; payload preservado; lote interrompido; sem correção automática |
| 401/403 no POST | erro, mensagem de sessão/permissão/CSRF; autenticação solicitada; fila preservada e lote interrompido |
| 401/403/identidade incompatível antes dos POSTs | operação ainda não enviada preserva estado/attempts; interrompe preparo |
| 5xx, fetch rejeitado, timeout, JSON inválido ou receipt incompatível | resultado_desconhecido, comando inteiro preservado e mensagem técnica; interrompe lote, health decide comunicação/estabilidade; retry somente manual |
| Fechamento durante envio | recover converte enviando em resultado_desconhecido, sem trocar comando/UUID; não envia ao reabrir |
| Falha visual depois de record ter sido confirmado | receipt já persistido não regride para resultado_desconhecido nem perde vínculo oficial |

Não é criado estado paralelo confirmado: o nome persistido é **confirmada**,
igual ao backend. HTTP 409 sem o receipt completo/compatível de venda, inclusive
resposta curta de colisão de UUID/conteúdo, é rejeitado como falha de protocolo;
comando fica recuperável, sem fingir confirmação/conflito comercial persistido.

400/401/403 preservam a semântica retryável erro já existente. Não se tenta corrigir
formato ou identidade automaticamente. Falha técnica não vira conflito definitivo.
Health mantém sua regra: falha real reinicia estabilidade; resposta técnica de
negócio/protocolo não inventa uma nova regra de conexão. O usuário ordena cada retry.

## Resposta perdida e histórico

Se backend confirmou e a resposta se perdeu, frontend persiste
resultado_desconhecido. Na próxima ação manual envia novamente os mesmos campos,
mesmo UUID/hash/sequence/created_at. O backend idempotente da 2.5 retorna o receipt
original. record_id e completed_at são preservados no server_result, sem repetir
estoque, itens, evento, recebimento, conta ou despesa.

Confirmadas não são apagadas de operations. history copia a operação com scope,
device, sequence, comando/hash, server_result/completed_at e synchronized_at,
conforme arquitetura existente. O tombstone da 2.6 não ganha outra PK/tabela:
operation_id→operations.server_result.record_id é o único vínculo oficial local.
Nenhum saldo/projeção financeira ou subtração de estoque é criada no IndexedDB.

## Interface e abas

saleOperationState em core.js projeta estado da operação para painel e tela.
sales-draft-ui lê operations pelo UUID do tombstone, verifica scope/device/type e
renderiza com textContent; não altera tombstone, payload, preço ou status.
Leituras assíncronas antigas são cercadas por geração para não sobrescrever estado
mais recente ou reapresentar link após mudança de identidade.

- pendente: Venda offline aguardando sincronização, sem número oficial.
- enviando: Enviando venda offline, ainda sem confirmação.
- confirmada válida: Venda #ID sincronizada; botão principal continua bloqueado,
  e link Ver venda #ID aponta somente para a rota conhecida `/vendas/ID/`.
- conflito: Venda offline precisa de revisão + mensagem literal do servidor,
  sem edição, descarte ou novo UUID automático.
- erro/resultado_desconhecido: não foi possível sincronizar ou resultado ainda
  não confirmado, com mensagem apropriada e comando preservado.
- receipt/registro local inválido: não exibir ID/URL oficial como confirmado.

Não armazenar URL em receipt nem consumir URL arbitrária do servidor. O link usa
somente PK validada e abre com noopener/noreferrer. O bloqueio da montagem libera
apenas esse link conhecido; controles de edição continuam desabilitados.
Mensagem de conflito é texto, inclusive quando contém `<img ...>` malicioso.
O painel detalhado de vendas não usa tarefa/observação/evento # para criar_venda.

changed publica BroadcastChannel offline-pilot e CustomEvent
offline-operation-updated após as gravações. Tela atual e outras abas releem
IndexedDB; não confiam em um record_id vindo do broadcast. recover também emite
evento local. Focus/visibility atualizam leitura quando necessário. Reload e
restart reconstroem a mensagem/link da operação persistida; não criam draft novo.
O banner de retorno online não promete restaurar uma montagem concluída como draft.

## Arquivos

- static/offline/app.js: contexto de vendas sem pilot, receipt/status HTTP,
  preservação pós-record, publicação de atualização e textos de painel.
- static/offline/core.js: validação de receipt/record e projeção da venda.
- static/offline/sales-draft-ui.js: resolver operação, estados, link e multitab.
- static/offline/sales.js: permitir link oficial validado e texto de reabertura.
- static/offline/service-worker.js: cache v20→v21, mesmos assets e estratégias.
- offline/tests_sale_sync_browser.py: oito testes integrados novos, com subcasos.
- offline/tests_browser.py: expectativa de cache v21/limpeza de versões anteriores.
- offline/tests_sales_drafts_browser.py: aguardar startup do app e consultar/clicar
  autocomplete na mesma tarefa CDP, evitando corrida do teste entre dois comandos.
- Este documento.

Backend, modelos, migrations, serviço oficial da venda e protocolo/hash sem
alteração. O finalizador em sales-drafts.js e o template online permanecem intactos;
a apresentação da montagem concluída evoluiu conforme descrito acima. Banco local continua versão 1 com
metadata/operations/history/snapshots. Template protegido conserva modificação
local anterior; .bak, .txt antigos, diagnostics e scripts auxiliares preservados.
Sem staging, commit ou push.

## Testes executados

Chrome/CDP real, IndexedDB nativo, Django/SQLite em memória, settings isolados
offline.test_settings. Testes Chrome fora do sandbox, como autorizado nas etapas
anteriores; nenhuma utilização do banco operacional. Logs novos no TEMP, prefixo
venda27-. Relógio injetado acelera 30 intervalos de health de 30 segundos, sem mudar
POLICY.window, e usa respostas reais do health para observar a janela inteira.

1. `manage.py test offline.tests_sale_sync_browser offline.tests_sale_finalization_browser
   offline.tests_sales_drafts_browser offline.tests_sales_browser
   offline.tests_browser.OfflineBrowserTests.test_checklist_visible_resume_with_suspended_probe
   --settings=offline.test_settings --noinput --verbosity=1`: **23 aprovados**.
2. Após incluir snapshot comercial real preenchido: `manage.py test
   offline.tests_sale_sync_browser --settings=offline.test_settings --noinput
   --verbosity=1`: **8 aprovados**. Cache de snapshot permanece idêntico após sync.
3. `manage.py test offline.tests_sale_creation offline.tests offline.tests_commercial
   estoque.tests_servico_vendas estoque.tests_consumo_proprio
   --settings=offline.test_settings --noinput --verbosity=0`: **91 testes, 88
   aprovados, 3 pulados**, pois exigem PostgreSQL isolado.
4. `manage.py test offline.tests_browser offline.tests_commercial_browser
   --settings=offline.test_settings --noinput --verbosity=1`: 22 testes;
   a rodada ampla após correção do fallback teve 19 casos aprovados, 2 casos
   visuais preexistentes falhando e 1 timeout na prontidão do checklist. Este
   último foi repetido sem testes concorrentes no item 1 e **passou**.

Verificação final após explicitar limpeza de last_error na confirmação:
`offline.tests_sale_sync_browser.SaleSyncBrowserTests.test_resposta_perdida_apos_commit_retry_mesmo_comando_sem_duplica_efeitos`,
**aprovado**, com os três subcasos de pagamento (prazo/vista/consumo).

Os dois casos visuais são os mesmos documentados na 2.6 e reproduzidos naquela
etapa na base intacta: global_indicator_on_normal_page_and_mobile espera texto
verde em vez do branco atual; shared_state_visuals_on_pilot_and_sales espera fundo
claro em vez do verde atual nas larguras 1280/390/320. Foram 4 asserts de cor nesses
2 casos. Não corrigir CSS fora do escopo; não afirmar suíte ampla totalmente verde.

Rodadas intermediárias detectaram e corrigiram fallback sem sales-identity no
piloto; depois passaram os testes correspondentes de saúde/sessão/probe/navegação.
Corrida de autocomplete do harness foi corrigida aguardando startup e consultando/
clicando na mesma tarefa. O timeout restante de checklist passou na repetição
isolada sem mudança da política de saúde ou dos scripts de checklist.

### Evidências dos oito testes novos

| Teste/casos | Prova |
| --- | --- |
| Ação manual e janela | online e 29 ticks não enviam; 30 ticks (900000) ainda não enviam; botão e confirmação manual enviam; sem snapshot de tarefas |
| Enviando/confirmada/multitab | Resposta retida depois do POST mostra enviando nas duas abas, sem link falso; liberação mostra mesma Venda #ID; contador vai a 0 |
| Confirmação/reload/restart/history | Receipt/last_error/attempts persistidos; link local validado; history com referência; F5 e novo processo Chrome mostram mesma Venda |
| Resposta perdida real, três pagamentos | Backend processa e confirma, cliente consome resposta e lança erro; estado incerto; segundo POST igual ao primeiro; conta/movimentos/despesa/itens/eventos/estoque idênticos após retry |
| Conflitos comerciais | Mudar saldo ou custo após conclusão gera 409; zero venda; payload/receipt preservados; erro visível; reload mantém conflito; nenhuma tentativa adicional |
| Texto inseguro | `<img onerror=...>` permanece texto, sem elemento/script na tela |
| Receipt falso | UUID/hash errados, PK zero/string/boolean, data inválida/ano sem timestamp, 200/conflito, 409/confirmada e conflito incompleto não confirmam nem criam history |
| Técnicos/auth | 400/401/403→erro; 500/502/503/fetch/timeout real de 5s/JSON inválido→resultado_desconhecido; mesmo comando; reload/recover enviando preservam retry |
| Duas vendas/fila mista | Dois UUIDs e sequences consecutivas criam IDs distintos; venda em conflito não impede locação/entrega; replay dos dois IDs não troca receipts nem duplica efeitos |
| Falha após commit local | Erro injetado no render depois de record não sobrescreve receipt confirmado nem history/link |
| Identidade fresca incompatível | Actor/environment/protocolo diferentes no GET de preparo impedem qualquer POST; attempts continuam 0 e payload permanece |
| Snapshot/financeiro local | Snapshot comercial real não muda após confirmação/replay; efeitos financeiros somente no banco oficial, nenhuma store financeira nova |

As contagens finais do teste de resposta perdida nos três pagamentos são três
vendas distintas, uma ContaReceber, dois movimentos da venda à vista dividida e
uma DespesaDiaria de consumo; cada venda tem um ItemVenda e um evento inicial.
O replay compara registros/saldos integrais antes/depois, não só count.

`git diff --check`: aprovado. Warnings anteriores: namespace estoque duplicado e
normalização LF→CRLF. Conferência automática: POLICY/canonical/commandOf iguais
à base e nenhum diff dos arquivos backend protegidos pelo escopo.

## Riscos e etapa 2.8

Não há concorrência PostgreSQL nova nesta etapa; três testes permanecem pulados
por ausência de banco PostgreSQL isolado, como na 2.5. As falhas visuais anteriores
continuam registradas. Persistência depende do armazenamento do navegador, como
antes; falha técnica mantém comando para nova ação manual.

Não foi criado editor de operação, novo draft automático, correção de preço/
quantidade/produto, descarte de conflito, projeção de saldo ou atualização de estoque
no snapshot. A montagem concluída permanece bloqueada mesmo confirmada; consulta
oficial é feita pelo link. Revisão/correção de conflitos e gestão de novas montagens
ficam para etapa futura, sem trocar UUID de um retry legítimo.

Uma venda concluída offline só é enviada por ação manual após estabilidade e
identidade compatível. Confirmação/conflito/incerteza ficam persistidos. Resposta
perdida é recuperável com o mesmo comando, sem segunda venda. Não foi avançada a 2.8.
