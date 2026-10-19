# Diagnóstico: Vendas permanece com aviso offline após reconexão

Data: 19/10/2026, America/Sao_Paulo. Worktree isolada
`.worktrees/diagnostico-offline-52334d9`, base
`52334d9b21f261119305831c4f8eb179fbe95900`. Revisão final para commit e push
autorizados pelo proprietário; deploy depende de autorização posterior.

## Conclusão e grau de certeza

Foi comprovado um defeito de apresentação no código servido em produção:
`offline-global.dataset.connection` pode estar `online` enquanto o aviso de
Vendas permanece “Modo offline — dados locais de referência”. A recuperação
chama `salesDraftUI.canRecoverOnline()`, que retorna false para venda finalizada
localmente, pendência sem resolução, conflito, resultado desconhecido, envio em
andamento ou montagem protegida. Esse false encerrava a recuperação silenciosamente,
sem atualizar o aviso amarelo. A proteção é intencional; a descrição como falta de
conexão é incorreta nesse cenário.

Reprodução em Chrome, usuário e dados fictícios: depois de saúde restabelecida,
o indicador estava online, a venda pendente ficou preservada e o aviso antigo
continuou exibido. A nova asserção “Servidor online” falhou no código original;
após a correção, o cenário passou. Testes existentes já exigiam que pendências
impedissem a troca automática do formulário para online.

O proprietário confirmou na aba afetada que a expressão de leitura retornou
`online`, embora o aviso de Vendas permanecesse offline. Relatou também
`net::ERR_CONNECTION_TIMED_OUT` no health e uma ocorrência de
`net::ERR_NETWORK_CHANGED`. Essas falhas podem explicar a entrada inicial em
modo local; o retorno da saúde não libera automaticamente uma montagem protegida.
A divergência foi, portanto, observada no aparelho e reproduzida isoladamente.
A causa de retenção mais provável é a operação aguardando sincronização ou
resolução informada no relato. Ainda não é possível afirmar que é a única causa nesse
aparelho: não houve acesso à sua aba, cookies, IndexedDB, worker instalado ou
console. O texto exato do diagnóstico de Gelo Escama e o estado da operação não
foram disponibilizados. Não se conclui que a venda deva ser reenviada, descartada
ou alterada; uma validação fracionária pode ser uma pendência comercial distinta
da conectividade.

## Evidências públicas, sem autenticação

Em 19/10/2026 às 08:05:24 de Brasília, consultas GET sem cookies retornaram:

| Endpoint | Resultado |
|---|---|
| `/api/offline/health/` | 200; `ok=true`; protocolo 1; ambiente igual ao hostname; sem redirect |
| `/api/offline/session/` | 200; `authenticated=false`, `can_prepare=false`; sem redirect |

A sessão false nessa consulta é esperada, pois nenhum cookie foi enviado. Não
representa a sessão do proprietário. O health não exige login e executa SELECT 1;
a view retorna 503 quando detecta falha na sua consulta. Com sessão no cookie,
uma falha global de banco pode também atingir o middleware antes da view e
produzir 500; isso não é perda de internet do dispositivo. Isso comprova disponibilidade
pública nesse instante, não ausência de falhas desde ontem ou por outra rede.

Os conteúdos públicos de `sales.js`, `app.js` no caminho `local-controlled-v1`
e `/service-worker.js` coincidiram com a base após normalizar CRLF/LF. A comparação
não comprova o SHA Live inteiro nem qual worker/script já está instalado no
navegador do usuário. Não havia acesso conectado aos logs privados do Render;
nenhum histórico de erros do provedor foi inventado ou inferido do GET atual.

## Mecanismo de conexão e persistência

- `navigator.onLine` e eventos online/offline são pistas. A decisão usa health
  com JSON/protocolo/ambiente válidos, não acesso a outros sites.
- A rotina consulta a cada 30 segundos, timeout de 5 segundos. Confirma offline
  após três falhas em pelo menos 10 segundos. Uma resposta saudável atualiza a
  conexão para online; os 15 minutos são a proteção da sincronização manual,
  não o prazo para declarar o servidor online.
- Erros HTTP, JSON inválido ou ambiente divergente no health entram no mesmo
  fallback conservador de falha de comunicação. Assim, esse aviso não distingue
  sozinho falha de rede, indisponibilidade do banco ou resposta inesperada.
- `/api/offline/session/` é uma consulta pública que informa autenticação; ela
  não deve ser confundida com saúde de rede. Snapshots e sincronização exigem
  sessão e permissão e podem retornar 401/403 legitimamente.
- Para recuperar o catálogo, o cliente faz GET `/vendas/`, valida identidade,
  ambiente e elementos do formulário. Redirect para login, 401/403 ou HTML
  inadequado impedem recuperação. Antes, o erro de catálogo tinha texto genérico.
