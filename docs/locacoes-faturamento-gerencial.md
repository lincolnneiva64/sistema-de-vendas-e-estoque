# Faturamento gerencial de mercadorias e locações — integração local

Implementado em 28/11/2026, após confirmação de que a competência gerencial das
locações é a data de entrega do contrato. Não houve publicação.

## Regra aprovada e reutilização

- Mercadorias: `estoque.services.relatorios.resumo_vendas`, com todas as regras
  comerciais e financeiras já aprovadas, sem alteração de seus cálculos.
- Competência gerencial atualizada em 28/11/2026: contratos não cancelados com
  `data_entrega` no período e menor ou igual à data local de hoje. A confirmação
  operacional não é requisito de faturamento. A consulta operacional permanece
  intacta e exclusiva dos seus fluxos de entrega/recolhimento.
- Competência de locações: `Locacao.data_entrega` dentro do intervalo do filtro,
  incluindo os dois dias extremos. Não usar cadastro, evento, recebimento ou data
  em que a confirmação foi digitada.
- Valor: `Locacao.total`, já calculado e armazenado pelo módulo de Locações.
  Somar uma vez por contrato, sem join de itens, parcelas ou movimentos financeiros.
  Não recalcular diárias/preços históricos para o relatório.

O cálculo único é `locacoes.consulta.faturamento_locacoes(inicio, fim)`, usado
pelo adaptador `estoque.services.faturamento_negocio.consolidar_faturamento` e
por `locacoes.consulta.resumo_financeiro_locacoes`. Faz uma única agregação
de quantidade e total por entrega, sem recalcular preço, pagamento ou operação.
Na Consulta, os quatro indicadores anteriores por cadastro/pagamento são
preservados com suas chaves e critérios originais; o resultado por entrega é
adicionado na chave `entrega`. São três agregações independentes no resumo,
em vez das duas anteriores, sem dependência de paginação ou filtros da tabela.
O fragmento financeiro reutiliza o mesmo resumo e template. Limites abertos
permanecem permitidos na consulta histórica, com corte de entregas até hoje.
A consulta, medida nos recortes reais, levou aproximadamente 145 ms por período,
incluindo o acesso ao banco remoto; é medição pontual, não garantia de latência.

### Estados

Todos os estados não cancelados entram quando a data de entrega já chegou,
inclusive `reservada` e `saiu_para_entrega`. Datas futuras ficam fora mesmo
que o estado operacional seja de entrega concluída. A data de hoje é inclusiva;
o horário previsto não modifica a competência diária. Nenhum contrato, tarefa
ou checklist é atualizado pela apuração. Uma devolução posterior mantém a
receita no mês da entrega.

O valor integral é considerado mesmo com saldo pendente. Sinal e parcelas não
são somados novamente nem usados como denominador. Nenhuma regra operacional
ou tela de Locações foi modificada.

## Conferência histórica do critério anterior (confirmação operacional)

Executada em transação READ ONLY e REPEATABLE READ, sem repetir a auditoria geral.

- Outubro: 27 contratos, sendo 26 devolvidos (R$ 2.246,18) e um entregue
  (R$ 50,00). O entregue #95 tem evento de confirmação de entrega. Total:
  **R$ 2.296,18**.
- Novembro até 27: 18 contratos devolvidos, total **R$ 1.354,00**.
- Não foram encontrados estados reservada/saída pendente com evidência de
  conclusão completa conflitante nos recortes conferidos.
- Canceladas por data de entrega ficaram fora: #87 e #92 em outubro,
  R$ 190,00; #70 e #71 em novembro, R$ 200,00.
- Em novembro há quatro contratos cadastrados em outro mês: #77 (cadastro em
  setembro, entrega em 08/11), #105 (cadastro em outubro, entrega em 20/11),
  #111 (cadastro em outubro, entrega em 08/11) e #112 (cadastro em outubro,
  entrega em 01/11). Entram em novembro, independentemente do cadastro.
- Há três contratos com múltiplos pagamentos em cada recorte: #81/#103/#108 em
  outubro; #105/#124/#126 em novembro. Cada um entra uma única vez pelo contrato.

Datas e estados refletem os registros atuais, conforme o módulo operacional.
O relatório não cria um fechamento histórico imutável nem corrige registros.

