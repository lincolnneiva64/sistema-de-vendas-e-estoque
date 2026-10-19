# Publicação e reversão do recolhimento individual

Este roteiro descreve ações futuras e separadamente autorizadas. O commit/push
não executa migration no Neon nem deploy no Render. O commit usa `[skip render]`,
marcador documentado pelo Render para não iniciar auto-deploy por esse push.
Fonte: [Render — deploys](https://render.com/docs/deploys#skipping-an-auto-deploy).

## Estado retomado e integração

Na retomada não havia commit nem push desta correção. A worktree existente estava
em `4785a5855e99e67c87619544e30a03a1c3049b66`, com alterações preservadas e evidência
de 33 testes aprovados. A consulta efetiva ao GitHub confirmou a branch remota em
`76a0b3f5e6da6d2595796eb59edc048eed53026e`.

A correção foi preservada no stash `4b61ac523cd35dbf733690d3e7ef99ae4b6565fa` e
aplicada sobre esse SHA remoto na **mesma worktree**, sem recriação, reset ou clean.
O stash não foi removido. A árvore original com outros trabalhos não foi alterada.
Foram preservados o checklist embutido, a identificação de transporte e os fluxos
administrativos da base remota. Nenhuma alteração dos outros projetos foi incluída.

A migration correta agora é `locacoes.0017_autorizacao_recolhimento`, dependente de
`0016_locacao_horario_previsto_devolucao` já existente na branch. Não existem duas
migrations 0016 concorrentes da correção.

## Segurança da migration

A 0017 tem **uma única operação CreateModel**, criando a tabela de autorizações
com seus índices e referências. Não tem RunPython, RunSQL, backfill, UPDATE ou
DELETE de registros comerciais. Não altera colunas de locações, tarefas,
conferências, estoque, vendas ou financeiro. O post-migrate padrão do Django cria
o content type e as permissões do novo modelo, sem atribuí-las a usuários.

No PostgreSQL descartável foi usado o DDL real de `Migration.apply/unapply`,
apenas desta migration. Com locação e conferência fictícias já persistidas, a
comparação integral das linhas das tabelas de locações, estoque, offline e
inventário permaneceu idêntica após aplicação e reversão. O executor geral de
migrations continuou bloqueado; nenhuma conexão externa foi permitida pelo launcher.

Isso valida a preservação dos registros no ensaio. Em produção, a criação de FKs
ainda pode esperar por locks: executar em janela aprovada com timeout e backup.

## Permissões exatas do responsável

Para emitir, substituir ou revogar links, a conta administrativa precisa de:

1. `locacoes.add_conferenciarecolhimentolocacao`
2. `locacoes.change_locacao`
3. `locacoes.change_tarefaoperacionallocacao`
4. `locacoes.autorizar_recolhimento` — **nova, não atribuída automaticamente**.

Se o responsável tem apenas as três permissões atuais, será preciso conceder a
quarta pelo usuário ou grupo no Django Admin, após a migration. Superusuários
já satisfazem a verificação, mas não é necessário promover ninguém a superusuário.
O cadastro real de permissões não foi consultado nem modificado nesta etapa.

Procedimento: um administrador autorizado abre `/admin/auth/user/` ou
`/admin/auth/group/`, escolhe o responsável/grupo existente e adiciona a permissão
“Pode autorizar links individuais de recolhimento”, mantendo as três anteriores.
O funcionário que recebe o link não precisa de conta, senha ou permissões Django.

Para conferir uma conta específica, somente na execução administrativa autorizada,
definir `RESPONSAVEL_USERNAME` com o login existente correto e executar:

```sh
python manage.py shell -c 'import os; from django.contrib.auth import get_user_model; from locacoes.pickup_security import PICKUP_PERMISSIONS; u=get_user_model().objects.get(username=os.environ["RESPONSAVEL_USERNAME"]); print({p:u.has_perm(p) for p in PICKUP_PERMISSIONS+["locacoes.autorizar_recolhimento"]})'
```

Esse comando apenas consulta as permissões; não cria usuário nem concede acesso.

## Publicação — somente após autorização para Neon e Render

1. No Render, registrar serviço, domínio HTTPS, branch e SHA **realmente Live**.
   Guardar esse SHA e o deploy bem-sucedido para reversão; não presumir que o Live
   é igual à ponta da branch. Conferir Auto-Deploy desativado e ausência de migrations
   automáticas em build/start/pre-deploy. O `build.sh` e o Dockerfile do repositório
   foram inspecionados; não foi adicionada execução automática de migrations.
2. Validar backup/restauração do Neon pela rotina operacional aprovada e registrar
   a janela. Preparar um checkout/job administrativo do **SHA completo informado
   após o push**, com o ambiente já autorizado para o banco correto. Não apontar o
   launcher de testes ao Neon e não executar testes, flush ou `--fake` em produção.
3. Nesse job, verificar o alvo e o plano, sem imprimir senha ou DATABASE_URL:

```sh
python manage.py shell -c 'from django.db import connection; print(connection.vendor, connection.settings_dict["HOST"], connection.settings_dict["NAME"])'
python manage.py showmigrations locacoes --database default
python manage.py migrate locacoes 0017_autorizacao_recolhimento --database default --plan
```

4. Prosseguir somente se o alvo for o banco autorizado, a 0016 e demais dependências
   já estiverem aplicadas e o plano contiver **apenas a 0017 desta correção**.
   Se houver outras migrations pendentes, parar e revisar/autorizar separadamente.
   Aplicar uma única vez, em execução controlada:

```sh
PGOPTIONS='-c lock_timeout=5000 -c statement_timeout=60000' python manage.py migrate locacoes 0017_autorizacao_recolhimento --database default --noinput
python manage.py showmigrations locacoes --database default
```

5. Confirmar `[X] 0017_autorizacao_recolhimento` e configurar a permissão específica
   do responsável como acima. Não iniciar a versão nova antes de existir a tabela.
6. Com a autorização de deploy, no Render abrir **Manual Deploy → Deploy a specific
   commit**, informar o SHA completo publicado e selecionar **Deploy Commit**.
   Conferir o novo deploy Live, HTTPS, logs sem tokens secretos e estáticos.
7. Homologar com tarefa **fictícia** aprovada e navegador mobile novo: responsável
   emite link; funcionário abre sem login; confirma uma vez; consulta o comprovante
   e seu destino WhatsApp; reenvio não cria outra conferência. Não usar a locação 90
   ou outra tarefa comercial real para esse ensaio.

Comandos `migrate --plan`/migration direcionada: [Django 6 — django-admin](https://docs.djangoproject.com/en/6.0/ref/django-admin/#migrate).
Deploy por SHA: [Render — deploy específico](https://render.com/docs/deploys#deploying-a-specific-commit).

## Substituição dos links antigos

Não há conversão em massa nem alteração de conferências. Para cada tarefa ainda
pendente, o responsável abre a ação de envio, escolhe o funcionário habilitado,
clica em gerar e envia o **novo link individual**. O link antigo sem token continua
sendo consulta e não passa a autorizar POST. Gerar outro token revoga os anteriores
da mesma tarefa, sem apagar conferências ou eventos existentes.

Se já houve conferência parcial, emitir outro link permite registrar apenas o
saldo mediante nova conferência. Se a tarefa terminou, usar o comprovante existente:
a aplicação recusa criar outra autorização de escrita. Não repetir recolhimento
para “testar” se o link antigo funcionou.

## Reversão segura

A reversão recomendada é **do código, mantendo a tabela aditiva e todos os dados**:

1. Suspender emissão de links e aguardar confirmações em andamento. Se possível,
   revogar links pendentes pela interface autenticada. Caso o serviço esteja
   indisponível, não fazer alteração manual em registros comerciais para resolver.
2. No Render, abrir **Deploys**, localizar o deploy bem-sucedido registrado antes
   da publicação, escolher **Rollback** e confirmar **Rollback to this deploy**.
   Isso reverte o serviço, não restaura nem apaga o banco. Manter Auto-Deploy desligado.
3. Manter `locacoes_autorizacaorecolhimento`, conferências e eventos. A versão antiga
   ignora a nova tabela; os novos links ficam sem rota funcional. Informar o depósito
   para não reenviar confirmações. Antes de reativar a funcionalidade, revogar os
   tokens pendentes e gerar novos por tarefa.
4. Conferir o SHA Live e os mesmos registros de locação/conferência. Não executar
   restore integral do banco: ele poderia apagar vendas/recebimentos posteriores.

Opcionalmente, **só com autorização adicional para reversão de schema**, backup e
nenhuma linha de autorização que precise ser preservada, consultar o plano:

```sh
python manage.py migrate locacoes 0016_locacao_horario_previsto_devolucao --database default --plan
```

Se houver autorizações gravadas, manter a tabela por padrão; o reverse apagaria
esse histórico de acesso. Se o plano incluir algo além de desfazer a 0017, parar.
Somente após conferir ambos os critérios e a autorização específica:

```sh
PGOPTIONS='-c lock_timeout=5000 -c statement_timeout=60000' python manage.py migrate locacoes 0016_locacao_horario_previsto_devolucao --database default --noinput
```

A reversão do DDL desta tabela foi ensaiada em PostgreSQL descartável, mas não é
necessária para voltar o código. Não há force push, reset da branch nem remoção
de migrations já aplicadas como procedimento de reversão.
Fonte: [Render — rollbacks](https://render.com/docs/rollbacks).

## Evidências finais

Os registros locais da rodada ampla e da revalidação ficam em
`diagnostics/pickup-session/release-broad-tests.log`,
`release-broad-validation.json`, `tests.log` e `validation.json`.
O teste de navegação tinha uma asserção antiga do cache v38, embora a base remota
já sirva v40. Foi atualizado para verificar a versão efetivamente servida,
mantendo as verificações de preservação de IndexedDB e remoção do cache obsoleto.
Nenhum código de service worker foi alterado por esta correção.

Resultado: **65 cenários únicos aprovados na última execução de cada caso**.
A rodada ampla executou 65 testes (64 aprovações e a asserção desatualizada de cache);
após o ajuste exclusivamente no teste, o módulo de navegação foi reexecutado: 2/2
aprovados. Não foi repetida a suíte inteira. O ensaio de aplicação/reversão da
migration foi aprovado na rodada ampla. Todos os clusters foram encerrados.
O manifesto versionado, sem logs ou tokens, está em
[`validacao-final-recolhimento.json`](validacao-final-recolhimento.json).
