# Etapa 2.8A + 2.8B

Base do worktree: `84ecc94`. Não há edição/correção comercial ou operação substituta.

## Diagnóstico

Operações em conflito, erro ou resultado desconhecido têm detalhes na tela de
vendas e no painel `/offline/`. A projeção é somente leitura, usa textContent e
respeita ator/ambiente. Exibe comando original, UUID completo, hash, tentativas,
datas, cliente, itens, condições, erro e receipt. Nomes são referências originais
do rascunho, preservadas nas novas conclusões fora do comando/hash. Operações
antigas mostram IDs e ausência de nomes/data; não consultam catálogo para preencher
lacunas. A data do snapshot disponível na conclusão não prova a origem de todos
os campos de um rascunho anteriormente montado.

## Consulta e retry

`GET /api/offline/operations/<uuid>/` requer sessão e a permissão offline existente.
Query: actor_id, environment_id, type, device_id e hash. O hash cobre o comando
completo (payload, sequence e created_at incluídos); antes da consulta o cliente
recalcula e compara o hash, sem substituir o hash persistido. O servidor compara
identidade e hash com o registro e verifica o hash do comando armazenado. Não
trafega o payload comercial em URLs. Consulta não cria/atualiza operações ou
efeitos oficiais e não expõe receipt de identidade/comando incompatível.

- `encontrada`: receipt persistido, com identidade compatível; cliente valida o
  receipt e grava operations/history atomicamente. Não há novo POST.
- `nao_encontrada`: somente o lote manual já confirmado pode enviar o comando
  original, após nova verificação de estabilidade. Não libera edição/novo UUID.
- `incompativel` (409): diagnóstico técnico explícito, mantendo desconhecido.
- `indeterminada`/falha/JSON/receipt inválido: mantém desconhecido e não envia
  essa operação. Falha de autenticação interrompe lote; outras falhas de consulta
  permitem continuar com operações independentes.

Não encontrada não prova ausência de uma transação ainda em execução. O retry
continua dependente do UUID único e da transação oficial. A consulta não muda o
contrato de idempotência nem o resultado definitivo de conflitos comerciais.

O 409 de reutilização divergente possui `code: uuid_comando_divergente`. Não é
receipt comercial; fica como resultado desconhecido com diagnóstico explícito,
sem liberar nova venda. O próximo processamento manual consulta o UUID.

Transições de tentativa/erro e persistência de receipt releem operations dentro
da transação para impedir regressão de confirmação por respostas/abas antigas.
Web Locks, janela de 900000 ms, microqueda/reconexão e envio manual permanecem.

## Assets

Grafo JS em `/offline/assets/2-8ab/<filename>`, endpoint público com whitelist
de arquivos JS sem contexto de sessão. Imports apontam ao mesmo namespace.
O worker antigo ignora esses caminhos, evitando servir core antigo para app novo.
Worker `offline-pilot-shell-v24-2-8ab` prepara o grafo e shell antes de ativar.
Não usar apenas query versions: o worker anterior ignora a query ao buscar cache.
Alterações futuras incompatíveis devem incrementar o namespace e cache juntos.

## Testes

Backend: consulta ausente/confirmada/conflito sem efeitos, hash/identidade/device/
tipo divergentes, autenticação, falha de banco e namespace de assets.
Browser: confirmação perdida sem segundo POST, ausência e retry idêntico,
conflito recuperado, falhas/receipts inválidos, falha de persistência, duas abas,
refresh, lote misto, UUID divergente e confirmação que não regride.
Expectativas anteriores de segundo POST/attempt no replay confirmado agora
esperam consulta sem novo envio. Teste de conclusão local configura explicitamente
o estado offline: activate() já recusava ativação online em 84ecc94.

### Validação realizada

Banco SQLite isolado em memória e Chrome real, fora do sandbox (Chrome sofreu
encerramentos de conexão dentro do sandbox). Nenhum acesso ao banco operacional.

- Conjunto obrigatório: 63 casos de consulta, criação, conclusão, sincronização
  e conectividade de vendas; 61 aprovados e 2 PostgreSQL pulados. A rodada com
  o teste genérico de continuidade adicional teve 64 casos e somente esse teste
  falhou por preparação do relógio; ele foi corrigido e passou isoladamente.
