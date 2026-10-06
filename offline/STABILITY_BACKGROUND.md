# Observação em segundo plano e retomada

## Diagnóstico anterior

`visibilitychange` chamava `updateObservation`, incrementava a geração,
apagava o `observedSince` local, abortava o health-check e removia o intervalo.
Se `document.hidden` fosse verdadeiro, não instalava outro monitor.
Na volta, o primeiro sucesso não creditava intervalo: servia de referência.
O próximo sucesso, normalmente 30 segundos depois, voltava a acumular.

O `observed_since` persistido no IndexedDB não era apagado por ocultação.
`observed_until` permanecia no último endpoint creditado. O evento de sucesso
da aba retomada levava `observed_since: null`, impedindo crédito retroativo.
O progresso confirmado era preservado. Perdiam-se o período oculto inteiro e
o trecho entre último sucesso e saída (normalmente 0–30 segundos; não há
limite de 30 segundos se os timers já estiverem atrasados).

Ocultar por 30 segundos, 2 ou 10 minutos tinha a mesma regra: nenhum crédito
da pausa, health-check imediato ao mostrar, nova referência sem reset.
`pagehide` marcava `leaving`, pausava definitivamente e abortava envio;
`pageshow` reativava a observação. `beforeunload` invalidava e abortava só
o probe, preservando o timer caso outro handler cancelasse a navegação.

Outra aba visível do mesmo ambiente/usuário já podia continuar validando.
O IndexedDB compartilhado, com transações de escrita serializadas, calcula
a união dos intervalos a partir de `max(observed_until, observed_since)`.
BroadcastChannel apenas solicita releitura; não acumula minutos.

## Correção mínima

O monitor continua instalado quando a aba está oculta. A ocultação preserva
o último sucesso local e invalida qualquer requisição em trânsito, sem
escrever falha compartilhada. Só um novo health-check real pode creditar
o intervalo anterior. Não há crédito apenas por `Date.now()` ter avançado.

O limite continua sendo `maxGap=60000`: sucessos separados por mais de
60 segundos não creditam o intervalo, nem mesmo parcialmente. Não existe
crédito automático de 60 segundos sobre uma pausa de 10 minutos.
Com probes válidos a cada 30 segundos, a aba oculta acumula normalmente.
Se os timers forem limitados a intervalos maiores que 60 segundos, os
minutos sem observação não contam. Todas as abas ocultas podem observar
se ainda executarem probes; todas suspensas não inventam tempo.

Mostrar a aba continua exigindo health-check imediato e referência nova,
preservando o acumulado. Ainda pode perder o trecho desde o último sucesso
até a volta, normalmente até 30 segundos, por conservadorismo. A saída não
descarta mais esse trecho se houver sucessos regulares em segundo plano.

Uma requisição de health que só termina depois de `timeout + 1000ms`
(medido por relógio de parede ou monotônico) é tratada como observação
interrompida: sem sucesso creditado, suspeita ou reset; agenda nova checagem.
A margem de 1 segundo identifica entrega atrasada, não altera o timeout
de transporte de 5 segundos. Um probe ainda pendente após `maxGap` é
invalidado pelo próximo disparo, que verifica de novo sem aguardá-lo.
Resultados tardios da geração antiga são ignorados.

O navegador não oferece prova universal para distinguir tela apagada,
sono do computador, suspensão de timers ou congestionamento do event loop.
`document.hidden` indica visibilidade, não perda de rede. Atraso excessivo
indica observação insuficiente, não prova sono nem falha de comunicação.
Falhas recebidas no orçamento normal seguem suspeita + confirmação real
após 5 segundos, inclusive em segundo plano. Um resultado muito atrasado
também pode esconder uma falha real; o novo probe e sua confirmação
resolvem essa ambiguidade sem zerar por ausência de observação.

Não se libera sincronização apenas por retomar: continuam necessários
900000ms observados, sucesso recente e ausência de suspeita. Backend,
fila, UUID, idempotência, histórico e sincronização manual não mudaram.
`interval=30000`, `timeout=5000`, `window=900000`, `maxGap=60000` permanecem.
O cache do shell passa a v16; isso não limpa IndexedDB. Páginas já abertas
precisam recarregar para carregar o módulo atualizado.

## Validação

As regressões do navegador usam relógios controlados e callbacks reais do
monitor para testar os intervalos sem esperar 15 minutos em cada cenário.
Cobrem 5 minutos anteriores + pausas de 30s/2min/10min sem callbacks,
2 minutos ocultos com health-checks, suspensão de resposta e probe preso,
abas concorrentes sem duplicação, suspeita/confirmação, navegação/F5,
preservação da fila e bloqueio da sincronização prematura.
Isso não simula fisicamente o sono de um notebook; valida os efeitos
observáveis dessa suspensão na lógica do monitor.

Comando da suíte isolada:
`venv\Scripts\python.exe manage.py test offline --settings=offline.test_settings --verbosity=1`.

Resultado final da suíte: 47 testes, 44 aprovados, 2 métodos com falhas
visuais (4 assertions, incluindo três larguras), 1 ignorado por exigir
PostgreSQL isolado. Nenhum erro de execução nessa rodada. As regressões
direcionadas de segundo plano, retomada e múltiplas abas passaram; o caso
ampliado de resposta válida atrasada e falha real na retomada também passou
separadamente depois de acrescentar essas assertions.

As falhas visuais são `test_global_indicator_on_normal_page_and_mobile`
(texto branco encontrado; teste espera verde escuro) e
`test_shared_state_visuals_on_pilot_and_sales` (aba verde forte encontrada;
teste espera verde claro). O CSS já presente em HEAD define explicitamente
`background:#16a34a;color:#fff` para o summary online. Nenhum CSS foi editado.
Uma tentativa separada de executar esses dois testes com o app anterior
foi inconclusiva por encerramento da conexão do Chrome; a evidência de
incompatibilidade anterior é a regra CSS versionada, não essa execução.
As expectativas visuais não foram alteradas nesta correção.

`git diff --check` passou. Arquivos deste trabalho: `static/offline/app.js`,
`static/offline/service-worker.js`, `offline/tests_browser.py` e este documento.
A alteração pré-existente em `separacao_vendas_fila.html` foi preservada.
Sem commit ou push.
