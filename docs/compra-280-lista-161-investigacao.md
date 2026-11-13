# Compra #280 / Lista #161 — investigação e correção isolada

Base exclusiva: `e52bd9fb53bb6c10d66af6ead89983f01ea9f22a`.
Worktree: `.worktrees/compra-280-e52bd9f`, HEAD destacado, alterações sem commit.

## Limites do diagnóstico

Nenhuma conexão com Render/Neon ou banco operacional. Compra #280 e Lista #161 não foram consultadas, alteradas, recriadas ou finalizadas. Portanto, 11 produtos, total R$ 738,67, quantidade 10 UN e custo R$ 2,95 permanecem informações do relato, sem confirmação no rascunho real. Antes de qualquer finalização operacional, conferir esses valores e os preços revisados na própria compra. A correção não executa saneamento de dados históricos.

## Fluxo rastreado

1. `compras_lista_fornecedor_editar` lê `lista_payload`, monta os itens da lista e pode gerar compra. `compras_lista_fornecedor_gerar_compra` também permite gerar da lista já salva. Ambos reutilizam `_criar_compra_rascunho_da_lista_fornecedor`; o vínculo `lista_fornecedor` é único e a rota consulta compra existente.
2. A geração copia quantidade final, unidade, custo e total do item da lista para `ItemCompra`. Não existe conversão de embalagem nessa passagem.
3. `compra_editar` / `_dados_compra_post` leem arrays paralelos de produto, quantidade, unidade e preço. O preço visível é sincronizado para `preco_unitario[]` oculto; Unidade é o próprio input enviado por `FormData`.
4. `_salvar_compra_e_itens` salva o rascunho e reconstrói seus itens, preservando snapshots individuais/vinculados e estado de revisão por produto. Esse comportamento preexistente não foi substituído. O endpoint de pré-revisão grava antes de emitir a prévia assinada dos preços.
5. `decisoes_precos_compra` assina a projeção de itens, custos, preços, autoria e grupos. O servidor confere identidade do operador, compra, validade temporal e conteúdo atual sob bloqueios. Alterações relevantes ou token vencido continuam exigindo revisão.
6. `compra_finalizar` salva itens e plano financeiro e chama `_finalizar_compra_com_financeiro` dentro da mesma transação e bloqueio do catálogo. A compra é bloqueada por linha; compra finalizada/estoque já lançado encerra o reenvio antes de nova entrada. Estoque, observador de movimento, custos, preços, fornecedores, status e financeiro integram a transação. Não foi removida nenhuma dessas proteções.

## Causas comprovadas e correção

- **Unidade monetária aceita:** input de texto com `datalist` apenas sugere valores. `_dados_compra_post` apenas aplicava `strip`, sem validação Django de unidade; `bulk_create` não executa `full_clean`. Um teste executa o parser extraído da versão de referência e comprova que aceita `2,95`.
- **Aviso obsoleto:** o aviso era HTML condicionado ao valor persistido durante GET, sem atualização ao editar. No mobile, a clonagem da linha também herdava o aviso para o formulário vazio. Agora o aviso acompanha a validação, é limpo ao reutilizar a linha e é reavaliado ao selecionar produto, salvar/cancelar edição e concluir item.
- **Persistência:** não foi encontrada serialização separada ou campo oculto para Unidade. Teste PostgreSQL inicia com `2,95`, envia ` un `, 10 e `2,95` de preço, verifica `UN`, 10 e 2,95 no banco e reabre sem divergência. O GET preexistente mostra a unidade base quando o valor persistido está vazio; o novo salvamento explicita essa base. Nenhum GET corrige dados.
- **Revisão repetida após falha:** o `catch` do navegador apagava incondicionalmente prévia, decisão e confirmação, inclusive em falha financeira/rede sem mudança de preços. Agora só invalida quando o servidor retorna `revisar_precos`. A assinatura continua validada pelo servidor em todo reenvio.
- **Espera sem término definido:** o fetch da finalização não possuía timeout, ao contrário do pré-salvamento. Agora há limite de 30 segundos e restauração do botão em `finally`. Timeout informa ausência de confirmação e pede conferir o estado, pois abortar o navegador não cancela a transação no servidor.
- **Erros por redirecionamento:** falhas de validação e exceções gerais da finalização devolviam redirect/HTML mesmo na chamada JSON. Agora essas falhas respondem JSON com status 400/500 e mensagem, mantendo o comportamento normal para POST sem o cabeçalho da interface.

