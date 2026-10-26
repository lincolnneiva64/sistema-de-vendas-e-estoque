# Resumo financeiro na Consulta de Locações

Resumo integrado à página existente, sem alterar rotinas de gravação financeira.

## Regras

Posição: cabeçalho → resumo financeiro → filtros operacionais → abas e Histórico
completo → tabela. Os blocos reaproveitam a
identidade visual da consulta: quatro colunas no desktop, duas no celular e uma
em telas abaixo de 360 px. Não há nova página. “Aplicar período” é a única ação
adicionada, exclusiva do intervalo personalizado. Dois formulários
GET na mesma página mantêm independentes os parâmetros financeiros e operacionais.

O período financeiro tem campos próprios (`resumo_inicio`, `resumo_fim`), separados
das datas de entrega/recolhimento da tabela (`data_inicio`, `data_fim`):

- quantidade e contratado: cadastro (`Locacao.criado_em`) no intervalo;
- recebido: `PagamentoLocacao.data_hora` no intervalo, inclusive de contratos anteriores;
- saldo: saldo atual dos contratos cadastrados no intervalo.

Os limites incluem o dia inicial e o dia final completos no fuso ativo do Django.
Sem datas, não há limite temporal; uma data isolada limita somente sua extremidade.
Datas inválidas deixam o resumo indisponível, sem valores que aparentem ser válidos.

Cliente, Status, Financeiro, Mostrar, abas e datas operacionais afetam somente a
tabela. O resumo depende somente do próprio período e considera todos os contratos
e pagamentos elegíveis. Marco operacional e paginação não entram no resumo.
A tabela mantém sua interpretação anterior das datas de entrega/recolhimento.

Canceladas ficam fora dos indicadores principais e aparecem em linha separada,
com quantidade e contratado por cadastro, e pagamentos por data do pagamento.
Selecionar Status Cancelada na tabela não muda o resumo; o grupo cancelado continua
separado, sem transformar seu saldo cadastral em dívida normalmente cobrável.

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

Um único seletor na faixa do título do resumo oferece: Hoje, Ontem, Esta semana,
Fim de semana, Este mês, Mês passado e Intervalo à escolha. Não existem atalhos
de 7/30 dias nem de 3/6 meses. As datas financeiras aparecem nessa faixa somente
em Intervalo à escolha, claramente identificadas, sem compartilhar nomes ou valores
com as datas operacionais.

O calendário é calculado no servidor em `America/Belem`, independentemente do
fuso do navegador ou de outro fuso ativo no processo. Hoje/ontem são dias civis;
Esta semana vai da segunda-feira até hoje; Fim de semana usa o último sábado e
domingo, limitado a hoje se estiver em andamento; Este mês vai do dia 1 até hoje;
Mês passado usa o mês anterior inteiro, incluindo fevereiro bissexto e virada de ano.

O atalho preenche as datas e reutiliza o envio automático com debounce existente.
Intervalo à escolha não limpa nem envia as datas ao ser selecionado. A edição
manual troca a indicação para Intervalo à escolha e aguarda “Aplicar período”,
sem interromper a digitação das duas datas e sem limite arbitrário de duração.
O botão fica junto aos campos, na faixa do resumo, somente na opção personalizada.
Selecionar um período predefinido oculta os campos e aplica automaticamente.
Valores explícitos de datas sempre
prevalecem: o nome do atalho não pode sobrescrever um intervalo manual.
Cada formulário preserva os parâmetros do outro em campos ocultos. A tabela mantém
o período financeiro aplicado no servidor, sem incorporar rascunhos de datas ainda
não aplicados. O formulário financeiro preserva os filtros operacionais ao enviar.
Mudar o período preserva inclusive a página da tabela. Filtrar a tabela reinicia
sua paginação como antes, mantendo o período financeiro. Abas, Histórico e links
de paginação preservam o período; Limpar filtros limpa somente o operacional.
URLs anteriores com `data_inicio`/`data_fim` continuam filtrando a tabela e não
definem implicitamente o período financeiro. Sem parâmetros `resumo_*`, o resumo
considera todo o histórico elegível.

## Limites históricos

O contratado e os status refletem os dados atuais, não uma posição congelada do
passado. Recebido pode superar o contratado no intervalo, por incluir contratos
anteriores, e pode incluir cartões ainda não liquidados no banco. Saldo atual
não é previsão de vencimento/entrada dentro do intervalo. Os desfazimentos atuais
podem alterar retrospectivamente os pagamentos atribuídos ao período original.
Os indicadores não representam lucro nem faturamento fiscal.

## Validação executada

**128 testes aprovados, sem skips:** a regressão anterior adaptada à independência,
cinco cenários em `locacoes.tests_consulta_independente` e três novos testes em
`locacoes.tests_intervalo_personalizado`.

Cobertos: cadastro/pagamento no mesmo mês; contrato antigo recebido no mês;
parcelas múltiplas sem multiplicar contratado; canceladas com/sem pagamento;
recebimento e desfazimento pelas funções reais do recebimento unificado; filtros
combinados na função de agregação; dias inicial/final inclusivos e fronteira UTC/local; limites abertos;
datas inválidas; paginação; abas/Mostrar/Histórico sem afetar resumo; duas consultas
de agregação; ausência de escrita durante consulta. Na página, filtros operacionais
não alteram o resumo. Datas inválidas em um conjunto não invalidam o outro. Mudança
do período financeiro preserva linhas, ordenação e página operacional; links e
limpeza mantêm os parâmetros do conjunto independente.

Atalhos: viradas de mês/ano, fevereiro comum e bissexto, segunda/domingo, fim de
semana passado e em andamento, UTC em dia diferente de Belém, intervalo livre
maior que um ano, campos manuais prevalecendo e preservação de filtros/links.
No navegador, cada atalho preenche e aplica uma única consulta; a seleção manual
não apaga datas e a edição dos dois campos não envia até “Aplicar período”.
Validados janeiro a junho, ano inteiro, intervalo entre anos e um único dia;
preservação das datas no retorno, independência operacional e datas inválidas.
Data inicial posterior à final mostra mensagem específica e nenhum indicador.
Prévias específicas da opção aberta ficam em subpastas `personalizado-*`.

Após o ajuste final de CSS, os três testes de intervalo personalizado foram
reexecutados e aprovados. Em 1366 e 1051 px, seletor, datas e botão têm altura
de 38 px, topos alinhados com tolerância de 1 px e espaços de 8 px. A regra é
restrita ao desktop (acima de 820 px). As capturas mobile de 390 e 320 px foram
comparadas por SHA-256 e permaneceram idênticas às aprovadas. A captura desktop
manteve as mesmas dimensões, sem aumentar a faixa superior. São 128 testes na
rodada funcional e a reexecução de três deles após o refinamento visual.

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
