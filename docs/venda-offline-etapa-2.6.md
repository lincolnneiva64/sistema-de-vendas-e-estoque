# Venda Offline — etapa 2.6

## Auditoria antes das alterações

Base `2f7a9d2`. Banco IndexedDB `vendas-offline-pilot`, versão 1, stores metadata
(key), operations/history (operation_id), snapshots (key). Rascunhos estão em
metadata sob `rascunho_venda:` + JSON de [environment, actor, device], com UUID
draft_id e revision. Uma chave :revision permanece após descarte e cerca abas
antigas. sales-identity guarda usuário/ambiente; device guarda UUID e sequence.

Repository.identity incrementa sequence numa transação metadata; create de
observação grava depois em operations. A conclusão de venda precisa incluir
metadata e operations juntos, reutilizando a mesma identidade/contador.
crypto.randomUUID é o padrão. core.hash usa SHA-256 de canonical(commandOf(op)),
com chaves ordenadas e sem payload_hash/estados locais.

Envelope: operation_id/device_id/actor_id/environment_id/type/schema_version/
aggregate_id/payload/created_at/sequence. Estado inicial pendente, attempts 0,
last_error vazio, server_result null. recover converte enviando em
resultado_desconhecido. O sincronizador manual genérico percorre pendente/erro/
resultado_desconhecido por ator/ambiente, sem filtro de tipo. Não há autoenvio.
O indicador conta todas as operações não confirmadas; painel detalhado ainda
usa linguagem de observação. POLICY.window=900000 e verificações continuam iguais.

sales-drafts.js projeta campos do rascunho, faz CAS de revision e valida scope
na mesma transação. sales-draft-ui.js faz autosave/restauração/flush e usa
BroadcastChannel sales-drafts. Marker online independente em localStorage é
por scope/draft_id/revision, estados enviando/concluido; protege POST online e
limpeza após sucesso. Não será a fonte de verdade da conclusão offline.

O sucesso online confirma esse marker, remove draft por CAS e limpa formulário.
sales.js ativa snapshot/shell e atualmente bloqueia os botões oficiais. O template
contém gravarVendaAtual, fechamento caixa/banco e salesDraftBridge de captura e
renderização. Service Worker v19 já inclui os JS de venda e shell público seguro;
não cacheia /vendas/ autenticado. Browser tests existentes são CDP/Chrome real.

## Arquitetura escolhida

Um finalizador local em sales-drafts.js, sem fetch, cria comando criar_venda
schema 1 e aggregate_id=operation_id, removendo nomes/valores calculados/contexto
de edição/Pedido. A revisão e o draft_id identificam a montagem. Metadata recebe
tombstone de conclusão com vínculo operation_id, sem duplicar o payload da venda.
Operations é a única representação comercial persistente da venda pendente.
Save/discard de tombstone não podem recriar/remover a conclusão.

Como WebCrypto é assíncrono, preparar comando/hash fora da transação de escrita;
no commit readwrite(metadata, operations), revalidar scope, draft_id/revision e
sequence. Se outra operação consumiu sequence, recalcular antes de tentar de novo;
se o draft mudou, rejeitar. Se outra conclusão venceu, retornar o vínculo existente.
Não manter uma transação artificialmente aberta durante WebCrypto.
Contador só muda junto com operação e tombstone; abort desfaz os três.

UI de menor impacto: botão Salvar venda offline e bloqueio do formulário após
conclusão, com UUID abreviado e aviso sem número oficial, aguardando envio manual
futuro. BroadcastChannel auxilia outras abas; armazenamento é a autoridade.
Marker online de resultado incerto continua impedindo transformar a mesma venda
em nova operação offline, evitando duplicar um POST possivelmente já confirmado.

## Implementação final

Assinatura:

```js
finalizeDraftOffline(repo, scope, {
    revision,
    draft_id,
    origem_recebimento // opcional, caixa/banco em strings
})
```

