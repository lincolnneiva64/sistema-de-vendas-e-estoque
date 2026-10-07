# Etapa 2.4 — auditoria antes da extração

Base: `ccb2d91`. O template de fila já tem alteração local; deve permanecer intacto.

## Entrada e validações atuais

`gravar_venda`, em `estoque/views.py`, aceita POST JSON: itens (produto_id,
produto_nome, item_id opcional, quantidade, unidade, preco_unitario), cliente_id
opcional, data_venda, data_vencimento opcional, tipo_pagamento, operador,
origem_recebimento opcional, venda_id/next/ajuste_separacao_id para edição e
pedido_id para nova venda. Não há decorador de login/permissão específico nessa
view: permanecem os controles atuais de middleware, CSRF e require_POST.

JSON ilegível retorna 400. A URL `next` é validada contra host/HTTPS do request.
Venda de edição deve existir e não estar cancelada (404 se ausente); separação
de ajuste deve pertencer a ela. Exige itens e data de venda válida. Cliente,
quando enviado, deve existir e estar ativo; cliente ausente é permitido.
Quantidade/preço são convertidos e arredondados pelo auxiliar `_decimal_do_front`,
devem ser positivos. Produto é resolvido por ID ativo/não excluído, com fallback
por nome; item existente pode manter produto desativado. Preço deve alcançar
o custo cadastrado para a unidade; custo fracionado usa fallback custo/fator.
Subtotal e total são recalculados; custo e estoque do navegador são ignorados.
Conversão e quantidade por unidade são revalidadas na movimentação de estoque.

Não existem nesta entrada validações obrigatórias de operador, cliente ausente,
vencimento de nova venda a prazo, relação cronológica entre datas ou enum estrito
de pagamento. A normalização atual de pagamento permanece. Não introduzir essas
validações nesta etapa. Vencimento inválido vira None; conversão vista→prazo exige
vencimento válido. Consumo próprio não pode ser convertido para pagamento normal
nem o inverso (409). Origem financeira informada é validada contra o total, com
tratamento específico de edição vista→vista.

## Nova venda: ordem e transação

Após validações iniciais, `transaction.atomic` envolve: bloquear Pedido opcional
(deve estar aberto/parcial); baixar estoque; obter snapshots de estoque/custo;
criar Venda; bulk_create de ItemVenda; EventoVenda `venda_gravada`; sincronizar
ContaReceber; sincronizar DespesaDiaria de consumo próprio; registrar recebimento
imediato; atualizar pedido e registrar evento `pedido_parcial`, se necessário.

Sem pedido, `_baixar_estoque_movimentos` ordena produtos para bloqueio, usa
select_for_update e lança ValueError para saldo insuficiente. Com pedido, vende
somente saldo disponível, converte unidade e deixa pendências. Sem qualquer saldo,
retorna 400 com `toast_duracao_ms=12000`. Pedido parcial atualiza ItemPedido e total
via `_atualizar_saldo_pendente_pedido`; pedido completo muda status para convertido.

À vista: `_registrar_movimentos_venda_a_vista` cria entradas em ContaFinanceira
caixa/banco/cartões segundo origem (ou padrão do pagamento), sem ContaReceber
aberta. A prazo: `_sincronizar_conta_receber` cria conta com emissão, vencimento,
cliente e total oficial. Consumo próprio: sem recebimento/conta fictícia;
`_sincronizar_despesas_consumo_proprio` usa pessoa/categoria/catálogo cadastrado,
update_or_create por venda/origem/chave e zera grupos antigos. Catálogo pode ser
criado automaticamente; grupo sem regra usa sem_catalogo. Não há save customizado
de Venda/ItemVenda nem sinal de venda registrado em EstoqueConfig.

## Edição: ordem e transação

Há bloqueios prévios de venda quitada, consumo e reaplicação de origem financeira.
Dentro de atomic: bloquear Venda e separação opcional; revalidar quitação e
conversões; bloquear itens; rejeitar IDs duplicados/de outra venda; atualizar itens
existentes (troca de produto/unidade devolve estoque antigo e baixa novo; quantidade
movimenta apenas diferença); registrar eventos e snapshots, preservando custo
histórico quando produto não mudou; registrar ItemVendaRemovido/devolver estoque
e excluir ausentes; baixar/criar novos itens (não duplicar produto já presente);
garantir ao menos um item; salvar cabeçalho/total e evento de alteração.

Depois, ainda dentro de atomic: prazo→vista regulariza conta e recebe;
vista→prazo exige itens inalterados, estorna e abre conta; vista→vista ajusta
movimentos pelo delta; consumo sincroniza despesas; demais sincronizam conta
preservando recebimentos. Recalcula/sincroniza checklist de separação com usuário
explícito e conclui ajuste atendido, com evento. Nenhum desses efeitos é best effort.

