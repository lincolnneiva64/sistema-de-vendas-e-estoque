# Preços vinculados — interfaces e integração final

Base: `6add971`, branch `backup/wip-pedidos-base-2026-06-04`.
Estado local validado em 09/10/2026. Sem staging, commit, push ou publicação.
Este relatório sucede os registros históricos da preparação e do serviço/compras.

## Resultado

As interfaces principais estão integradas ao serviço transacional. Os caminhos
legados de edição isolada permanecem bloqueados com orientação clara. Não foi
identificado caminho operacional auditado que grave isoladamente os quatro
preços de um integrante ativo. Isso não equivale a uma constraint contra SQL
externo ou contra código que contorne deliberadamente os managers protegidos.

Não houve ativação de grupos reais, alteração de preços reais ou conexão de
escrita com Render/Neon. Os 30 grupos/82 produtos informados não foram
regularizados automaticamente. O módulo offline não foi alterado.

A regressão ampliada ainda contém três falhas visuais de origem anterior.
Não declarar a regressão completamente aprovada nem publicar automaticamente.

## Interfaces concluídas

### Gerenciar grupos vinculados

- A tela existente apresenta participantes, preços atuais, os quatro preços
  aplicáveis e a comparação antes/depois a partir da referência escolhida.
- Apresenta divergências de unidade/fator/fracionamento e impedimentos comerciais
  de cada integrante, incluindo a proteção fracionada usada pelas vendas.
- Exige escolha explícita de referência e confirmação da compatibilidade física
  e da prévia. O servidor verifica novamente o conjunto observado sob transação.
- A ação “Aplicar preços e ativar vinculação” só grava após confirmação. Releitura
  ou seleção de referência não altera preços.
- Novos grupos também exigem prévia e confirmação: criação de vínculos,
  regularização e auditoria pertencem à mesma transação; falha desfaz tudo.
- Entrada em grupo ativo possui prévia própria e confirmação. Novos integrantes
  recebem os preços atuais do grupo; integrantes antigos não recebem preços
  arbitrários dos novos. Grupos pendentes exigem regularização primeiro.
- Saída mantém os preços do produto e invalida a cadeia automática de restauração
  de compras quando o conjunto de integrantes muda.
- Aplicações públicas de preços exigem operador autenticado.

Arquivos: `views_grupos_produtos.py`, `grupos_produtos.html` e serviço central.
Funções principais: `grupos_criar`, `grupos_detalhe`, `regularizar_grupo`,
`adicionar_produtos_regularizados`.

### Cadastro principal do produto

- Identifica grupo, estado e demais integrantes, com aviso de compartilhamento.
- Envia identidade/versão do grupo e observação do cadastro individual.
- `salvar_formulario_produto` executa a edição e o comando de preços na mesma
  transação. Somente preços efetivamente alterados são propagados; o alias
  `preco_venda` acompanha `preco_vista`.
- A leitura de preços e versão do formulário ocorre sob o controle do catálogo;
  o produto é relido antes da montagem de um formulário não vinculado a POST.
- No envio, alteração do grupo, dos preços, do cadastro, do custo ou do saldo
  depois da leitura recusa o formulário antigo. Isso evita sobrescrever valores
  recentes ao enviar outros campos do cadastro junto com o preço.
- Custo editado permanece no integrante de origem. A validação usa o custo final;
  rejeição comercial desfaz custo, preços, cadastro e auditoria integralmente.
- Estoque, identificação, fornecedores e demais dados seguem o formulário
  comercial existente e não são copiados para os outros integrantes.
- Grupos pendentes não permitem alteração isolada dos preços de venda.

Arquivos: `forms.py`, `views.py`, `cadastrar_produto.html`, serviço central.
Funções: `ProdutoForm.__init__/clean`, `produto_editar`,
`salvar_formulario_produto`, `versao_cadastro_produto`, `alterar_precos_grupo`.

### Compras: revisão durante e depois

- As opções de produto trazem preço e versão do grupo da mesma consulta com
  `select_related`; não associam um preço antigo a versão lida posteriormente.
- O simulador durante a compra mostra o compartilhamento e envia
  `versao_grupo_produto_<ID>` junto aos campos selecionados. Grupo pendente exibe
  impedimento antes do envio dos preços; custo independente não exige propagação.