- O worker usa rede primeiro na navegação `/vendas/`; em falha de transporte,
  devolve shell anônimo em cache. Conseguir abrir a URL não comprova acesso ao
  servidor. APIs não são respondidas pelo cache do worker inspecionado.
- Assets conhecidos usam cache por caminho. Um worker/script antigo no aparelho
  continua uma hipótese a verificar, sem exigir limpeza ou desregistro.
- O estado de comunicação reside em metadata do IndexedDB por identidade/ambiente.
  Observações com mais de 60 segundos ficam obsoletas; nova saúde válida supera
  estado offline antigo. Há verificações em retomada da aba e visibilidade.
- Não foi encontrada uma chave de LocalStorage que force modo offline. Há
  registros de proteção contra duplicação de venda; não devem ser removidos.
- O catálogo comercial é referência preparada explicitamente e separada da
  saúde do servidor. A data 16/10 não prova falta de internet desde essa data.

## Relação com inventário

A condição que bloqueia recuperação na presença de pendência já existia no
commit `a0f5f45`, de 11/10/2026, anterior à integração de inventário. `cad2620`
alterou a confirmação de estoque; `52334d9` alterou configuração/testes/relatório.
Esses commits não alteraram o health ou a recuperação de Vendas. A integração
`67ffb11` incluiu mudanças de identidade/autenticação e assets offline; isso exige
cautela com versões antigas, mas não prova que tenha causado o estado do aparelho.
A sessão de contagem atual é separada da sessão comercial e não exige login do
inventário para consultar health. Nenhuma configuração de estoque foi alterada
nesta correção; a flag continua desabilitada por padrão.

## Correção preparada, sem liberar pendências

`static/offline/sales.js` passa a informar:

- Servidor online, montagem mantida com referência local para proteger pendência
  ou revisão. Exibe a data do catálogo e orienta conferir Status; não envia nada.
- Servidor online, sessão ausente/expirada: orientar autenticação do mesmo usuário,
  preservando a venda. Atualização de sessão reavalia a apresentação.
- Resposta de catálogo 401/403 ou redirect: autenticação/permissão, não falta de
  internet; HTML sem identidade/formulário esperado: validação de catálogo/sessão.
- Outros erros HTTP do catálogo: informar o status e preservar montagem; os probes
  periódicos continuam tentando recuperar quando for seguro.
- Em nova perda de comunicação, o aviso volta a informar operação local. Um
  shell recém-aberto do cache aguarda contato com o servidor nesta página antes
  de anunciar conexão; não toma metadata antiga como nova resposta de rede.

A função `canRecoverOnline`, o conteúdo das operações, a política de sincronização,
os hashes, as identidades e serviços de gravação permanecem intactos. Não há
sincronização automática, alteração de status ou descarte na correção. Ela corrige
um aviso falso; não promete que o formulário protegido poderá voltar online antes
da resolução segura da pendência.

URLs novas para `connection-recovery-v1/sales.js` e
`connection-recovery-v1/sales-draft-ui.js` evitam servir os módulos antigos por
cache-first quando a correção vier a ser publicada. O import da UI de rascunhos
também aponta ao mesmo módulo sales.js, para inicializar o controlador uma única
vez. Template, rota e pré-cache usam os dois caminhos novos. Não há limpeza de IndexedDB,
LocalStorage ou novo comando de purga/desregistro de worker. Nenhum navegador de
produção foi manipulado. Chrome de teste usa perfil temporário independente.

## Coleta segura no aparelho afetado

Sem recarregar, limpar ou reinstalar nada:

1. Procurar o pequeno controle de conexão junto à área superior de Vendas; ele
   pode estar rotulado “Online”, “Espera”, “Atenção”, “Verificando” ou “Erro”, e não
   literalmente “Status”. Abrir seus detalhes apenas para leitura. Não clicar em
   Sincronizar, descartar, resolver, atualizar catálogo ou criar outra venda.
2. Se necessário, abrir F12 → Console e executar o conteúdo de
   `docs/diagnostico-offline-console-leitura.js`, disponível nesta worktree.
   Esse snippet consulta apenas DOM e dois endpoints GET. Não abre IndexedDB,
   não altera LocalStorage/cache/worker e não executa funções de sincronização.
3. Compartilhar somente o JSON filtrado gerado. Ele contém flags, status HTTP e
   caminhos de scripts; não imprime cookies, tokens, nome/ID de usuário, clientes,
   valores, conteúdo da venda ou comandos da fila. Não enviar HAR, cabeçalhos de
   requisição, armazenamento bruto ou captura com dados comerciais.

Interpretação do JSON:

