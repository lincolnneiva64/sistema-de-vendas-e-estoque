# Persistência de boletos na edição de compras finalizadas

## Causa

O formulário e a função JavaScript `salvarEdicaoFinalizada` de
`compras_nova.html` enviam `acao_compra=salvar_correcao_simples`.
Em `compra_editar`, essa ação chama `correcao_compra.aplicar(automatica=True)`.
O planejamento desse serviço considera os itens e recalcula as contas pelo
total comercial, preservando seus vencimentos. Ele não lia os campos
`compra_forma_cobranca_nota`, `compra_valor_nota_boleto` nem as linhas dos boletos.
Assim, podia confirmar sucesso e gravar produtos/total/conta única, deixando
`ParcelaCompra` e `valor_nota_boleto` antigos. Isso explica a combinação
informada para a compra #240 sem atribuir uma pendência financeira ao Sprite.

O serviço `edicao_compra.salvar_edicao` já sincronizava parcelas e contas, mas
estava associado a outra ação, que o botão atual não enviava.

## Correção restrita

- O salvamento atual identifica a edição de boletos com itens e base comercial
  preservados e reutiliza esse serviço dentro da transação e bloqueios existentes.
- Mantém os IDs das contas informadas, cria somente as novas necessárias e
  cancela, com justificativa, contas abertas retiradas. Nenhuma conta é apagada.
- Sincroniza plano, valor financeiro da nota, primeiro vencimento da compra e
  contas em uma única transação. Vencimentos iguais são aceitos.
- Reenvio do mesmo token/payload retorna a auditoria anterior. Nova edição após
  reabrir também não duplica as obrigações.
- Itens inalterados não são regravados. Não há alteração de estoque nesse fluxo.
- O valor financeiro explicitamente informado pode diferir do total comercial;
  sua soma deve coincidir com as parcelas. Não se igualam automaticamente os
  três campos `valor_nota_boleto`, `total_produtos` e `total`.
- Pagamentos (inclusive cancelados), saldo movimentado, movimentos financeiros,
  documento legado, cartão, vínculos ambíguos e conciliação auditada impedem
  substituir o plano. A rota histórica também protege planos conciliados.
- Alterar itens e o plano de boletos simultaneamente exige conciliação
  específica; os boletos não são mais ignorados silenciosamente.
  A correção de itens existente permanece para envios sem alteração do plano.

Arquivos de implementação: `estoque/services/correcao_compra.py`,
`estoque/services/edicao_compra.py`, `estoque/views.py`.
Testes: `estoque/tests_compra_boletos_finalizada.py`.
Não há mudança de modelo nem nova migration.

## Validação

76 testes passaram no PostgreSQL descartável criado por
`validar_compra_timeout_pg.py`, com conexões limitadas ao novo cluster em
127.0.0.1 e encerramento confirmado. Foram executados:

```
estoque.tests_compra_boletos_finalizada.BoletosFinalizadaTests
estoque.tests_edicao_compra.EdicaoCompraTests
estoque.tests_correcao_automatica.Backend
estoque.tests_correcao_automatica.Concurrency
estoque.tests_compra_interface_finalizada.Backend
```

Os dez testes novos cobrem os valores informados, três vencimentos iguais,
reabertura, reenvio do mesmo token e com token novo, edição posterior mantendo
IDs, valor financeiro distinto, soma inválida, rollback e bloqueios por
pagamento/conta paga/parcial/conciliação/documento legado/movimento financeiro.
As demais classes verificam as proteções e correções existentes, incluindo
concorrência em PostgreSQL.

O teste adicional `BoletosFinalizadaBrowserTests` passou em Chrome real nas
larguras 1280 e 390 px: alterar o painel existente para três boletos, clicar
em Salvar alterações, aguardar a resposta e conferir parcelas/contas e estoque.
Total: **77 testes aprovados nas seleções finais**, com 11 testes novos.

A execução ampliada inicial não foi inteiramente verde: encontrou falhas nos
testes de criação de compra com reenvio (HTTP 409 em vez de 302), navegadores
antigos que procuram controles de conciliação ausentes e cenários antigos de
pré-revisão; o teste de migration antiga não consegue reverter a migration
0136, irreversível. Esses testes não foram corrigidos nesta intervenção.
Não foi feita comparação dessas falhas com HEAD; não se atribui sua origem
a esta correção nem se declara a suíte completa aprovada.

## Regularização posterior da compra real #240

Procedimento proposto para depois da publicação autorizada; não foi executado:

1. Abrir a edição atualizada da compra #240. Conferir fornecedor, total de
   R$ 3.938,96, Sprite presente e situação finalizada; não editar os produtos.
2. Conferir se a conta #144 continua aberta, integralmente sem pagamentos,
   movimentos ou outros vínculos protegidos. Se houver conciliação auditada,
   o reparcelamento será bloqueado: tratar por conciliação específica, sem
   excluir histórico ou remover o bloqueio.
3. No painel existente, escolher **Vários boletos**, quantidade **3**, valor
   financeiro **3.938,96**. Preencher **1.958,19**, **1.341,12**, **639,65**,
   todos com vencimento em **08/11/2026**. Informar o motivo e salvar.
4. Reabrir a compra e conferir as três parcelas persistidas, o primeiro
   vencimento e o valor financeiro. Em Contas a Pagar, conferir exatamente
   três obrigações ativas (1/3, 2/3, 3/3), soma e saldo de R$ 3.938,96.
   A conta #144 é reaproveitada quando seu ID permanece na primeira linha;
   contas retiradas são canceladas com auditoria, nunca apagadas.
5. Conferir histórico da edição e preservação dos produtos/estoque. Não
   adicionar os R$ 10,29 novamente e não recriar, cancelar ou refinalizar a compra.

Não houve acesso ao Neon, alteração de dados reais, commit, push ou deploy.
Migrations existentes foram aplicadas somente na criação dos bancos descartáveis.
