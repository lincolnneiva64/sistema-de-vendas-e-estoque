# Inteligência comercial — Balcão × Clientes cadastrados

Implementação somente local, em 28/11/2026. Sem commit, push, deploy, migrations
ou alteração de dados e configurações. O cadastro #1126 já se chamava
`Venda De Balcão` quando foi consultado; nenhuma renomeação foi feita nesta tarefa.

## Regras e apresentação

O ID #1126 é uma origem coletiva confirmada, independentemente do nome e da
condição de pagamento. As vendas à vista de outros clientes permanecem nos
clientes cadastrados. Outras classificações só usam as confirmações por ID que
já existiam; não se inferem identidades por nome ou forma de pagamento.

Na seção existente Participação dos clientes na variação, o quadro compacto
Balcão × Clientes cadastrados apresenta faturamento, participação, operações,
CMV, lucro e margem. Usa o intervalo dos indicadores financeiros, explicitado no
cabeçalho; continua disponível quando não há período anterior comparável.

O ranking de cinco maiores compradores cadastrados, por faturamento, é
expansível. O balcão aparece como comparação separada, sem posição individual.
Os rankings existentes de redução e crescimento continuam ordenados pelo impacto
em reais, excluindo origens coletivas. O balcão permanece na evolução comercial
e na investigação por produto, mas não é contado como pessoa ausente, recorrente
ou fidelizada. Sua ausência em um produto não aumenta a contagem de clientes
individuais ausentes.

Se houver notas sem cliente vinculado ou outros coletivos já confirmados,
aparecem linhas próprias; não são somadas indevidamente aos clientes cadastrados.
O faturamento e a quantidade de operações vêm das notas, sem join multiplicador
com itens. Os custos reutilizam as validações do Resumo de Vendas, inclusive
custos históricos e suas evidências; não há nova regra financeira ou fallback
para custo atual. A separação acrescenta duas consultas, sem N+1 ou repetição da
validação de custos.

Quando a cobertura é parcial, o CMV conhecido e o lucro/margem da parcela coberta
são identificados como parciais, com quantidade de itens e faturamento coberto.
O resultado total fica indisponível. Notas e itens divergentes também impedem
mostrar resultado total. Em períodos vazios, operações e valores são zero;
participação e margem sem denominador são exibidas como traço.

## Conferência com dados existentes, somente leitura

Consultas em transação READ ONLY e REPEATABLE READ. Faturamento, CMV validado e
receita coberta conferiram com Resumo de Vendas e Análise de Classes e Produtos.
Não houve duplicação de notas nem alteração de custos, estoque ou recebimentos.

### Outubro de 2026 — 01/10 a 31/10

| Origem | Faturamento | Participação | Operações | CMV conhecido parcial | Lucro da parcela coberta | Margem da parcela coberta |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Venda de Balcão | R$ 7.216,43 | 10,07% | 124 | R$ 5.132,99 | R$ 1.428,31 | 21,77% |
| Clientes cadastrados | R$ 64.476,42 | 89,93% | 348 | R$ 47.360,74 | R$ 11.957,11 | 20,16% |
| Total geral | R$ 71.692,85 | 100,00% | 472 | R$ 52.493,73 | R$ 13.385,42 | 20,32% |

Cobertura do balcão: **153 de 191 itens**, sobre **R$ 6.561,30**, equivalentes a
**90,92%** do seu faturamento. Clientes cadastrados: 1.231 de 1.527 itens,
R$ 59.317,85 cobertos (92,00%). Geral: 1.384 de 1.718 itens, R$ 65.879,15 cobertos
(91,89%). **Os lucros e margens acima não são o resultado total de outubro.**

A comparação individual de outubro com o período anterior pode estar
indisponível pela cobertura documental preexistente de setembro. Isso não
impede o resumo de origens nem o ranking por faturamento de outubro.

Maiores cadastrados por faturamento: Tamara Helena Olliveira #1120,
R$ 4.618,93; Marcio Oliveira #31, R$ 3.712,20; João De Almeida E Silva #44,
R$ 3.470,00; Janaina Oliveira Do Nascimento #1139, R$ 3.119,55;
Marcila Chagas Balneário #701, R$ 3.065,03. O balcão é comparado à parte.

