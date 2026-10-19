# Auditoria final — Etapa 3

Worktree `.worktrees/conferencia-ajustes-d8c06db`, base direta
`d8c06dbf292333b80664842bac18098b855ec053`, sem commit. Esta auditoria não acessou
o Neon, não executou DDL/DML em produção e não habilitou os ajustes reais.

## Resultado e correção localizada

Preparação, aprovação/rejeição e aplicação são separadas. O fluxo aprovado foi
preservado. Foi corrigida uma lacuna do limite operacional: o limite de 500 produtos
era imposto apenas na página GET. Um token anterior poderia chegar ao serviço via
POST depois de a conferência crescer. Agora aprovação e aplicação também conferem
o limite sob o lock da conferência, antes de gravar. Regressão verifica chamada
direta, token anterior e POST. Nenhum CSS, template ou arquivo de migration foi
modificado nesta revisão.

## Migration 0006 — propostas e aplicações

Arquivo: `inventario/migrations/0006_propostas_aplicacoes_ajuste.py`.
Depende de `inventario.0005` e do modelo de usuário configurado.

| Tabela nova | Campos |
|---|---|
| `inventario_propostaajuste` | `id` UUID PK; `assinatura` varchar(64); `evidencia` jsonb; `justificativa` text; `estado` varchar(12); `criado_em` timestamptz; `autorizado_por_id` FK usuário; `confirmacao_id` FK confirmação física; `rodada_id` FK rodada |
| `inventario_eventoajuste` | `id` UUID PK; `acao` varchar(20); `dados` jsonb; `criado_em` timestamptz; `usuario_id` FK usuário; `proposta_id` FK proposta |
| `inventario_aplicacaoajuste` | `id` UUID PK; `aplicado_em` timestamptz; `lote` UUID; `antes`, `diferenca`, `depois` numeric(12,3); `unidade` varchar(20); `evidencia` jsonb; `decisao_id` FK única decisão; `usuario_id` FK usuário; `proposta_id` FK única proposta |

São 26 colunas nas três tabelas novas. Todos os campos são NOT NULL; os defaults
de UUID, horários e JSON são fornecidos pelo Django. UUID não exige extensão nova.
Não há backfill nem alteração de coluna preexistente.

**12 índices B-tree**, confirmados no catálogo após execução pelo executor Django:

- Proposta: PK UUID; índices das três FKs; índice único composto
  `(rodada_id, assinatura)`, restrição `inventario_proposta_evidencia_unica` (5).
- Evento: PK UUID; índices de `usuario_id` e `proposta_id` (3).
- Aplicação: PK UUID; únicos de `decisao_id` e `proposta_id`; índice de `usuario_id` (4).

São seis índices únicos (três PKs e três unicidades adicionais) e seis não únicos.
As oito FKs mais três PKs e três unicidades compõem 14 restrições. `PROTECT` impede
exclusão pelas APIs Django; as referências também são FKs no PostgreSQL. As choices
de estado não constituem por si um CHECK SQL: a aplicação e a 0007 controlam as
transições. Não há índice ou restrição nova em tabelas comerciais preexistentes.

## Migration 0007 — imutabilidade

Arquivo: `inventario/migrations/0007_auditoria_ajuste_imutavel.py`, dependente da 0006.
Cria duas funções PL/pgSQL no schema `public`, com `search_path = pg_catalog`:

- `inventario_auditoria_ajuste_fn()`: recusa UPDATE/DELETE de registros auditados.
- `inventario_proposta_congelada_fn()`: recusa DELETE de propostas, alteração dos
  campos congelados e qualquer transição diferente de aprovado → rejeitado/aplicado.

Instala três triggers BEFORE UPDATE OR DELETE, somente nas tabelas novas:
`inventario_aplicacao_imutavel`, `inventario_evento_ajuste_imutavel` e
`inventario_proposta_congelada`. Não altera o trigger comercial do observador.
Não cria tabelas, colunas ou índices adicionais e não modifica registros existentes.
As funções não usam SECURITY DEFINER nem exigem extensão/superusuário.

Ambas as migrations mantêm atomicidade padrão PostgreSQL: cada arquivo constitui
sua própria transação, não há uma transação única envolvendo os dois arquivos.
Não possuem RunPython, UPDATE, INSERT ou DELETE de dados operacionais. O comando
normal `migrate` registra o progresso em `django_migrations` e o `post_migrate`
pode criar content types e permissões padrão dos novos modelos; não concede essas
permissões automaticamente a usuários/grupos e não altera quantidades comerciais.

