# Venda Offline — etapa 2.5

## Auditoria anterior à implementação

Base `ea3cf54`. `offline.models.OperacaoSincronizacao` possui operation_id UUID
globalmente único, device_id UUID, actor FK protegido, environment_id, type,
schema_version, aggregate_id (40 caracteres), payload, payload_hash, comando
completo, status, resultado JSON, recebido_em, concluido_em e erro. A referência
FK existente é específica de EventoLocacao. record_id/receipt já ficam em
resultado; não é preciso criar coluna/FK de venda ou migration.

O nome real da validação é `validate_command`, em offline/services.py (não existe
validate_operation). Ela valida envelope, ator contra sessão, ambiente, UUIDs
canônicos, versão 1, sequence inteiro positivo seguro para JS, data com timezone,
conteúdo e hash. `command_hash` usa SHA-256 do comando completo, JSON ordenado por
chaves, separadores compactos, UTF-8 e allow_nan=False. Ordem das chaves não muda
o hash; ordem de itens, tipos de valores e textos permanecem significativos.

`POST /api/offline/observations/` já é o endpoint do dispatcher. Requer sessão,
CSRF e offline.registrar_observacao. A venda online não possui permissão específica
no decorador; o comando mantém o gate atual do piloto, sem inventar permissão de
venda ou liberar o endpoint. Limite existente de corpo: 20000 bytes.

`process_operation` usa atomic externo e get_or_create arbitrado por UNIQUE UUID.
Reserva em enviando, cria efeito oficial e salva receipt/confirmada na mesma
transação. Replays com comando/hash/ator diferentes dão 409 preservando registro;
replays iguais retornam resultado armazenado (200 confirmado, 409 conflito).
Observações incompatíveis são conflitos definitivos persistidos. Exceção técnica
propaga, desfaz efeito e reserva. Não há estado durável de processando separado.

Sequence é metadata do comando e do hash; o cliente atual incrementa contador
por device na criação de operação, mas backend não impõe unicidade/ordenação de
sequence. Não criar outra sequência nem mudar essa semântica. Rascunhos não
consomem sequence e não serão convertidos nesta etapa.

## Decisão

Adicionar criar_venda ao dispatcher existente. Envelope/receipt/hash/status/gate
permanecem. aggregate_id de nova venda será o próprio operation_id UUID, pois
ainda não existe ID oficial. O UUID global continua sendo a chave idempotente.
Somente criação comum, sem Pedido nesta versão: rejeitar pedido_id e quaisquer
campos de edição, inclusive quando vazios. IDs de produto são obrigatórios;
nome é resolvido e bloqueado no servidor, nunca substitui ID ausente/inválido.
O serviço oficial da 2.4 mantém todas as regras comerciais.

Atomic externo inclui reserva, savepoint do adaptador/serviço e confirmação.
Erro de negócio desfaz savepoint e persiste conflito no atomic externo; erro
técnico desfaz tudo. UNIQUE arbitra dois inserts concorrentes; a operação existente
é lida com select_for_update antes do replay. O serviço mantém seu atomic interno.
Não alterar interface, Service Worker, IndexedDB, rascunhos, arquivos antigos ou
template protegido. Teste PostgreSQL será adicionado e pulado se indisponível.

## Protocolo final

Enviar ao mesmo `POST /api/offline/observations/`, com sessão e CSRF:

```json
{
  "operation_id": "11111111-1111-4111-8111-111111111111",
  "device_id": "22222222-2222-4222-8222-222222222222",
  "actor_id": "7",
  "environment_id": "ambiente-do-snapshot",
  "type": "criar_venda",
  "schema_version": 1,
  "aggregate_id": "11111111-1111-4111-8111-111111111111",
  "created_at": "2026-10-07T12:00:00-03:00",
  "sequence": 1,
  "payload": {
    "schema_version": 1,
    "cliente_id": "12",
    "data_venda": "2026-10-07",
    "data_vencimento": "2026-11-07",
    "tipo_pagamento": "A prazo",
    "operador": "Operador",
    "itens": [
      {"produto_id": "34", "quantidade": "2", "unidade": "UN", "preco_unitario": "10.00"}
    ]
  },
  "payload_hash": "SHA256-do-comando-sem-o-campo-payload_hash"
}
```

