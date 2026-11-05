# Correção auditada de recebimentos diretos — implementação local

## Estrutura aprovada somente para PostgreSQL descartável

Uma tabela nova, `CorrecaoPagamentoLocacao`, sem alterar campos de pagamentos,
estoque, fechamento ou operações unificadas:

- Pagamento original: FK com PROTECT; seu ID, token de recibo e criação permanecem.
- UUID idempotente exclusivo e hash da requisição normalizada.
- Versão anterior (SHA-256 do estado relevante); unicidade por pagamento/versão.
- Snapshots JSON antes/depois do pagamento e movimento financeiro.
- Motivo obrigatório, usuário autenticado (FK PROTECT), nome do usuário e horário
  real de registro no servidor.
- Vínculos protegidos para entrada anterior, estorno e nova entrada. Estorno e
  nova entrada exclusivos de uma correção.
- HTML dos recibos anterior e corrigido, congelados sem controles de escrita.

O pagamento existente passa a representar o valor vigente, com a alteração feita
somente pelo serviço auditado. O estado original completo fica preservado no
primeiro snapshot, sem exclusão do pagamento ou do movimento anterior. Assim,
as somas financeiras existentes continuam usando `PagamentoLocacao.valor`;
não há segundo cálculo de saldo ou duplicação de pagamentos.

O endereço antigo do recibo passa a consultar a primeira versão preservada.
Cada correção tem endereço de versão próprio, ainda sujeito ao acesso interno
existente. Nenhuma confirmação anterior de envio será apagada ou apresentada como
envio da versão corrigida. Dados de recibos antigos que nunca foram armazenados
como snapshot não podem ser reconstruídos retroativamente: o congelamento usa
a apresentação disponível imediatamente antes da primeira correção.

## Fechamentos e concorrência

Nesta etapa, restringir correção a superusuário ativo, como o fluxo financeiro
atual; não ampliar permissões. Somente PostgreSQL com isolamento READ COMMITTED.

Pagamentos diretos não guardam rota histórica. Portanto, bloquear se existir
qualquer fechamento nas datas de origem ou destino, independentemente da rota ou
status, em vez de inferir uma rota que pode ter mudado. Isso é mais restritivo
somente para a nova correção e não muda novos recebimentos ou fechamentos.

Um bloqueio de tabela SHARE em `FechamentoRotaRecebimento`, obtido antes da
verificação, impede inserção/alteração concorrente de fechamentos até o commit.
Depois são bloqueados locação, pagamento, contas e movimento. O bloqueio é curto,
sem chamadas externas. Conta ausente/inativa, movimento divergente, unificado,
saldo excedido ou impossibilidade de verificar o cenário: recusar sem gravar.

O POST existente de fechamento também obtém o bloqueio incompatível (ROW EXCLUSIVE)
antes de calcular seu resumo, dentro da transação. Sem essa pequena integração,
um fechamento poderia esperar a correção terminar e depois gravar um resumo
calculado anteriormente. Não muda valores, critérios ou permissões de fechamento.

## Migration

Preparar `0019_correcao_pagamento_direto`, dependente de `0018_comprovante_cliente`,
da tabela financeira já existente e do usuário configurado. Apenas CreateModel,
índices/restrições da tabela nova; sem migração de dados antigos.

Após aprovação explícita do usuário, a migration foi aplicada e validada somente
em clusters PostgreSQL novos e descartáveis. Sem Neon, dados reais ou publicação.

## Interface

Usar o próprio `modal-recebimento`, `form_pagamento.html` e campos do
`PagamentoLocacaoForm`. O histórico oferece Editar para pagamento direto, troca
o título para Editar recebimento e acrescenta Motivo da correção obrigatório.
Não criar outro modal. Pagamento unificado recebe explicação e não é editável
individualmente. Confirmado o servidor, atualizar os fragmentos existentes.

O menu Mais da consulta também dá acesso ao mesmo modal/histórico para locações
quitadas, permitindo corrigir um recebimento e reabrir seu saldo sem outra tela.

## Arquivos preparados

- `locacoes/models.py` e `migrations/0019_correcao_pagamento_direto.py`: auditoria
  e restrições; parâmetro opcional de consulta da conta sem criá-la.
