# Alertas de revisão — correção local

## Comportamento

A classificação física (Correto, Falta, Sobra) e a atualização administrativa
(Ajuste aplicado, Sem ajuste necessário etc.) continuam independentes do histórico.

Após a cadeia ser validada, os detalhes passam a informar explicitamente:

- Histórico validado. Revisão administrativa não registrada.
- Histórico validado. Revisão administrativa desatualizada.
- Histórico validado. Revisão administrativa registrada.

A consulta não registra revisão, conciliação, contagem nem movimento. Sinalizações
não resolvidas e falhas reais continuam produzindo Revisão necessária, com o motivo
nos detalhes. Esse alerta também permanece visível quando um ajuste já foi aplicado
e existe uma pendência posterior. O registro Ajuste aplicado não é apagado ou revertido.

## Evidências existentes

`revisao_historico.py` é um adaptador somente de leitura para a tabela e a seção
Investigar produtos. Reutiliza `conciliacao_documental.conciliar` sem relaxar seus
requisitos de identidade, snapshot, unidade, datas, eventos e cancelamentos de venda.
Não altera o serviço de aplicação nem `services.validar_cadeia`.

Para ajuste da própria conferência, cruza aplicação, decisão, proposta e assinatura,
confirmação original, evidência congelada, produto/conferência/rodada, usuário,
movimento manual, origem/referência, sequência, saldos, diferença, unidade, lote e
eventos de aplicação dos dois históricos. Auditoria incompleta/incompatível não
é aceita automaticamente. Referência textual ou ajuste manual genérico não basta.
Conciliações explícitas já existentes continuam sujeitas ao validador de cadeia.

Todos os movimentos posteriores continuam entrando na cadeia. A aplicação própria
não libera vendas canceladas, movimentos desconhecidos, lacunas ou outras alterações
sem comprovação. Os detalhes mostram a comprovação documental sem criar registros
de conciliação textual. Movimentos também ficam consultáveis após uma aplicação;
ações que o serviço já recusa para produtos aplicados não são oferecidas.

## Desempenho e validação

O contexto existente fornece vendas/documentos em lote. A auditoria acrescenta
três consultas por lote somente quando existem movimentos de ajuste manual:
aplicações com relações, eventos de ajuste e eventos da conferência. O reconhecimento
usa um mapa por referência, sem busca ou consulta por produto.

Medição no mesmo cenário sintético com ajuste aplicado: 9 consultas no caminho
antigo de contexto/cadeia, 12 no caminho novo com comprovação da auditoria.
Após carregar o contexto, a validação por produto executa zero consultas. O teste
com mais produtos aplicados mantém o mesmo número de consultas. Esta medição não
é um benchmark de latência de produção.

Testes isolados verificam leitura sem escritas, ausência/desatualização de revisão,
vendas válidas e inválidas, ajuste individual/lote, venda posterior, movimentação
genérica posterior, origem desconhecida, descontinuidade, sinalização não resolvida,
auditoria ausente/adulterada e a apresentação independente dos estados.

Resultado final: **66 testes PostgreSQL aprovados em 22,144 s**, sem skips,
cluster `inventario-confiabilidade-pg-dnmjuyqh` encerrado. Seleção:
`tests_revisao_historico.RevisaoHistoricoTests`,
`tests_contexto_ajustes.ConciliacaoLoteTests`, `tests_projecao_fisica`,
`tests_confirmacao_unica.ConfirmacaoUnicaTests` e
`tests_aperfeicoamentos.AperfeicoamentosTests`. Inclui regressões de rollback,
idempotência e concorrência do serviço existente. A primeira rodada ampliada
identificou uma expectativa antiga de texto (-6 UN), ajustada ao rótulo físico
Falta 6 UN já aprovado. Outro teste revelou que o template ocultava movimentos
de produtos aplicados; a consulta desses movimentos foi preservada também nesse estado.

Também passaram **19 testes de apresentação sem banco**, das classes
`tests_classificacao_visual.ClassificacaoVisualTests`,
`tests_previa_visual.PreviaVisualTests` e `tests_tabela_visual.TabelaVisualTests`.
`git diff --check` sem erros. Avisos existentes: namespace estoque duplicado e
diretório staticfiles ausente; não são falhas dos testes.

Não houve acesso a Neon/Render nem dados operacionais. Os testes que criam/aplicam
ajustes usam somente fixtures no PostgreSQL descartável protegido pelo runner
`inventario.validar_confiabilidade_postgresql`, sem executar migrations.

## Limites

Evidência antiga incompleta permanece bloqueada; não existe reparo automático de
auditoria. Os limites conservadores da validação documental existente são mantidos.
Uma alteração concorrente durante a consulta pode exigir recarregar a página; a
consulta não promete snapshot transacional global nem autoriza alterações de estoque.
Não foi inspecionada a conferência real nº 25. A interface mobile de contagem e os
serviços de escrita permanecem inalterados; não foi realizada inspeção em navegador.

Arquivos desta etapa: `inventario/revisao_historico.py`, `inventario/views.py`,
`inventario/templates/inventario/detalhe.html`, `detalhes_tabela.html`,
`linha_tabela.html`, `inventario/tests_revisao_historico.py`,
`inventario/tests_aperfeicoamentos.py` (expectativa atualizada para o rótulo físico
Falta 6 UN já aprovado), e este documento. Melhorias visuais locais anteriores
e alterações preexistentes de locações preservadas. Sem commit, push ou deploy.
