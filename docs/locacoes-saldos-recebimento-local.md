# Consulta de Locações — saldos e recebimento local

Somente local, sem acesso ao Neon, alteração de dados reais, commit, push ou deploy.
Não há alteração de campos do banco nem migration necessária.

Atualização posterior: **Responsável obrigatório** nos novos recebimentos diretos
e no sinal da reserva. As validações, a preservação do histórico e a preparação da
prévia estão em [Responsável no recebimento](locacoes-responsavel-recebimento.md).
Os campos do banco não mudaram; a atualização acrescenta validação ao modelo/serviço.

## Critérios

O resumo superior preserva `resumo_financeiro_locacoes`: contratos e contratado
por data de cadastro; recebido por data informada do pagamento, inclusive contratos
antigos. **Saldo a receber no período** mantém o saldo atual dos contratos cadastrados
no intervalo; a legenda esclarece que não é previsão de vencimentos.
O padrão é **Este mês**, do dia 1 até hoje, pelo calendário de Belém. Há outros
períodos, intervalos livres e **Todo o histórico**, independentes dos filtros operacionais.

Os indicadores clicáveis consideram sempre todo o histórico:

| Ordem | Título | Critério com saldo positivo |
| --- | --- | --- |
| 1 | Entregues a receber | `entregue` ou `pendente_devolucao`, incluindo recolhimento parcial |
| 2 | Em atraso | `devolvida` ou `devolvida_com_avaria`; legenda “(Recolhidas, mas não quitadas)” |
| 3 | Reservas futuras | `reservada` ou `saiu_para_entrega`, ainda sem confirmação de entrega |
| 4 | Total em aberto | Soma exclusiva das três categorias |

Saldo zero é quitado e não entra em aberto. Canceladas não compõem dívida cobrável.
Uma reserva com vencimento antigo não vira atraso nessa classificação. Não houve
alteração de `saldo_vencido_em`, regras de cobrança, estados ou encerramento.
Os valores vêm de `total`, `total_pago` e `saldo_devedor`, mantidos pelo serviço
financeiro existente. Não há cálculo paralelo nem soma de movimentos aos pagamentos.
Cada contrato aparece uma vez; clientes com várias locações têm linhas distintas.

## Interface e desempenho

Os quatro cards mantêm títulos, valores e quantidades alinhados na mesma linha,
inclusive em duas colunas no celular. **Receber / Quitar** usa verde `#15803d`, texto
branco, altura mínima de 44 px e foco visível.

A abertura adiciona uma agregação para os indicadores históricos. A lista só é
consultada após um clique, pagina 20 locações e mostra cliente, número, contratado,
pagamentos, saldo, datas, situação e acesso direto à locação. São três consultas
para carregar a lista, sem crescimento por cliente/parcela: agregação, contagem e
página com cliente relacionado. O resumo temporal conserva suas duas agregações.

## Recebimento e histórico

O modal reutiliza `PagamentoLocacaoForm`, a rota `registrar_pagamento` e
`Locacao.registrar_pagamento`. O formulário também é compartilhado com a tela
original. Formas, destinos financeiros, responsável, observação, validações,
auditoria, CSRF e política de acesso permanecem os existentes.

O valor vem com o saldo preenchido, com foco e seleção. Data/hora vem do servidor
em Belém ao abrir o modal e pode ser alterada. Somente o modal usa explicitamente
`America/Belem` para apresentar e interpretar esse campo, mesmo com outro fuso no
processo/navegador. `PagamentoLocacao.criado_em` e o evento preservam o horário real
do registro, separado do horário informado. Erros mantêm os dados e a identidade.

O token assinado existente vincula usuário, locação e UUID único de recibo. O
serviço bloqueia a locação antes de buscar um pagamento já registrado com esse UUID.
Reenvio idêntico retorna o mesmo pagamento; alteração dos dados é recusada. O botão
fica bloqueado durante a confirmação. Não há repetição automática de POST. Após
falha de resposta, o mesmo formulário pode ser repetido com a identidade original.

Após confirmação no servidor, os indicadores do período e os saldos/lista atuais
são atualizados por fragmentos, sem recarregar a tabela operacional. Se essa leitura
falhar, a mensagem informa que o recebimento foi confirmado e oferece **Atualizar
saldos**, que apenas lê dados. Há acesso ao recibo em outra aba.

**Histórico de pagamentos** abre sob demanda no modal, com 10 registros por página.
Reutiliza `includes/horarios_pagamento.html`: horário informado, registro real,
confirmações do recibo, valor, forma, responsável, observações e acesso ao recibo.
Consultar o histórico não emite identidade de recebimento nem altera registros.

## Pendência: correção de pagamentos diretos

O histórico é **somente consulta**. Não existe serviço auditado de correção de
`PagamentoLocacao` direto. Esses pagamentos, inclusive os produzidos pelo modal,
não possuem `operacao_recebimento_cliente`.

Foram investigados `estoque.views.receber_cliente_corrigir_recebimento` e
`_desfazer_operacao_recebimento_cliente`. O serviço seguro existente atende operações
**unificadas**, vinculadas à operação original: desfaz e reaplica atomicamente,
correlaciona original/correção, exige motivo e respeita bloqueios de rota/data
conferidos e créditos utilizados. Pode abranger vendas e várias locações; não
equivale a editar um pagamento direto individual.

