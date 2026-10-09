# Preços vinculados — correção dos dois bloqueios finais

Base `6add971`, branch `backup/wip-pedidos-base-2026-06-04`, 09/10/2026.
Continuação das etapas anteriores, preservadas. Sem publicação ou dados reais.

## Conclusão

PRONTO para publicação da funcionalidade de preços vinculados, após revisão e
autorização de publicação. Os dois bloqueios autorizados foram corrigidos e não
houve nova falha funcional nos testes específicos. A regressão global permanece
com resultados negativos anteriores ao trabalho; não declarar todos os testes
aprovados nem publicar automaticamente.

## Preços legados

Consumidores/gravadores auditados: campos dos modelos; configuração antiga de
widgets/initial no ProdutoForm; fallback opcional do JavaScript de cadastro;
Django Admin; gravações por save/update/bulk_update; seletor de unidades de
`pedido_criar.html`, compartilhado com edição de pedidos.

- `preco_venda_1` e `preco_venda_2` não foram apagados, zerados ou copiados.
- Produto de grupo ativo não pode alterar isoladamente esses valores via admin,
  save, update ou bulk_update. A validação orienta usar o cadastro principal.
- Pedidos de grupos ativos usam `preco_vista` na apresentação principal e
  `preco_vista_fracionado` na secundária. Isso mantém a modalidade à vista que já
  era o fallback anterior do seletor; não introduz nova regra de prazo no pedido.
- Grupo ativo sem fracionamento não apresenta unidade secundária residual.
- Produtos sem grupo ativo conservam exatamente a prioridade/fallback dos campos
  legados, inclusive grupos ainda pendentes de regularização.
- As consultas de criação/edição de pedidos carregam o grupo com select_related.
- Itens e valores de pedidos históricos não são recalculados. Valores negociados
  já existentes no pedido seguem o comportamento comercial anterior.

## Autenticação, autorização e autoria

Reutilizada a permissão existente `estoque.change_produto`, também utilizada pelo
admin para edição de produtos. Não foi criada permissão, papel ou migration nova;
nenhuma conta real recebeu permissões automaticamente.

- Revisão posterior, edição principal e conferência antiga recusam POST anônimo
  ou sem permissão, com HTTP 403 e mensagem clara, antes de qualquer alteração.
- Nova compra, edição, finalização e pré-salvamento verificam a mesma autorização
  quando o envio contém comando de alteração de preços. Operações sem comando de
  preços mantêm o fluxo comercial atual de custos, estoque e financeiro.
- O parser de preços de compra também valida o operador antes de retornar valores.
  O operador autenticado segue até aplicar_precos_compra e o serviço central.
- Criação, ativação, entrada, remoção, renomeação e exclusão de grupos exigem a
  mesma autorização nas ações de escrita. Sugestões e prévias continuam somente
  leitura; não propagam preços.
- Edição inline não permite alterar preços sem autorização. As escritas isoladas
  dos vinculados continuam bloqueadas e direcionadas ao cadastro principal.
- Exclusão de compra exige autorização de preços quando há evidência de efeito
  a restaurar em preços de venda. Recusa preserva compra e demais efeitos.
- `_gravar` exige operador autenticado e autorizado antes de criar qualquer
  evento do grupo. Não é possível emitir novo evento humano com operador nulo.
  Registros anteriores não foram alterados; o FK continua nullable por
  compatibilidade histórica, sem alteração de schema.

## Testes finais

| Conjunto | Executados | Aprovados | Falhos | Erros | Ignorados |
| --- | ---: | ---: | ---: | ---: | ---: |
| Backend proporcional, incluindo pedidos | 256 | 251 | 4 | 1 | 0 |
| PostgreSQL local isolado: serviço, interfaces, bloqueios e concorrência | 49 | 49 | 0 | 0 | 0 |
| Chrome desktop/mobile: pedidos, edição, grupos e compras | 12 | 12 | 0 | 0 | 0 |

Contagens por execução, com repetição de testes em PostgreSQL. Os nove testes
novos de backend cobrem gravação legada por ORM/admin, manutenção do legado sem
grupo, anônimo, usuário sem permissão, ator autorizado, endpoints equivalentes,
ações de grupos, recusa do serviço sem ator, restauração e pedidos históricos.

Navegador testa valores legados divergentes preservados no banco, mudança iniciada
por outro integrante e igualdade efetiva dos preços de apresentação/unidade dos
integrantes no pedido. Produto sem grupo mantém os valores legados anteriores.
Também valida os fluxos de regularização, criação de grupo, edição e revisão de
compras em desktop e mobile, com registros reais do banco isolado verificados.

### Resultados negativos anteriores