## Depois da transação e erros

Após atomic, monta mensagem, URL da nota, payload de separação (inclui URL pública
dependente do request) e estoque atualizado. São leituras/formatação, não efeitos
obrigatórios de gravação; falha nessa montagem não desfaz venda já gravada.
ValueError dentro dos blocos é traduzido em 400 após rollback; erros inesperados
propagam com rollback. Os retornos de validação de pedido dentro de atomic ocorrem
antes de efeitos persistidos nesses caminhos. Não ampliar o limite transacional.

## Arquitetura escolhida

Serviço único com ramificações explícitas de nova venda/edição, extração conservadora
do corpo existente. Auxiliares de domínio existentes são reutilizados no local
atual, sem duplicação; imports locais explícitos evitam ciclo durante carga da view.
Essa dependência legada do módulo views é um limite arquitetural conhecido, não
uma dependência de request: os auxiliares usados não recebem HTTP.
O serviço retorna Venda, mensagem, flag de edição e IDs de produtos alterados.
A view mantém parsing, diagnóstico sanitizado, next, serialização e resposta HTTP.
Erros de negócio transportam mensagem, status e extras; lançá-los dentro de atomic
garante rollback. O usuário da requisição é passado explicitamente ao serviço.

Após a 2.4, o serviço compartilhado ainda não é idempotente por operação offline.
Fila criar_venda, UUID/idempotência, sincronização, reconciliação, conflitos e
conclusão offline ficam exclusivamente para a etapa 2.5. Não alterar arquivos offline.

## Entrega e verificação

Serviço: `estoque/services/vendas.py`, assinatura
`criar_ou_atualizar_venda(*, usuario, dados) -> ResultadoVenda`.
`ResultadoVenda` contém venda, mensagem, edicao e produtos_estoque_atualizados_ids.
`ErroGravarVenda` contém mensagem, status (400 por padrão) e extras opcionais
(toast do pedido sem estoque). Nenhuma referência a request, JsonResponse,
templates ou URLs existe no serviço. O campo `next` fica inteiramente no adaptador.

Saiu da view a orquestração das validações, cálculos, estoque, persistência,
eventos, financeiro, consumo, pedido e atualização de separação. Permaneceram
require_POST/controles HTTP existentes, parsing JSON, logging sanitizado,
validação de next, tradução de erros, URLs, payloads de apresentação e JsonResponse.
As duas ramificações conservaram seus limites de atomic e a ordem dos efeitos.
Não há cópia ativa do corpo antigo. Uma comparação com `git show ccb2d91:estoque/views.py`
confirmou igualdade integral fora de gravar_venda, descontando apenas o novo import.

Arquivos desta etapa:

- Modificado: `estoque/views.py`.
- Criados: `estoque/services/vendas.py`, `estoque/tests_servico_vendas.py` e este documento.
- Template protegido, arquivos locais antigos e arquivos offline preservados.
- Sem migrations, commit, push ou staging.

### Testes executados

Todos usaram `--settings=offline.test_settings --noinput`, banco SQLite em memória
isolado do banco operacional. O runner existente ignora apenas o ALTER varchar
específico de PostgreSQL da migration 0124, como já fazia antes desta etapa.

1. Antes da extração: `manage.py test estoque.tests_servico_vendas
   estoque.tests.VendaEdicaoUnificadaTests estoque.tests_consumo_proprio
   estoque.tests.PedidoTests estoque.tests.SeparacaoVendaFase1Tests --verbosity=0`.
   308 testes: 295 passaram, 12 falharam e 1 erro preexistente.
2. Depois da extração, mesma seleção e mesmos 308 testes: mesmo resultado,
   mesmos nomes de falhas/erro. Comparação automática das listas confirmou igualdade.
3. Após acrescentar testes diretos/delegação/next: `manage.py test
   estoque.tests_servico_vendas --verbosity=0`: 13 testes, todos passaram.
4. Os 20 métodos `PixRecebidoTests.test_gravar_venda*` foram selecionados por AST e
   executados antes/depois: todos passaram em ambas as execuções. A comparação
   anterior carregou somente a função original via AST de `git show ccb2d91`, em
   memória, antes de carregar as URLs; não restaurou nem modificou arquivos.
5. Seleção final: mesmos módulos do item 1, mais `offline.tests` e
   `offline.tests_commercial`: 335 testes, 321 passaram, 12 falharam, 1 erro e
   1 pulado. As 13 ocorrências são exatamente as da seleção anterior à extração.
   O pulado exige PostgreSQL isolado para teste de concorrência.