## Compatibilidade e ensaio

Compatibilidade de SQL confirmada em PostgreSQL descartável. O teste usa o executor
Django para aplicar apenas 0006/0007 sobre o schema-base reconstruído e populado
com fixtures fictícias; outras migrations comerciais não são executadas.

Foi comparado o conteúdo de todas as tabelas preexistentes, exceto o registro
esperado de migrations, por quantidade de linhas e digest dos registros; também
foram comparadas colunas, restrições e triggers preexistentes. Compra, venda, estoque,
OK e conferência protegida fictícia permaneceram iguais. Foram simuladas:

1. Falha durante CREATE TABLE da 0006: nenhuma tabela nova residual, nenhuma marca
   de aplicação e nenhum dado preexistente alterado; nova tentativa aprovada.
2. Falha depois do CREATE das funções/triggers da 0007: rollback das proteções e
   ausência de marca da 0007; a 0006 permaneceu aplicada; nova tentativa aprovada.

O ensaio isolado inicial passou em 2,198 s no cluster `inventario-confiabilidade-pg-h8o9de_7`,
encerrado. Foi repetido após acrescentar assertivas para as 26 colunas e 14 restrições:
aprovado em 2,020 s no cluster `inventario-confiabilidade-pg-6etb6k90`, encerrado.
O runner também deixou de informar um valor fixo `migrations_executed=false`: agora
registra as tentativas reais do executor no `validation.json` do cluster descartável.
Esse metadado não controla aplicação nem acessos. O teste integra a suíte final.