Retorna `{finalization, operation, alreadyFinalized:false}` no primeiro commit;
repetição retorna `{finalization, alreadyFinalized:true}`. O vínculo é o tombstone
no mesmo draftKey, tipo venda_concluida_offline, com schema, scope, draft_id,
revision incrementada, operation_id e finalized_at. Não guarda itens/total nem
segunda cópia comercial da venda. loadDraft retorna draft null e finalization.
saveDraft/discardDraft rejeitam esse estado com code draft-finalized-offline e UUID.

operation_id reutiliza o próprio draft_id, que já é um crypto.randomUUID único
gerado e persistido quando o rascunho é criado. A promoção desse UUID a operação
evita criar/reservar identificadores fora do commit: falha, retry, abas concorrentes
e clique duplo usam o mesmo UUID. Não é um Venda.id local. aggregate_id é esse UUID.
Novo rascunho de outro scope recebe outro UUID pelo mecanismo existente.

O payload usa schema_version 1, cliente_id (string/null), data_venda,
data_vencimento, tipo_pagamento, operador e itens. Cada item contém apenas
produto_id, quantidade, unidade e preco_unitario. Caixa/banco, quando informados,
preservam as strings do fechamento atual (inclusive vírgula decimal).
Nomes, custo, subtotal/total, estoque, conversão de referência, item_id, venda_id,
Pedido e contexto de edição não são enviados. Produto/custo/saldo/pagamento serão
revalidados pelo backend; aqui se valida estrutura, não preço/custo/saldo atual.
Editor de item ainda em montagem precisa ser adicionado/encerrado antes da conclusão.

O hash é o core.hash existente, calculado sobre o mesmo comando canônico da 2.5.
Campos locais status/attempts/last_error/server_result não entram no hash.
Operação: status pendente, attempts 0, last_error vazio, server_result null.

### Transação e sequence

1. Leitura metadata numa transação: scope, draft atual, revisão e contador.
2. Projetar payload/envelope e calcular SHA-256 fora de transação readwrite.
3. Abrir readwrite(metadata, operations), revalidar identidade, draft e revisão.
4. Se outra conclusão venceu, devolver marcador existente, sem writes/sequence.
5. Se apenas contador mudou, sair sem writes e repetir preparação com nova sequence.
6. reserveSequence no store metadata; operations.add; tombstone e revision.put.
7. Retornar sucesso somente em tx.oncomplete, nunca somente em request.onsuccess.

reserveSequence é helper comum extraído do incremento existente. Repository.identity
continua usando metadata e esse mesmo contador; observações não ganham outra
sequência. No finalizador a reserva acontece dentro do commit conjunto. Valida
inteiro JS seguro/overflow. Abort em qualquer request ou antes do commit desfaz
contador, operação e tombstone. A revisão antiga fica preservada na falha.
Troca de actor/environment/device é rejeitada dentro da transação.

### UI, abas e reload

O ramo offline de gravarVendaAtual espera salesDraftReady e chama somente o
finalizador local; edição/Pedido continuam bloqueados. Venda à vista usa o modal
de fechamento existente e passa origem_recebimento ao finalizador. Online permanece
com POST, submission marker e limpeza anteriores.

Durante finalização, edição e botões ficam bloqueados; falha local devolve controles
ao estado anterior e mantém o draft. Sucesso limpa montagem visual e bloqueia o
formulário, mostrando: venda salva neste aparelho, aguardando sincronização, sem
número oficial e UUID abreviado. Não cria financeiro/despesa/estoque local oficial.
Não há mutação do snapshot. O formulário dessa montagem permanece bloqueado ao
reabrir; não se implementou edição da operação concluída nem botão de nova montagem.

Promise de finalização compartilhada impede duas chamadas de UI. A proteção real
é a transação IndexedDB com CAS/tombstone, inclusive com duas conexões do banco.
Evento sales-drafts com type draft-finalized-offline, scope, revision e operation_id
avisa outras abas, que leem o estado persistido e bloqueiam a edição. Aba sem receber
broadcast não pode salvar versão antiga, pois saveDraft encontra o tombstone.
Reload/restart carrega esse estado e não restaura o rascunho como editável.

