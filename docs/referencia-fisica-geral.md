# Referência física para conferências antigas e futuras

Base: `111d3d89f6285b1fd21ffa01a8115d98479a990b`.
Worktree isolada: `.worktrees/referencia-fisica-111d3d8`, HEAD destacado.
Não foram incorporadas alterações de outras worktrees.

## Pontos compartilhados identificados antes da implementação

- `marco_fisico.validar_contagem` e `ajustes.avaliar`: selecionavam e conferiam
  separadamente a última confirmação compatível com a rodada.
- `marco_fisico.comprovar`/`resultante`: já partiam do OK escolhido, descartavam
  o histórico anterior e consideravam apenas movimentos efetivos posteriores.
- `marco_fisico.recibos`: já reconhecia aplicação com movimento e o evento
  auditado de diferença zero, sem inventar movimentação.
- `views.linhas_revisao`: reconhecia recibos da conferência, mas classificava
  o saldo como não estimado pela existência de uma proposta aplicada em qualquer
  conferência, sem exigir o recibo nessa classificação global.
- `historico.investigar`, `ajustes.preparar_resultado` e `encerramento.resumo`:
  consultavam referências e confirmações por caminhos parcialmente distintos.
- `contagem_admin.confirmar` e `services.salvar_contagem`: já preservavam o OK
  na reconfirmação idêntica; a correção mantinha seu tempo e a nova contagem
  explícita reutilizava `services.recontar`.
- `ajustes.atualizar`/`aplicar`/`bloquear`: permanecem os únicos caminhos
  existentes de autorização e aplicação, com transação, locks e revalidação.

## Mudanças

`marco_fisico.ok_referencia` centraliza a escolha da confirmação imutável atual
e a compatibilidade com valores, unidade da rodada, versão, tempo e sequência.
É reutilizado na avaliação, validação de aplicação, histórico e encerramento.
Confirmações substituídas continuam preservadas e são identificadas no histórico.

Quando a confirmação é administrativa, o evento precisa distinguir uma
declaração explícita de contagem física de uma correção anterior. Correções
encadeadas precisam manter horário, sequência e saldo de referência até a
observação física original. Uma simples confirmação administrativa antiga, sem
essa evidência, não é aceita como prova de um novo corte temporal. Não há
inferência por números iguais nem atualização automática dos registros antigos.
Os eventos necessários são carregados em lote no contexto de aplicação e no
encerramento, evitando uma consulta por produto.

`referencias_estoque` seleciona recibos existentes por produto, inclusive de outras
conferências. Carrega evidências completas somente da aplicação mais recente e
das rodadas históricas solicitadas, sem reconstruir o prefixo anterior ao marco.
O reconhecimento aceita também o formato assinado anterior a `marco_fisico_v1`,
sem converter ou regravar sua auditoria. O recibo precisa manter identidade da
rodada/decisão, assinatura, evidência, unidade e igualdade entre seus saldos.
Estado Aplicado sem recibo não passa a referência confirmada e não é reaplicado.

A apresentação usa três descrições compartilhadas:

- **Saldo provável**: sem referência aplicada comprovada, ou saldo/unidade/
  sequência atual incompatível com a referência disponível.
- **Referência física aplicada**: saldo registrado no ponto da aplicação.
- **Saldo atualizado por movimentações posteriores**: existem escritas efetivas
  posteriores no observador. É saldo operacional após a referência; as origens
  dos movimentos permanecem no histórico. Não é outra contagem física nem
  autorização de ajuste.

A referência histórica permanece visível mesmo se uma mudança de unidade exigir
verificação do saldo atual. Uma nova aplicação passa a ser a referência mais
recente; a anterior continua identificada e preservada no histórico. O bloqueio
que impede aplicar um OK antigo sobre um marco mais recente permanece intacto.

Tabela, histórico, revisão, confirmação e encerramento usam a expressão
**referência física**. Os estados da conferência continuam **Confirmado**,
**Aguardando confirmação** e **Revisão necessária**. Foram preservadas as sete
colunas compactas, a contagem original, a diferença aritmética e a ressalva das
comparações temporais. Detalhes técnicos continuam recolhidos.