O exemplo contém placeholders de ambiente/hash e IDs ilustrativos. Calcular hash
com a função existente `offline.services.command_hash(command)` (equivalente ao
protocolo do core.js). Não incluir payload_hash dentro do comando a ser hashado.
Não alterar UUID, device, ator, sequence, created_at ou qualquer conteúdo no retry.
Schema 1 é explícito tanto no envelope quanto no payload de criação.

Campos obrigatórios do payload: schema_version, data_venda, tipo_pagamento,
operador, itens. Opcionais: cliente_id (string positiva, vazio ou null),
data_vencimento (string), origem_recebimento (objeto caixa/banco com valores
numéricos em strings). IDs de produtos são strings positivas; cada item exige
produto_id, quantidade/preco_unitario numéricos finitos em strings e unidade string.
Lista: 1–200 itens, ainda sujeita ao limite global existente de 20000 bytes.
Quantidade/preço negativos ou zero, unidade divergente e regras comerciais são
decididos pelo serviço oficial, não pela validação estrutural.

Nenhum campo de edição é aceito, nem vazio/null: venda_id, next,
ajuste_separacao_id ou item_id. Pedido não é permitido nesta primeira versão.
produto_nome enviado também é rejeitado: o adaptador bloqueia os produtos ativos
por ID em ordem de PK e obtém o nome atual exclusivamente no servidor. Não há
fallback silencioso para outro produto. Depois chama
`estoque.services.vendas.criar_ou_atualizar_venda(usuario=user, dados=dados)`.
Sem cópia de cálculo/estoque/financeiro/eventos/consumo no adaptador.

Enviar somente campos necessários. Campos calculados de compatibilidade
total/subtotal/estoque/custo/saldo/valor_total, se recebidos no payload ou item,
são descartados antes do serviço. Nunca são autoridade. O comando original
continua preservado para auditoria e hash, inclusive esses campos quando enviados.
Outros campos desconhecidos são rejeitados. As permissividades comerciais da
2.4 (cliente opcional, operador vazio, prazo sem vencimento) continuam sendo
responsabilidade do serviço, sem criar regras divergentes aqui.

## Receipt e replay

Receipt mantém exatamente os campos do motor existente:

```json
{
  "operation_id": "11111111-1111-4111-8111-111111111111",
  "hash": "sha256-do-comando",
  "status": "confirmada",
  "record_id": 123,
  "completed_at": "2026-10-07T15:00:00+00:00"
}
```

O nome público existente é `hash`, equivalente ao payload_hash persistido;
não foi renomeado. record_id é a PK de Venda. completed_at é persistido e não
recalculado no replay. Não incluir URL/estoque corrente: isso faria receipt
depender de estado posterior ou contexto HTTP. O receipt é salvo antes do commit
e montado como JsonResponse pela view depois do commit.

| Situação | Resultado |
| --- | --- |
| UUID novo, venda válida | 200, confirmada, Venda e efeitos únicos |
| Mesmo comando confirmado | 200, mesmo receipt integral, serviço não executa |
| Mesmo comando em conflito | 409, mesmo receipt/erro persistido, serviço não executa |
| Mesmo UUID, conteúdo/hash/device/ator/ambiente/sequence diferente | 409, conflito de reutilização, operação original intacta |
| Actor não corresponde à sessão ou ambiente não corresponde ao servidor | 400 antes do dispatcher, sem efeitos |
| Envelope/hash/UUID/versão/shape inválidos ou edição/Pedido | 400 antes da reserva |
| Erro comercial do serviço | 409 persistido, record_id null, erro com mensagem oficial |
| Exceção técnica inesperada, erro de banco, falha de confirmação | propaga para tratamento 500 do Django; rollback integral, retry seguro |
| Resposta perdida após commit | retry com o mesmo comando retorna receipt anterior |