- `locacoes/payment_corrections.py`: serviço transacional e validações.
- `locacoes/payment_correction_views.py`, `urls.py`, `forms.py`: edição autenticada
  com o mesmo formulário, assinatura da identidade/versão e motivo obrigatório.
- `locacoes/views.py`: histórico com auditoria e versões congeladas dos recibos.
- Templates `includes/form_pagamento.html`, `includes/historico_pagamentos_resumo.html`,
  `lista.html`, `recibo.html` e `static/locacoes/resumo-saldos.js`: mesmo modal,
  histórico, recibos e atualização dos indicadores existentes.
- `estoque/views.py`: somente sincronização do POST de fechamento com a correção,
  antes de calcular o resumo.
- `locacoes/tests_correcao_pagamento_direto.py` e `tests_saldos_consulta.py`: novos
  testes e atualização da expectativa visual de histórico que antes não tinha editor.
- `offline/validate_payment_corrections_postgresql.py`: runner independente de
  produção, conexão limitada a cluster novo de loopback e migration permitida
  somente para a tabela 0019, após aprovação explícita.

## Validação antes da aprovação (histórico)

Compilação Python e templates, rota e comparação estática entre modelo/migration
sem consulta ao banco: aprovadas. `git diff --check`: sem erros de whitespace.
Naquela etapa, os testes financeiros e de navegador estavam preparados, mas não
executados. Nenhuma tabela havia sido criada antes da aprovação.

Após aprovação, o runner cria o esquema anterior sintético no cluster descartável
e aplica de fato somente a migration 0019. Não reproduz migrations antigas de dados.
Pode manter uma prévia fictícia com `--preview-port 8767`; encerrar a prévia também
encerra o cluster. Testes previstos: valores, datas/Belém, forma/conta, funcionário,
fechamentos, estorno e reaplicação, rollback, versões/recibos, CSRF, permissões,
UUID/reenvios e concorrência, além do mesmo modal em 1366, 390 e 320 px.

Esta migration não foi incluída em nenhuma lista de publicação autorizada. A
publicação permanece fora desta etapa e exige revisão própria.

## Resultado após aprovação

**81 testes aprovados em PostgreSQL descartável, em 28,481 segundos, sem skips.**

| Grupo | Testes |
| --- | ---: |
| Serviço e modal de correção direta | 18 |
| Resumo, saldos e correção unificada existente | 16 |
| Gravação segura de novos recebimentos | 15 |
| Responsável obrigatório | 9 |
| Conferência/fechamento existente | 23 |

Inclui correções sucessivas com cadeia exata de recibos, endereço original
preservado, valores/data/forma/responsável, saldo reaberto e nova quitação,
contas verificadas, estorno/entrada líquidos, unificados bloqueados, datas de
origem/destino fechadas, estado/versionamento, UUID, permissões e CSRF. Rollback
comprovado tanto na criação da auditoria quanto em falha posterior ao gravá-la.
Concorrência testada com conexões separadas: mesmo UUID, formulários conflitantes
e fechamento antes/depois da correção. Navegador real em 1366, 390 e 320 px;
apenas um dialog e o formulário existente, com atualização dos indicadores.

O runner verificou programaticamente host 127.0.0.1, porta efêmera, usuário
`correction_fixture`, banco `correction_disposable` e diretório do cluster criado
por initdb. Uma tentativa sintética de destino externo foi rejeitada antes de
chamar o driver. Settings independentes de produção, sem carregar `.env`.

A introspecção antes/depois confirmou: somente a tabela de auditoria foi
acrescentada; nenhuma tabela removida; descrições de pagamentos, movimentos,
contas e fechamentos permaneceram iguais. As restrições de unicidade aprovadas
foram verificadas no PostgreSQL. A 0019 foi executada por MigrationExecutor; as
dependências antigas foram representadas por esquema sintético, sem executar
migrations legadas de dados. Não é simulação de atualização de um banco real.

Evidências locais (não publicar): `diagnostics/correcao-pagamento/tests.log`,
`validation.json` e `editar-1366.png`, `editar-390.png`, `editar-320.png`.

## Prévia fictícia e reprodução

