# Resumo financeiro na Consulta de Locações

Resumo integrado à página existente, sem alterar rotinas de gravação financeira.

## Regras

Posição: filtros → resumo financeiro → abas → tabela. Os blocos reaproveitam a
identidade visual da consulta: quatro colunas no desktop, duas no celular e uma
em telas abaixo de 360 px. Não há novas ações, formulários ou botões.

O intervalo existente é reaproveitado com significados explícitos:

- quantidade e contratado: cadastro (`Locacao.criado_em`) no intervalo;
- recebido: `PagamentoLocacao.data_hora` no intervalo, inclusive de contratos anteriores;
- saldo: saldo atual dos contratos cadastrados no intervalo.

Os limites incluem o dia inicial e o dia final completos no fuso ativo do Django.
Sem datas, não há limite temporal; uma data isolada limita somente sua extremidade.
Datas inválidas deixam o resumo indisponível, sem valores que aparentem ser válidos.

Cliente, Status e Financeiro restringem ambos os conjuntos. Status e financeiro
são atuais; a restrição é identificada no resumo. Abas, Mostrar, marco operacional,
situação de entrega/recolhimento e paginação não entram no cálculo financeiro.
A tabela mantém sua interpretação anterior das datas de entrega/recolhimento.

Canceladas ficam fora dos indicadores principais e aparecem em linha separada,
com quantidade e contratado por cadastro, e pagamentos por data do pagamento.
Status Cancelada apresenta somente esse grupo, com saldo “Não se aplica” e aviso
de que o saldo cadastral não é apresentado como dívida cobrável.

## Fonte e desempenho

Há exatamente duas consultas de agregação, independentemente da quantidade de
locações e parcelas. A primeira agrega contratos com filtros condicionais para
separar canceladas; a segunda agrega pagamentos, selecionando as locações elegíveis
por subconsulta sem aplicar o intervalo de cadastro. Nenhuma carrega relações por
registro. Não há junção de parcelas na soma de contratos, nem soma de movimentos
financeiros aos pagamentos. Os totais consideram todos os elegíveis, não a página.

O desfazimento unificado já remove a parcela e recalcula o saldo. O resumo soma
somente os pagamentos remanescentes: não desconta novamente o movimento de estorno.
Nenhuma rotina de pagamento, contrato, estoque ou movimentação foi modificada.

## Atalhos de período

Um único seletor junto aos campos existentes oferece: Hoje, Ontem, Esta semana,
Fim de semana, Este mês, Mês passado e Intervalo à escolha. Não existem atalhos
de 7/30 dias nem de 3/6 meses, e nenhum campo de data foi duplicado.

O calendário é calculado no servidor em `America/Belem`, independentemente do
fuso do navegador ou de outro fuso ativo no processo. Hoje/ontem são dias civis;
Esta semana vai da segunda-feira até hoje; Fim de semana usa o último sábado e
domingo, limitado a hoje se estiver em andamento; Este mês vai do dia 1 até hoje;
Mês passado usa o mês anterior inteiro, incluindo fevereiro bissexto e virada de ano.

O atalho preenche as datas e reutiliza o envio automático com debounce existente.
Intervalo à escolha não limpa nem envia as datas ao ser selecionado. A edição
manual troca a indicação para Intervalo à escolha e mantém o envio automático
existente, sem limite arbitrário de duração. Valores explícitos de datas sempre
prevalecem: o nome do atalho não pode sobrescrever um intervalo manual.
Cliente, Status, Financeiro e aba são preservados. A identificação do período
acompanha links de abas, paginação e Histórico. Limpar filtros mantém seu fluxo.

## Limites históricos

O contratado e os status refletem os dados atuais, não uma posição congelada do
passado. Recebido pode superar o contratado no intervalo, por incluir contratos
anteriores, e pode incluir cartões ainda não liquidados no banco. Saldo atual
não é previsão de vencimento/entrada dentro do intervalo. Os desfazimentos atuais
podem alterar retrospectivamente os pagamentos atribuídos ao período original.
Os indicadores não representam lucro nem faturamento fiscal.

## Validação executada

**120 testes aprovados, sem skips:** os 109 anteriores, 10 de períodos e a regressão
de frontend do debounce/limpeza.

Cobertos: cadastro/pagamento no mesmo mês; contrato antigo recebido no mês;
parcelas múltiplas sem multiplicar contratado; canceladas com/sem pagamento;
recebimento e desfazimento pelas funções reais do recebimento unificado; filtros
combinados; dias inicial/final inclusivos e fronteira UTC/local; limites abertos;
datas inválidas; paginação; abas/Mostrar/Histórico sem afetar resumo; duas consultas
de agregação; ausência de escrita durante consulta.

Atalhos: viradas de mês/ano, fevereiro comum e bissexto, segunda/domingo, fim de
semana passado e em andamento, UTC em dia diferente de Belém, intervalo livre
maior que um ano, campos manuais prevalecendo e preservação de filtros/links.
No navegador, cada atalho preenche e aplica uma única consulta; a seleção manual
não apaga datas e a edição dos dois campos usa um único envio com debounce.

Navegador: 1366, 1051, 390 e 320 px. Ordem dos painéis, número de colunas e ausência
de rolagem horizontal verificados. Capturas com valores e clientes fictícios em
`amostras/previa-resumo-financeiro/`. HTML real renderizado pela view, com CSS local;
dependência do CDN e scripts foram removidos somente da prévia de layout do resumo.
O teste dos períodos executa o script real dos filtros e captura os parâmetros do
envio, sem navegação externa. As imagens de cada rodada ficam em subpastas novas,
preservando prévias anteriores e evitando conflitos com arquivos abertos.

Runner reutilizado: `validar_compra_timeout_pg.py`. PostgreSQL criado do zero em
loopback/porta exclusiva, com verificação de diretório e bloqueio de conexões fora
do cluster. `TEST.MIGRATE=False` definido no processo antes do setup; esquema
descartável criado dos models, sem executar migrations. Fixture sintética de faixa
de preço para os testes legados de pagamentos, sem dados operacionais. Cluster
encerrado ao final. O aviso preexistente de namespace estoque duplicado permanece.

A validação não acessou produção e não executou migrations ou deploy.
