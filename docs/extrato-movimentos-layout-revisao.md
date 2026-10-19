# Revisão visual das movimentações do estoque

Worktree isolada: `.worktrees/extrato-movimentos-layout-20261019`.
Base: `a751a2d73ec0de7fb80a0583e00dd34dad2674ac`.
Layout final aprovado para envio ao GitHub e publicação manual. Nenhum deploy autorizado nesta etapa.

## Apresentação

Cada movimentação tem quatro colunas: data/hora, identificação da operação, entrada ou saída e saldo após. No desktop, elas ocupam a mesma faixa horizontal. O controle nativo “Detalhes e origem” permanece dentro da célula da operação e alinhado abaixo de sua identificação.

No alinhamento final aprovado, somente a célula de entrada/saída fica centralizada no desktop (`min-width: 701px`), afastando seu conteúdo do saldo. As posições da data, identificação da operação, detalhes e saldo foram mantidas, assim como o layout mobile. Captura aprovada: `artifacts/extrato-movimentos/alinhamento-final-b-1366.png`.

Compras apresentam somente a entrada em azul; vendas apresentam somente a saída em vermelho. Ajustes e registros digitais usam a comparação dos valores brutos antes/depois para identificar a direção, independentemente do título. Quando esses registros não comprovam uma unidade histórica, a tela informa essa limitação e não fabrica quantidade convertida ou saldo. Registros sem variação são identificados como tal; fontes sem evidência de direção continuam indisponíveis.

No celular, data e operação ficam acima; movimento e saldo ficam lado a lado, sem corte de dados. Foram preservadas as duas apresentações existentes: A (tabela) e B (linha do tempo, padrão).

Não há mudanças em filtros, consultas, serviços, cálculos, conversões, ordenação, auditoria ou reconstrução dos saldos. Não foram criadas migrations, permissões ou autenticações. Nenhum dado real foi acessado ou alterado.

## Arquivos

- `inventario/templates/inventario/historico.html`: união das colunas de entrada/saída e correção da abrangência da linha vazia.
- `inventario/templates/inventario/historico_quantidade.html`: apresentação da quantidade documentada ou direção bruta, sem cálculos.
- `static/inventario/historico.css`: dimensões das quatro colunas, cores e adaptação mobile.
- `static/inventario/historico_b.css`: posicionamento das quatro células na mesma linha desktop, mantendo o componente de linha do tempo.
- `inventario/tests_layout_movimentos.py`: testes visuais, capturas e comparação com a apresentação anterior.
- `inventario/tests_historico.py`: expectativa visual atualizada de cinco para quatro colunas.
- Este relatório. Capturas e logs locais em `artifacts/extrato-movimentos/`.

## Testes e capturas

Executor: `python -m inventario.validar_confiabilidade_postgresql`, com Chrome headless e PostgreSQL 17.11 descartável, protegido contra conexões fora do cluster local. Nenhuma migration foi executada. Clusters encerrados após os testes.

Foram exercitados os 69 testes existentes de `inventario.tests_historico`, `inventario.tests_extrato_consolidado`, `inventario.tests_saldos_historicos` e `inventario.tests_periodos_historico`. A expectativa de cinco colunas do teste visual foi atualizada para quatro e somente esse teste foi reexecutado, com aprovação. Os 68 demais já haviam passado.

Os dois novos testes de `inventario.tests_layout_movimentos` validam as apresentações A/B em desktop de 1366 px e mobile de 390 px. A regressão visual existente cobre também 320 px. Considerando o último resultado de cada caso, são **71 testes distintos aprovados**.

Após o último ajuste exclusivamente em CSS, foi reexecutada somente a verificação desktop de `inventario.tests_layout_movimentos`, com aprovação e novas capturas A/B. Nenhuma bateria completa foi repetida para o commit. Capturas, logs e o utilitário local de geração de imagens permanecem fora do commit.

Cobertura específica: compra +4 UN, duas vendas −1 UN, saldo inicial zero, saldos 4/3/2 UN, ajustes de entrada/saída/sem variação, registros digitais cujo título não corresponde à direção bruta, colunas uniformes no desktop, detalhes alinhados e expansíveis, links de compra/venda/registro manual e ausência de escrita nos registros fictícios durante a navegação.

Nas comparações, o HTML de cada identificação de operação, detalhe e saldo, bem como o horário de cada movimento, permaneceu idêntico ao anterior. Os testes também comparam os registros das tabelas de inventário e dos documentos comerciais fictícios antes/depois da navegação.

Galeria local: `artifacts/extrato-movimentos/revisao.html`. Há oito capturas (antes/depois, A/B, desktop/mobile), geradas com os mesmos exemplos fictícios e inspecionadas visualmente.

Para gerar a base comparativa, antes de alterar os templates/estilos: definir `EXTRATO_LAYOUT_CAPTURA_ANTES=1` e executar somente `inventario.tests_layout_movimentos`. Na versão nova, definir `EXTRATO_LAYOUT_COMPARAR_ANTES=1` para exigir a comparação com esses arquivos. Sem essas opções, os testes de layout funcionam independentemente dos artefatos locais anteriores.

As advertências sobre namespace `estoque` duplicado e diretório `staticfiles` ausente no ambiente de teste já pertencem à base.