A prévia desta rodada está em `http://127.0.0.1:8767/admin/login/?next=/locacoes/`.
Usuário e senha temporários são informados no terminal, sem gravá-los no projeto.
Cookies próprios (`correcao_previa_session`, `correcao_previa_csrf`), segredo
aleatório, banco descartável e escuta somente em loopback. O cluster permanece
ativo enquanto a prévia estiver aberta; Ctrl+C encerra servidor e PostgreSQL.

Na consulta, clicar Entregues a receber → Receber / Quitar → Editar pagamento (rodapé)
→ Editar. Alterar campos e informar motivo; Salvar correção usa o
mesmo modal e atualiza os indicadores. O menu Mais → Histórico / Editar
recebimentos também permite abrir o mesmo modal para uma locação já quitada.

Para reproduzir a rodada completa, após aprovação da 0019 no ambiente descartável:

```powershell
$closureLabels = @(rg '^    def test_conferencia_recebimentos_rota' estoque/tests.py | ForEach-Object {
    if ($_ -match 'def (test_[a-z_]+)\(') { 'estoque.tests.PixRecebidoTests.' + $Matches[1] }
})
venv/Scripts/python.exe -m offline.validate_payment_corrections_postgresql --apply-approved-0019 --preview-port 8767 locacoes.tests_correcao_pagamento_direto locacoes.tests_saldos_consulta locacoes.tests_gravacao_segura locacoes.tests_responsavel_recebimento @closureLabels
```

## Limites que permanecem

- Somente superusuário ativo; nenhuma permissão ampliada.
- Fechamento em qualquer rota nas datas envolvidas bloqueia a nova correção,
  inclusive status aberto: limite conservador devido à ausência de rota histórica.
- O bloqueio curto da tabela de fechamento pode fazê-lo esperar pela correção.
  Em contenção prolongada, a operação é recusada com orientação; não ignora locks.
- Preservação imutável pelo serviço/modelo, sem trigger de imutabilidade. SQL
  direto e QuerySet.update não são protegidos por esse bloqueio de aplicação.
- Recibos anteriores nunca armazenados não podem ser recuperados retroativamente
  como eram no momento da emissão; o primeiro congelamento usa a apresentação
  disponível antes da primeira correção. As versões congeladas não são regravadas.
- Correção individual de recebimento unificado continua proibida.
- Publicação/migration operacional continuam sem autorização.

## Ajustes de UX — modos e retorno aos saldos (etapa anterior)

No início do mesmo modal ficam visíveis Novo recebimento e Corrigir pagamento.
Novo recebimento mantém o formulário existente. Corrigir pagamento oculta esse
formulário e carrega imediatamente uma lista compacta e paginada de pagamentos,
ordenada por data/hora informada, da mais recente para a mais antiga (ID como
desempate). Cada linha mostra valor, data em Belém, forma, responsável, recibo e
Editar. Unificados continuam com o aviso de bloqueio, sem edição individual.

Selecionar Editar carrega o mesmo formulário de correção aprovado. É possível
voltar à lista e escolher outro pagamento. Os dados digitados em Novo recebimento
e seu token original permanecem somente em memória enquanto o modal está aberto;
alternar modos não cria pagamento nem troca sua identidade de envio. O histórico
completo, auditorias e todas as versões de recibos continuam disponíveis abaixo.

Após confirmação de uma correção pelo servidor: fechar o modal, atualizar os
fragmentos existentes, manter categoria/paginação quando possível e rolar até
o início de Quem compõe o saldo a receber. A confirmação fica logo abaixo do
título da seção. Não rola para o topo absoluto. Erro de validação mantém o modal,
os dados e a posição da página; não executa esse retorno. Novo recebimento não
recebe a rolagem exclusiva de correções.

Arquivos alterados somente nesta etapa de UX:

- `locacoes/templates/locacoes/includes/form_pagamento.html`: duas opções e região
  de seleção no início do modal existente.
- `locacoes/templates/locacoes/includes/historico_pagamentos_resumo.html`: modo
  compacto reutilizando o histórico, seus links e avisos.
- `locacoes/views.py`: seleção de apresentação/ordenação, sem escrita financeira;
  evita carregar snapshots de auditoria na lista compacta.
