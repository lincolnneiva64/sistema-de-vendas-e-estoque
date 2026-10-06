# Investigação da estabilidade após reabertura

## Evidência e limites

Foram reproduzidos em Chrome headless dois defeitos no código anterior:

1. Uma rejeição do health-check durante `beforeunload`, antes de `pagehide` ou
   `visibilitychange`, passava pelo tratamento de falha. Esse tratamento zerava
   `observed_ms`, limpava `stable_since` e gravava `failed_at`/`reconnecting=true`.
   Ao reconectar, o indicador mostrava amarelo em 0/15, mesmo quando a rejeição
   era cancelamento da navegação, e não perda da conexão.
2. Após a recuperação completa, `reconnecting=true` continuava persistido e a
   mensagem do reset anterior era exibida permanentemente. O teste confirmou
   que essa flag permanecia verdadeira mesmo após estabilidade e reinício.

Não há acesso ao IndexedDB, tráfego HTTP ou versão de assets do navegador de
produção. Portanto não é possível atribuir com certeza o episódio das 20:12
a esses defeitos nem afirmar que seu monitor não estava funcionando.
Foi solicitado o registro `communication:...` do diagnóstico para essa confirmação.

Um contador permanecer em 0 por menos de um minuto pode ser apenas o arredondamento
para minutos. Repetidas saídas/cancelamentos tratados como falhas também podem
zerar continuamente o ciclo. O teste local de falha real e retomada confirmou que
o monitor e o contador funcionam; não foi reproduzido travamento permanente com
a página visível, health-checks OK e a versão atual dos assets.

## Auditoria

- Persistência: `metadata` no IndexedDB, chave formada por ambiente e usuário.
  Não depende de BroadcastChannel e sobrevive ao fechamento do navegador.
- Tempo acumulado: `observed_ms` (`Stability.accumulated` em memória), com
  `stable_since`, `last_success_at` e `observed_until`. Não existem campos chamados
  `stableAccumulated` ou `lastObservedAt` nesta versão.
- Compatibilidade: registros anteriores sem `observed_ms` adotam a diferença
  entre `last_success_at` e `stable_since`. Registros com tempo acumulado explícito
  preservam esse valor; não se inventam minutos durante uma pausa sem observação.
- Inicialização: `start()` lê o snapshot, instala listeners e inicia o monitor.
  A instância começa desconectada, não em reconexão; `reconnecting` vem do registro
  persistido. A projeção pode mostrar verde com fila vazia sem os 15 minutos completos,
  quando nunca houve falha: verde anterior, sozinho, não comprova 15 minutos acumulados.
- Health-check: sucesso recente revalida o tempo já acumulado. Ausência de probes
  durante fechamento/pausa invalida a informação de conexão recente, mas não apaga
  os minutos. O primeiro sucesso após reabrir restaura prontidão já alcançada.
- Monitor: intervalo de 30 segundos, separado do render de 1 segundo. `stopped`
  interrompe envio em curso, não o monitor. Páginas ocultas pausam observação;
  retomadas reiniciam o monitor e verificam imediatamente.
- BroadcastChannel: notifica para reler armazenamento compartilhado. Não é a fonte
  do acumulador; transações serializadas evitam contar intervalos em duplicidade.
- Apresentação: `presentation.js` apenas recebe a projeção; não escreve estabilidade,
  não aciona probes e não reseta o acumulador. Abrir/recolher a aba é independente.
- Cache: o service worker guarda assets com estratégia cache-first, separada do
  IndexedDB. Cache antigo é uma possibilidade de versão desatualizada em produção,
  não uma causa confirmada. A versão do shell foi incrementada de v13 para v14;
  isso não limpa fila nem metadados. Uma página já aberta mantém seus módulos até
  a próxima carga depois da atualização do worker.

## Correção

- `app.js`: em `beforeunload`, invalida a geração do probe e aborta apenas o
  health-check. Seu resultado tardio não pode registrar falha. O timer permanece
  instalado nesse momento para suportar navegação cancelada por outro handler;
  `pagehide` continua cuidando da pausa definitiva.
- `core.js`: sucesso com os 15 minutos acumulados encerra `reconnecting`.
  Mantém `failed_at` como diagnóstico histórico e preserva a contagem.
- `app.js`: a mensagem de reset fica visível durante falha/recuperação e sai da
  apresentação quando a estabilidade está pronta. Não interfere na projeção.
- `service-worker.js`: somente incremento de versão do cache dos assets.
- `tests_browser.py`: regressões de estabilidade concluída, navegação, cancelamento,
  retomada, falha real e atualização do teste da versão do cache.

Sem mudanças em backend, fila, UUID, idempotência, envio, histórico sincronizado
ou `separacao_vendas_fila.html`. Sem staging, commit ou push.

## Testes

Antes da correção, o teste de cancelamento falhou: metadata passou de conectado
para desconectado com `observed_ms=0` e reset gravado. A regressão de estabilidade
concluída permaneceu verde nos ciclos de navegação, mas falhou na flag persistida
`reconnecting=true` depois de reiniciar o navegador.

Depois da correção, os dois testes direcionados passaram. A suíte completa é
executada com `venv\Scripts\python.exe manage.py test offline --settings=offline.test_settings --verbosity=1`,
usando banco isolado. Inclui testes de retomada dos intervalos reais do monitor,
fila preservada e sincronização manual, além das novas regressões.

Resultado final: 44 testes, OK, 1 ignorado por exigir PostgreSQL isolado.
`git diff --check` passou. O aviso preexistente `urls.W005` permanece.
Uma execução anterior recebeu `auth` no teste visual que esperava reconexão;
o teste isolado e a repetição completa passaram. Uma tentativa de inicialização
isolada do Chrome também falhou com desconexão do DevTools/crash do navegador.
As regressões novas passaram na execução final. Isso não comprova o episódio
específico de produção nem valida outros navegadores/dispositivos físicos.