Neon oferece PostgreSQL e documenta migrations Django. O SQL utilizado é padrão
do PostgreSQL suportado pelo projeto; a compatibilidade operacional depende de
confirmar no ambiente real versão, schema `public`, proprietário/privilégios de
CREATE/REFERENCES/TRIGGER/EXECUTE/USAGE e situação das dependências. Essas condições
de produção **não foram verificadas por conexão nesta auditoria**. Recomenda-se
conexão direta TLS, sem pool, para a futura execução, conforme o
[guia oficial do Neon](https://neon.com/docs/guides/django-migrations).
A atomicidade por migration é descrita na
[documentação Django](https://docs.djangoproject.com/en/6.0/topics/migrations/);
os privilégios de triggers estão na
[documentação PostgreSQL](https://www.postgresql.org/docs/16/sql-createtrigger.html).

## Auditoria dos caminhos de aplicação

| Controle | Verificação |
|---|---|
| Acesso gerencial | GET/POST do painel passam por `gestor()` e cabeçalhos privados; aprovação/rejeição/aplicação exigem `aprovar_ajuste` no serviço. Revisor pode consultar, mas não executar essas ações. Anônimo e funcionário sem permissão recebem 403. CSRF obrigatório. |
| Escopo | Rodada precisa pertencer à conferência; propostas da aplicação precisam pertencer à mesma conferência. Token assinado vincula a preparação à rodada e a aplicação à seleção exata. |
| Preparação | Somente SELECT. Não cria proposta nem altera saldo. |
| Aprovação/rejeição | Exige justificativa, declaração física expressa e evidência atual revalidada sob locks. Persiste proposta/auditoria, sem movimento de estoque. |
| Bloqueio técnico | `verificar_execucao()` recusa a operação com flag False antes de qualquer escrita de estoque. Também exige PostgreSQL e três proteções SQL habilitadas. |
| Rota antiga | `services.aprovar` recusa aplicação mesmo se futuramente habilitada a flag; não contorna a confirmação separada. |
| Individual/lote | Uma única função de execução, seleção explícita e confirmação obrigatória; pendentes, rejeitados e bloqueados são recusados. |
| Concorrência | Locks da conferência, itens/rodadas e produtos, em ordem estável; produtos permanecem bloqueados até commit. Venda posterior espera e usa o saldo resultante; compra anterior faz a aplicação esperar e depois bloquear a proposta desatualizada. |
| Idempotência | UUID de proposta, assinatura única por rodada, decisão única por item e aplicação única por proposta. Repetição da seleção inteiramente aplicada retorna a aplicação original; lote parcialmente executado por outra ação exige nova confirmação. |
| Rollback | Todos os selecionados ou nenhum: falha no segundo item, na auditoria ou na atribuição do observador reverte saldos, registros manuais, decisões, observador e auditoria de execução. Aprovações anteriores permanecem. |
| Mudanças após OK | OK original, versão, rodada atual, cadastro congelado, sequência, saldo e sinais precisam corresponder à evidência aprovada; recontagem, lacuna, edição ou unidade incompatível bloqueiam. |
| Unidade histórica | Movimento posterior/intermediário sem unidade congelada permanece bloqueado mesmo com conciliação textual e fechamento numérico. Conversão atual não é prova histórica. |
| Conferência nº 1 | Proteção explícita no servidor. GET permite consulta; nenhuma decisão nova ou aplicação é permitida nessa conferência pela Etapa 3. |
| Observador | Serviço manual existente gera um documento; trigger existente gera um observador. Identidade exata de produto/sequência/saldos/referência é verificada antes do commit, sem duplicação ou soma entre fontes. |
| Ativação futura | Não há scheduler, signal, GET ou hook de inicialização que aplique propostas. Habilitar não executa nada. Cada aplicação exige nova prévia assinada de até 20 minutos e confirmação gerencial; evidência é revalidada, inclusive para propostas antigas. |

Auditoria é append-only nos caminhos da aplicação e protegida contra UPDATE/DELETE
no banco. A proteção não equivale a armazenamento externo inviolável: proprietário
do banco pode desabilitar triggers/alterar funções, e TRUNCATE não é interceptado
por esses triggers de linha. Restringir privilégios da conta operacional faz parte
da futura ativação. Não há rota de exclusão/edição desses registros.

## Integração e regressões

O acesso está no painel gerencial da conferência; a página tem retorno para essa
conferência, estados legíveis e consulta às evidências. Auditoria paginada. Layout
desktop/mobile, tipografia, CSS e templates aprovados preservados nesta revisão.
Chrome verifica o clique do painel para ajustes e o link de retorno, além das
larguras 1366/390/320 e do fluxo fictício de aprovação/aplicação individual.

**415 testes aprovados em 75,458 s**, cluster `inventario-confiabilidade-pg-52whh7b4`,
encerrado (`Resultado: 0; estado: stopped`): 168 testes de inventário/auditoria e
247 de Vendas, Compras, Estoque manual, consumo próprio/financeiro e Locações/disponibilidade.
Inclui os 163 testes anteriores e cinco novas regressões. O ensaio adicional de
migrations com contagens explícitas de campos/restrições passou novamente conforme acima.
Nenhum arquivo
de código comercial, configurações, autenticação, contagem cega ou extrato A/B foi
alterado. A mudança é restrita ao inventário, migrations novas, testes, CSS e docs.

Comando da suíte ampliada:

```
python -B -m inventario.validar_confiabilidade_postgresql inventario.tests_ajustes inventario.tests_extrato_consolidado inventario.tests_saldos_historicos inventario.tests_historico inventario.tests_painel inventario.tests_periodos_historico inventario.tests_saldo_inicial inventario.tests_fluxo inventario.tests_postgresql inventario.tests_aperfeicoamentos estoque.tests_servico_vendas estoque.tests_estoque_manual estoque.tests_consumo_proprio estoque.tests_edicao_compra.EdicaoCompraTests locacoes.tests locacoes.tests_disponibilidade_periodo inventario.tests_migrations_ajustes
```

PostgreSQL local 17.11, Django 6.0.1. `git diff --check` aprovado; `makemigrations
--check --dry-run --settings=inventario.test_settings`: `No changes detected`.
Avisos preexistentes: namespace `estoque` duplicado e pasta `staticfiles` ausente
no ambiente descartável; o servidor de testes serve o CSS nas verificações Chrome.

A primeira execução ampliada expôs duas fixtures incompletas, sem alteração da
lógica comercial: o bootstrap de schema não carregava as três faixas iniciais de
Locações (normalmente inseridas pela 0001); o runner agora chama a função de dados
iniciais apenas no banco guardado. Um teste antigo de estoque criava produto com
unidades NULL e pretendia vender UN; passou a declarar `unidade_compra='UN'` na
fixture e preço de venda 15,00 (em vez de 2,00 abaixo do custo 10,00), mantendo
todas as assertivas originais. O serviço de vendas não foi afrouxado para aceitar
unidade desconhecida ou preço abaixo do custo. A execução final repete esses casos.

## Procedimento recomendado para futura publicação — NÃO executado

1. Obter autorização explícita para commit/push, execução das duas migrations e
   deploy manual. Ativação real será uma autorização separada. Auto-Deploy permanece
   Off; nenhum comando de publicação foi executado por esta auditoria.
2. Conferir o SHA remoto/base e o diff final, incluindo apenas o manifesto abaixo.
   Não publicar `artifacts/`, dumps, backups, dados de demonstração ou credenciais.
3. Manter `INVENTARIO_AJUSTES_HABILITADOS = False`. O `build.sh` atual só instala
   dependências e coleta estáticos; não contém `migrate`. Confirmar também os comandos
   efetivos do painel Render, que não foram acessados nesta auditoria.
4. Estabelecer backup/restauração verificável e janela controlada. Validar primeiro
   em ambiente descartável com versão/configuração equivalentes. Não abrir acesso
   dos usuários ao painel novo antes de concluir as duas migrations.
5. Com o código exato autorizado disponível em tarefa isolada, usar a conexão
   protegida direta TLS do destino confirmado. Não imprimir URL/senha. Conferir
   versão, schema, privilégios, dependências e colisão de nomes das funções/tabelas.
6. Consultar `showmigrations inventario` e `migrate inventario
   0007_auditoria_ajuste_imutavel --plan`. O plano deve conter somente 0006/0007; se
   trouxer outra migration pendente, interromper para revisão. Configurar timeouts
   de lock/statement adequados nessa sessão para não esperar indefinidamente.
7. Somente após autorização, executar uma única tarefa de migration com alvo 0007,
   sem `--fake`, sem execuções concorrentes. Validar tabelas, 12 índices, 14 restrições,
   duas funções, três triggers e as marcas de 0006/0007. Não executar dados de exemplo.
8. Fazer deploy manual do SHA autorizado com flag False e testar consulta, retorno,
   permissões e recusa de execução. Confirmar que nenhuma quantidade/OK mudou.
9. Para eventual ativação, nova autorização, revisão de privilégios e piloto com
   conferência nova e dados atuais; aplicação individual explicitamente confirmada
   antes de qualquer lote. Nunca ativar um job de aplicação automática.

### Se uma migration falhar

- Não publicar a nova interface, não ativar ajustes e não usar `--fake` para pular
  falhas. Identificar destino e causa sem divulgar credenciais.
- Falha na 0006: o PostgreSQL reverte esse arquivo; conferir ausência de sua marca
  e dos objetos novos. Corrigir privilégio/lock/colisão após revisão e tentar de novo.
- Falha na 0007: sua transação reverte; 0006 já pode estar confirmada, com tabelas
  novas vazias. Manter flag False e aplicação antiga. Corrigir e reaplicar apenas
  a pendência. Não apagar tabelas ou alterar o observador para "destravar".
- Se a conexão cair e o resultado do commit for incerto, consultar as marcas em
  `django_migrations` e os objetos antes de repetir. Não presumir sucesso/fracasso.
- Um rollback de código pode manter as tabelas adicionais sem uso. Não reverter
  0006 após existirem propostas/aplicações: sua reversão derrubaria as tabelas novas;
  reverter 0007 retiraria proteções. Preferir correção aditiva e preservar auditoria.
- Restaurar backup não é rotina para um erro DDL atômico; restauração total poderia
  sobrescrever vendas/compras legítimas posteriores. Exige avaliação e autorização
  próprias caso haja dano externo, não executadas automaticamente.

## Limitações remanescentes

- Movimentos sem unidade histórica congelada continuam exigindo nova contagem.
- Até 500 produtos por conferência; preparação mantém custo de validação individual,
  ainda sem ensaio de latência/carga no Neon.
- Migrações dependem dos privilégios e schema reais, não consultados nesta auditoria.
- Triggers locais não substituem segregação de privilégios e backup independente.
- Prévia parcialmente aplicada por ação individual exige nova confirmação do restante.

## Manifesto destinado ao futuro commit (19 arquivos)

```
docs/inventario-etapa3-ajustes.md
docs/inventario-etapa3-auditoria.md
estoque/tests_estoque_manual.py
inventario/ajustes.py
inventario/ajustes_views.py
inventario/migrations/0006_propostas_aplicacoes_ajuste.py
inventario/migrations/0007_auditoria_ajuste_imutavel.py
inventario/models.py
inventario/services.py
inventario/templates/inventario/ajustes.html
inventario/templates/inventario/detalhe.html
inventario/test_runner_confiabilidade.py
inventario/tests_ajustes.py
inventario/tests_fluxo.py
inventario/tests_migrations_ajustes.py
inventario/tests_postgresql.py
inventario/urls.py
inventario/validar_confiabilidade_postgresql.py
static/inventario/ajustes.css
```

Fixtures são geradas apenas por testes em banco descartável; não há dump, seed
operacional ou arquivo de dados fictícios para carregar em produção. Capturas e
artefatos não pertencem ao manifesto. Nenhum arquivo foi staged nesta auditoria.