Não foram criados vínculos artificiais, sobrescritas, exclusões ou estorno paralelo.
A correção direta no modal fica pendente conforme o limite aprovado. Nenhuma
proteção de caixa, permissão ou política de enquadramento foi alterada. A interface
informa que a correção ainda não está disponível no modal.

Os testes do serviço unificado existente confirmam correção autorizada e auditada,
recusa de repetição, bloqueio nas rotas de origem/destino conferidas e recusa de
usuário não autorizado. Após uma correção unificada existente, consultar as
categorias lê o saldo atualizado. Isso não representa um editor novo no modal.

## Validação

**60 testes aprovados em PostgreSQL descartável, em 39,431 segundos, sem skips.**
Cluster exclusivo encerrado. Cobertos:

- sinal parcial, entrega com saldo, recolhimento completo com/sem avaria, parcial,
  quitação, cancelamento e contrato antigo pago no mês atual;
- independência do período, soma exata das categorias e ausência de duplicação;
- antecipação, parcial, quitação, reenvio, concorrência, assinatura, vínculo ao
  usuário/locação, CSRF, excesso de pagamento e histórico somente leitura;
- Belém com dia UTC diferente e processo em outro fuso; alteração manual separada
  do horário real do servidor e reenvio idêntico com horário explícito;
- correção unificada existente, auditoria, autorização e restrições de caixa;
- regressão de períodos, intervalo livre, filtros independentes e gravação segura;
- Chrome em 1366, 390 e 320 px: ordem/rótulos, alinhamento, ausência de rolagem
  horizontal, botão verde/branco, foco, histórico e atualização após parcial/quitação.

Comando reproduzível no repositório:

```powershell
& .\venv\Scripts\python.exe validar_compra_timeout_pg.py locacoes.tests_saldos_consulta locacoes.tests_resumo_financeiro locacoes.tests_gravacao_segura locacoes.tests_consulta_independente locacoes.tests_periodos_consulta locacoes.tests_intervalo_personalizado
```

O launcher cria PostgreSQL novo em loopback/porta exclusiva, confere diretório/porta
e recusa conexões fora do cluster. Migrations de preparação rodam exclusivamente
nesse banco de teste; nenhuma migration nova ou execução no Neon.

Capturas fictícias ficam em `tmp_whatsapp_visual_resumo_financeiro/`, pasta já
ignorada pelo Git. Capturas anteriores foram preservadas. Houve inspeção visual
do desktop, celular e modal com histórico. O aviso de namespace `estoque` duplicado
é preexistente.

## Abrir a prévia no notebook

No PowerShell, dentro deste repositório:

```powershell
& .\venv\Scripts\python.exe -m locacoes.previa_financeira
```

Abra **http://127.0.0.1:8766/entrar/**. Usuário: **previa**. A senha é gerada e
exibida no terminal. A prévia usa configurações independentes de teste, sem importar
o `.env` operacional, e cria SQLite em diretório temporário. Não executa migrations,
não se conecta ao Neon e escuta somente no próprio notebook. Cookies têm nomes
próprios para não substituir os de outro sistema local.

Há reserva com sinal, entregue, parcial, recolhida com dívida/avaria, quitada e
cancelada. A recolhida com dívida tem cadastro antigo e pagamento no mês atual.
Todos os recebimentos são fictícios. **Ctrl+C** encerra e descarta o banco; executar
novamente reinicia os exemplos. Se a porta estiver ocupada, use `--port 8767`.

Confira os cards, selecione uma categoria, abra **Receber / Quitar**, veja o histórico
e registre um parcial e uma quitação. Mude o período e volte a **Todo o histórico**.
O recebimento não deve tirar você da Consulta de Locações.

Limitações: SQLite da prévia serve para inspeção, não prova concorrência — essa
garantia foi testada em PostgreSQL. Celular foi simulado no Chrome, sem teste em
aparelho físico. A correção direta continua pendente conforme acima.

## Arquivos desta melhoria

- `locacoes/views.py`: período, fragmentos, modal, fuso e histórico.
- `locacoes/saldos_consulta.py`: agrupamento/apresentação dos saldos existentes.
- `locacoes/templates/locacoes/lista.html` e `pagamento.html`: integração.
- `locacoes/templates/locacoes/includes/form_pagamento.html`, `resumo_valores.html`,
  `saldos_consulta.html` e `historico_pagamentos_resumo.html`: componentes reutilizados.
- `static/locacoes/resumo-saldos.css` e `resumo-saldos.js`: layout e interação.
- `locacoes/tests_saldos_consulta.py`: cobertura nova.
- `locacoes/tests_periodos_consulta.py`: opção Todo o histórico e capturas locais;
  `tests_resumo_financeiro.py` e `tests_intervalo_personalizado.py`: destino ignorado
  para novas capturas, preservando as anteriores.
- `locacoes/previa_financeira.py` e `templates/locacoes/previa_login.html`: prévia isolada.
- Este relatório e `docs/locacoes-resumo-financeiro.md`: documentação e pendência.

Nesta etapa inicial não houve mudança de modelos, migrations, estoque ou permissões.
A validação posterior de responsável está documentada no relatório vinculado acima;
não altera campos, cálculos financeiros ou permissões de funcionários reais.
