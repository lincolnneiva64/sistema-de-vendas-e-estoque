# Preços vinculados — atualização seletiva e adoção por integrante

Validação local em 10/10/2026. Base: `bfe91d3`, branch
`backup/wip-pedidos-base-2026-06-04`.

Implementação local concluída. Nenhum staging, commit, push, deploy ou acesso ao
Neon foi realizado. Este relatório substitui a regra comercial de igualdade
permanente descrita nas etapas anteriores, preservadas como histórico.

## Comportamento implementado

O serviço central continua usando transações, mutex do catálogo, bloqueios
ordenados de grupos/produtos, autorização `estoque.change_produto`, versão e
auditoria. Recebe agora destinatários explícitos, confirmação e assinatura da
prévia dos preços, campos legados e estado de adoção. Não existe mais exigência
de igualdade permanente entre integrantes.

Propaga apenas os campos efetivamente alterados pelo integrante iniciador:
`preco_vista`, `preco_prazo`, `preco_vista_fracionado` e
`preco_prazo_fracionado`, conforme a modalidade aplicável. Para os selecionados,
`preco_venda` acompanha `preco_vista`. Outros campos de venda permanecem próprios.

Custos, estoque, códigos, identificação, compras e histórico comercial não são
copiados entre integrantes. Percentuais fracionados continuam calculados a
partir do custo individual. Prejuízo, unidade, conversão e fracionamento mantêm
as validações existentes. Uma recusa desfaz integralmente o comando.

### Revisão durante a compra

Há uma revisão de preços por grupo na mesma compra. O integrante apresentado
inicia aquele reajuste; isso não cria uma referência permanente. Os demais
comprados acompanham o reajuste. A confirmação mostra todos os integrantes,
preços atuais/propostos, diferenças, origem de preços no pedido e custos atuais.

Participantes da compra ficam selecionados e obrigatórios. Ausentes aparecem
desmarcados e podem ser escolhidos individualmente. O servidor deriva os
participantes a partir dos itens persistidos da compra, sob a transação; não
confia apenas nas caixas de seleção do navegador.

A deduplicação da revisão de venda não elimina as atualizações individuais de
custo dos itens comprados, inclusive ao escolher revisar depois. Não há cópia
do custo de um sabor para outro.

### Revisão posterior e edição normal

A revisão posterior agrupa pendências por compra/grupo, usa a mesma confirmação
e conclui as pendências dos participantes em uma única transação. Pendências de
compras diferentes permanecem separadas.

No editor normal, somente o iniciador é obrigatório. Os demais integrantes
começam desmarcados. O formulário envia a versão individual do cadastro, a
versão do grupo, a assinatura e os destinatários confirmados. Uma recusa conserva
os preços observados no banco para permitir uma nova confirmação sem tratar os
valores rejeitados como se já tivessem sido gravados.

Revisões legadas em lote com valores contraditórios ou iniciadores ambíguos são
recusadas em vez de produzir uma propagação parcial. Um produto cujo preço não
mudou não substitui o verdadeiro iniciador do reajuste.

### Ativação e adoção

`Produto.precos_canonicos_adotados` controla a origem efetiva nos pedidos,
independentemente da ativação do grupo. É um campo não editável diretamente.

- Ativar um grupo não modifica preços, adoção ou linhas de produtos.
- A primeira revisão pode partir de um grupo pendente, sem referência fixa.
- Um reajuste confirmado adota os preços canônicos somente nos selecionados.
- Ausentes não selecionados conservam integralmente seus registros, incluindo
  preços legados, canônicos, adoção, autoria e data de atualização.
- Grupos podem conter integrantes adotados e não adotados, com preços distintos.
- Integrantes inativos existentes são identificados e só recebem reajuste quando
  selecionados explicitamente ou participantes obrigatórios da compra. Não são
  reativados pelo reajuste.
- Novos integrantes conservam seus preços e sua adoção anterior ao entrar.
  A tela mantém a regra de entrada de produtos ativos disponíveis. Entrar não
  copia os preços do primeiro integrante.
- Sair ou excluir o grupo conserva a adoção já confirmada do produto; não
  reintroduz silenciosamente a preferência legada.