| Observação | Interpretação |
|---|---|
| health 200/ok, conexão online, montagem local ativa | Proteção local/pendência ou catálogo não recuperado; não falta de internet |
| health 200, sessão false | Rede/servidor acessíveis; autenticação ausente/expirada |
| health 503 | Servidor respondeu, mas saúde do banco indisponível |
| health 401/403, redirect ou HTML | Resposta de acesso/proxy/sessão inesperada para esse endpoint público |
| TypeError/TimeoutError | Falha de transporte/timeout até esse servidor; outros sites não a excluem |
| health ok com ambiente divergente | Incompatibilidade de ambiente/HTML/cache; preservar dados e revisar versões |
| health ok e conexão permanece offline | Investigar erro JS, metadata/IDB inacessível ou módulo antigo; não apagar armazenamento |

A ação segura imediata é identificar o estado e, se necessário, autenticar o mesmo
usuário, sem tocar na fila. Uma eventual consulta/sincronização/resolução comercial
exige decisão separada do responsável; não foi autorizada nem executada aqui.

## Validação

Reprodução original: teste de navegador falhou porque a conexão estava online e
o aviso ainda continha Modo offline. Primeira verificação após correção: três
testes Chrome aprovados. A suíte ampliada inclui HTTP 401/403/503, HTML de login,
sessão expirada, retomada segura e igualdade dos bytes das operações antes/depois
da tentativa de recuperação. A primeira regressão ampla encontrou três falhas:
um mock preexistente de banco e duas interações da candidata com o shell/cache.
O mock foi isolado à consulta do health (sem derrubar leitura de sessão no
middleware); a candidata foi corrigida para aguardar contato na página do shell
e carregar sales.js uma única vez pelo mesmo caminho de importação. Os oito
cenários críticos passaram depois dessas correções, em 59,250 segundos.

Teste adicional de cache antigo: aprovado em 9,427 segundos. Uma operação
pendente fictícia foi criada e um módulo obsoleto foi mantido no cache antigo.
Depois de reabrir Vendas, a UI carregou a URL nova uma única vez, a cópia antiga
permaneceu intacta no cache, o JSON completo da operação ficou idêntico e os
modelos comerciais do banco descartável não mudaram.

**Validação final: 50 testes aprovados em 322,274 segundos**, sem falhas, mais o
teste específico de cache antigo aprovado em 9,427 segundos: **51 cenários distintos
aprovados**. Chrome desktop/mobile e PostgreSQL descartável. Os dois clusters
foram encerrados; nenhuma migration foi executada nessas rodadas. Incluídos
health e sessão, reconexão após falhas temporárias, metadata obsoleta, catálogo,
montagem protegida, autenticação, conflito, resultado desconhecido, recibos,
reabertura/F5/reinício do navegador, abas concorrentes e sincronização manual.

O teste de cache foi executado separadamente após ser acrescentado ao módulo;
os outros 50 correspondem à execução integral final dos cinco módulos. Não se
declara execução de todos os testes do projeto. Avisos locais preexistentes:
namespace `estoque` duplicado, `staticfiles` não coletado no ambiente e Broken pipe
em navegações de teste. Todos os cenários finais terminaram aprovados.

Evidências locais em `artifacts/diagnostico-offline/`: `regressao-50.log/json`,
`cache-pendencia-1.log/json` e `reproducao-original.log`. Os logs usam exclusivamente
dados fictícios e não foram publicados. `git diff --check` sem erros. No diagnóstico,
a worktree mantinha HEAD em `52334d9`, com alterações sem commit, sem mudanças de configuração,
modelos, migrations, permissões ou serviços de processamento comercial.

Até essa etapa não haviam sido executados commit ou push. Não foram executados
deploy, migrations de produção, sincronização real ou alterações de estoque/documentos.
O deploy continua dependendo de autorização.


## Investigação direcionada da divergência de estados

O novo print e a leitura `offline-global.dataset.connection === "online"`
confirmam que o aviso e o indicador usam estados diferentes. O caminho publicado é:

1. `app.js` valida health e atualiza metadata de comunicação, restaura `Stability`,
   renderiza o atributo `data-connection` e emite `offline-connectivity`.
2. `sales.js`, ao receber offline confirmado, chama `state.activate()`: mantém
   `window.salesOffline.active=true`, usa snapshot comercial e mostra o aviso local.
3. Ao receber online, chama `state.deactivate()`. Este método não zera active
   diretamente: aguarda `salesDraftUI.canRecoverOnline()` e valida o catálogo fresco.
4. `canRecoverOnline()` recusa uma montagem protegida, finalização local, envio,
   resultado desconhecido, conflito/pendência sem resolução ou identidade diferente.
   Com a operação pendente do relato, essa recusa é a explicação mais forte para
   a retenção do formulário local. O aviso antigo não explicava a recusa.
