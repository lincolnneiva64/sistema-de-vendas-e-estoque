# Validação da consulta de listas mobile

Base: `9b117de12c7a8d42f2904da7b3b0793bb5c589dc`.

Compactação restrita aos cartões mobile de Consultar Listas por Fornecedor. A tabela desktop, os filtros, critérios de consulta, status e condições das ações permanecem inalterados. Nova Lista e Editar Lista não foram modificadas.

## Validação executada

- Chrome em 320, 360 e 390 px: cartões menores, ações em três colunas, botões com pelo menos 44 px de altura, sem rolagem horizontal ou erros JavaScript.
- Comparação com a base em 1280 e 1440 px: conteúdo visível, links, geometria e propriedades CSS calculadas idênticos, nos modos pendentes, histórico e canceladas.
- Estados de listas e compras geradas, nomes longos, valores elevados, navegação e confirmação de cancelamento verificados com dados sintéticos, sem efeitos em compras, estoque ou financeiro.
- Somente PostgreSQL 17.11 descartável em loopback, encerrado após os testes. Sem acesso ao Neon.

Os dois testes novos de navegador passaram na rodada final, em 38,801 segundos. Na rodada ampla inicial, foram executados 47 testes: 42 passaram e cinco falharam. Uma das cinco falhas era do comparador CSS, que precisava ordenar as propriedades retornadas pelo Chrome; o comparador foi ajustado e os dois testes novos passaram na reexecução.

Considerando os testes distintos pelo resultado mais recente: **43 aprovados e quatro falhas preexistentes**. Não foi feita uma nova execução conjunta dos 47 testes depois do ajuste do comparador.

## Quatro falhas preexistentes

Todas pertencem a `estoque.tests.ComprasListaFornecedorConferenciaTests`. As quatro foram reproduzidas com o template original de `9b117de`, em outro PostgreSQL descartável, e permanecem na versão modificada. Não foram corrigidas, suprimidas ou marcadas como esperadas nesta alteração.

| Teste | Causa observada |
|---|---|
| `test_consulta_mobile_editar_lista_e_link_real_com_mesma_rota_do_desktop` | Exige a URL Editar sem parâmetros; a implementação já inclui `retorno_consulta`. |
| `test_detalhe_exibe_resumo_compacto_da_conferencia_salva` | Exige `pediu 2.000 UN, chegou 1.000 UN`; a página renderiza `pediu 2,000 UN, chegou 1,000 UN`, com vírgula decimal. |
| `test_detalhe_gera_link_externo_com_conferente` | Exige o domínio fixo do Render; o ambiente isolado gera `https://testserver/checklist/...`. |
| `test_edicao_lista_fornecedor_tem_layout_mobile_em_cards_sem_tabela_horizontal` | Exige literalmente `class="sugestao-mobile-card"`; o cartão também tem a classe `item-adicionado-lista`. |

## Reexecução dos testes específicos

```powershell
python validar_compra_timeout_pg.py estoque.tests_consulta_listas_mobile
```

Requer o Chrome e os binários locais do PostgreSQL usados pelo runner. O teste comparativo obtém o template histórico por `git show`, alterna temporariamente o template da worktree e o restaura em `finally`; execute sem outro servidor usando a mesma worktree. As capturas e os logs são artefatos locais, fora do commit.