- Uma revisão sem mudança efetiva de preço não provoca adoção automática.

## Consumidores e caminhos de escrita auditados

A busca de `preco_venda_1`/`preco_venda_2` em Python, JavaScript e templates de
`estoque`, `static`, `offline` e `sistema` encontrou a escolha operacional de
origem no template `pedido_criar.html`. Ele também é usado por `pedido_editar`.
Vendas, cadastro e revisões já usam os preços canônicos de venda. Nenhum código
do módulo offline foi alterado.

Pedidos agora decidem a origem por produto. Não adotados mantêm os mesmos
fallbacks legados e a apresentação anterior; adotados usam os preços canônicos.
O diálogo de confirmação exibe os efeitos dessa escolha na embalagem e no
fracionado. Pedidos e vendas históricos mantêm seus próprios preços gravados.

Admin, edição inline, conferência antiga e gravações por `save`, `update` e
`bulk_update` continuam sem poder alterar isoladamente preços canônicos de
integrantes. Mudanças diretas do indicador de adoção são bloqueadas. Campos
legados de produtos adotados não podem ser editados para contornar o reajuste.
Produtos não vinculados e não adotados mantêm o comportamento anterior.

## Snapshots e restauração de compras

O evento mantém os estados anteriores/novos de todos os integrantes observados,
os destinatários, campos, alterações efetivas e autoria. Registra também a
adoção anterior/nova e sua autoria quando ela muda. Não reescreve eventos antigos.

A restauração recupera somente as alterações comprovadas, inclusive a adoção,
sem exigir igualdade entre preços. Continua bloqueada quando o grupo recebeu
alteração posterior, seus integrantes mudaram ou valores/autoria divergem da
evidência. O bloqueio por alteração posterior permanece conservador mesmo quando
o reajuste posterior atingiu outro integrante do grupo.

Snapshots antigos, sem a nova seção de adoção, continuam restauráveis quanto aos
preços; mantêm a adoção já efetiva da versão anterior. A exclusão da compra não
restaura parcialmente um grupo quando há ambiguidade: exige revisão manual e
preserva a transação. A ativação administrativa do grupo não é desfeita como
efeito colateral da restauração de preços.

## Migration

Necessária: `0132_adocao_precos_por_produto`, dependente de `0131`.

Adiciona o indicador e marca como adotados somente os integrantes de grupos que
já estavam ativos. Isso conserva a origem canônica que `bfe91d3` já utilizava
para esses integrantes. Não altera valores de preço, custos, estoque, grupos,
vendas, pedidos ou auditoria. Não ativa grupos pendentes nem sincroniza os
82 produtos ou os 30 grupos em massa.

O teste de migration executou a transição 0131 → 0132 em SQLite e PostgreSQL
isolados, confirmando preços intactos e preservação de origem em grupos ativos e
pendentes. `makemigrations --check --dry-run` não identificou diferenças.

## Resultados finais

| Validação | Executados | Aprovados | Falhas | Erros | Ignorados |
|---|---:|---:|---:|---:|---:|
| Backend e regressão proporcional, SQLite isolado | 271 | 266 | 4 | 1 | 0 |
| Serviços, interfaces backend, migration e concorrência, PostgreSQL isolado | 80 | 80 | 0 | 0 | 0 |
| Navegador Chrome, desktop e mobile | 20 | 20 | 0 | 0 | 0 |

São execuções distintas, com sobreposição de cenários entre SQLite e PostgreSQL;
não representam uma soma de testes únicos. Todos os cenários desta alteração
passaram. As cinco ocorrências negativas da regressão ampla são as mesmas
documentadas em `precos-vinculados-bloqueios-finais.md`, anteriormente reproduzidas
na base `6add971`:

1. `GruposProdutosTests.test_contagem_apos_remover_e_renomear_preserva_vinculos`:
   expectativa de texto contínuo separada por marcação HTML.
2. `GruposProdutosTests.test_listagem_com_e_sem_vinculo`: mesma causa.
3. `ConferenciaPrecosAntigoSnapshotTests.test_indicador_visual_na_tela_de_vendas`:
   expectativa antiga de `precoConferidoBadge`.
