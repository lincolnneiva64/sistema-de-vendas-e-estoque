# Fornecedor original das conferências históricas

Worktree isolada `.worktrees/inventario-fornecedor-historico-994cda8`, base
`994cda85a53152d255d8d0843cff9b4085cafa57`. Commit e push autorizados após
aprovação da correção; deploy permanece manual, pelo proprietário.

## Evidência encontrada para a Conferência nº 1

Consulta somente leitura à cópia `recuperacao-pre-ajustes-2026-10-18`, endpoint
de recuperação conferido, usando TLS `verify-full`. A conexão iniciou com
`default_transaction_read_only=on`, transação REPEATABLE READ/READ ONLY;
`SHOW transaction_read_only` retornou `on`. Somente SELECTs foram executados,
seguidos de rollback e fechamento. Nenhuma conexão de produção foi utilizada.

Registros persistidos nessa cópia:

| Campo | Valor |
|---|---|
| Conferência | 1 |
| Título persistido | Fornecedor |
| Critério | fornecedor |
| Filtro original | `{"fornecedor_id": "10"}` |
| Criação | 18/10/2026 06:52:14,200703 UTC |
| Fornecedor nº 10 | Garcia Distribudora |

Os seis snapshots pertencem a Fermento Angest, trigos sem/com fermento,
Trigo 25 kg, Trigopan e Bril Condicionador. Isso corresponde à seleção conhecida.
O fornecedor foi identificado pelo filtro original, não pela marca dos produtos,
categoria, título genérico ou pelos vínculos atuais ProdutoFornecedor.

Resultado esperado: **Conferência nº 1 — Garcia Distribudora**.
A grafia é exatamente a do cadastro consultado; não foi corrigida ou presumida.
A evidência é da cópia histórica, não uma nova certificação do cadastro Live.
Na exibição, o sistema resolve o mesmo ID persistido no cadastro do ambiente
em execução. Se o filtro ou o cadastro não forem suficientes, mostra aviso;
não utiliza um nome fixo de fornecedor, nem um tratamento especial para o ID 1.

## Correção

Somente a listagem recebe `titulo_exibicao` e eventual aviso, em memória.
Não é executado UPDATE do título persistido ou de qualquer conferência.

- Títulos genéricos de fornecedor são resolvidos apenas quando o filtro original
  identifica um único fornecedor válido e disponível no cadastro.
- Filtros ausentes, inválidos, múltiplos ou com chaves conflitantes não permitem
  escolher um nome: “Fornecedor não identificado”, com explicação.
- Categorias genéricas usam a categoria salva no filtro original. Seleção manual
  permanece “Produtos selecionados”; não se deduz fornecedor pelos produtos.
- Títulos personalizados e automáticos novos permanecem intactos.
- Datas, estados, ordem e seções da listagem publicada foram preservados.

## Testes

**25 testes aprovados em 11,774 s**, PostgreSQL 17 descartável e Chrome com
perfis temporários. Zero falhas; cluster encerrado; nenhuma migration executada.
Inclui seleção histórica da conferência fictícia nº 1, links atuais divergentes,
evidência ausente/múltipla/inválida, IDs malformados, títulos personalizados e
novos, categorias/manual, leitura sem escrita, datas e permissões, saldos
históricos aplicados e prévias desktop/mobile. Os dados de contagem e estoque
do navegador são fictícios, não uma captura da Conferência nº 1 de produção.

Não foram alterados contagens, aprovações, ajustes, vendas, compras ou auditoria
reais. Nenhuma migration, escrita no Neon ou deploy foi realizada.

## Verificação final para publicação

Branch `backup/wip-pedidos-base-2026-06-04` conferida sem avanço, ainda em
`994cda85a53152d255d8d0843cff9b4085cafa57`. Regressão final: **25 testes
aprovados em 12,288 s**, PostgreSQL descartável e Chrome desktop/mobile.
Cluster `inventario-confiabilidade-pg-b2eatyp8` encerrado; zero falhas e nenhuma
migration executada. Diff verificado sem erros. Nenhuma nova consulta ao Neon
foi necessária nessa etapa. Apenas a correção de apresentação, seus testes e
este relatório pertencem ao commit; capturas, logs e temporários ficam fora.
