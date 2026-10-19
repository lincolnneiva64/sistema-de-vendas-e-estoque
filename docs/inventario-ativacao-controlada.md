# Preparação da ativação controlada de ajustes reais

Base publicada informada: `cad2620c2dbeed848f9959f41c1b6090bd9aea38`.
Candidata local: `.worktrees/ativacao-controlada-cad2620`, HEAD destacado nessa base.
Data: 19/10/2026, America/Sao_Paulo. Commit e push autorizados após revisão final;
sem autorização de deploy, alteração de variáveis do Render ou ativação.

## Alteração preparada

Na versão publicada, `INVENTARIO_AJUSTES_HABILITADOS = False` é literal. Definir
uma variável no Render não muda essa versão. A candidata passa a ler a variável
do mesmo nome com `python-decouple`, valor padrão textual `False` e comparação
exata com `True`. O resultado da configuração é sempre booleano.

Sem variável, permanece False. O valor operacional recomendado para desligar é
`False`; para ligar futuramente, somente o texto exato `True`. Valores inválidos,
vazios ou alternativos (`true`, `TRUE`, `1`, `yes`, `on` ou espaços adicionais)
resultam em False, sem interromper a inicialização da aplicação. A chave é lida no início de cada
processo, não em tempo real a cada requisição. Nenhum .env, segredo, migration,
modelo, tela, serviço de aplicação ou fluxo de confirmação foi alterado.

O ambiente publicado e suas variáveis não foram modificados. A atualização real
só foi habilitada por override em testes no PostgreSQL descartável.

## Auditoria dos controles preservados

- Consulta gerencial exige autenticação e permissão de inventário; execução exige
  especificamente `inventario.aprovar_ajuste`. Ativar a flag não concede permissões.
- A única confirmação continua no POST com CSRF, seleção assinada e expiração de
  20 minutos. GET e inicialização da configuração não escrevem propostas ou saldos.
- `atualizar` verifica flag, PostgreSQL e triggers imutáveis antes da execução.
- Conferência, itens, rodadas e produtos são travados na ordem existente. Há
  revalidação da confirmação física, unidade, saldo, sequência e documentos tanto
  na aprovação interna quanto imediatamente antes da aplicação.
- Aprovação interna, evento de confirmação gerencial, movimento de estoque,
  decisão e aplicação persistem na mesma transação. Falha reverte o lote integral.
- Repetição reconhece registros aplicados e não cria novos movimentos. Lote
  parcialmente aplicado exige uma nova seleção. Seleção antiga após mudança de
  evidência/saldo é recusada, sem aprovações remanescentes da tentativa.
- Aplicação registra usuário, `aplicado_em`, lote, antes, diferença, depois,
  unidade, confirmação original e evidências. A decisão e eventos também mantêm
  responsáveis e horários próprios. O OK físico não recebe horário novo.
- Produtos sem diferença não geram proposta, aplicação ou movimento. Falta de
  evidência bloqueia o produto individualmente; o novo resumo exclui o bloqueado.
- O saldo final é sempre atual + diferença apurada no OK. Não se grava diretamente
  a contagem antiga nem os valores históricos citados pelo proprietário.

A conciliação automática existente cobre vendas com snapshots históricos
corroborados. Compras sem evidência histórica suficiente continuam bloqueadas;
a ativação não amplia essa elegibilidade. Uma compra depois da preparação também
invalida a seleção anterior. Não foram alteradas regras/documentos de vendas,
compras, pedidos ou horários comerciais.

## Conferência nº 1: revalidação somente leitura

Consulta concluída em 19/10/2026 às 07:48:41 de Brasília, usando o código publicado.
Transação REPEATABLE READ, READ ONLY; `default_transaction_read_only=on` desde a
conexão; guarda adicional aceita apenas SELECT e comandos transacionais de leitura.
TLS cliente confirmado por libpq com `sslmode=verify-full` e CA confiável. Banco,
role e IDs do endpoint/branch foram comparados com o preflight anterior. O campo
`pg_stat_ssl` do backend retornou false; o indicador da conexão TLS cliente foi
true. Ambos foram registrados sem usar o campo do backend como prova do TLS cliente.

