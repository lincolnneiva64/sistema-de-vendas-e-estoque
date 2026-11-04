# Responsável obrigatório no recebimento de locações

Decisão operacional aplicada somente no código local. Nenhum acesso ao Neon,
alteração em dados reais, commit, push, deploy ou migration de produção.

## Regra e validação

O formulário de pagamento, compartilhado pela tela normal e pelo modal da Consulta,
agora exige selecionar um funcionário cadastrado, ativo e habilitado para operar
o caixa, usando o filtro existente `Funcionario.operadores_do_caixa()`.
Não há seleção inicial nem atribuição automática de funcionário.

Sem seleção, a mensagem é exatamente:

> O campo Responsável é obrigatório. Selecione quem recebeu o pagamento.

O navegador usa essa mensagem de validação e o servidor a valida novamente. Alterar
o HTML, omitir o campo ou enviar ID de funcionário inativo, sem caixa, inexistente
ou um nome em vez do ID não permite registrar o pagamento. O modal mantém o valor,
a identidade do envio e os demais campos; o primeiro campo com erro recebe foco,
com a mensagem visível no celular.

O serviço `Locacao.registrar_pagamento` recusa identificação vazia para um novo
pagamento antes de recalcular ou gravar valores. `PagamentoLocacao.clean` também
recusa a criação de novos registros sem identificação. Nos formulários, a escolha
por ID é validada contra os funcionários habilitados; o nome é armazenado como no
fluxo anterior, no pagamento, evento e operador do movimento financeiro.

## Outros pontos investigados

- **Sinal na criação da reserva:** o fluxo passava responsável vazio. Agora há
  seleção de responsável pelo recebimento, obrigatória somente se houver sinal.
  A validação ocorre antes de criar a reserva. Reserva sem sinal continua podendo
  ser criada sem responsável de recebimento. As transações e cálculos existentes
  foram mantidos.
- **Recebimento unificado:** já identifica o usuário autenticado e o fornece ao
  serviço. Essa autoria e suas regras existentes foram preservadas, sem introduzir
  uma escolha automática de funcionário. A chamada interna sem qualquer identificação
  passa a ser recusada. Correção e restrições de caixa do fluxo unificado continuam
  iguais; a regressão existente cobre esse caminho.
- **Pagamentos históricos:** os campos do banco continuam permitindo os registros
  antigos vazios. A validação do modelo exige identificação somente na criação.
  Não há preenchimento retroativo. O serviço procura primeiro um UUID já registrado:
  um reenvio idêntico de registro antigo retorna o original, sem criar ou alterar
  pagamentos, movimentos, eventos ou saldos.

Não há migration. Valores, formas de pagamento, destinos financeiros, estoque,
estados operacionais, CSRF e permissões de acesso não foram modificados. Permanecem
o bloqueio transacional da locação e a identidade única do envio. Corrigir pagamentos
diretos continua pendente conforme o relatório do resumo; este ajuste não é um editor.

## Prévia da porta 8766

Apenas na carga fictícia, `Operador fictício` agora é criado com
`pode_operar_caixa=True`. Isso não altera nenhum funcionário real. O seletor começa
em **Selecione o responsável**, mesmo havendo somente um funcionário elegível.
A prévia foi reiniciada com banco temporário e exemplos fictícios novos.

Abra `http://127.0.0.1:8766/entrar/` e use o usuário `previa` com a senha gerada no
terminal da instância atual. Para iniciar uma nova instância no notebook:

```powershell
& .\venv\Scripts\python.exe -m locacoes.previa_financeira
```

Na Consulta, selecione uma categoria e abra **Receber / Quitar**. Informe R$ 30 e
tente confirmar sem responsável: deve bloquear com a mensagem acima. Selecione
**Operador Fictício** e confirme: o recebimento será fictício e deverá atualizar o
saldo. O histórico deve mostrar o nome escolhido e o horário real do servidor.
Repita na tela normal pelo acesso a pagamentos da locação.

O banco SQLite da prévia é temporário e separado. Não use `manage.py runserver`
com configurações operacionais para esta conferência. Se já existir prévia ativa,
use-a ou encerre a instância antes de iniciar outra; uma porta alternativa pode ser
escolhida com `--port 8767`.

## Testes executados

**97 testes aprovados em PostgreSQL descartável, em 45,223 segundos, sem skips.**
Banco de testes destruído e cluster encerrado. Incluem os testes aprovados do resumo,
períodos, histórico, recebimento, recibos e gravação segura, agora com responsáveis
explícitos nas fixtures de pagamentos novos.

Cobertura específica:

- sem responsável no painel e na tela normal: recusa no servidor e comparação
  integral dos registros antes/depois, sem alteração financeira;
- funcionário ativo/habilitado: aprovação; inativo, sem caixa e ID forjado: recusa;
- ausência de seleção automática e listagem somente de funcionários elegíveis;
- parcial, quitação e dinheiro/Pix/cartão/outro nas duas interfaces;
- identificação no pagamento, evento e movimento financeiro;
- repetição da mesma operação e concorrência: sem duplicação;
- serviço e criação direta de modelo recusando novos pagamentos sem identificação;
- registro histórico vazio preservado, inclusive validação e reenvio idêntico;
- sinal com/sem responsável e reserva sem sinal;
- Chrome mobile a 390 px: mensagem exata, tentativa deliberada de contornar a
  validação nativa recusada pelo servidor, foco no campo, mensagem visível e
  recebimento aprovado após selecionar o funcionário.

Também houve conferência na prévia ativa: um funcionário elegível disponível,
nenhuma seleção automática e tentativa de R$ 30 recusada no navegador e no servidor.
Essa conferência não criou novo pagamento na prévia. Capturas locais ignoradas pelo
Git em `tmp_whatsapp_visual_resumo_financeiro/responsavel-obrigatorio-390.png` e
`previa-responsavel-obrigatorio-390.png`.

Comando de validação:

```powershell
& .\venv\Scripts\python.exe validar_compra_timeout_pg.py locacoes.tests_responsavel_recebimento locacoes.tests_saldos_consulta locacoes.tests_resumo_financeiro locacoes.tests_gravacao_segura locacoes.tests_consulta_independente locacoes.tests_periodos_consulta locacoes.tests_intervalo_personalizado locacoes.tests_formulario_erros locacoes.tests.LocacoesPagamentosTermoTests
```

Limitações: celular simulado no Chrome, sem aparelho físico. O SQLite da prévia não
é usado como prova de concorrência; essa garantia foi validada em PostgreSQL. O
aviso de namespace `estoque` duplicado já existia e permanece fora deste escopo.

## Arquivos desta etapa

- `locacoes/forms.py`: responsável obrigatório no pagamento e condicional no sinal.
- `locacoes/models.py`: recusa de novos recebimentos sem identificação, preservando
  pagamentos antigos e reenvios idênticos.
- `locacoes/views.py`: envio do responsável selecionado no pagamento inicial.
- `locacoes/templates/locacoes/nova.html`: seleção explícita no sinal.
- `static/locacoes/resumo-saldos.js`: foco e apresentação do erro no modal.
- `locacoes/previa_financeira.py`: habilitação exclusiva do funcionário fictício.
- `locacoes/tests_responsavel_recebimento.py`: testes novos.
- `locacoes/tests.py`, `tests_gravacao_segura.py`, `tests_resumo_financeiro.py` e
  `tests_saldos_consulta.py`: fixtures explícitas de responsáveis para as novas
  regras, preservando as verificações anteriores.
- Este relatório e `docs/locacoes-saldos-recebimento-local.md`: documentação.

Todas as alterações das etapas anteriores foram preservadas.