4. `PedidoTests.test_pedido_criar_sugestoes_tem_controles_seguros_no_html_e_script`:
   expectativa de `data-suggestion-decrease` ausente anteriormente.
5. `PedidoTests.test_pedido_parcial_com_saldo_aparece_na_fila_e_influencia_sugestao`:
   `IndexError` ao acessar uma lista de sugestões vazia.

Os asserts desses cinco testes não foram modificados nem ignorados. Não declarar
a regressão global totalmente aprovada.

Os testes novos cobrem compra de um, alguns e todos os integrantes, ausentes
selecionados/não selecionados, quatro preços, aumentos/reduções, fracionamento,
custos/margens individuais, inativos, novos integrantes, ativação, formulário
desatualizado, falta de confirmação, rollback, exclusão/restauração e histórico.

Os testes PostgreSQL cobrem seleção disjunta concorrente, compra versus editor,
restauração versus editor e as regressões anteriores de concorrência. Houve um
vencedor por versão concorrente, sem perda da proteção transacional.

O PostgreSQL reutilizado foi identificado antes de cada execução por banco,
endereço `127.0.0.1`, porta `50498` e diretório de dados. Usou exclusivamente
`linked_prices_isolated` / `test_linked_prices_isolated`; nunca Neon. Está parado.
Seus arquivos temporários foram preservados para eventual remoção posterior.
Chrome usou perfis novos e bancos de testes, sem acessar IndexedDB real.

`manage.py check` não encontrou erros; permanece o aviso anterior `urls.W005`
sobre namespace `estoque` duplicado. `git diff --check` passou.

## Riscos e publicação futura

- A regressão global mantém os cinco resultados negativos preexistentes.
- Restauração após alteração posterior ou mudança de membros exige revisão manual.
- Grupos incompatíveis continuam bloqueados; não foram inspecionados ou corrigidos
  os cadastros reais da produção.
- O deploy futuro deve coordenar migration e troca de versão: pausar gravações de
  preços/ativação e encerrar os workers antigos durante a transição, evitando uma
  ativação pela versão antiga depois do backfill do indicador. Reabrir formulários
  e revisões após atualizar a aplicação. Comandos antigos sem confirmação/
  assinatura são recusados com segurança.
- Não fazer rollback automático para a regra de igualdade permanente depois de
  utilizar reajustes seletivos; preços diferentes passam a ser dados legítimos.

Nenhuma publicação foi solicitada ou realizada nesta execução.

## Manifesto seletivo — 19 arquivos

15 arquivos rastreados modificados:

```text
estoque/forms.py
estoque/models.py
estoque/services/precos_compra.py
estoque/services/precos_vinculados.py
estoque/templates/estoque/cadastrar_produto.html
estoque/templates/estoque/compras_nova.html
estoque/templates/estoque/grupos_produtos.html
estoque/templates/estoque/includes/revisao_precos_posterior.html
estoque/templates/estoque/pedido_criar.html
estoque/tests_precos_vinculados.py
estoque/tests_precos_vinculados_bloqueios.py
estoque/tests_precos_vinculados_interfaces.py
estoque/tests_precos_vinculados_servico.py
estoque/views.py
estoque/views_grupos_produtos.py
```

4 arquivos novos:

```text
docs/precos-vinculados-seletivos.md
estoque/migrations/0132_adocao_precos_por_produto.py
estoque/templates/estoque/includes/precos_seletivos.html
estoque/tests_precos_seletivos.py
```

Não incluir `estoque/templates/estoque/separacao_vendas_fila.html`, `.worktrees/`,
`offline/diagnostics/`, backups, diferenças exportadas, relatórios temporários ou
outros arquivos preexistentes. A alteração local protegida não foi editada.

Os auxiliares de testes antigos agora enviam confirmação explícita de todos os
integrantes nos cenários que continuam testando essa opção. Não introduzem
propagação implícita no código operacional. Testes novos exercitam diretamente
as recusas por ausência de confirmação e a opção de deixar ausentes intactos.