- A revisão posterior carrega grupo/versão no JSON, mostra o compartilhamento e
  envia a versão observada. Ao avançar, relê o próximo item, pois uma alteração
  pode afetar outros integrantes também presentes na fila.
- Formulário antigo ou preços conflitantes para o mesmo grupo são recusados;
  não se escolhe silenciosamente o primeiro preço de um lote.
- Erros comerciais/versionamento são apresentados ao operador. Versão malformada
  no editor da compra não causa gravação nem erro 500.
- O serviço de compras preservado registra evento de todos os integrantes e sua
  autoria/versão, com referências tanto no item quanto na compra. Reconstrução ou
  retirada de item não perde a evidência da compra.
- A exclusão de compra mantém a restauração integral protegida. Alteração
  posterior, mudança de membros ou evidência insuficiente exige revisão manual e
  desfaz a exclusão inteira, inclusive seus efeitos de estoque/financeiro.

Arquivos: `compras_nova.html`, `includes/revisao_precos_posterior.html`, `views.py`,
`services/precos_compra.py` e serviço central.
Funções: `_produto_opcoes_compra`, `_produtos_preco_venda_atualizar_post`,
`_atualizar_precos_venda_produtos_compra`, `revisao_precos_posterior_pendentes`,
`revisao_precos_posterior_salvar`, `aplicar_precos_compra`,
`restaurar_precos_da_compra`, `_compra_excluir_transacional`.

## Auditoria dos demais caminhos

| Caminho | Tratamento dos preços vinculados |
| --- | --- |
| Cadastro principal `/produto/<pk>/editar/` | Propaga pelo serviço versionado; protege também os dados individuais contra formulário antigo. |
| Editor inline da listagem `home` | Alteração isolada é recusada e orienta abrir o cadastro principal. |
| Django admin | Alteração isolada é recusada pelo form/model, com orientação; POST mantém validação e gravação sob o controle do catálogo. |
| Conferência de preços antigos | Alteração isolada é recusada com JSON de erro claro; custo/preços do mesmo envio não ficam parcialmente gravados. |
| Cadastro comum e cadastro rápido na compra | Criam produto novo sem grupo; não atualizam por nome um integrante existente. |
| Aplicação/revisão/finalização de compras | Comando central, versão observada e evidência coletiva; custos individuais. |
| Restauração/exclusão de compras | Cadeia comprovada integral ou recusa com revisão manual; snapshots individuais legados não são inferidos como evidência coletiva. |
| Recalcular preço pelo custo no navegador do cadastro | Desabilitado para vinculados; preços não mudam implicitamente por atualização de custo. |
| `Produto.save`, `QuerySet.update`/`bulk_update` | Proteções contra escrita direta dos preços e alteração estrutural incompatível. |
| Revisão de produtos importados | Revisa identificação/fornecedores; não grava os quatro preços compartilhados. |

Não há propagação dedicada no admin, inline ou conferência antiga. Esses caminhos
estão protegidos e direcionam ao cadastro principal. Não foram implementados
SQL externo, painel novo de auditoria ou administração de preços pelo modo
offline, que ficam fora desta etapa.

## Validação final

| Conjunto | Executados | Aprovados | Falhos | Ignorados |
| --- | ---: | ---: | ---: | ---: |
| Backend e regressão proporcional, SQLite isolado | 167 | 164 | 3 | 0 |
| Serviço/interfaces/concorrência, PostgreSQL local isolado | 40 | 40 | 0 | 0 |
| Chrome desktop/mobile | 10 | 10 | 0 | 0 |

Contagens por execução, com repetição de testes do backend no PostgreSQL.
Os 15 testes de interface passaram em ambos os bancos. As três disputas novas
cobrem dois integrantes/operadores na mesma versão, revisão de compra versus
operador e restauração versus operador. Também passou a regressão existente de
finalização concorrente da compra.

Os dez testes de navegador incluem regularização/edição, grupo novo, revisão
vinculada durante a compra, prévia sem escrita e regressão do pré-salvamento da
compra, cada fluxo em desktop e mobile. Os testes verificam registros efetivos:
preços dos integrantes, custos/estoques individuais, eventos da compra e ausência
de aplicação duplicada; não pressupõem sucesso pela aparência da interface.