LocalStorage continua sendo marker exclusivamente de envio online, não participa
do commit offline. Se há marker de envio/confirmado para esse draft/revision, a
conclusão offline é rejeitada explicitamente; não se converte um envio anterior
com resultado desconhecido em outra venda. Marker de outro draft/revision não afeta
a montagem nova. O tombstone offline é identificado antes de ler marker online.

### Fila, envio e Service Worker

Não há fetch/probe/synchronize no finalizador. Um evento local e BroadcastChannel
offline-pilot apenas atualizam contagem/lista. O painel genérico agora nomeia
criar_venda como Venda offline, sem texto undefined/tarefa ou número oficial fictício.

A função synchronize foi comparada integralmente com 2f7a9d2: **igual**. Já reconhece
operations por status/ator/ambiente sem filtrar tipo, como solicitado. Não foi criado
novo botão de envio nem chamada automática. O sincronizador manual preexistente
permanece disponível sob sua própria política; conclusão não o invoca. A volta da
conexão mantém pendente e não dispara POST. POLICY, janela de 15 minutos e health
foram comparados/preservados; testes de estabilidade continuam usando essa política.
A etapa 2.7 fará o fluxo de envio/reconciliação na interface da venda.

Cache do Service Worker: v19→v20, para distribuir versões atualizadas dos assets
e do shell existentes. Nenhum asset novo foi acrescentado. Lista de assets, shell
seguro, política de GET/navegação e proibição de cache autenticado permanecem iguais.
Banco IndexedDB continua versão 1 e usa apenas stores existentes.

## Arquivos desta etapa

- static/offline/core.js: helper de sequence reutilizado.
- static/offline/sales-drafts.js: finalizador/transação/tombstone.
- static/offline/sales-draft-ui.js: conclusão, bloqueio e broadcast.
- static/offline/sales.js: apresentação offline e bloqueio de ações oficiais.
- estoque/templates/estoque/vendas_layout_teste.html: ramo local do botão/fechamento.
- static/offline/app.js: rótulo de fila e atualização do indicador, sem mudar sync.
- static/offline/service-worker.js: versão v20.
- offline/tests_sale_finalization_browser.py: testes novos de IndexedDB/Chrome.
- offline/tests_sales_drafts_browser.py e offline/tests_sales_browser.py: expectativas
  atualizadas de botão agora habilitado offline; espera de prontidão no preparo.
- offline/tests_browser.py: expectativa v20 e limpeza dos caches anteriores.
- Este documento.

Backend da 2.5 e serviço/view oficial da venda sem alterações. Não há migration,
commit, push, staging ou alteração de arquivos locais antigos. Template protegido
estoque/templates/estoque/separacao_vendas_fila.html conserva a alteração local anterior.

## Testes e evidências

Chrome real/CDP, IndexedDB nativo e banco Django isolado SQLite em memória,
`--settings=offline.test_settings --noinput`. Logs novos somente no TEMP, prefixo
venda26-. Chrome falhou no sandbox (conexões encerradas); testes de navegador
foram executados fora dele com aprovação. Não foi usado banco operacional.

Antes das alterações: tests_sales_drafts_browser + tests_sales_browser, 7 testes,
todos aprovados na base, após repetir a execução fora do sandbox.

Após alterações: `manage.py test offline.tests_sale_finalization_browser
offline.tests_sales_drafts_browser offline.tests_sales_browser --verbosity=1`:
14 testes aprovados. Após promover o UUID persistente a operation_id, repetiu-se
o módulo dos 7 testes novos para validar também retry com o mesmo UUID.

`manage.py test offline.tests_sale_creation offline.tests offline.tests_commercial
estoque.tests_servico_vendas estoque.tests_consumo_proprio --verbosity=0`:
91 testes, 88 aprovados, 3 pulados por exigirem PostgreSQL isolado.

Regressão geral `offline.tests_browser offline.tests_commercial_browser`:
22 testes; 20 aprovados e 2 casos visuais anteriores falhando. O caso de cache foi
atualizado para v20 e reexecutado: passou dessa verificação e apresentou somente
as expectativas antigas de cor. Ambos os casos foram executados numa cópia temporária
**intacta** extraída de `git archive 2f7a9d2`, com SECRET_KEY apenas de teste e SQLite
isolado. A base apresentou as mesmas cores divergentes:

- test_global_indicator_on_normal_page_and_mobile: branco atual vs verde esperado.
- test_shared_state_visuals_on_pilot_and_sales: verde atual vs fundo claro esperado,
  nas larguras 1280/390/320. A execução da base registrou 4 falhas de assert em 2 casos.

Não corrigidas cores/estilos fora do escopo. Não afirmar aprovação integral da suíte.
As demais verificações de estabilidade, health, observações, fila manual, login,
snapshot e navegação não apresentaram falhas na seleção executada.

### Provas dos testes novos

| Cenário | Evidência |
| --- | --- |
| Duplo clique e chamadas concorrentes | Promise.all com duas Repository/conexões: uma operação, N→N+1, mesmo UUID; dois clicks na UI |
| Protocolo/hash | Envelope enviado à validate_command da 2.5 em Python; hash recomputado; schemas/actor/environment/device/aggregate corretos |
| Payload mínimo | Itens só com 4 campos, sem totais/custo/estoque/nomes; snapshot e tabelas backend intactos |
| Falha IndexedDB | Erro no add, put de tombstone e abort após requests antes do commit: operations=0, contador/revision/draft idênticos; retry funciona |
| Aba antiga | save/discard da revisão antiga rejeitados, tombstone não ressuscita, abas identificam mesmo UUID |
| Reload e restart | Sem draft editável, operação persistente e indicador com uma pendência |
| Sequence | Consumida uma vez; próxima conclusão usa próxima sequence; consumo concorrente por identity recalcula hash sem duplicar; outro device inicia seu contador |
| Scope | Actor/environment/device incompatíveis não concluem o draft |
| Pagamentos | Vista preserva caixa/banco, prazo preserva vencimento, consumo apenas comando local |
| Marker online | Envio anterior impede conversão; liberação permite conclusão, estados offline separados |
| Sem servidor | Fetch substituído por função que falha e conta chamadas: zero durante conclusão; tabelas oficiais inalteradas |
| Offline real | Shell cacheado, rede CDP bloqueada em página e SW, fechamento vista com vírgula, operação pendente; evento online sem POST |
| Validação mínima | Draft vazio, ID ausente, data/pagamento/unidade vazios não concluem |

Testes antigos só tiveram expectativas do comportamento autorizado atualizadas:
botão offline agora permite conclusão local e cache é v20. Testes de descarte,
autosave, editor, multitab, limpeza online e proteção de sucesso confirmado seguem
executando. Não foi reduzida a validação de ausência de efeitos oficiais durante
montagem/conclusão local.

`git diff --check`: aprovado. Avisos de normalização LF→CRLF e namespace estoque
duplicado são anteriores. Hash/protocolo backend e synchronize/POLICY preservados.

## Limitações e etapa 2.7

- A montagem concluída fica bloqueada, inclusive ao reabrir. Nova montagem/gestão
  de vendas locais concluídas e sua integração de envio ficam para a próxima etapa.
- Não há edição/Pedido offline, reconciliação comercial, projeção oficial de estoque
  ou financeiro nem venda_id local. Snapshot antigo é referência, não autoridade.
- O sincronizador manual genérico já reconhece o comando, mas não foi chamado nem
  integrado automaticamente pela conclusão. Não existe envio na volta da conexão.
- Persistência depende do armazenamento do navegador/dispositivo, como antes;
  falhas locais preservam o último draft confirmado e são mostradas ao usuário.
- Erros de negócio serão resolvidos no servidor no envio futuro; não há correção
  silenciosa de preços, estoque, unidade ou vencimento nesta etapa.
- Duas falhas visuais preexistentes permanecem documentadas. Concorrência do backend
  PostgreSQL continua fora deste teste local; IndexedDB foi testado de verdade.

A conclusão local grava uma única operação persistente, reserva uma sequence uma
vez, desativa o draft atomicamente e é recuperável após reload, sem falar com o
servidor. Não foi implementado fluxo da etapa 2.7.
