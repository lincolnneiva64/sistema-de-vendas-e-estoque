# Resolução individual de vendas offline pendentes

Implementação local de 11/11/2026. Complementa o encerramento histórico em
`venda-offline-encerramento-administrativo.md`. Validação final retomada e
concluída em 11/11/2026: quatro testes Chrome, sete de concorrência PostgreSQL
e dez de servidor aprovados. A publicação depende da sequência coordenada abaixo.

## Operação prevista

A ação **Resolver venda pendente** aparece na lista e na montagem concluída.
Ela abre um único diálogo com cliente/valor, motivo, confirmação explícita e
consulta de resultado. Não exige interpretar identificadores nem cria outra
venda ou revisão. Havendo substituta local aberta, apresenta primeiro a última
tentativa; após a confirmação do servidor, permite voltar à original.
Dependências encontradas somente no servidor também podem ser resolvidas pelo
mesmo diálogo, preservando os comandos e o histórico do dispositivo de origem.

Somente o recibo administrativo definitivo permite retirar a tentativa das
pendências ativas. GET sem registro, indisponibilidade ou resposta perdida não
atestam encerramento. A intenção completa fica salva antes do POST; uma nova
tentativa usa a mesma solicitação. Enquanto essa intenção existir, a
sincronização manual não envia o comando afetado. A consulta automática também
ignora operações já encerradas e intenções administrativas ainda pendentes.

Se já houver venda oficial, a resolução mostra seu número e link e não registra
abandono. Se o servidor ainda estiver processando, mantém tudo preservado e
pede nova consulta. Nenhuma ausência é interpretada como sucesso.

## Garantias do servidor

- `AbandonoVendaOffline` reserva permanentemente o identificador e o comando,
  mesmo se `OperacaoSincronizacao` ainda não existir. Guarda operador, ambiente,
  dispositivo solicitante, motivo, data e evidência do recibo anterior.
- `ControleResolucaoVendaOffline` ordena decisões e envios da mesma conta.
  No PostgreSQL, uma trava transacional por identificador também arbitra contas
  diferentes e impede reutilização da identidade pela modalidade LOCAL.
- A ordem é conta, identificador, operação/original. Encerramento antigo e
  envio autenticado adotam a mesma ordem. O serviço comercial não é chamado
  pela resolução administrativa.
- Original com dependências não resolvidas não é encerrada. Cada dependência
  exige sua própria decisão, da última tentativa para a primeira. Recibos,
  comandos, vínculos e operações existentes permanecem intactos.
- Reenvio de operação reservada sem registro anterior é recusado. Se já havia
  recibo comercial, o replay conserva esse recibo; a decisão administrativa é
  consultada separadamente. Novas substitutas de original encerrada são recusadas.
- Espelhar a decisão administrativa e liberar a montagem são etapas separadas.
  Um marcador posterior/incompatível é preservado e não desfaz o encerramento.
  Resultado desconhecido de venda independente não impede o encerramento nem
  força sua consulta ou envio por este fluxo.

As garantias pressupõem os escritores do servidor atualizados. Antes de uma
publicação futura, é necessário coordenar schema e processos: código antigo
não consulta a nova reserva nem adota as novas travas. Não misturar escritores
antigos e novos durante a habilitação do recurso.

## Migration e cache

`offline/migrations/0006_resolucao_venda_offline.py` cria somente as duas tabelas
novas; não saneia nem encerra operações existentes. O arquivo foi escrito, mas
não aplicado em produção. Os testes criam diretamente schema descartável,
sem executar o executor de migrations. Um teste compara as declarações dessa
migration com os modelos sem executá-la.

Novo namespace de módulos: `/offline/assets/sale-resolution-v1/`.
Service Worker: `offline-pilot-shell-v52-sale-resolution`. Atualizar o worker
online e reabrir a página será necessário após eventual publicação autorizada.
Não limpar dados do site, IndexedDB ou histórico.

### Sequência segura de publicação futura

1. Preparar uma etapa de schema explicitamente autorizada para `offline.0006`,
   mantendo o backend antigo antes da ativação da resolução. A migration é
   aditiva, cria tabelas vazias e não encerra nenhuma tentativa existente.
   Conferir suas dependências antes da aplicação; a situação do Neon não foi
   consultada nesta auditoria.