As três falhas anteriormente confirmadas em `6add971` permanecem visíveis:

- `GruposProdutosTests.test_listagem_com_e_sem_vinculo`;
- `GruposProdutosTests.test_contagem_apos_remover_e_renomear_preserva_vinculos`;
- `ConferenciaPrecosAntigoSnapshotTests.test_indicador_visual_na_tela_de_vendas`.

A ampliação para a classe PedidoTests encontrou mais dois resultados, ambos
reproduzidos nos testes originais da cópia temporária de `6add971`:

- `PedidoTests.test_pedido_criar_sugestoes_tem_controles_seguros_no_html_e_script`:
  expectativa `data-suggestion-decrease` ausente no HTML original;
- `PedidoTests.test_pedido_parcial_com_saldo_aparece_na_fila_e_influencia_sugestao`:
  IndexError ao acessar `context['linhas'][0]`, também na base original.

Não foram modificadas essas expectativas, os controles de sugestões ou o cálculo
de sugestões de compras. Não são regressões introduzidas nesta correção.

PostgreSQL temporário identificado antes do runner: loopback 127.0.0.1:50498,
banco `linked_prices_isolated`, testes `test_linked_prices_isolated`, diretório
`C:\Users\Camila Neiva\AppData\Local\Temp\codex-linked-prices-pg-20zmn4om`.
Instância desligada no finally; arquivos temporários preservados. Não houve
conexão de teste com Neon, modificação do .env ou instalação administrativa.

`manage.py check`: nenhum erro, aviso preexistente `urls.W005`.
`makemigrations --check --dry-run`: nenhuma migration adicional.
`git diff --check`: aprovado.

## Migrations e riscos restantes

Somente a migration 0131 já existente continua necessária. Ela mantém grupos
pendentes e não altera preços; não foi aplicada na produção. Não há atualização
automática dos 30 grupos/82 produtos informados.

- Operadores de preços precisam estar autenticados e ter change_produto. Conferir
  os usuários/grupos existentes antes do uso; esta tarefa não altera contas reais.
- Permanecem os cinco resultados negativos anteriores da regressão ampliada.
- O mutex conservador serializa escritores; não foi feita medição de carga real.
- Snapshots insuficientes, integrantes alterados ou preços posteriores exigem
  revisão manual na restauração; não forçar exclusão sobre preços recentes.
- SQL externo ou ORM que contorne deliberadamente os managers não está protegido
  por constraints específicas de preço. Não administrar preços por esses caminhos.
- Catálogo offline não recebe atualização automática por esta funcionalidade;
  o módulo e o fluxo existente de preparação/revalidação não foram alterados.

## Manifesto consolidado — 27 arquivos

```text
docs/precos-vinculados-etapa-preparatoria.md
docs/precos-vinculados-servico-compras.md
docs/precos-vinculados-etapa-3.md
docs/precos-vinculados-bloqueios-finais.md
estoque/admin.py
estoque/forms.py
estoque/grupos_produtos.py
estoque/migrations/0131_precos_vinculados_auditoria.py
estoque/models.py
estoque/services/precos_compra.py
estoque/services/precos_vinculados.py
estoque/templates/estoque/cadastrar_produto.html
estoque/templates/estoque/compras_nova.html
estoque/templates/estoque/grupos_produtos.html
estoque/templates/estoque/includes/revisao_precos_posterior.html
estoque/templates/estoque/pedido_criar.html
estoque/tests.py
estoque/tests_compra_pre_revisao.py
estoque/tests_conferencia_precos_antigo.py
estoque/tests_grupos_produtos.py
estoque/tests_revisao_precos_posterior.py
estoque/tests_precos_vinculados.py
estoque/tests_precos_vinculados_bloqueios.py
estoque/tests_precos_vinculados_interfaces.py
estoque/tests_precos_vinculados_servico.py
estoque/views.py
estoque/views_grupos_produtos.py
```

As alterações em tests.py, tests_conferencia_precos_antigo.py e
tests_revisao_precos_posterior.py somente autenticam a fixture autorizada. Os
asserts das falhas anteriores permanecem intactos. O suporte de autorização das
fixtures fica apenas nos testes; não é dependência do código operacional.

Excluir separacao_vendas_fila.html, backups, temporários, diagnósticos, diffs
locais e .worktrees/. Nada foi colocado em staging, commit ou publicado.

Após autorização de publicação, aplicar 0131 sem sincronização de preços e
regularizar um grupo por vez pela tela: operador autorizado, referência explícita,
conferência física/preços, confirmação e verificação do evento/valores. Não ativar
grupos automaticamente nem executar esse procedimento real nesta intervenção.
