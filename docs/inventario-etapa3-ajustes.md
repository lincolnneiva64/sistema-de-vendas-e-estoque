# Etapa 3 — ajustes controlados, somente desenvolvimento isolado

Base: `d8c06dbf292333b80664842bac18098b855ec053`.
Worktree: `.worktrees/conferencia-ajustes-d8c06db` (detached HEAD).
Não houve acesso ao Neon, alteração de produção, migrations de produção, commit,
push ou deploy. Configuração publicada de execução permanece `False`.
Auditoria final, correção do limite também no servidor e ensaio das migrations
exclusivamente descartável: [inventario-etapa3-auditoria.md](inventario-etapa3-auditoria.md).

## Prévia e fluxo

Painel gerencial **Ajustes de estoque**, acessível pela conferência. Previews com
dados fictícios em `artifacts/ajustes/ajustes-{1366,390,320}.png`; não publicar artifacts.

1. **Preparar ajustes** refaz uma consulta: esperado e contado no OK, diferença,
   movimentos posteriores, estoque atual, saldo proposto e situação. Somente SELECT;
   não cria propostas persistentes nem escreve em estoques. Paginação de 30 produtos.
2. **Revisar e aprovar proposta** exige permissão `aprovar_ajuste`, justificativa e
   declaração expressa de verificação física e ausência de lançamentos pendentes.
   Aprovação persiste evidências e auditoria, sem alterar estoque. Pode rejeitar uma
   proposta pendente ou revogar uma aprovação antes da aplicação; rejeição permanece
   auditada e exige nova confirmação física para outra aprovação.
3. **Revisar aplicação individual** ou **Aplicar todos os ajustes aprovados** abre
   uma prévia com produtos aptos, bloqueados, pendentes e já aplicados e as alterações
   exatas selecionadas. Sem confirmação expressa, nada é aplicado.
4. Na configuração atual, executar é recusado pelo servidor, inclusive em POST
   forjado, chamada direta do serviço e repetição. O botão desabilitado comunica o
   mesmo bloqueio. Ativar uma configuração não inicia nenhuma execução automática.

O histórico existente A/B, suas validações e as telas dos funcionários permanecem
intactos. O endpoint legado `services.aprovar` foi fechado: não poderá aplicar
estoque quando o recurso for ativado, pois não possui a nova confirmação separada.
As regressões antigas desse endpoint agora verificam a recusa; aplicação, rollback
e concorrência do fluxo novo possuem testes próprios.

## Segurança quantitativa e bloqueios

Fórmula documentada: **diferença = contado_base − esperado no OK**;
**saldo proposto = estoque atual + diferença**. Não se substitui automaticamente
o saldo atual por uma contagem antiga. Nenhuma conversão de movimentos históricos
usa o fator atual do cadastro.

Exigências cumulativas:

- Rodada atual, ativa, com OK e confirmação original compatíveis; mesmo cadastro
  congelado da seleção, produto ativo, saldo conhecido e observador habilitado.
- Cadeia existente validada, sem lacunas, movimentos desconhecidos, pendências de
  conciliação ou sinalizações físicas sem resolução.
- **Política conservadora desta versão:** havendo movimentos entre confirmações
  ou depois do OK, permanece bloqueado mesmo com conciliação textual e coincidência
  dos saldos. O observador atual não congela unidade histórica; texto livre não é
  prova suficiente. Solicitar nova rodada e contagem após regularizar os lançamentos.
  Cadastro/conversão alterados exigem nova conferência com seleção correta.
- A conferência **nº 1** é protegida explicitamente no servidor: somente consulta,
  sem aprovação, rejeição ou aplicação pela Etapa 3, mesmo se futuramente ativada.
- Diferença não nula, resultado não negativo e menor que 1.000.000.000.
- A versão do OK, confirmação, cadastro, sequência, saldo e evidências precisam
  coincidir com a preparação e a aprovação. Assinatura SHA-256 da evidência e token
  Django assinado, com validade de 20 minutos, impedem adulteração e prévias vencidas.