2. O `build.sh` chama `publicar_cancelamento`, cujo conjunto `AUTHORIZED` não
   inclui `offline.0006`. Se ela estiver pendente, esse comando recusa o plano
   completo. Não desabilitar esse bloqueio: aplicar a migration numa etapa
   anterior autorizada ou revisar explicitamente o plano de publicação.
   Não executar um `migrate` geral como atalho.
3. Drenar envios em andamento e impedir a resolução administrativa durante a
   troca. Atualizar todos os processos que recebem comandos e encerramentos
   antes de permitir decisões. Escritores antigos não consultam a reserva;
   misturar versões do servidor não é uma publicação segura.
4. Com schema e backend novos conferidos, disponibilizar o worker v52 e os
   módulos versionados. Atualizar o worker online e reabrir as páginas sem
   limpar IndexedDB. A instalação só ativa o cache novo após obter todos os
   assets; uma aba aberta continua executando o JavaScript que já carregou.
5. Navegadores com código antigo podem continuar mostrando a tentativa na fila
   até atualizar a página. O reenvio do mesmo comando/protocolo é recusado pelo
   backend novo, independentemente da confirmação armazenada no dispositivo.
   As rotas e os namespaces antigos foram mantidos. Não presumir que uma aba
   antiga já oferecerá o botão de resolução ou atualizará seu histórico visual.

Depois da primeira reserva administrativa, não retornar escritores à versão
antiga sem preservar a verificação das reservas. O upgrade real de um worker
antigo para v52 e um rollout misto não foram ensaiados nesta rodada: o primeiro
foi revisado por código; o segundo é expressamente incompatível com a garantia.

## Verificação e limitações

- **81 testes Django aprovados na versão final**, SQLite exclusivo em TEMP:
  resolução, encerramento anterior, criação/revisão, consultas, venda online
  e modalidade LOCAL. Inclui venda fictícia preservada com seis produtos,
  total R$ 301,20, a prazo e vencimento em 10/12/2026; compara estoque, contas,
  movimentos, eventos e operações antes/depois.
- **11 testes JavaScript isolados aprovados**, executados em V8 com transporte
  e armazenamento fictícios. O arquivo `offline/tests_sale_resolutions_isolated.mjs`
  também pode ser executado com Node. Abrange ausência, confirmação explícita,
  substituta pendente/desconhecida, operação independente, resposta perdida,
  consulta inconclusiva, confirmação inválida, persistência local e outra aba.
  Não substitui o teste real de interface/IndexedDB.
- **Sete testes PostgreSQL aprovados após o reforço final da trava**. Cluster
  exclusivo em `127.0.0.1:58477`, banco `inventario_confiabilidade`, diretório
  temporário `inventario-confiabilidade-pg-sale-resolution-6ju4mw3c`. Porta e
  `data_directory` verificados, destinos externos recusados e cluster encerrado.
- **Quatro testes Chrome aprovados na rodada final**, com perfis novos e
  SQLite exclusivo em TEMP. Dois testes inicialmente atingiam outro elemento
  `data-reason` da página e deixavam o motivo do diálogo vazio. Os seletores
  foram limitados ao diálogo; não houve alteração do código funcional.
  O teste de resposta perdida também verifica F5, recibo em outra aba e reenvio
  HTTP 409 de outro perfil, com identificador de dispositivo diferente.
  Operação independente, histórico e efeitos comerciais permaneceram idênticos.
- **Dez testes de `SaleResolutionTests` reexecutados e aprovados**, incluindo
  comparação da migration com os modelos, recusas de reenvio e preservação da
  venda oficial fictícia. Os 81 testes anteriores e os 11 JavaScript não foram
  reexecutados integralmente nesta retomada, nem somados aos 21 desta rodada.

Lista exata dos quatro Chrome em `offline.tests_sale_resolutions_browser.SaleResolutionBrowserTests`:

```text
test_confirmed_replacement_shows_official_sale_without_abandonment
test_lost_response_inconclusive_get_and_repeat_preserve_one_decision
test_simple_ui_resolves_child_then_root_preserving_independent_and_history
test_two_tabs_do_not_send_during_resolution_and_absence_is_not_ack
```

Lista exata dos sete PostgreSQL:

```text
offline.tests_sale_resolutions.SaleResolutionConcurrencyTests:
  test_concurrent_same_and_different_requests_share_one_receipt
  test_global_uuid_is_reserved_even_when_another_actor_sends
  test_parent_closure_versus_child_send_never_creates_sale_after_closure
  test_send_versus_abandon_absent_command_has_one_outcome
offline.tests_sale_closures.SaleClosureConcurrencyTests:
  test_revision_and_closure_serialize_on_same_parent
  test_two_distinct_closures_one_decision_no_effects
  test_two_replays_same_closure_return_identical_receipt
```

Lista exata dos dez de servidor em `offline.tests_sale_resolutions.SaleResolutionTests`:

```text
test_absent_replacement_reservation_blocks_send_then_parent_can_close
test_additive_migration_matches_models_without_executing_it
test_chain_must_close_from_leaf_and_preserve_lineage
test_closed_parent_blocks_new_replacement
test_confirmed_operation_returns_official_record_without_closing
test_conflict_history_and_existing_official_sale_preserved
test_independent_unknown_does_not_block_or_change
test_replay_and_lost_response_lookup_return_same_decision
test_scope_hash_csrf_permissions_and_inconclusive_lookup
test_unknown_server_operation_preserved_until_definitive
```

Resultados finais locais:
`diagnostics/sale_resolution_chrome_final_resume_verified.log`,
`diagnostics/sale_resolution_pg_final_resume.log` e
`diagnostics/sale_resolution_server_final_resume.log`.
`git diff --check` passou. Esses diagnósticos não pertencem à publicação.
Permanecem o aviso de namespace `estoque` duplicado e Broken pipe ao fechar abas.

## Procedimento futuro para Zeca Caroço

Somente após publicação autorizada e conclusão dos testes pendentes:

1. No dispositivo que contém as tentativas, abrir a lista e localizar a tentativa
   de R$ 301,20 de 10/11/2026, identificada tecnicamente por
   `5d88a2a6-7fd0-433e-b88f-8931508cc064`. Escolher **Resolver venda pendente**,
   informar que a venda correta já foi registrada e confirmar. Aguardar
   **Encerramento confirmado pelo servidor**.
2. Abrir a tentativa de R$ 207,20 identificada por
   `de028343-834f-427b-96e4-97ff146df6fe`. O diálogo apresenta primeiro a
   substituta `57bff246-47bd-4cae-8301-2a18c0d59881`. Confirmar o abandono
   dessa substituta e aguardar a confirmação do servidor, mesmo com zero envios.
3. Usar **Voltar à venda original** e confirmar separadamente o encerramento
   da tentativa de R$ 207,20. Aguardar novamente a confirmação definitiva.

Se uma consulta mostrar venda oficial ou resultado incerto, parar o encerramento
daquela tentativa e seguir a informação exibida; não presumir ausência nem refazer
a venda. Conferir individualmente os três recibos administrativos no histórico.

Não selecionar, sincronizar ou alterar a operação de Sta. Bárbara
`044ea84a-7928-4e4d-ad35-245965bad6e6` (R$ 77,60). Não usar sincronização da fila
para executar esse procedimento. A nota oficial #1278 não participa do comando
de encerramento e permanece preservada; não há edição nem vínculo automático
entre ela e uma tentativa rejeitada.

## Arquivos desta implementação

```text
docs/vendas-offline-resolucao-pendencias.md
offline/closures.py
offline/models.py
offline/resolutions.py
offline/services.py
offline/urls.py
offline/views.py
offline/migrations/0006_resolucao_venda_offline.py
offline/tests_sale_resolutions.py
offline/tests_sale_resolutions_browser.py
offline/tests_sale_resolutions_isolated.mjs
sistema/acesso.py
static/offline/app.js
static/offline/operation-details.js
static/offline/sales-closures.js
static/offline/sales-draft-ui.js
static/offline/sales-resolutions.js
static/offline/service-worker.js
```

A correção preexistente de `sales-closures.js` foi mantida e aproveitada.
Locações, compras e proteção de preços não foram modificadas. Não houve
operação sobre os identificadores reais, acesso ao Neon, limpeza de dados,
sincronização real, staging, commit, push ou deploy.