Preço abaixo do custo retorna conflito com a mensagem oficial
`Preco de venda abaixo do custo. Custo: ...`; não substitui o preço recebido.
Estoque insuficiente em venda comum retorna conflito, sem venda parcial. Produto
inexistente/inativo retorna `Produto informado nao foi encontrado no estoque ativo.`
Erros de cliente/quantidade/unidade/data/origem mantêm as mensagens do serviço.

Conflito é definitivo para esse UUID, como nas observações: mesmo após reposição
de estoque, o replay mantém o conflito. Revisão/correção precisa de nova operação
com novo UUID, explicitamente decidida pelo usuário na futura interface.
Outro UUID sempre representa outra operação; não há deduplicação por semelhança
de conteúdo, por device ou por sequence. O endpoint online continua sem esse
protocolo de idempotência; sua implementação permanece exatamente igual à base.

## Atomicidade e concorrência

O atomic externo de process_operation contém:

1. get_or_create da operação em enviando, arbitrado por UNIQUE(operation_id).
2. Savepoint do adaptador; resolução/bloqueio dos produtos por ID.
3. Serviço oficial, com seu atomic interno preservado e todos os efeitos.
4. Salvar receipt, status e concluido_em.
5. Commit externo; somente depois a view constrói JsonResponse.

Se um efeito obrigatório falhar, o serviço/savepoint desfaz todos os efeitos.
Somente ErroGravarVenda é convertido em conflito persistido. Exceções técnicas
desfazem também a reserva da operação. Se salvar a confirmação falhar após a
venda/financeiro, o atomic externo desfaz venda, financeiro, estoque e operação.
Se o commit ocorreu e a resposta sumiu, o registro confirmado já existe no banco.

No PostgreSQL, uma segunda inserção com mesmo UUID espera a resolução da primeira
pela constraint UNIQUE. get_or_create trata a colisão em seu savepoint existente;
o processador lê/bloqueia o registro obtido com select_for_update e retorna o
resultado persistido. Não usa filter seguido de create como garantia. Produtos
também são bloqueados em ordem de PK. Nenhuma alteração em sequence/global schema.

## Arquivos e verificações

- Modificado: offline/services.py (validação, dispatch e conclusão de criação).
- Criados: offline/sale_commands.py, offline/tests_sale_creation.py e este documento.
- Sem migration: campos, UUID único e resultado JSON existentes são suficientes.
- `makemigrations --check --dry-run --settings=offline.test_settings`: No changes detected.
- View online e serviço da 2.4 conferidos byte a byte contra ea3cf54 (normalizando CRLF): iguais.
- Template protegido conserva alteração local anterior. Nenhum arquivo visual,
  JS, Service Worker, rascunho, diagnostics, .bak, .txt antigo ou script existente foi alterado.
- Sem staging, commit ou push.

### Testes executados

SQLite em memória, usando o runner isolado já existente; nenhum acesso ao banco
operacional. Logs novos no TEMP com prefixo venda25-.

Antes: `manage.py test offline.tests estoque.tests_servico_vendas
estoque.tests_consumo_proprio --settings=offline.test_settings --noinput --verbosity=0`:
56 testes, 55 aprovados, 1 pulado (concorrência PostgreSQL existente).

Final: `manage.py test offline.tests_sale_creation offline.tests
offline.tests_commercial estoque.tests_servico_vendas estoque.tests_consumo_proprio
--settings=offline.test_settings --noinput --verbosity=0`: 91 testes, 88 aprovados,
3 pulados (1 concorrência existente e 2 novos de criação, todos exigem PostgreSQL).

