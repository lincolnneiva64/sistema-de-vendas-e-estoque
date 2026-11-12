# Matemática da tabela compacta — comparação atual

Implementação isolada em `.worktrees/matematica-conferencia-1349cd9`, branch
`correcao/matematica-conferencia-1349cd9`, baseada em `1349cd916ec09ff94f0a693ce6a4295d8f235890`.
Sem commit, push, deploy, migrations ou acesso a banco operacional.

## Regra

Diferença atual = contagem comparável − estoque exibido, na mesma unidade-base.
Percentual = diferença atual / |estoque exibido| × 100. Estoque zero deixa o
percentual indefinido, inclusive quando a diferença é zero.
Estimativa ao custo = diferença atual × custo-base congelado da conferência.
Total financeiro e classificação usam essas mesmas diferenças atuais.

A consulta da conferência reutiliza uma única vez a comprovação somente leitura
que já fazia, por meio de `marco_fisico.comprovar()`. A contagem comparável vem de
`marco_fisico.resultante()`: contagem no OK mais efeitos efetivos comprovados
posteriores. O ajuste da própria conferência não é somado novamente, conforme
a regra já publicada. Nenhum serviço de aplicação ou autorização foi alterado.

`comparacao_atual` é um dicionário transitório de apresentação. Os campos originais
da linha, rodada, confirmação e aplicação permanecem intactos. A coluna Contado
exibe a quantidade comparável e identifica a projeção após o OK. Diferença,
percentual, cores, situação e estimativa usam os dois valores da tabela.

Quando unidade, saldo, sequência ou movimentação posterior não são comprováveis,
a tabela preserva o estoque conhecido e mostra “Não comparável” com a limitação.
Não reutiliza a diferença antiga, não presume movimento zero e não gera estimativa.
Equivalências secundárias só aparecem quando a comparação é comprovada; a operação
principal permanece na mesma unidade dos números exibidos.

Os valores do OK e do ajuste aplicado continuam nos detalhes históricos recolhidos.
A sétima coluna mantém o estado da atualização e o botão Ver detalhes, sem misturar
saldo aplicado anteriormente com a comparação atual. Mantidas as sete colunas,
o editor administrativo e o CSS existente.

## Arquivos

- `inventario/views.py`: reutilização da comprovação já consultada e montagem da
  projeção atual, separada da diferença original.
- `inventario/painel.py`: matemática, classificação, equivalências e resumos atuais.
- `inventario/templates/inventario/linha_tabela.html`: valores comparáveis na linha.
- `inventario/templates/inventario/situacao_atualizacao.html`: estado sem saldo histórico.
- `inventario/templates/inventario/detalhes_tabela.html`: identificação explícita
  da diferença original e do percentual atual.
- `inventario/templates/inventario/detalhe.html`: legendas e critérios do resumo atual.
- `inventario/tests_matematica_tabela.py`: novos testes direcionados com dados fictícios.
- `inventario/tests_painel.py`: adequação das expectativas diretamente afetadas.
- Este documento.

## Resultado da validação

**25 testes aprovados em 3,820 segundos**, sem skips. Onze cenários novos e catorze
testes existentes de cálculo e HTTP do painel. Executados exclusivamente pelo
runner protegido `inventario.validar_confiabilidade_postgresql`, em PostgreSQL
descartável local com identidade do banco/diretório/porta/usuário verificada.
Cluster final: `inventario-confiabilidade-pg-2zv6hin4`, encerrado ao término.

Exemplos verificados:

| Estoque exibido | Contagem comparável | Diferença |
|---|---|---|
| 1 MIL | 1 MIL | 0 MIL |
| 0 MIL | 0 MIL | 0 MIL |
| 2 MIL | 0,5 MIL | −1,5 MIL |
| 0,7 MIL | 1 MIL no OK − venda comprovada de 0,3 MIL = 0,7 MIL | 0 MIL |

Também cobertos: compra e venda posteriores, comparação após marco aplicado sem
duplicar seu ajuste, KG, CX/UN com conversão congelada, unidade alterada, movimento
desconhecido, custo ausente, custos negativos, base zero, resumo parcial e consultas
sem escrita. Os valores originais são verificados separadamente.

Chrome headless com HTML real e dados fictícios em 1366, 390 e 320 px: sete colunas,
projeção identificada, detalhes recolhidos e sem transbordamento horizontal.
Capturas estáticas em `diagnostics/matematica-tabela/tabela-*.png`; não são capturas
de produção nem uma validação de todos os cliques da aplicação.

`git diff --check` aprovado. Não foram repetidas as suítes de aplicação do marco
zero nem testes de vendas offline, locações ou financeiro. Permanecem os avisos
preexistentes de namespace `estoque` duplicado e ausência de `staticfiles` coletados
na worktree; a verificação visual usa o CSS fonte local.

Limites: sem projeção comprovada não há diferença atual. O custo utilizado continua
sendo o custo histórico congelado, identificado como estimativa, e a precisão
operacional permanece de três casas decimais. Não há mudança de estoque ou da
política de aplicação do marco zero.