- Regressão ampliada: 117 casos, 109 aprovados, 5 falhas e 3 pulados. Duas falhas
  eram asserts de URLs anteriores ao namespace novo, atualizados. Duas falhas
  de startup/sessão de navegador passaram na reexecução isolada. A quinta era o
  teste de continuidade, revalidado após aguardar probes completos, importar o
  módulo do documento atual e medir o contador após o último probe anterior ao gap.
- Reexecução de offline.tests/offline.tests_commercial e três cenários genéricos:
  26 casos, 24 aprovados, 1 PostgreSQL pulado, somente continuidade ainda falhando
  naquele momento. A reexecução final de continuidade passou (1 caso, OK).
- O teste de conflito/mensagem literal também passou isoladamente após uma
  ocorrência transitória na rodada direcionada anterior.
- Todos os casos das cinco falhas da regressão ampliada passaram nas reexecuções;
  a suíte ampliada não foi repetida integralmente após os últimos ajustes de testes.
- PostgreSQL não disponível/configurado: concorrência real do banco não foi
  revalidada. Web Locks/múltiplas abas foram verificados com Chrome.
- git diff --check aprovado. Sem commit, push ou staging.

Logs de execução no TEMP: offline-28ab-complete-final.log,
offline-28ab-remaining.log, offline-28ab-approved-checks.log e
offline-28ab-stability-final.log. Avisos preexistentes: namespace estoque duplicado
e ausência de staticfiles no worktree de testes.

### Revisão final: nova execução integral

Esta execução substitui a conclusão de validação anterior. Após todos os ajustes,
a suíte ampliada foi executada integralmente novamente: **117 testes em 412,831 s;
113 aprovados, 1 falha, 3 pulados**. Não está totalmente verde.

Falha: `offline.tests_browser.OfflineBrowserTests.test_checklist_visible_resume_observed_minutes`.
O teste não chegou às verificações do contador: o formulário `[data-local-note]`
não foi preparado em `/entregas/136/checklist/`. O diagnóstico mostrou snapshots
vazios e sessão autenticada com can_prepare=true. A causa não foi demonstrada;
não classificar a falha como preexistente ou resolvida por reexecuções anteriores.

Depois da suíte integral, `offline.tests_operation_lookup` e
`offline.tests_sale_sync_browser` foram executados: **21 testes em 54,602 s, todos
aprovados, nenhum pulado**. Logs no TEMP: offline-28ab-review-full-external.log e
offline-28ab-review-directed.log. A tentativa em sandbox falhou na inicialização
do Chrome; os resultados acima são da execução fora do sandbox com banco isolado.

Os três pulados são os mesmos testes de concorrência já existentes, que exigem
OFFLINE_TEST_DATABASE_URL apontando para PostgreSQL isolado:

- offline.tests.PostgreSQLConcurrencyTests.test_concurrent_same_uuid_creates_one_event
- offline.tests_sale_creation.SaleCreationPostgreSQLTests.test_mesmo_uuid_concorrente_cria_uma_venda_e_um_financeiro
- offline.tests_sale_creation.SaleCreationPostgreSQLTests.test_mesmo_uuid_concorrente_conteudo_diferente_preserva_vencedor

Na autoauditoria, as mudanças em base/checklists/commercial-ui foram mantidas:
são somente URLs/imports necessários ao namespace coerente de módulos. Não há
alteração de regra de locação ou conferência. Removida variável sem uso realRecord
e corrigido acento corrompido em mensagem de teste; nenhuma alteração comercial.
Whitelist de assets e aliases do worker conferidos (11 arquivos, iguais).
Comparação com 84ecc94: prefixo do core anterior a record (políticas, estabilidade,
hash, comunicação, recuperação), probes/microqueda e startup/eventos permanecem
iguais. git diff --check aprovado; index vazio; HEAD permanece 84ecc94.

Revisão concluída com pendência de regressão no checklist. Sem staging, commit,
push ou implementação de 2.8C.