## Garantias preservadas

Não foi criado outro saldo persistido, serviço de estoque ou mecanismo de
aplicação. `Produto.quantidade` continua sendo o saldo operacional único.
As escritas existentes continuam respeitando o observador e seus movimentos.
A aplicação continua calculando contagem no OK mais efeitos comprovados
posteriores; diferença operacional é esse resultado menos o estoque atual.

Histórico desconhecido anterior ao OK não bloqueia o novo marco. Movimento
posterior desconhecido não vira zero e continua impedindo a aplicação.
Quantidade zero e diferença zero continuam válidas, com autorização explícita
e recibo sem movimento fictício. Reenvios devolvem o recibo existente; não
reaplicam seu ajuste, inclusive quando outro marco já foi aplicado depois.

As funções de gravação, autorização, locks, transações e idempotência de
`ajustes.py` não mudaram. A mudança de `avaliar` é o uso do seletor compartilhado.
`services.py`, `contagem_admin.py`, `painel.py`, `confirmacao_estoque.py`,
`confirmacao_ajustes.py` e `conversao.py` permanecem idênticos à base.
Nenhum modelo, migration, serviço de compra, venda, financeiro ou configuração
operacional foi alterado. Encerrar continua sem aplicar ajustes.

## Arquivos alterados/criados

- `inventario/marco_fisico.py`.
- `inventario/contexto_ajustes.py`.
- `inventario/ajustes.py`.
- `inventario/encerramento.py`.
- `inventario/historico.py`.
- `inventario/views.py`.
- `inventario/templates/inventario/linha_tabela.html`.
- `inventario/templates/inventario/detalhes_tabela.html`.
- `inventario/templates/inventario/historico_conferencia.html`.
- `inventario/templates/inventario/ajustes.html`.
- `inventario/templates/inventario/ajustes_diretos.html`.
- `inventario/templates/inventario/atualizacao_resultado.html`.
- `inventario/templates/inventario/detalhe.html`.
- `inventario/templates/inventario/encerramento.html`.
- `inventario/templates/inventario/mobile.html`.
- `inventario/templates/inventario/previa_ajustes_modal.html`.
- `static/inventario/ajustes_modal.js` (somente nomenclatura).
- `inventario/tests_referencia_fisica_geral.py`.
- `inventario/tests_marco_fisico.py` (expectativas de nomenclatura).
- `inventario/tests_padronizacao_conferencia.py` (expectativa de nomenclatura).
- `docs/referencia-fisica-geral.md` (este registro).

## Validação

24 testes direcionados distintos aprovados no PostgreSQL descartável protegido
por `inventario.validar_confiabilidade_postgresql`: rodada de 20 testes em
9,409 s e quatro cenários adicionais em 1,501 s. A rodada inicial de 16 testes
se sobrepõe à final e não é somada novamente.

Cobertura: primeira contagem após histórico desconhecido, zero antigo sem
movimento, contagem antiga pendente e venda posterior, aplicação antiga assinada
sem versão nova, estado aplicado sem recibo, correção administrativa sem novo
tempo, confirmação administrativa antiga ambígua, seleção da referência mais
recente, preservação de aplicações anteriores, bloqueio de OK antigo, unidades
discretas/contínuas e avulsos, unidade incompatível, reenvio, aplicação concorrente
com e sem ajuste, venda concorrente e encerramento sem aplicação automática.

Verificadas quatro consultas somente SELECT para a leitura em lote de referências,
sem consulta por produto. Verificado que a consulta não carrega os recibos
intermediários quando só a referência recente e uma rodada antiga são pedidas.
Snapshots dos registros fictícios confirmam que consultas e reconhecimento não
reescrevem estoque, confirmações ou aplicações históricas.