Regressão ampla: `manage.py test estoque.tests.VendaEdicaoUnificadaTests
estoque.tests.PedidoTests estoque.tests.SeparacaoVendaFase1Tests
--settings=offline.test_settings --noinput --verbosity=0`: 276 testes, 263 aprovados,
12 falhas e 1 erro. As 13 ocorrências foram comparadas automaticamente e são todas
as mesmas já documentadas na etapa 2.4; não foram corrigidas fora do escopo.

20 métodos `estoque.tests.PixRecebidoTests.test_gravar_venda*`, selecionados por AST
e executados com os mesmos settings: todos aprovados.

Novos testes comprovam:

- Receipt/hash/UUID e replays idênticos sem reexecutar serviço; mudança de ordem
  das chaves preserva hash; mudança de conteúdo/identidade não reutiliza operação.
- Perda de resposta e contagem/snapshot integral antes/depois para prazo, vista
  dividida caixa/banco, consumo e venda com dois itens. Estoque, ItemVenda,
  EventoVenda, ContaReceber, MovimentoFinanceiro, ContaFinanceira, DespesaDiaria
  e catálogo ficam iguais no replay.
- Perda de JsonResponse **depois de commit real**, em TransactionTestCase,
  verificando conexão fora de atomic, receipt persistido e retry sem efeitos.
- Rollback no segundo item e depois de evento/conta; rollback técnico no financeiro;
  falha ao salvar confirmação após criação de conta, movimentos ou despesa/catálogo,
  seguida de retry seguro.
- Produto/cliente inválido/inativo, quantidade, unidade/conversão, custo/preço,
  estoque, origem, data, edição/Pedido rejeitados; preços do snapshot não ajustados.
- total/subtotal/estoque=999999 e custo=0 não controlam valores oficiais.
- Confirmação anterior não revalida venda quando produto é desativado ou custo/saldo
  muda depois: devolve a mesma venda, sem tentar criá-la novamente.
- Autenticação, permissão do piloto, CSRF, ambiente, device, sequence, limite do
  corpo, no-store e schema preservados. Produto por nome não substitui ID.
- Dois testes PostgreSQL: UUID/conteúdo iguais cria uma venda e financeiro;
  UUID igual/conteúdo diferente permite apenas um vencedor e um conflito.

PostgreSQL não executado: não há OFFLINE_TEST_DATABASE_URL isolado configurado nem
binários PostgreSQL/Docker disponíveis no PATH. SQLite não prova locks/concurrency.
Para executar os testes reais, fornecer essa variável com banco PostgreSQL
**isolado** e rodar `offline.tests_sale_creation.SaleCreationPostgreSQLTests` com
offline.test_settings; o runner cria o banco de teste. Não usar banco operacional.

`git diff --check`: aprovado. Avisos preexistentes: namespace estoque duplicado;
Git avisa normalização LF→CRLF. A fixture de catálogo foi ajustada para comparar
com os catálogos semeados pelas migrations, em vez de assumir tabela vazia;
as falhas dessa rodada intermediária eram de expectativa do teste, corrigidas.

## Limites e próxima etapa

Garantia implementada por UUID único + transação: se houve criação/commit para
UUID X, retransmissão válida de X devolve a mesma venda/receipt, sem nova baixa ou
efeito financeiro. Os testes de replay/commit passaram. Testes de concorrência
real estão prontos, mas não foram executados neste ambiente.

Permanecem as 13 ocorrências anteriores da regressão ampla e permissividades do
serviço online. UUID deve ser preservado no retry; trocar UUID é nova operação.
Nenhum conflito é corrigido automaticamente. Não há Pedido ou edição offline.

Falta, em etapa futura: conclusão offline na tela, criação explícita de operação
a partir do rascunho com reserva da sequence existente, fila/retry da criação,
tratamento visual de receipt e conflitos comerciais, reconciliação e eventual
projeção local de estoque/financeiro. Não implementados nem ativados nesta etapa.
