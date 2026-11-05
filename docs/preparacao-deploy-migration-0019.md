# Gate de publicação da correção auditada de pagamentos

Base local: `d485dd217506e56b136d7edd109b1dbf3f2879d8`. Correção somente local;
não autoriza commit, push, deploy ou acesso a bancos operacionais.

## Alteração de produção restrita ao comando

`inventario/management/commands/publicar_cancelamento.py`:

- Acrescenta somente `locacoes.0019_correcao_pagamento_direto` à allowlist anterior.
- Atualiza o alvo explícito de locações de 0018 para 0019, mantendo inventário e
  estoque nos alvos já autorizados.
- Exige exatamente uma CreateModel, com dependências, atomicidade, campos,
  PROTECT/OneToOne, opções e restrições correspondentes à auditoria aprovada.
  Não permite RunSQL, RunPython, alterações ou exclusões de tabelas financeiras.
- Verifica os 16 campos da tabela física: tipos, NOT NULL, ausência de defaults
  não aprovados, identidade da chave primária e ausência de colunas extras.
- Confere PK, UUID único, unicidade pagamento/versão, estorno e movimento novo
  exclusivos e todas as cinco FKs para os destinos corretos. Recusa unicidade
  parcial, FK NOT VALID, ON DELETE CASCADE e deferrabilidade incompatível.
- Confere a auditoria quando não há pendências e também quando a 0019 consta
  aplicada, mas outra migration autorizada ainda está pendente.

Mantidos preflight READ ONLY, plano completo das folhas, bloqueio de migrations
desconhecidas e reversões, histórico consistente, detecção de conflitos, advisory
lock compartilhado, rechecagem sob lock, lock_timeout de 5s, statement_timeout de
60s e transação única de DDL/recorder/verificação. `--plan` não grava.

A definição da própria migration 0019, serviços financeiros, modelos, recibos,
permissões operacionais, build.sh e Dockerfile não foram modificados nesta etapa.

## Testes e arquivos

- `inventario/tests_publicacao_correcao_pagamento.py`: casos novos da 0019,
  incluindo definições adulteradas, esquema incorreto e falhas com rollback.
- `inventario/tests_publicacao_comprovante.py`: a cadeia anterior de 0018 agora
  considera a 0019 dependente, o alvo novo e a allowlist estendida. Injeções de
  migrations inesperadas foram movidas para depois da folha 0019.
- `inventario/tests_publicacao_cancelamento.py`: exclui a tabela nova e vazia da
  comparação de dados comerciais ao testar a cadeia de dependências antigas.
- Este documento.

O runner utilizado foi `inventario.validar_postgresql`, com configurações
standalone de `sistema.settings_test_compras` no processo de teste, acrescentando
inventário e apontando exclusivamente para o cluster criado naquele processo.
O driver recusa conexões fora do host, porta, usuário e bancos descartáveis e
confere o data_directory/porta do servidor. Nenhuma configuração real ou `.env`
foi carregada. Os bancos foram preparados por migrations reais no PostgreSQL
descartável, inclusive as dependências, sem reproduzir dados operacionais.

Cobertura: aplicação exclusiva da 0019 com dados financeiros idênticos e todas
as tabelas anteriores inalteradas; esquema já aplicado correto; tabela ausente
apesar do recorder; tipos, nulls, defaults, identidade e colunas adulterados;
cada chave removida; FK incorreta/não validada; índice parcial; `--plan`/noop sem
escritas; conflitos, migration desconhecida, histórico inconsistente, reversão;
falha depois da criação da tabela e na verificação final com rollback integral;
lock concorrente e interrupção do build em caso de erro.

Rodada final: **51 testes aprovados, em 152,163 segundos, sem skips**, no
PostgreSQL descartável: 17 da publicação da correção, 15 dos comprovantes e 19
do cancelamento/build. Banco de teste destruído e cluster encerrado. Também
passaram compilação Python e `git diff --check`. Evidências locais, não publicar:
`diagnostics/gate-0019/final-tests.log` e `final-validation.json`.

Para reproduzir, o runner existente pode ser usado com configurações standalone
somente no processo, sem carregar settings operacionais:

```powershell
@'
import os, sys, django
from inventario import validar_postgresql as runner
original_setup = django.setup
def setup(*args, **kwargs):
    import dj_database_url
    from sistema import settings_test_compras as isolated
    isolated.DATABASES = {'default': dj_database_url.parse(os.environ['INVENTARIO_TEST_POSTGRES_URL'])}
    isolated.DATABASES['default']['TEST'] = {'NAME': 'test_inventario_disposable'}
    isolated.INSTALLED_APPS = [*isolated.INSTALLED_APPS, 'inventario']
    isolated.INVENTARIO_AJUSTES_HABILITADOS = False
    os.environ['DJANGO_SETTINGS_MODULE'] = 'sistema.settings_test_compras'
    return original_setup(*args, **kwargs)
django.setup = setup
sys.argv = ['validation', 'inventario.tests_publicacao_correcao_pagamento',
            'inventario.tests_publicacao_comprovante', 'inventario.tests_publicacao_cancelamento']
sys.exit(runner.main())
'@ | venv/Scripts/python.exe -
```

## Pendência preexistente de testes de compras

A rodada ampla também incluiu os 11 testes de `inventario.tests_publicacao_compra`.
Todos falham no `setUp`, antes de chamar o gate: a preparação tenta voltar até
`estoque.0134`, atravessando a migration irreversível `estoque.0136`.

Erro: `django.db.migrations.exceptions.IrreversibleError: Operation <RunPython ...
corrigir_precisao ...> in estoque.0136_itemvenda_quantidade_precisao is not reversible`.

O caso `PublicacaoCompraTests.test_plan_does_not_write` foi repetido em outro
cluster, carregando somente o gate original de HEAD em memória; falhou no mesmo
setUp e com o mesmo erro. Não é regressão desta correção. O gate e a migration de
precisão não foram enfraquecidos para contornar a preparação antiga. A atualização
dessa fixture permanece fora desta entrega; não declarar a suíte de compras aprovada.

A primeira rodada teve 62 testes e 12 erros: os 11 de compras descritos acima
e uma expectativa do teste novo de definição, ajustada para validar os metadados
carregados sem construir um grafo artificialmente cíclico. A rodada final de 51
testes passou integralmente. Os logs da rodada ampla e da reprodução com HEAD
ficam em `diagnostics/gate-0019/ampla-tests.log` e `baseline-tests.log`.

## Conferências ainda necessárias no Render

Não houve acesso ao painel Render nem ao Neon. Antes de qualquer publicação
autorizada, conferir serviço, branch, SHA, runtime, Build/Start/Pre-deploy Command
e Auto-Deploy. A correção local precisa estar na revisão efetivamente publicada;
o commit d485dd2 original não contém esta alteração do gate.

`build.sh` chama o gate e interrompe com erro; o Dockerfile não chama migrations.
Se o serviço usar Docker ou comandos personalizados, verificar onde o gate será
executado antes de disponibilizar Gunicorn. Não assumir que o comando local
corresponde à configuração remota.

Em futura janela expressamente autorizada, consultar primeiro o plano com
`publicar_cancelamento --plan` no ambiente de publicação. Para esta liberação,
espera-se somente a 0019 pendente; qualquer outra pendência exige revisão. As
autorizações históricas de inventário e estoque permanecem válidas no código e
podem entrar no plano se ainda não estiverem aplicadas. Não usar migrate irrestrito
ou --fake para ultrapassar o gate. A publicação efetiva não faz parte desta tarefa.