5. Apenas quando todos os requisitos permitem, o catálogo atual é carregado por
   GET, a montagem é restaurada e active/shell são desligados. Não há envio de venda.

`active` é memória da página, não uma chave de LocalStorage esquecida. O shell em
cache pode iniciar com active true e reconstruir a montagem do IndexedDB. A fila
persistida é lida e pode continuar bloqueando a troca de modo por segurança.
Metadata de comunicação expirada não tem o mesmo papel: o teste de observação
obsoleta mostra checking e uma nova resposta válida recupera online. Nenhum
registro precisa ser apagado para fazer essa reavaliação.

Há também uma janela de inicialização: o evento online pode chegar enquanto
`state.activating` está true ou antes de `salesDraftUI.ready`. O método retorna
nessas condições. O monitor periódico tentaria de novo mais tarde, mas não havia
uma reavaliação explícita ao terminar a inicialização. Essa janela não demonstra
a causa da permanência desde ontem — a pendência protegida continua a hipótese
principal — e foi corrigida na candidata para evitar espera desnecessária.

A revisão acrescenta o evento `sales-draft-ready` após a restauração inicial e
reavaliação ao terminar activate se a conexão já voltou. As duas entradas chamam
o mesmo fluxo protegido: não ignoram `canRecoverOnline`, não sincronizam e não
redefinem identidade/rascunho. Enquanto a UI não estiver pronta, não se atribui
erroneamente a recusa a uma pendência.

O motivo da última recusa passa a ficar apenas em memória, na propriedade
`lastOnlineRecoveryBlock`. Pode indicar inicialização, contexto inelegível,
identidade, resultado desconhecido, finalização local, envio, montagem protegida
ou operação pendente. Não cria metadata nova nem altera registros da fila.
Esse campo só existe na candidata; ausência dele na versão publicada é esperada.

O teste adicional pausa a prontidão do rascunho, restabelece health, verifica
conexão global online com formulário ainda local e libera a prontidão. A tela
recupera automaticamente o catálogo sem outro probe, sem POST, sem operação nova
e com a montagem idêntica. Os testes de pendência continuam exigindo bloqueio e
igualdade dos bytes da operação antes/depois das tentativas.


Revalidação desta revisão direcionada: **11 testes aprovados em 93,343 segundos**,
Chrome e PostgreSQL descartável; zero falhas, cluster encerrado. Abrange os seis
cenários de conexão (incluindo novo evento de prontidão e cache antigo), shell
sem rede, navegação lenta, preservação/reabertura de rascunho, sincronização
somente manual e mudança de identidade sem POST. A regressão anterior de 51
cenários está descrita acima; esta rodada reexecuta os casos afetados e acrescenta
o teste de prontidão, sem afirmar que toda a suíte foi repetida nesta revisão.
Evidências locais: `artifacts/diagnostico-offline/reavaliacao-11.log/json`.

As novas razões de recusa são exclusivamente diagnóstico em memória. Todos os
predicados que impediam a recuperação foram preservados. Não houve alteração
em tabelas, modelos, migrations, configuração de ajustes de estoque, comandos
comerciais, hashes ou permissões. `git diff --check` permanece sem erros.
Essa rodada ocorreu antes da autorização de commit e push. Sem deploy ou
modificação do navegador/armazenamento real.


## Revisão final autorizada para commit e push

Em 19/10/2026, a branch remota `backup/wip-pedidos-base-2026-06-04`
foi conferida em `52334d9b21f261119305831c4f8eb179fbe95900`.
A execução integral final dos cinco módulos offline terminou com **52 testes
aprovados em 326,057 segundos**, zero falhas, Chrome com perfis temporários e
PostgreSQL exclusivo descartável. O cluster foi encerrado; nenhuma migration
foi executada. Evidência local: cluster temporário
`inventario-confiabilidade-pg-fm6_0m88`, arquivos `tests.log` e `validation.json`.

Foram confirmados: servidor online identificado corretamente; aviso explícito
quando a venda local impede a recuperação; reavaliação após inicialização;
preservação integral das operações pendentes e da montagem; ausência de envio
na recuperação; sincronização somente pela ação manual já existente. Os cenários
incluem autenticação expirada, HTTP 401/403/503, catálogo inválido, falhas de rede,
cache antigo, F5, reabertura, abas concorrentes, identidade divergente e recibos.

O escopo do commit é a correção offline, seus testes e documentação. Capturas,
logs, backups e artefatos temporários ficam fora. Não há alteração de modelos,
migrations, configuração de estoque ou processamento comercial. O padrão de
`INVENTARIO_AJUSTES_HABILITADOS` continua `False`, sem mudança de ambiente.
Commit e push sem force foram autorizados; deploy continua aguardando autorização.
Nenhuma venda real, armazenamento do usuário ou variável do Render foi alterada.
