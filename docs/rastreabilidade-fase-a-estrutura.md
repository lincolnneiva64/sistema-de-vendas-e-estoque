# Rastreabilidade futura — Fase A: estrutura isolada

Implementação local em 07/11/2026. Não instrumenta serviços, não exige complemento
e não preenche registros antigos. Fase B não iniciada.

## Contrato

`DetalheMovimentoEstoque` tem vínculo OneToOne protegido com `MovimentoObservado`.
Produto, instante, sequência e saldos continuam exclusivamente no observador.
`quantidade_movimentada` é uma propriedade: depois − antes; retorna `None` se
qualquer saldo for desconhecido, sem normalizar `NULL` para zero.

- `operacao_id` e `ordem` positiva identificam unicamente cada evento efetivo.
- `acao`, `documento_tipo`, `documento_id` e `item_referencia` descrevem o evento.
  Os identificadores textuais não dependem da sobrevivência do documento comercial.
- `executor`/`executor_nome` e `operador`/`operador_nome` são identidades distintas.
  As FKs são protegidas; os nomes preservam a identificação declarada na ocasião.
- `contexto`: autenticado exige executor e nome; local exige operador declarado;
  sistema exige identificação textual do executor técnico. Não se inventa usuário.
- `unidade_base`, `quantidade_entrada`, `unidade_entrada`, `conversao` e `fator`
  registram o operando utilizado. Quantidade de entrada não é saldo nem precisa
  coincidir com o delta: num ajuste absoluto pode ser o saldo-alvo informado.
- Unidades devem ser siglas explícitas em maiúsculas. Não há conversão automática.
  Sem conversão exige unidades iguais e fator nulo; divisão/multiplicação exigem
  fator positivo finito. Quantidade/fator possuem nove casas na auditoria, sem
  alterar as três casas ou a precisão dos campos operacionais.

O modelo não certifica a veracidade da unidade/documento informado por um futuro
chamador nem resolve resíduos. Captura atômica e vínculo exato serão Fase B.
O instante existente deve continuar apresentado em America/Belem; não há coluna
duplicada de data/hora. Não há FK nova para offline nesta fase: o UUID da operação
pode reutilizar o UUID existente sem alterar o protocolo.

## Migration 0009

`inventario/migrations/0009_detalhe_movimento_estoque.py` cria somente a tabela
complementar, suas FKs, unicidades e checks, mais função/trigger de imutabilidade
na própria tabela nova (e equivalentes SQLite). Não altera o observador, seus
triggers, tabelas de estoque, documentos comerciais ou os dados existentes.
Nenhum backfill. Não foi aplicada a banco operacional.

Proteções de atualização/exclusão existem no modelo e no banco, inclusive para
QuerySet e SQL direto. Não há registro no Admin nem endpoint novo. Registros do
observador antigos e novos continuam válidos sem complemento.

## Validação

Executor existente: `inventario.validar_confiabilidade_postgresql`, com novo
cluster por execução. Confere loopback, porta, usuário, banco e diretório real
do servidor e recusa conexões diferentes. Todos os clusters iniciados são
encerrados pelo runner. O sandbox inicialmente impediu o pg_ctl de iniciar;
as execuções seguintes usaram a mesma guarda fora dessa limitação de processo.

12 testes específicos aprovados no PostgreSQL descartável: unicidade do vínculo
e evento, FKs, autoria, unidades discretas/contínuas, conversões, rejeição de NaN,
imutabilidade, rollback conjunto e compatibilidade sem complemento. A migration
real foi aplicada nesse banco, com falha DDL injetada e rollback antes da aplicação
bem-sucedida. Foram comparados hashes dos dados e colunas/restrições/triggers
funcionais das tabelas anteriores. Triggers internos de FK são criados normalmente
pelo PostgreSQL; não são confundidos com alteração do trigger de estoque.

Também verificado, sem conexão a banco, que o estado do modelo corresponde ao
estado das migrations pelo autodetector Django. `git diff --check` sem erros.
Aviso preexistente: namespace de URLs `estoque` duplicado.

Regressão ampliada: 24 de 27 testes aprovados, incluindo novamente os 12 novos.
As três falhas também foram reproduzidas em outro cluster com
`HEAD:inventario/models.py` carregado somente em memória, sem substituir arquivos:

- `EstoqueManualServiceTests.test_entrada_normal_de_compra_nao_cria_historico_manual`:
  fixture chama finalização sem a decisão de preços atualmente exigida.
- `VendasInventarioTests.test_consumo_proprio_antes_e_depois_do_ok` e
  `test_vendas_antes_e_depois_ok_e_devolucao`: fixture recebe HTTP 401 por ausência
  de autenticação, antes de efetuar a venda.

São pendências de testes existentes; não foram alteradas autenticação, decisões
de preços ou fixtures comerciais para contorná-las. Aviso adicional preexistente:
diretório `staticfiles` ausente. Não se declara aprovação da suíte completa.

Não houve acesso a Neon/Render, commit, push ou deploy. Alterações locais anteriores
de inventário, locações e demais módulos preservadas.
