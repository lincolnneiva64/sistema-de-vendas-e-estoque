# Aplicação controlada de offline.0006

Este documento prepara uma execução futura. Não autoriza acesso ao Neon,
migration em produção, commit, push, build ou deploy. O comando não integra
`build.sh` e não modifica `publicar_cancelamento`.

## Escopo e garantias

Comando: `python manage.py publicar_resolucao_offline`.

Somente a definição revisada de `offline.0006_resolucao_venda_offline` é aceita.
O SHA-256 do texto UTF-8, sem BOM, com quebras LF e extremos removidos, é
`37b81bca1f8fc67bf9ff429e1feb3f1274ab9315866168b28adf39e297fbbbb3`.
Também são conferidos os objetos carregados: dependências, atomicidade, metadados,
operações e campos. Alterar a migration exige nova revisão; não atualizar o hash
apenas para fazer uma definição divergente passar.

Antes de escrever, o comando verifica todo o grafo e histórico. Todas as
dependências devem estar aplicadas; nenhuma outra migration pode estar pendente.
Não executa `migrate` geral, `--fake`, reparos, permissões ou sinais `post_migrate`.
Cria exclusivamente as duas tabelas administrativas e seus índices/restrições e
registra a 0006 em `django_migrations`. Não contém operações sobre vendas,
estoque, financeiro, fila, IndexedDB ou decisões administrativas existentes.

O preflight é uma transação somente leitura. A aplicação usa a mesma trava
transacional advisory `5161650017` de publicação existente e os mesmos timeouts
(`lock_timeout=5s`, `statement_timeout=60s`). Identidade, definição, dependências,
plano e schema são revalidados sob a trava. DDL, registro e verificação final
estão na mesma transação. Uma falha provoca rollback. A verificação final exige
colunas, tipos, nulidade, defaults, identidade, PKs, unicidades, FKs validadas,
índices e ausência de RLS/triggers/regras inesperados nas duas tabelas.

Se já aplicada, `--apply` também exige a trava e apenas confere o schema, sem
recriar tabelas ou registros. `--plan` é somente leitura e não reserva a trava:
seu sucesso não substitui a revalidação de `--apply`.

## Ambiente de execução e autorização

Executar em ambiente administrativo controlado com o **código candidato
revisado**, suas dependências e a configuração de produção autorizada. Ele pode
ser um ambiente administrativo temporário no Render, sem HTTP/worker/cron
iniciados. Não usar o shell da versão antiga se ele não contiver o comando e a
migration. Não executar em build, pre-deploy ou startup: o build atual chama
`publicar_cancelamento` e bloqueia a 0006 ainda pendente antes do pre-deploy.

Manter o build existente intacto. Não cadastrar este comando como execução
automática permanente. Não iniciar um serviço temporário que execute
`publicar_cancelamento` antes de disponibilizar o shell administrativo.

Requer **nova autorização explícita do responsável**, identificando a versão
revisada, ambiente, endpoint, porta, banco e usuário. Os valores esperados devem
vir da aprovação independente, não ser copiados automaticamente da conexão
carregada. A referência registra qual aprovação humana embasou a execução; não
é um mecanismo externo de autenticação. Quem possui credenciais continua
responsável por obter a autorização. O comando exige os parâmetros mesmo para
`--plan` em produção e recusa redirecionamentos libpq e conexões sem TLS.

Host, porta, banco e usuário devem estar explicitamente presentes na configuração
da conexão e coincidir com os parâmetros aprovados (inclusive a porta 5432,
quando utilizada). Não depender de defaults de libpq. Se a configuração atual
não atender a isso, interromper e obter autorização para o ajuste do ambiente;
não substituir silenciosamente o destino ou os segredos.

Não registrar senha, DSN ou conteúdo de comandos de vendas nos logs. Utilizar
segredos protegidos do ambiente administrativo; não passá-los na linha de comando.
Confirmar que a configuração não utiliza `service`, `hostaddr`, `options` ou
variáveis `PGSERVICE`, `PGSERVICEFILE`, `PGHOSTADDR`, `PGOPTIONS`.

## Sequência futura (cada ação externa depende de autorização)

1. **Autorizar preparação da publicação:** revisar o conjunto seletivo de arquivos,
   testes e versão imutável. Commit/push e qualquer gatilho de deploy exigem
   autorização própria; não enviar trabalhos locais de locações ou evidências.
2. **Autorizar janela e ambiente administrativo:** verificar backup/restauração,
   inventariar todas as instâncias HTTP, workers, crons e serviços que enviam
   vendas. Suspender escritores antigos e drenar requisições. Bloqueio público
   sozinho não garante interrupção de serviços internos. Não ativar resolução
   administrativa durante coexistência de backends antigos: eles desconhecem as
   reservas e poderiam reenviar uma operação administrativamente abandonada.
3. Conferir versão do ambiente administrativo, endpoint/porta/banco/role
   aprovados, TLS e permissões DDL. Confirmar que não há execução de publicação
   concorrente. Não consultar nem sincronizar pendências reais nesta etapa.
4. **Autorizar consulta ao banco de produção** e executar o plano no ambiente
   administrativo candidato, substituindo apenas os placeholders aprovados:

   ```sh
   python manage.py publicar_resolucao_offline --plan --environment production --expected-host ENDPOINT_APROVADO --expected-port PORTA_APROVADA --expected-database BANCO_APROVADO --expected-user USUARIO_APROVADO --authorize-production offline.0006_resolucao_venda_offline --approval-reference REFERENCIA_DA_AUTORIZACAO
   ```

   Esperado: somente 0006 pendente, ou já aplicada com schema íntegro. Qualquer
   outra pendência/dependência ausente interrompe a sequência. Não aproveitar
   esta autorização para aplicá-la por outro comando.