Backend cobre os quatro preços, qualquer integrante, fracionamento,
incompatibilidade, prejuízo, referência/confirmação, entrada/saída, formulário
antigo, custo/saldo alterado sem nova versão de preços, rollback, revisão e
restauração protegida, item retirado, admin/conferência/inline e vendas históricas.

PostgreSQL: `127.0.0.1:50498`, banco base `linked_prices_isolated`, banco temporário
de testes `test_linked_prices_isolated`. Banco, endereço, porta e diretório de
dados foram conferidos antes do runner; configuração apenas no processo.
Instância desligada no `finally`, diretório temporário preservado em
`C:\Users\Camila Neiva\AppData\Local\Temp\codex-linked-prices-pg-20zmn4om`.
Não houve modificação do `.env`, instalação administrativa ou serviço Windows.

### Três falhas visuais de origem anterior

1. `GruposProdutosTests.test_listagem_com_e_sem_vinculo`.
2. `GruposProdutosTests.test_contagem_apos_remover_e_renomear_preserva_vinculos`.
   Ambas usam `assertContains` procurando a string inteira na resposta HTML.
   O template já existente separa a quantidade por `<strong>...</strong>`;
   portanto o texto visual não existe como substring contínua do HTML.
   `home.html` é idêntico ao HEAD e já contém essa marcação. Não foram alterados
   os dois asserts nem o visual para mascarar as falhas.
3. `ConferenciaPrecosAntigoSnapshotTests.test_indicador_visual_na_tela_de_vendas`.
   Espera `id="precoConferidoBadge"`, ausente também no template do HEAD.
   `vendas_layout_teste.html` está inalterado e a AST da função `vendas` é igual
   à do HEAD. Os atributos de preço conferido passam; a falha ocorre na expectativa
   do badge antigo. Não foi modificada a funcionalidade de vendas para corrigi-la.

Os testes antigos de grupos foram adaptados somente ao novo protocolo obrigatório
de referência/prévia/confirmação e às unidades da fixture; as falhas independentes
foram mantidas visíveis.

`manage.py check`: nenhum erro, aviso preexistente `urls.W005` de namespace
duplicado. `makemigrations --check --dry-run`: nenhuma migration adicional.
`git diff --check`: aprovado.

## Migrations e limites operacionais

Continua necessária somente `0131_precos_vinculados_auditoria.py`, produzida na
etapa anterior: controle de serialização, auditoria, estado/versão dos grupos e
referências de eventos em Compra/ItemCompra. Não atualiza preços e mantém todos
os grupos existentes pendentes. Não foi aplicada no Neon.

- Regularização física de apresentações exige decisão explícita do operador;
  unidade/fator iguais não comprovam equivalência física.
- Formulários/abas antigos devem ser reabertos quando a versão ou o cadastro
  mudar. A recusa é intencional; não há fusão automática de decisões concorrentes.
- Um lote com preços diferentes propostos para o mesmo campo/grupo precisa ser
  revisto pelo operador. Não há escolha arbitrária de referência por custo.
- O controle conservador único serializa os escritores protegidos e pode reduzir
  paralelismo de compras/edições; os testes não substituem medição de carga real.
- Restaurar compras antigas sem evidência coletiva ou depois de nova alteração
  exige revisão manual. Não sobrescrever preços recentes para forçar exclusão.
- SQL externo e código que contorne as proteções ORM não estão cobertos por
  constraints do banco. Não usar esses caminhos para administrar preços.
- Permanecem três falhas de regressão visual, classificadas acima; o resultado
  global não é totalmente verde. A decisão de publicação permanece posterior.

## Manifesto consolidado para staging seletivo

21 arquivos, incluindo as etapas anteriores preservadas:

```text
docs/precos-vinculados-etapa-preparatoria.md
docs/precos-vinculados-servico-compras.md
docs/precos-vinculados-etapa-3.md
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
estoque/tests_compra_pre_revisao.py
estoque/tests_grupos_produtos.py
estoque/tests_precos_vinculados.py
estoque/tests_precos_vinculados_interfaces.py
estoque/tests_precos_vinculados_servico.py
estoque/views.py
estoque/views_grupos_produtos.py
```

Não incluir `estoque/templates/estoque/separacao_vendas_fila.html`, `.worktrees/`,
backups, diffs locais, diagnósticos, temporários ou qualquer outro arquivo fora
do manifesto. Nenhum deles foi descartado, colocado em staging ou publicado.