O painel limita a avaliação a 500 produtos por conferência; acima disso recusa
preparação/aplicação pela interface e aprovação/aplicação no serviço sob lock,
sem truncar silenciosamente o lote. O preflight
faz validação individual: ainda não foi otimizado para conferências muito grandes
ou medido com a latência do Neon. A paginação limita a apresentação, não esse custo.

## Transação, concorrência e idempotência

Uma única entrada de serviço executa aplicações individual e em lote. Política
**todos os selecionados ou nenhum**: pré-valida o lote inteiro e executa dentro de
uma transação PostgreSQL. Uma falha desfaz saldos, movimentos manuais, observador,
decisões e auditoria de execução; aprovações anteriores permanecem disponíveis.

Ordem de bloqueio: conferência, itens ordenados, rodadas ordenadas e produtos
ordenados. O lock do produto é mantido desde a revalidação até o commit, incluindo
o UPDATE do serviço comercial existente. Uma venda concorrente espera; se ocorreu
antes, a sequência/evidência desatualizada bloqueia a aplicação. Recontagens e novas
confirmações são serializadas pelo mesmo lock da conferência.

UUID da proposta, restrição única rodada/assinatura, decisão única por item,
aplicação única por proposta e lock da conferência impedem duplo clique e execução
simultânea. Repetição de uma seleção inteiramente aplicada retorna os registros
originais, sem novo movimento. Prévia parcialmente executada por outra ação exige
nova preparação; não aplica o restante silenciosamente. Token expirado também exige
nova consulta, sem repetir operações. Nenhum lote aplica bloqueados/rejeitados/pendentes.

O serviço manual existente cria **um** movimento e o trigger cria **um** observador;
a atribuição exige produto, sequência e antes/depois exatos. A aplicação verifica
essa referência antes de concluir; falha de atribuição reverte a transação. O extrato
publicado já elimina a duplicação dessa identidade documental.

## Auditoria e recuperação

Novas tabelas:

- `PropostaAjuste`: UUID, rodada, confirmação física, evidência congelada/assinatura,
  justificativa, estado, usuário autorizador e data.
- `AplicacaoAjuste`: UUID único da operação, proposta, decisão/movimento manual,
  usuário executor, data/hora, UUID do lote, antes/diferença/depois, unidade congelada
  e cópia das evidências e da autorização.
- `EventoAjuste`: eventos imutáveis de aprovação, rejeição e aplicação, com usuário,
  horário e conteúdo. Evento de aplicação também integra a auditoria existente.

Auditoria de aplicações paginada, com proposta, confirmação, operação, lote e documento
manual consultáveis por identificação. Cada proposta mostra seu histórico de decisões.
Sem rotas de exclusão/edição da auditoria. `PROTECT` protege referências; triggers
PostgreSQL recusam UPDATE/DELETE dos eventos e aplicações e alterações nos campos
congelados da proposta. Só permitem aprovado → rejeitado/aplicado. A aplicação verifica
a presença desses triggers. Isso não protege contra um administrador do banco capaz
de desabilitar triggers, alterar funções ou executar TRUNCATE; tais privilégios devem
ficar fora da conta operacional na futura liberação.

Procedimento compensatório previsto (não executado nesta etapa):

1. Suspender novas aplicações e identificar operação/lote e documentos afetados.
2. Recontar fisicamente e revisar movimentos e unidades, sem apagar a aplicação original.
3. Elaborar nova proposta independente com evidência atual, referenciando explicitamente
   o UUID original na justificativa. O inverso aritmético automático é proibido: pode
   haver vendas/compras posteriores.
4. Submeter a aprovação e confirmação expressas. A nova operação gera seu próprio UUID,
   movimento e auditoria, preservando integralmente os registros anteriores. Conferência
   nº 1 permanece fora desse procedimento.

## Migrations pendentes e futura ativação

Dois arquivos novos, **não executados em produção**:

- `0006_propostas_aplicacoes_ajuste`: três tabelas e restrições de identidade.
- `0007_auditoria_ajuste_imutavel`: funções/triggers PostgreSQL de imutabilidade.

O bootstrap descartável cria o schema diretamente e instala o SQL equivalente das
proteções; a auditoria adicional testa o executor Django para 0006/0007 somente
nesse banco descartável. O teste de consistência `makemigrations
--check --dry-run --settings=inventario.test_settings` verifica ausência de alterações
de models sem migration; não aplica schema.

Uma publicação futura precisará de autorização separada para o código e para aplicar
essas migrations em janela controlada. Não publicar o painel sem criar as tabelas.
Manter `INVENTARIO_AJUSTES_HABILITADOS = False` durante a publicação e revisão.
Ativação real exige autorização posterior e mudança explícita da configuração já
existente, conferência de papéis/permissões e proteções do banco, piloto com uma
conferência nova e aplicação individual expressamente confirmada. Ativar não varre
aprovações nem inicia lote; propostas antigas/desatualizadas permanecem bloqueadas.

## Testes

**163 testes aprovados em 56,940 segundos**, incluindo 31 testes novos da Etapa 3.
Cluster `inventario-confiabilidade-pg-_qerfo2q`, encerrado (`Resultado: 0; estado: stopped`).
Tudo usa dados fictícios e cluster local guardado por host/porta/database/data_directory.
Nenhuma conexão Neon.

Comando da validação final:

```
python -B -m inventario.validar_confiabilidade_postgresql inventario.tests_ajustes inventario.tests_extrato_consolidado inventario.tests_saldos_historicos inventario.tests_historico inventario.tests_painel inventario.tests_periodos_historico inventario.tests_saldo_inicial inventario.tests_fluxo inventario.tests_postgresql inventario.tests_aperfeicoamentos
```

`git diff --check` aprovado. Verificação de models/migrations sem execução: `No changes
detected`. Avisos preexistentes: namespace `estoque` duplicado e ausência da pasta
`staticfiles` no ambiente descartável; CSS servido pelo servidor estático de teste.
O teste antigo de contagem cega tinha uma lista desatualizada: passou a reconhecer
`guardado` (booleano) e `ultimo_ok` (somente embalagens/avulsos/horário da própria
contagem), já presentes na base publicada. O payload não foi alterado e a lista
restrita continua verificando ausência de saldo, custo e dados gerenciais.

Cobertura: preparação SELECT, aprovação/rejeição, permissão/CSRF/escopo, bloqueio
técnico/rota antiga, confirmação explícita, aplicação individual/lote, repetição,
duas pessoas simultâneas, venda concorrente no lock, compra/venda entre etapas,
recontagem/OK alterado, unidades incompatíveis, movimentos não conciliados e cadeia
numericamente coincidente, falha no segundo item, falha da auditoria/atribuição do
observador, proteções SQL e integridade da auditoria. Chrome em 1366/390/320 px e
fluxo real fictício de aprovação/aplicação em 390 px com flag habilitada só no teste.

## Arquivos da implementação

- `inventario/ajustes.py`, `ajustes_views.py`: preparação, decisões, confirmação e execução.
- `inventario/models.py`, migrations `0006`/`0007`: propostas e auditoria.
- `inventario/services.py`: fechamento da execução legada.
- `inventario/urls.py`: painel gerencial GET/POST com guarda existente.
- Templates `ajustes.html`, `detalhe.html`: painel novo e acesso pelo painel existente.
- `static/inventario/ajustes.css`: apresentação desktop/mobile.
- `inventario/tests_ajustes.py`: testes novos; `tests_fluxo.py` e `tests_postgresql.py`:
  atualização das expectativas do fluxo legado e dos campos cegos já publicados.
- `inventario/test_runner_confiabilidade.py`: proteções SQL no schema descartável.
- Este relatório. Capturas em artifacts ficam fora da aplicação.

Models, views e serviços de compras/vendas, contas financeiras, configurações,
autenticação e payload dos funcionários não foram alterados.