5. **Autorizar especificamente o DDL em produção** e executar no mesmo ambiente:

   ```sh
   python manage.py publicar_resolucao_offline --apply --environment production --expected-host ENDPOINT_APROVADO --expected-port PORTA_APROVADA --expected-database BANCO_APROVADO --expected-user USUARIO_APROVADO --authorize-production offline.0006_resolucao_venda_offline --approval-reference REFERENCIA_DA_AUTORIZACAO_DDL
   ```

   Aguardar saída zero e confirmação de schema. Em queda da conexão ou resposta
   perdida, resultado é **desconhecido**: não presumir rollback nem avançar.
   Consultar com `--plan` autorizado e repetir `--apply` sob autorização, se
   necessário. Se houve commit, a repetição apenas verifica o schema.
6. **Autorizar build/deploy do backend candidato** com o build e gate existentes.
   A 0006 já aplicada deixa de ser uma pendência desconhecida para o gate.
   Verificar serviço, autenticação, endpoints e versão sem enviar vendas reais.
   Confirmar que todos os escritores executam o código novo antes de reabrir.
7. A atualização do Service Worker faz parte dos assets do candidato. Manter a
   janela até validar que esses assets e sua versão estão disponíveis. Não há
   garantia de instalação simultânea nos navegadores; clientes antigos continuam
   dependendo da proteção do **backend novo**. Não limpar fila/IndexedDB/caches
   manualmente nem forçar sincronização para validar a publicação.
8. **Autorizar reabertura** somente após as verificações. Resolver cada operação
   antiga é outra ação, com autorização e procedimento próprios. Aplicar esta
   migration não resolve nem modifica Zeca Caroço/Sta. Bárbara ou a nota #1278.

## Interrupção e recuperação

Interromper por destino divergente, revisão/hash diferente, outra pendência,
dependência ausente, trava ocupada, schema inesperado, erro/timeout, resposta
perdida, escritores antigos ativos ou serviço novo sem saúde. Não contornar com
`--fake`, edição de histórico, exclusão de tabelas, remoção da trava ou relaxamento
de validações. Investigar e repetir somente após nova confirmação segura.

Falha antes do commit desfaz o DDL e o registro conjuntamente. Se o schema já
estiver aplicado e o deploy falhar, preservá-lo: não reverter a 0006 e não apagar
decisões administrativas. Antes de existir qualquer decisão, um backend antigo
pode ignorar as tabelas adicionais, mas sua retomada exige avaliação e autorização.
Depois de gravada uma decisão, **não retomar escritores antigos**: manter a janela
e corrigir para frente ou usar somente versão compatível com as reservas. Um
rollback de código não desfaz decisões e não torna seu reenvio permitido.

## Teste isolado reproduzível

No ambiente Python do projeto, sem settings de produção:

```powershell
.\venv\Scripts\python.exe -B -m offline.validate_migration_0006_postgresql
```

O runner inicializa cluster novo em diretório temporário
`offline-migration-0006-pg-*`, somente loopback e porta aleatória; bloqueia
conexões fora dele, importa apenas settings de testes independentes de `.env`,
executa migrations reais no banco `test_offline_0006_disposable`, grava logs e
encerra o PostgreSQL. Não reutiliza bancos operacionais. `OFFLINE_PG_BIN` permite
indicar somente os binários PostgreSQL instalados. Logs e cluster parado ficam
no diretório informado, fora do staging.

Validação local em 11/11/2026: **27 testes aprovados**, em uma rodada de 26
testes e uma execução direcionada adicional da integração com o gate existente.
Cobertura: aplicação real exclusiva da 0006, plano somente leitura, repetição sem
escritas e com histórico preservado, hash/definições/campos divergentes, outras
pendências, dependência ausente, histórico desconhecido, trava ocupada antes e
depois da aplicação, falha real durante DDL, falha após DDL antes do commit,
revalidação sob trava, perda da resposta após commit, destino/diretório incorretos,
autorização/TLS/redirecionamento, schema órfão/ausente, colunas/defaults/tipos/
identidade/restrições/índices/RLS/trigger/FK não validada e compatibilidade com
`publicar_cancelamento` sem alterar suas autorizações.

Evidências preservadas no diretório temporário de cada execução:

- Rodada de 26: `offline-migration-0006-pg-c24itd21/tests.log` e `validation.json`.
- Integração: `offline-migration-0006-pg-lxdh8idw/tests.log` e `validation.json`.

Ambos os clusters foram encerrados. O check Django emitiu `urls.W005` sobre o
namespace `estoque` duplicado; nenhum arquivo de rotas foi alterado nesta tarefa.
`git diff --check` e a conferência de whitespace dos arquivos novos passaram.

Limites: testes locais não comprovam configuração do dashboard Render, credenciais,
TLS, permissões reais, backup ou drenagem de todos os escritores. Nenhuma dessas
verificações de produção foi autorizada nesta preparação. Os parâmetros validam
o destino da conexão e a identidade reportada pelo servidor; não substituem a
revisão humana do endpoint aprovado nem o controle de acesso aos segredos.