Os clusters finais `inventario-confiabilidade-pg-erbbcooo` e
`inventario-confiabilidade-pg-86tx8u88` foram encerrados. O runner verifica host,
porta, usuário e diretório temporários; nenhuma conexão operacional foi usada.
Não foram executadas migrations nem auditoria geral. `git diff --check` aprovado.
Avisos locais preexistentes: namespace estoque duplicado e staticfiles não coletado.

## Limitações

Não houve acesso aos registros de produção nem classificação de conferências
reais. Contagens administrativas antigas ambíguas permanecem em revisão até
comprovação da referência física original ou registro de nova contagem física;
nenhuma referência temporal é corrigida por inferência. Ausência ou inconsistência
de recibo não é tratada como autorização/aplicação, nem reparada automaticamente.

Os testes iniciais de interface verificam HTML real, sete colunas, detalhes
recolhidos e textos. A navegação interativa final está registrada abaixo; não
houve medição de desempenho em produção. A classificação de movimentos posteriores descreve
o saldo operacional registrado, sem certificar uma nova quantidade física no
depósito e sem substituir a revalidação documental antes de qualquer aplicação.

## Verificação final solicitada antes de commit

Os 24 testes aprovados não foram repetidos. A revisão identificou uma falha
concreta de consistência: o encerramento podia exigir novamente a prova de uma
observação administrativa antiga mesmo quando sua aplicação já tinha recibo
válido e era reconhecida pela conferência e pelo histórico.

Corrigido somente esse caminho em `encerramento.resumo`: uma aplicação já
reconhecida usa a confirmação vinculada ao recibo, verificando a compatibilidade
dos dados da rodada, sem recalcular a contagem antiga nem exigir outra prova
administrativa do período anterior. Produtos ainda pendentes mantêm todas as
validações físicas e documentais. `encerramento.finalizar`, autorização e a
garantia de que encerrar não aplica ajustes permanecem intactos.

Executados exclusivamente a regressão dessa falha e o cenário interativo
`VerificacaoFinalReferenciaBrowserTests`, com Chrome headless e servidor local
no mesmo PostgreSQL descartável protegido. Ambos aprovados em 6,560 s. A
regressão foi então revalidada isoladamente em 0,318 s com um bloqueio explícito
ao recálculo da contagem aplicada. Clusters `inventario-confiabilidade-pg-nv2k0h97`
e `inventario-confiabilidade-pg-28qq7io7`, encerrados ao terminar.

No navegador:

- Cenário equivalente a Saco Chopp Grande: OK original 1,000 MIL, venda efetiva
  posterior 0,300 MIL e estoque 0,700 MIL. A prévia apresentou 0,700 → 0,700 MIL;
  a confirmação explícita criou apenas o recibo de diferença zero, preservando
  horário e dados do OK. Não criou entrada, movimento manual ou segundo movimento
  do observador. Conferência, revisão e prévia do encerramento reconheceram o
  mesmo recibo.
- Cenário equivalente a Sacola 2Kg: contagem anterior 0,500 MIL, estoque 2,000 MIL
  após movimento não comprovado e diferença −1,500 MIL. Os números e a ressalva
  temporal permaneceram visíveis em 1366 e 390 px, sem rolagem horizontal.
  Abrir/fechar detalhes funcionou; a revisão em lote explicou o impedimento e não
  ofereceu formulário de aplicação. O estoque permaneceu 2,000 MIL.
- Confirmações administrativas antigas sem evidência continuam bloqueadas quando
  pendentes. A regressão específica comprova que um recibo efetivamente aplicado
  é preservado e permite o encerramento sem reaplicar nem reescrever o histórico.

Revisados os mesmos 21 arquivos previstos. Sem migrations, configurações ou
alterações comerciais. Verificação AST confirmou as funções de aplicação,
escrita, idempotência, cálculo do marco zero e finalização intactas. A matemática
e os valores exibidos na tabela permanecem preservados. `git diff --check`
aprovado. Permanecem apenas as limitações sobre dados reais, comprovação física
fora do sistema e medição em produção descritas acima.

Sem staging, commit, push, deploy ou alterações em dados reais, Render ou Neon.