- `static/locacoes/resumo-saldos.js` e `.css`: alternância, preservação do rascunho,
  retorno aos saldos e confirmação próxima ao título.
- `locacoes/tests_correcao_pagamento_direto.py`: regressões de lista e navegador.
- `offline/validate_payment_corrections_postgresql.py`: somente seed da prévia,
  agora com três pagamentos fictícios para comparar/selecionar.
- Este documento.

Rodada final: **36 testes aprovados em PostgreSQL descartável, em 23,059 segundos,
sem skips**. Inclui todos os testes do serviço de correção e do resumo já existentes,
mais seleção ordenada sem alterações financeiras e navegador com três pagamentos.
Verificados modos em 1366, 390 e 320 px, um único dialog, escolha de pagamentos
diferentes, retorno ao rascunho/token original, motivo obrigatório no servidor,
ausência de rolagem da página no erro e retorno à seção após sucesso, sem perder
a categoria. Os testes usam o runner seguro e a 0019 já autorizada exclusivamente
no cluster temporário; a definição da migration não mudou nesta etapa.

Evidências adicionais: `diagnostics/correcao-pagamento/selecionar-pagamento-1366.png`,
`selecionar-pagamento-390.png`, `selecionar-pagamento-320.png` e
`correcao-retorno-saldos-320.png`. A prévia foi renovada na porta 8767 com novas
credenciais temporárias exibidas no terminal. Nenhuma mudança nos estornos,
recibos, permissões ou critérios de fechamento nesta etapa de UX.

## Interface final aprovada — edição no rodapé

Esta etapa substitui os dois botões grandes do topo descritos na seção anterior.
O modal abre diretamente em Receber / Quitar, com o formulário de recebimento.
Editar pagamento fica à esquerda no rodapé, na mesma linha de Cancelar e
Confirmar recebimento, que permanecem à direita. O botão usa o estilo secundário
existente, com altura mínima de 44 px. Os textos cabem inteiros em 320 px.

Editar pagamento mostra a lista na área principal do mesmo modal, com os valores,
formas, horários de Belém e responsáveis, em ordem decrescente de data/hora. O
botão Editar de cada linha carrega o formulário já existente; motivo obrigatório
e Salvar correção permanecem. Uma ação discreta Voltar ao novo recebimento fica
disponível tanto na lista quanto no formulário de correção, sem fechar o modal.
O rascunho e token do recebimento novo continuam preservados somente em memória.

O histórico completo permanece abaixo, com auditoria, recibos e avisos de
unificados. Sem pagamentos, a seleção mostra Nenhum pagamento registrado nesta
locação e permite voltar normalmente. O acesso não depende de abrir o histórico.
O retorno automático à seção de saldos após corrigir foi preservado, incluindo
confirmação próxima ao título e categoria selecionada; erros não o executam.

Alterados somente nesta etapa final:

- `locacoes/templates/locacoes/includes/form_pagamento.html`: rodapé e ações de volta.
- `static/locacoes/resumo-saldos.js`: apresentação da ação de volta na seleção.
- `static/locacoes/resumo-saldos.css`: alinhamento e largura dos botões sem cortes.
- `locacoes/tests_correcao_pagamento_direto.py`: expectativas do rodapé e caso sem pagamentos.
- `offline/validate_payment_corrections_postgresql.py`: somente seed da prévia,
  acrescentando uma locação fictícia sem pagamentos.
- Este documento.

Validação final: **37 testes aprovados em PostgreSQL descartável, em 25,159 segundos,
sem skips**, incluindo os serviços financeiros já aprovados e três testes de
navegador. Conferidos 1366, 390 e 320 px: um único modal, três botões alinhados e
sem texto cortado, seleção entre pagamentos, lista vazia, volta sem fechar nem
perder o rascunho, erros de validação e retorno aos saldos depois de salvar.

Capturas adicionais: `diagnostics/correcao-pagamento/rodape-editar-1366.png`,
`rodape-editar-390.png` e `rodape-editar-320.png`. A prévia foi renovada na porta
8767, com credenciais temporárias novas. Não houve alteração de serviço financeiro,
recibos, estornos, permissões, regras de fechamento ou definição de migrations.