A validação é por unidade base cadastrada (`unidade_venda_1`, com fallback para `unidade_compra`), ignorando caixa e espaços externos. Não usa regex que proíbe números: `CX12` cadastrado é aceito. Campo vazio usa a base; produto sem base exige cadastro correto. A geração da compra valida todos os itens antes de criar o rascunho. O finalizador também valida dados legados sob os bloqueios atuais. Este fluxo de compras já exigia quantidade/custo na unidade base; não suporta conversão explícita na finalização. Não foi adicionada conversão silenciosa, nem exceção por produto.

## Validação

Runner existente `validar_compra_timeout_pg.py`: cria cluster exclusivo, valida endereço loopback, porta, usuário, nomes de bancos e diretório de dados em cada conexão; usa settings independentes de `.env`/produção; encerra o cluster ao terminar. Apenas migrations existentes foram usadas para construir bancos de teste descartáveis; nenhuma migration foi criada e nenhum banco existente recebeu migrations.

Resultado final: **39 testes aprovados**, nenhum falho ou ignorado, em 22,424 s. Cluster `inventario-disposable-pg-1tc8syzq` encerrado pelo runner. `git diff --check` aprovado. Permanece o aviso preexistente Django `urls.W005` (namespace estoque duplicado). Logs completos e identidade do cluster ficaram no diretório temporário indicado pelo runner, fora da entrega.

Conjunto final: `estoque.tests_compra_rascunho_unidade`, `estoque.tests_decisoes_precos_compra`, `estoque.tests_compra_pre_revisao.CompraPreRevisaoTests` e os três métodos novos de `CompraFalhaBrowserTests`.

Cobertura: parser da referência aceitando unidade monetária; recusa antes de gravar; correção posterior para UN; códigos com números; vínculo lista/compra; pré-salvamento; assinatura expirada/adulterada/desatualizada; preços vinculados; decisão de manter/alterar/adiar; falha após criar contas; rollback de estoque, movimentos observados e financeiro; reenvio da mesma prévia; concorrência; financeiro à vista, prazo e cartão. Navegador Edge local: desktop 1280 e mobile 390 para unidade; revisão confirmada, falha simulada, botão liberado e reenvio sem reabrir revisão; timeout simulado com temporizador de 30s acelerado para 100ms.

Não foi reproduzida a duração anormal específica da Compra #280: não há logs daquele atendimento nem inspeção do PostgreSQL operacional. Não é possível atribuí-la a bloqueio, latência Neon, reinício Render ou outra exceção específica. Também não é possível afirmar o resultado das duas tentativas reais; foram verificadas as garantias do código e os cenários sintéticos. Permanecem necessárias evidências daquele atendimento para fechar a causa operacional da demora.

## Arquivos da entrega

- `estoque/views.py`: validação e resposta JSON de falha.
- `estoque/templates/estoque/compras_nova.html`: validação, aviso, timeout e preservação da revisão.
- `estoque/templates/estoque/includes/compra_produto_opcao.html`: unidade base no catálogo da página.
- `estoque/tests_compra_rascunho_unidade.py`: regressões PostgreSQL e referência.
- `estoque/tests_compra_falha_browser.py`: regressões no navegador.
- Este documento.

Sem alteração do módulo de conferência física, commit, push ou deploy. Alterações preexistentes da worktree principal foram preservadas.
