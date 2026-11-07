# Fase B — piloto da regularização física individual

Implementação somente local, em 07/11/2026. Sem acesso a Neon/Render, migração em
banco operacional, commit, push ou deploy. Lotes não instrumentados.

## Integração

`inventario/ajustes.py::_gravar_aplicacoes()` mantém o desvio anterior para
`_gravar_lote()` quando existem dois ou mais produtos. Somente o caminho existente
de uma aplicação cria o complemento. Uma seleção com apenas um produto apto usa
esse mesmo caminho individual, mesmo quando originada na seleção de todos.

A consulta anterior de existência do movimento agora retorna o próprio registro,
com os mesmos filtros: produto, sequência validada + 1, saldos anterior/posterior,
origem `ajuste_manual` e referência exata do movimento manual. Não há busca por
proximidade de horário ou pelo movimento mais recente depois da liberação do lock.

Após criar `AplicacaoAjuste`, chama `rastreabilidade_regularizacao.registrar()`.
A chamada está dentro da transação já existente de `atualizar()`/`aplicar()`, sob
os mesmos bloqueios. Não há callback pós-commit ou tarefa posterior. Falha na
identificação ou inserção desfaz estoque, observador, complemento, aplicação,
decisão e os eventos/propostas criados na transação. Aprovações já existentes
antes da chamada conservam seu estado anterior em caso de rollback.

Não há mudança de fórmula, precisão, conversão comercial, observador ou contrato
global de `identificar()`. A obrigatoriedade está restrita à inserção nesse caminho
individual; nenhuma condição nova exige complemento em movimentos antigos.

## Dados registrados

- Ação `regularizacao_conferencia`; origem técnica continua `ajuste_manual`.
- UUID da aplicação como `operacao_id`, ordem 1; documento `aplicacao_ajuste` com
  UUID preservado e referências textuais de conferência, proposta e rodada.
- Executor autenticado e nome. O nome registrado pelo movimento manual também
  é preservado em `operador_nome`; esse fluxo não seleciona outro funcionário.
  Não se infere FK pelo nome e não se confunde contador físico com operador da
  regularização. O contador original continua na confirmação vinculada à proposta.
- Unidade base da aplicação. Para contagem convertida, quantidade física de entrada,
  unidade secundária, divisão e fator congelados; para base sem conversão, contagem
  na unidade base, conversão `nenhuma` e fator nulo. Nenhum fator é lido novamente
  do cadastro atual para montar o complemento.

`quantidade_entrada` descreve a contagem física que originou a diferença, não uma
nova saída integral nem o delta aplicado. O delta assinado continua sendo derivado
exclusivamente dos saldos do observador. A operação preserva separadamente os
resíduos e a evidência congelada da aplicação, sem ajustar quantidades por conta
da auditoria. Horário/produto/saldos continuam somente no observador.

Os retornos idempotentes existentes saem antes desse escritor. Não há backfill
quando uma aplicação antiga sem complemento é consultada ou reenviada. Sem
diferença, as validações existentes impedem a aplicação e não há complemento.

## Ajuste de esquema expressamente autorizado

A ação solicitada tem 25 caracteres, mas a Fase A tinha campo de 24 e CHECK que
aceitava somente `regularizacao`. O proprietário autorizou ampliar o modelo e
criar nova migration. `0010_acao_regularizacao_conferencia` amplia para 32 e
acrescenta a nova ação, preservando todas as anteriores e a migration 0009 intacta.
Somente a tabela complementar é alterada. Não há alteração de dados nem backfill.

Em uma futura publicação autorizada, 0009/0010 precisam existir no banco antes
do escritor piloto. Sem a estrutura, o caminho individual falha e reverte, sem
deixar atualização de estoque sem complemento. Não executar retorno de esquema
depois de gravar ações novas: o CHECK antigo não as aceita. Rollback operacional
deve preservar o esquema e a auditoria.

## Testes

As três falhas anteriores foram corrigidas apenas nas fixtures:
`VendasInventarioTests` autentica o usuário já criado; o teste da entrada de compra
prepara uma decisão assinada real de manutenção dos preços, sem mocks das regras.
Os 15 testes de `estoque.tests_estoque_manual` e `inventario.tests_integracao`
passaram antes da integração.

Validação final direcionada: **76 testes aprovados**, em **25,786 s**, PostgreSQL
descartável `inventario-confiabilidade-pg-hejauhmz`, encerrado pelo runner.
Foram 18 testes novos do piloto/migration e 58 regressões:

```
venv/Scripts/python.exe -B -X utf8 -m inventario.validar_confiabilidade_postgresql inventario.tests_rastreabilidade_individual estoque.tests_estoque_manual inventario.tests_integracao inventario.tests_confirmacao_unica inventario.tests_ajustes.PreparacaoTests inventario.tests_ajustes.AplicacaoTests
```

Cobertura: metadados, saldos, sequência, ação, vínculos, fator congelado, DZ/PCT/FD,
normalização decimal, KG/MIL, conversão SC/KG, ausência de diferença, falhas antes/
depois da inserção e em evento posterior, rollback, reenvio nos dois fluxos,
concorrência real, histórico anterior com NULL, saldo atual desconhecido bloqueado,
legado sem complemento, lote e manual comum não instrumentados. A migration 0010
foi executada no banco descartável com registro legado preservado.

Avisos preexistentes: namespace `estoque` duplicado e pasta `staticfiles` ausente.
Não foram executados testes contra bancos reais nem se declara a suíte inteira.

## Antes de ampliar para lotes

- Projetar identidade e inserção em lote sem consultas adicionais por produto.
- Manter a ordem dos bloqueios e rollback integral se um complemento falhar.
- Validar contagens máximas contra os limites numéricos da auditoria da Fase A;
  totais na unidade secundária podem ser maiores que o saldo na unidade base.
- Preservar o UUID individual de cada aplicação e os vínculos pelo lote existente.
- Não tornar o complemento obrigatório globalmente: outros escritores continuam
  fora deste piloto, inclusive manuais comuns, compras/vendas e offline.

Alterações visuais anteriores do inventário e trabalhos locais de locações e
outros módulos preservados. Nenhuma integração de lotes iniciada.