| Produto | Estoque atual | Diferença no OK | Saldo proposto | Situação |
|---|---:|---:|---:|---|
| Trigopan | 68 UN | -2 UN | 66 UN | Apto |
| Bril Condicionador | 4 UN | -4 UN | 0 UN | Apto com venda nº 907 conciliada |
| Fermento Angest | 11 UN | -2 UN | 9 UN | Apto |
| Trigo sem fermento | 0,200 FD | -0,200 FD | 0 FD | Apto |
| Trigo com fermento | 2,100 FD | 0 FD | 2,100 FD | Sem ajuste |
| Trigo 25 kg | 1 SC | 0 SC | 1 SC | Sem ajuste |

Bril: esperado no OK 10 UN, contado 6 UN; venda documentada de 6 UN depois do OK
reduziu o estoque registrado para 4 UN. Assim, 4 + (6 − 10) = 0 UN. A quantidade
física histórica permanece 6 UN; a venda não foi reescrita.

Não havia aplicações registradas para a conferência. Migrations de inventário
0001 a 0007 constavam aplicadas. Observador de estoque e os três triggers de
proteção da auditoria estavam habilitados (`O`). Nenhum lock de atualização,
proposta, aprovação, ajuste, migration ou documento foi gravado pela auditoria.
Os valores da tabela são um retrato da consulta; a confirmação futura deve
recalcular tudo. A associação do endpoint ao serviço Render efetivamente Live
não foi inspecionada no painel nesta sessão; permanece uma conferência operacional
antes de qualquer futura ativação.

## Ensaios no ambiente descartável

O novo módulo `inventario.tests_ativacao_controlada` acrescenta dois testes de
configuração e sete ensaios transacionais com seis produtos fictícios, quatro
com diferença e dois sem ajuste. Cobre lote de quatro, segunda tentativa,
concorrência de duas confirmações do lote, falha no quarto com rollback completo,
venda/compra após a tela, horários/responsáveis, ausência de execução automática,
permissões e reversão da configuração.

A regressão inclui confirmação única individual/lote; duplo clique Chrome desktop
e mobile; recontagem; unidade/documentos; idempotência; concorrência com vendas,
compras e edição documental; auditoria imutável e migrations existentes.
O executor recusa conexões fora do cluster recém-criado em loopback. Os testes
de migrations executam exclusivamente no banco descartável, nunca no Neon.

**Resultado: 124 testes aprovados em 70,794 segundos, zero falhas, cluster
encerrado.** Inclui os nove novos testes e a regressão dos 115 testes anteriores.
Os avisos preexistentes foram namespace `estoque` duplicado, ausência de
`staticfiles` coletado nesse ambiente e eventual Broken pipe de navegação no
Chrome. Nenhuma falha ou teste ignorado foi registrado nesta execução. Não se
declara validação de todos os testes de todo o projeto.

Evidências da preparação conservadas apenas localmente em
`artifacts/ativacao-controlada/`: `testes-124.log`, `testes-124.json` e
`revalidacao-somente-leitura.json`. Esses arquivos não integram o commit.
A consulta de produção pertence à etapa anterior; nesta finalização para commit
e push não houve conexão ao Neon nem alteração das variáveis do Render.

## Procedimento futuro de publicação e ativação — não executado

1. Obter autorização para publicar esta candidata, identificando o SHA final,
   serviço, branch vinculada e configuração de deploy. Conferir a flag efetiva no
   serviço e assegurar `False` explicitamente antes da publicação, incluindo a
   origem em grupos de ambiente. Não preparar `True` com Save only: isso pode
   habilitar silenciosamente a funcionalidade em um próximo deploy.
2. Publicar a candidata ainda com `INVENTARIO_AJUSTES_HABILITADOS=False` e conferir
   o SHA Live e a flag efetiva de todos os processos. Nenhuma migration nova é
   necessária. Não executar comandos de ajuste como parte do build/start/deploy.
3. Confirmar conta gerencial e permissão de aprovação, identidade do banco e
   triggers; atualizar a consulta somente leitura dos seis produtos. Não conceder
   permissões novas nem resolver bloqueios automaticamente.
