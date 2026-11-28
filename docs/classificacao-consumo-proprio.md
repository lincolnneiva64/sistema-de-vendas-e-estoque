# Condição de pagamento e Consumo próprio

Correção funcional aprovada para publicação em 28/11/2026, sobre
`87b622a99276ee2e25d88f05131540e89b80d3a2`.

## Causa e regras preservadas

A regressão passou a inicializar o formulário como À vista e a reaplicar essa
condição em campos vazios e clientes comuns. A lista central também havia trocado
Roseli #10 por Roselene #1124, cadastros distintos conferidos somente em leitura.

A correção reutiliza a lista central compartilhada pelo contexto da tela e pelo
gravador, sem comparar nomes:

- Sem cliente: valor vazio, com a opção Selecione.
- Balcão #1126: À vista; nome e cadastro permanecem inalterados.
- Clientes comuns: A prazo.
- Lincoln #6, Roseli #10, Lorena #1078 e Camila #1086: Consumo próprio.
- Roselene #1124: A prazo, sem a opção Consumo próprio.

O padrão é aplicado na escolha efetiva ou troca de cliente. Atualizações de
resumo e respostas atrasadas não substituem a escolha manual. Edições mantêm a
condição persistida; consumo histórico continua bloqueado para conversão.
O backend conserva a validação da mesma lista central antes de processar itens.
Não há correção retroativa de vendas, recebimentos, contas, saldos ou estoque.

## Layout e validação

O CSS e a estrutura visual publicada foram preservados. O template altera apenas
a opção vazia e quatro funções relacionadas à condição de pagamento.

A comparação com medidas reais do Render foi reproduzida no Chrome com viewport
1536 × 695, DPR 1,25 e escala visual 1. Em estado equivalente de resumo do cliente,
Render e integração apresentaram formulário de 282,80 px e área útil dos produtos
de 235,60 px; campos de 36 px e botões principais de 32 px. As variações do resumo
do cliente já existem na base publicada e não são ajustes visuais desta correção.

Os testes pertinentes são executados pelo runner PostgreSQL isolado existente:

    python -m inventario.validar_confiabilidade_postgresql estoque.tests_consumo_restrito estoque.tests_condicao_pagamento

Esses testes cobrem seleção inicial, padrões por ID, troca de cliente, escolha
manual, atualização secundária, restrição manipulada no backend e preservação
de condições e recebimentos nas edições. O runner recusa conexões fora do cluster
descartável; não executa migrations para esta suíte.

Este pacote não contém regularização de vendas, renomeação do #1126, mudanças de
inventário ou locações, alterações de custos nem configuração de Render ou Neon.
