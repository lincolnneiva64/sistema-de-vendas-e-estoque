# Plano Mestre — clientes e rentabilidade gerencial

As análises existentes de clientes mantêm faturamento, ordenação, recortes e
critérios financeiros. Contas de vendas atuais aparecem nas seções de atenção e
ausências, em uma consulta agregada adicional. Status aberta/parcial e saldo
positivo são elegíveis; atraso usa vencimento anterior à data atual de Belém.
Contas quitadas, balcão #1126 e coletivos confirmados não viram dívidas individuais.
Pagamentos parciais usam os abatimentos da própria conta, sem somar o histórico
geral do cliente. Locações não compõem esse resumo de contas de vendas.

O aviso de dívida vencida é vermelho; dentro do detalhe, apenas valor vencido e
dias de atraso recebem destaque. Contas no prazo são neutras. Sem saldo aberto,
o painel financeiro é omitido e o histórico comercial permanece disponível.

A leitura de frequência utiliza dias distintos e semanas/meses observados. Oito
ou mais dias de compra, com dias × 7 maior ou igual à amplitude inclusiva entre
primeira e última compra, evidenciam compras frequentes. São avaliados histórico
anterior, recorte atual e união observada. A leitura não presume comprar em toda
semana nem exige vários meses. Meses consecutivos, atividade ocasional e histórico
insuficiente têm explicações próprias; classificações anteriores e prioridades
históricas permanecem preservadas. Ausência no recorte não significa abandono.

As semanas são deduplicadas mesmo quando atravessam meses. A consulta de histórico
é em lote; não há consulta por cliente. Os textos resumem dias/semanas, meses e
movimento no período, com retorno posterior quando existente. Dívida não é
apresentada como causa automática da redução das compras.

## Locações e simulações

O faturamento existente de locações continua por data de entrega até hoje,
excluindo canceladas e contando cada contrato uma vez, sem depender de pagamentos.
A participação no total é receita de locações / total da empresa × 100, em Decimal,
com duas casas e 0,00% quando o total é zero.

A simulação reutiliza o lucro gerencial já exibido: estimativa histórica parcial,
lucro completo ou lucro da parcela com custo, nessa ordem. Nenhuma regra nova de
CMV ou custo de locação é criada.

- Resultado bruto combinado simulado = lucro gerencial exibido + receita de locações.
- Margem simulada com locações = esse mesmo resultado / faturamento total × 100.
- A base em reais é calculada uma vez e alimenta ambos os indicadores.
- Cobertura incompleta/estimada sinaliza Parcial. Lucro indisponível deixa resultado
  e margem indisponíveis; ausência de informação não vira zero.
- Com lucro conhecido e total zero, a margem retorna 0,00% sem divisão por zero.

A receita de locações entra integralmente, sem deduzir custos operacionais ou
depreciação. Os indicadores são simulações gerenciais, não lucro líquido ou margem
consolidada efetivamente apurada. Receita, CMV, lucro e margem de mercadorias
continuam com os valores e validações anteriores.

Os três cartões superiores são preservados, com o resultado simulado dentro do
cartão de faturamento total. A linha inferior mantém os quatro cartões aprovados,
incluindo Margem simulada com locações. As legendas são legíveis e responsivas;
expansíveis e avisos financeiros não alteram as colunas comerciais existentes.

As validações usam PostgreSQL descartável identificado por diretório/porta, com
migrations bloqueadas e fixtures isoladas. Conferências de registros reais são
somente leitura. Logs, imagens, referências reais e cópias de trabalho ficam fora
do pacote da aplicação.