## Totais conciliados

| Período | Mercadorias | Locações realizadas pela entrega | Total da Santa Bárbara |
| --- | ---: | ---: | ---: |
| 01/10/2026 a 31/10/2026 | R$ 71.692,85 | R$ 2.296,18 | **R$ 73.989,03** |
| 01/11/2026 a 27/11/2026 | R$ 60.660,39 | R$ 1.354,00 | **R$ 62.014,39** |

As somas conferiram com a seleção existente de entregas realizadas e o Resumo
de Vendas. O indicador de Locações **Total contratado no período** continua
usando cadastro e pode diferir destes valores; ele não foi alterado ou renomeado.
Essa diferença de escopo já foi documentada na investigação anterior.

## Apresentação e preservação

Uma faixa com Mercadorias · Locações · Faturamento total foi acrescentada à página
existente de Resumo de Vendas. Ela ocupa 42 px no desktop e 102 px nos celulares
emulados de 390/320 px, sem criar cartões grandes, página nova ou rolagem lateral.
Uma legenda curta identifica a competência das locações e o escopo das análises.

Os nove indicadores de mercadorias foram mantidos. Os títulos de faturamento,
CMV, lucro e margem identificam explicitamente Mercadorias. Não há CMV, lucro
ou margem global inventados para o negócio sem custos de locação.

Balcão × Clientes cadastrados continua usando apenas vendas de mercadorias,
inclusive seu total, participação, rankings e frequência. O #1126 e seu nome não
foram alterados. Seus faturamentos permanecem R$ 7.216,43 e R$ 7.127,25 nos dois
recortes. Pagamento e Consumo próprio, estoque, custos, filtros, gráficos e
navegação existentes foram preservados. A tela operacional de Vendas não mudou.

## Arquivos deste pacote local

- `estoque/services/faturamento_negocio.py` — adaptador de leitura e soma das atividades.
- `estoque/views_relatorios.py` — contexto adicional na página existente.
- `estoque/templates/estoque/relatorios/vendas.html` — faixa e identificação do escopo.
- `static/estoque/relatorios.css` — estilos exclusivos da faixa nova.
- `estoque/tests_faturamento_negocio.py` — nove testes dirigidos.
- Este documento.

Backups, conferências por contrato, resultados, logs e capturas estão em
`artifacts/integracao-locacoes-entrega-20261128/`, fora de publicação automática.
Os demais arquivos locais não foram transportados, substituídos ou descartados.

## Testes e revisão local

80 testes aprovados no PostgreSQL isolado, incluindo o adaptador novo, a análise
do balcão, inteligência de clientes, custos/classes/produtos e navegação visual.
Cluster encerrado e nenhuma migration executada.

    python -m inventario.validar_confiabilidade_postgresql estoque.tests_faturamento_negocio estoque.tests_balcao_clientes estoque.tests_inteligencia_clientes estoque.tests_analise_classes_produtos

Cobertos: todos os estados; saída sem confirmação; reserva futura; cancelamento;
cadastro, evento e pagamento em meses diferentes; devolução posterior; parcelas
e saldo pendente; limites inclusivos, período personalizado e vazio; consulta
SELECT única, sem pagamentos ou escrita; CMV/lucro e balcão restritos a mercadorias.

No Django completo, porta 8000, foram conferidos os dois períodos em 1536/390/320
px, com os totais reais, nove indicadores, balcão preservado e ausência de
transbordamento horizontal. O formulário GET também foi submetido para o
intervalo personalizado 05/10–15/10, conferindo a soma das duas atividades.
Celular emulado no Chrome, não aparelho físico.

Para aprovação visual:

- http://127.0.0.1:8000/relatorios/vendas/?periodo=personalizado&data_inicial=2026-10-01&data_final=2026-10-31&modo=mensal
- http://127.0.0.1:8000/relatorios/vendas/?periodo=personalizado&data_inicial=2026-11-01&data_final=2026-11-27&modo=mensal

O servidor de revisão permanece READ ONLY e bloqueia métodos HTTP de gravação.
Nenhuma alteração de dados reais, pagamentos, locações, estoque, custos, Render
ou Neon. Sem migrations, branches, worktrees, commit, push ou deploy.
