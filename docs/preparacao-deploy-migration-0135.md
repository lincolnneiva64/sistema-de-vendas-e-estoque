# Preparacao segura do deploy: migration 0135

Base: e771b069e19d5460da7538bc4110a5709e6e8099, worktree correcao-automatica-compras-aa5fdb6. Preparacao local, sem commit, push, deploy ou acesso ao Neon. A informacao de que 0135 nao foi aplicada em producao foi fornecida pelo usuario; nenhum banco operacional foi consultado.

## Alteracoes restritas

- estoque/models.py e estoque/migrations/0135_conciliacao_compra.py: conciliacao_automatica agora tem default=False e db_default=False. A definicao da migration existente foi ajustada porque ainda nao foi aplicada em producao, conforme autorizado. Nenhuma migration 0136 foi criada.
- inventario/management/commands/publicar_cancelamento.py: somente estoque.0135_conciliacao_compra acrescentada a allowlist e aos alvos explicitos. As autorizacoes anteriores locacoes.0017 e inventario.0008 permanecem. Definicao exata da 0135 e schema resultante, incluindo default persistente, tipos e vinculos unicos, sao verificados.
- inventario/tests_publicacao_compra.py: regressao dedicada de DDL, preservacao dos dados, INSERT antigo/novo e bloqueios do gate.

Nao foram modificados services de correcao/edicao, views, templates, build.sh ou Dockerfile. A unica mudanca no modelo aprovado e a metadata do default de banco desse campo. Regras comerciais, conversoes, calculos, interfaces e financeiro permanecem iguais.

## Compatibilidade

O default Python continua False; o default PostgreSQL tambem permanece false depois da migration. Compras existentes recebem False, e INSERTs da versao anterior que nao incluem a coluna passam a receber False do banco. O modelo historico 0134 foi usado para executar efetivamente esse INSERT sem o campo novo; a compra persistida foi relida pelo modelo atual. A criacao pelo modelo novo tambem foi validada.

ItemCompra.estoque_base_registrado permanece nullable e inicia null. A nova tabela de conciliacoes inicia vazia. Nao ha calculo retroativo, mudanca de saldo, atualizacao ou exclusao de movimentos financeiros ou de itens comerciais. A migration consiste somente em duas AddField e uma CreateModel, atomicas, dependentes de estoque.0134_edicao_compra. As FKs apontam para Compra e EdicaoCompra existentes.

## Protecoes preservadas

O comando mantem preflight em READ ONLY; plano de todas as folhas do grafo; recusa de reversoes, pendencias nao autorizadas, historico inconsistente, migrations desconhecidas e conflitos; advisory lock compartilhado 5161650017; rechecagem sob lock; lock_timeout 5s; statement_timeout 60s; transacao integral de DDL e recorder. --plan nao grava; segunda execucao sem pendencias apenas verifica schema.

A verificacao da 0135 recusa mudanca em dependencia, atomicidade, operacoes, campos, defaults e vinculos. O noop recusa schema com default ausente. Se algum banco ja tiver aplicado a antiga definicao da 0135 sem default persistente, o gate vai interromper; nao marca nem simula a correcao. Esse caso exige plano separado, sem reescrever historico aplicado.

## Procedimento de publicacao futura

Nenhuma configuracao nova no Render e necessaria; manter o comando de build existente. A alteracao desta preparacao precisa de revisao e aprovacao antes de commit/push/deploy.

No codigo candidato e ambiente autorizado:

```bash
python -u manage.py publicar_cancelamento --plan
```

O unico novo pendente esperado e APLICAR estoque.0135_conciliacao_compra. A 0134 e demais dependencias devem estar realmente aplicadas. Qualquer outra pendencia requer revisao especifica; nao usar --fake nem um migrate irrestrito para contornar o gate.

O build.sh preservado instala dependencias, coleta estaticos e executa:

```bash
python -u manage.py publicar_cancelamento
```

O gate revalida sob lock e aplica o plano explicitamente autorizado numa transacao. Em nova execucao, deve indicar nenhuma pendencia e verificar o schema/default; nao duplica tabelas, conciliacoes ou registros de migration. Falhas de DDL, verificacao ou timeout abortam o build e revertem a transacao.

Com db_default persistente, a criacao de compras pela versao anterior continua compativel durante a transicao e eventual retorno a versao anterior nao exige remover a coluna. Confirmar o novo SHA Live na publicacao autorizada. A migration nao corrige nenhuma compra real automaticamente.


## Validacao final realizada

108 testes distintos aprovados em duas execucoes finais com zero falhas:

- 30 testes de publicacao: 19 anteriores e 11 novos; 107.518 segundos.
- 78 testes de compras, interface e correcao automatica; 113.368 segundos. Chrome em 1280, 1440, 320, 360 e 390 px, incluindo regressao de layout e credito zero.

makemigrations --check --dry-run passou no teste de consistencia de schema. git diff --check passou. Dados anteriores de estoque e financeiro permaneceram identicos ao aplicar a 0135. Aplicacao e reaplicacao foram realizadas em PostgreSQL 17.11 loopback, com conexoes fora do cluster recusadas pelo runner. Os bancos de teste foram destruidos e todos os clusters encerrados.

A primeira rodada ampla teve dois testes negativos esperando tipos de excecao inadequados: o Django recusa ciclo e historico inconsistente antes da validacao do comando. Somente as expectativas desses testes novos foram corrigidas. Houve tambem um timeout de autocomplete no Chrome; a repeticao integral dos 78 testes passou sem alterar interface ou seus testes. Logs da primeira rodada e das duas execucoes finais foram preservados em evidencias-migration-0135/preparacao.

Arquivos da entrega: estoque/models.py; estoque/migrations/0135_conciliacao_compra.py; inventario/management/commands/publicar_cancelamento.py; inventario/tests_publicacao_compra.py; este documento. Nenhum arquivo foi staged, commitado ou publicado nesta etapa.