4. Apenas com autorização específica de ativação, alterar no serviço a variável
   para `True` e usar uma implantação com a configuração nova. A documentação do
   Render oferece **Save and deploy** para reutilizar o build já aprovado com as
   variáveis novas; **Save only** só armazena e não altera o processo em execução.
5. Aguardar a implantação integral e conferir SHA e flag efetivos. Abrir o resumo
   não pode criar movimentos; comparar auditoria/contagens antes e depois desse
   acesso. Não aplicar nenhum ajuste automaticamente como teste de ativação.
6. O administrador atualiza a página e escolhe individual ou lote apto. O botão
   **Confirmar atualização** é a única autorização operacional. Depois da decisão,
   conferir o resultado e o registro do responsável/horário/saldos no histórico.
   Se houver alteração de evidência ou bloqueio, respeitar a recusa e revisar o
   novo resumo; não fixar saldos nem forçar o produto a ficar apto.

Fontes oficiais: [variáveis de ambiente no Render](https://render.com/docs/configure-environment-variables)
e [implantações no Render](https://render.com/docs/deploys), consultadas nesta etapa.
Nenhuma dessas ações administrativas foi executada.

## Procedimento futuro de reversão — não executado

- Se necessário interromper novas aplicações, solicitar que os gestores não
  enviem novas confirmações, definir a variável como `False` no serviço e fazer
  deploy da configuração no build aprovado. Aguardar substituição dos processos
  antigos e confirmar flag efetiva False em todas as instâncias.
- Um simples Restart service no Render reutiliza a configuração do deploy atual;
  não incorpora variáveis alteradas depois dele. Para efetivar o desligamento,
  usar implantação com as variáveis atualizadas, não apenas restart.
- O desligamento é por processo e não é um interruptor transacional global.
  Requisições já autorizadas em instâncias antigas podem terminar durante a troca.
  Conferir a auditoria após a conclusão antes de declarar a operação interrompida.
- Desabilitar não desfaz ajustes já confirmados. Preservar todos os registros;
  eventual correção exige movimentação compensatória revisada e autorização
  própria, nunca apagar auditoria, reabrir proposta ou regravar documento antigo.
- Se optar também por voltar ao código `cad2620`, manter a variável False para
  evitar reativação em uma publicação futura da candidata. Nenhuma reversão de
  schema é necessária para esta alteração, pois não há migrations novas.

Referência para o comportamento de restart:
[Render — Restarting a service](https://render.com/docs/deploys#restarting-a-service).

## Limites desta entrega

Não houve ativação em produção, escrita de estoque real, alteração dos dados da
Conferência nº 1, vendas ou compras, migrations no Neon ou deploy.
A versão publicada continua com a flag literal False. Os únicos arquivos de
implementação novos/alterados são a configuração candidata, os testes e este
relatório. Evidências e capturas locais não integram publicação alguma.
O envio do código ao GitHub foi autorizado nesta finalização. Deploy e ativação
permanecem aguardando autorização do proprietário.


## Revisão final para commit e push

A branch remota `backup/wip-pedidos-base-2026-06-04` foi conferida em
`cad2620c2dbeed848f9959f41c1b6090bd9aea38` antes da finalização. A configuração
foi restringida ao texto exato `True`, conforme o requisito final do proprietário;
a versão anterior da candidata usava conversão booleana genérica e não será
publicada. Os testes de configuração cobrem ausência, ativação explícita,
desligamento e valores inválidos/alternativos sem habilitação ou falha de startup.
Capturas, logs, HTML de simulação, backups e credenciais ficam fora do commit.
Não existem migrations novas nem mudanças em serviços ou telas nesta candidata.


Validação final da configuração restrita: **124 testes aprovados em 71,544 s**,
zero falhas. Inclui ausência da variável, texto exato `True`, desligamento com
`False` e valores não autorizados; todos os casos inválidos permanecem False
sem erro de inicialização. Mantidos os ensaios de ausência de aplicação automática,
individual/lote, duplo clique, concorrência, rollback, permissões e auditoria.
`git diff --check` sem erros. Nenhuma migration nova. O log final é conservado
apenas como artefato local, fora do commit.