### Novembro de 2026 — 01/11 a 27/11

| Origem | Faturamento | Participação | Operações | CMV validado | Lucro bruto | Margem bruta |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Venda de Balcão | R$ 7.127,25 | 11,75% | 110 | R$ 5.759,15 | R$ 1.368,10 | 19,20% |
| Clientes cadastrados | R$ 53.533,14 | 88,25% | 263 | R$ 43.253,47 | R$ 10.279,67 | 19,20% |
| Total geral | R$ 60.660,39 | 100,00% | 373 | R$ 49.012,62 | R$ 11.647,77 | 19,20% |

Cobertura integral: balcão 175/175 itens; cadastrados 1.128/1.128;
geral 1.303/1.303. Os faturamentos e participações do balcão coincidiram com as
referências históricas fornecidas, sem valores fixos no código.

Maiores cadastrados por faturamento: Marcila Chagas Balneário #701,
R$ 3.763,84; Tamara Helena Olliveira #1120, R$ 3.513,95;
Rezuene Roseno Da Costa #54, R$ 3.461,41; João De Almeida E Silva #44,
R$ 3.177,00; Janaina Oliveira Do Nascimento #1139, R$ 2.915,03.

## Arquivos do escopo

- `estoque/services/inteligencia_clientes.py`: identificação estável, resumo e ranking.
- `estoque/services/investigacao_comercial.py`: coletivo não vira ausência individual por produto.
- `estoque/views_relatorios.py`: reutilização do resumo financeiro na página existente.
- `estoque/templates/estoque/relatorios/participacao_clientes.html`: integração e identificação coletiva.
- `estoque/templates/estoque/relatorios/investigacao_lista.html`: rótulo de origem coletiva no produto.
- `estoque/templates/estoque/relatorios/balcao_clientes.html`: novo fragmento compacto, sem página nova.
- `static/estoque/relatorios.css`: apresentação restrita ao novo quadro.
- `estoque/tests_inteligencia_clientes.py`: expectativas atualizadas pela confirmação comercial do #1126.
- `estoque/tests_balcao_clientes.py`: testes de classificação, reconciliação, custos parciais, períodos e rankings.
- Este documento.

As correções de pagamento e Consumo próprio foram preservadas byte a byte,
incluindo seus testes. As alterações locais anteriores de outros módulos não
foram transportadas nem descartadas. A pasta de publicação não foi modificada.

## Validação e evidências

91 testes aprovados na suíte ampliada em PostgreSQL descartável protegido:

    python -m inventario.validar_confiabilidade_postgresql estoque.tests_balcao_clientes estoque.tests_inteligencia_clientes estoque.tests_investigacao_comercial estoque.tests_analise_classes_produtos

Após a revisão visual da legenda de cobertura, 11 testes dirigidos do quadro e
da navegação responsiva também passaram. As verificações incluem períodos vazios,
custos ausentes, valores divergentes, nomes antigos/novos, notas canceladas,
consumo próprio, vendas à vista individuais, notas com vários itens, ausência de
N+1, filtros, ordenação e restrições de acesso existentes.

Conferência do Django completo na porta 8000: outubro e novembro, larguras
1536/390/320 px, valores reais em leitura, ranking individual sem #1126,
abertura/fechamento e submissão GET dos filtros, sem transbordamento horizontal.
Celular emulado no Chrome; não houve teste em aparelho físico.

Evidências locais em `artifacts/balcao-clientes-20261128/`: cópias anteriores,
`conciliacao-real.json`, capturas e `visual/verificacoes.json`, além dos logs
dos clusters PostgreSQL. As evidências e backups não fazem parte de futura
publicação automática.

Para aprovação visual:

- http://127.0.0.1:8000/relatorios/vendas/?periodo=personalizado&data_inicial=2026-10-01&data_final=2026-10-31&modo=mensal
- http://127.0.0.1:8000/relatorios/vendas/?periodo=personalizado&data_inicial=2026-11-01&data_final=2026-11-27&modo=mensal

Abrir Participação dos clientes na variação. O servidor de revisão usa banco
READ ONLY e recusa métodos HTTP de gravação. Publicação aguarda aprovação e
autorização específicas.