Os logs foram escritos somente em arquivos novos no TEMP do sistema, com prefixo
`venda24-` (baseline, after, service, stock-before, stock-after e final).
Uma tentativa inicial de rodar a classe PixRecebidoTests inteira incluiu testes
de OCR/PIX fora do escopo; a comparação controlada acima usa os 20 testes de venda.
A fixture inicial dos novos testes precisou preencher preco_vista/preco_prazo
obrigatórios do Produto; essa correção antecedeu a comparação de 308 testes.

Cobertura dos cenários solicitados:

| Cenários | Evidência |
| --- | --- |
| 1–3: vista, prazo, consumo | tests_consumo_proprio e VendaEdicaoUnificadaTests |
| 4–8: cliente/produto/ativo/quantidade/saldo | ContratoGravarVendaTests e 20 testes de estoque |
| 9–10: unidade/conversão/preço/custo | 20 testes PixRecebidoTests.test_gravar_venda* e edição |
| 11–12: operador/vencimento | teste de caracterização explicita permissividade atual; conversão exige vencimento |
| 13–19: estoque/itens/eventos/contas/movimentos/despesas | testes de contrato, estoque, edição e consumo |
| 20–22: edição e alterações de estoque/itens | VendaEdicaoUnificadaTests e serviço direto |
| 23: pedido importado/parcial/sem estoque | PedidoTests, com falhas preexistentes registradas abaixo |
| 24: rollback | injeção de ValueError após evento/conta; RuntimeError no financeiro; rollback direto; testes de conversão existentes |
| 25–26: HTTP/campos/URLs/separação | contrato de JSON, spy do serviço real, next, métodos/JSON ilegível e separação existente |
| 27: sem duplicar efeitos | edição direta preserva IDs/contagens; consumo e ajustes financeiros existentes |
| 28: offline/rascunhos | nenhum arquivo offline alterado; testes offline/comerciais sem falhas novas |

### Falhas anteriores preservadas

Não corrigidas nesta etapa por estarem fora da extração. A lista abaixo foi
idêntica antes e depois; inclui comportamento real de separação que não deve
ser confundido com uma suíte totalmente aprovada.

- `PedidoTests.test_pedido_parcial_com_saldo_aparece_na_fila_e_influencia_sugestao`: IndexError em linhas da sugestão.
- `PedidoTests.test_pedido_criar_sugestoes_tem_controles_seguros_no_html_e_script`: expectativa de HTML/script.
- `VendaEdicaoUnificadaTests.test_tela_vendas_clique_edicao_usa_nome_real_sem_badge_estoque`: expectativa de script da tela.
- `SeparacaoVendaFase1Tests.test_alteracao_posterior_do_item_e_detectada_e_nao_processa`.
- `SeparacaoVendaFase1Tests.test_edicao_venda_remove_item_mas_mantem_com_pendencia_quando_resta_outra_pendencia`.
- `SeparacaoVendaFase1Tests.test_edicao_venda_remove_item_pendente_e_recalcula_separacao_para_separada`.
- `SeparacaoVendaFase1Tests.test_fila_exibe_pendencias_quantidade_faltante_e_botao_correcao`.
- `SeparacaoVendaFase1Tests.test_fila_mostra_editar_nota_apenas_quando_ha_pendencia`.
- `SeparacaoVendaFase1Tests.test_fila_mostra_editar_nota_para_kg_separado_diferente_da_nota_sem_pendencia_fisica`.
- `SeparacaoVendaFase1Tests.test_itens_adicionados_removidos_e_quantidade_alterada_entram_no_checklist_de_revisao`.
- `SeparacaoVendaFase1Tests.test_nova_edicao_depois_da_revisao_reabre_revisao`.
- `SeparacaoVendaFase1Tests.test_revisao_da_alteracao_concluida_mostra_nota_pronta_revisada`.
- `SeparacaoVendaFase1Tests.test_venda_enviada_para_separacao_sai_de_outras_vendas`.

Aviso preexistente do Django: namespace estoque duplicado (urls.W005).
`git diff --check`: aprovado, sem erros de whitespace. Git emite somente aviso
de normalização LF→CRLF para views.py.

### Limites e riscos

- Os auxiliares continuam no módulo legado views; o serviço importa apenas os
  auxiliares existentes necessários, localmente. Separá-los em módulos próprios
  seria outra refatoração, com escopo maior e risco de afetar outras views.
- As permissividades de validação e o fallback de produto por nome permanecem.
  O servidor mantém todas as validações que já existiam; não acrescenta outras.
- A resposta continua sendo montada depois da transação: erro nessa montagem
  pode ocorrer após commit, como antes. Repetir criação pode duplicar a venda.
- Testes de concorrência/locks PostgreSQL não foram executados; SQLite não
  demonstra comportamento de select_for_update em produção.
- Há 13 falhas anteriores fora da extração; não afirmar aprovação integral.
- Não foi implementada qualquer operação de criação/conclusão de venda offline.

Após a 2.4, o serviço compartilhado ainda não é idempotente por operação offline.
