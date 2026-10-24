# Correção automática de compras finalizadas — implementação para aprovação

Base remota confirmada: `backup/wip-pedidos-base-2026-06-04`, SHA `aa5fdb66d0b7d4e3186cf0a5b83905ddf3921cad`.

Nova worktree isolada: `.worktrees/correcao-automatica-compras-aa5fdb6`. As correções de interface e testes da worktree anterior foram reaproveitados por cópia. A worktree `.worktrees/diagnostico-compra-imperio-aa5fdb6` e suas evidências não foram modificadas nesta tarefa.

Somente PostgreSQL descartável e dados fictícios. Nenhum Neon, dado real, commit, push ou deploy. O número da compra real não foi presumido: os IDs nas imagens e testes são gerados no banco descartável.

## Funcionalidade entregue

Na edição de compra finalizada, abrir **Corrigir itens e conciliar automaticamente**, selecionar o motivo, revisar os itens, preencher a justificativa e conferir o valor pago. O botão passa a **Salvar correção**. O servidor prepara uma prévia sem gravar; o usuário confirma o total anterior/corrigido, diferença, pagamento, devolução, crédito/saldo e deltas de estoque. Somente após confirmar ocorre a transação.

A edição comum de observações/vencimentos permanece disponível, inclusive após uma conciliação, mantendo as proteções existentes. Alterações de itens/valores após uma conciliação devem usar a nova correção. Rotas antigas que poderiam desfazer pagamentos ou perder a normalização de estoque encaminham para o fluxo auditado; exclusão destrutiva de uma compra conciliada é recusada. A devolução real tem motivo próprio, inclusive devolução integral com zero itens restantes.

As correções anteriores da interface foram mantidas: entrada manual pendente bloqueia salvamento com nome do produto; sugestão automática intocada não entra na compra; nome e ID coincidem; cancelar edição restaura os valores; erro mantém campos e justificativa; sucesso só aparece após resposta positiva do servidor. Os estados Preparando prévia, Aguardando confirmação e Salvando correção são distintos.

## Motivos e efeitos automáticos

| Motivo | Estoque | Financeiro |
|---|---|---|
| Erro de lançamento | Reclassifica apenas o delta entre os itens lançados e os corretos | Mantém pagamentos comprovados; diferença paga a maior vira crédito. Pode retificar um registro automático único comprovadamente errado, conforme valor real explicitamente informado e permissão financeira, com antes/depois e sem novo pagamento |
| Devolução ao fornecedor | Somente reduzir/remover itens originais, preservando produto, unidade e preço; permite retorno integral | Zero recebido gera crédito; valor realmente recebido, conta e data geram uma entrada financeira vinculada e auditada. Não presume que redução de nota devolveu dinheiro |
| Diferença de preço ou quantidade | Aplica apenas os deltas validados | Recalcula obrigação/crédito, preserva pagamentos e só registra devolução financeira quando efetivamente informada |

Pagamentos em `PagamentoContaPagar`, inclusive seus valores e juros, não são sobrescritos. O principal pago é distinguido dos juros bancários. O valor informado deve coincidir com os registros comprovados, exceto a retificação restrita de um lançamento automático único sem outros vínculos/pagamentos/contas/cartão ou conciliação anterior. Esta exceção exige `change_movimentofinanceiro`; devolução de dinheiro exige `add_movimentofinanceiro`. Nenhum pagamento novo é realizado.

Contas a pagar abertas são recalculadas por diferença, mantendo a parte já paga e os vencimentos existentes. Se o novo total exceder o pago e não houver conta aberta suficiente, o sistema cria uma obrigação complementar — não uma saída de dinheiro. A situação da compra passa a considerar esse saldo, em vez de continuar indicando Nota paga apenas pela forma de pagamento original. O complemento não inventa vencimento contratual: fica sem data até ela ser informada pelos controles existentes.

`ConciliacaoCompra` registra o crédito do fornecedor e o saldo a pagar por compra. Um recebimento posterior do crédito pode ser registrado como Diferença de preço ou quantidade com os mesmos itens e o valor realmente recebido; o estoque não é movimentado novamente. Compensação de crédito em outras compras futuras não foi implementada.

## Caso Império reproduzido

Dados fictícios correspondentes aos informados:

- Antes: 7 PCT de 15/350ml × R$ 47,40 = R$ 331,80; 3 PCT de 15/269ml × R$ 36,80 = R$ 110,40. Total R$ 442,20.
- Depois: preservar os 7 PCT e substituir o segundo produto por Ultra 12/269ml, 3 DZ × R$ 35,40. Total R$ 438,00.
- Com pagamento comprovado de R$ 442,20: original preservado integralmente e crédito do fornecedor de R$ 4,20, sem dinheiro novo.
- Se o único lançamento automático do sistema estava errado e o valor efetivamente pago foi R$ 438,00: a prévia mostra a retificação explícita de R$ 442,20 para R$ 438,00, preserva seu ID/conta/data e registra a alteração na auditoria. Não cria outro movimento nem trata isso como devolução física.
- ID do ItemCompra preservado na substituição; FK com a lista preservada; os itens históricos da lista não são reescritos. Nada foi aplicado à compra real.

## Integridade e auditoria

Prévia assinada por compra, usuário, operação UUID, hash do payload e estado observado. Confirmação expira em 15 minutos. Alteração do formulário, estoque, cadastro, compra ou pagamentos invalida a prévia; não há aplicação silenciosa sobre estado diferente.

Aplicação em transação única, mutex já existente do catálogo e bloqueios de compra, itens, contas, pagamentos, movimentos e produtos. Reenvio do mesmo UUID/payload retorna o resultado anterior sem repetir itens, estoque, obrigações, devolução financeira ou auditoria. Duas operações concorrentes diferentes sobre o mesmo estado não podem ambas confirmar.

Históricos antes/depois completos ficam em `EdicaoCompra`: produtos/IDs originais e corrigidos, nomes, unidades, quantidades, preços, estoque, contas, pagamentos, movimentos e dados da conciliação. Itens novos recebem ID corrigido explícito na auditoria. Snapshots de reajustes do SKU original permanecem no histórico; ao substituir, não são atribuídos ao SKU novo. Não há atualização dos preços/custos atuais do catálogo.

Estoque: a finalização do commit base lançava a quantidade bruta do ItemCompra. Esse valor efetivamente lançado é retirado no primeiro ajuste histórico; a quantidade corrigida é convertida pela rotina existente para a unidade base. `estoque_base_registrado` armazena o valor realmente aplicado, para correções seguintes não descontarem novamente quantidades em outra unidade. Mudança posterior da unidade base exige conciliação específica, em vez de reinterpretar o histórico. Cada escrita é identificada na rastreabilidade existente do inventário.

Falha em estoque, itens, financeiro ou auditoria reverte integralmente todos os efeitos, incluindo eventual retificação do registro financeiro original.

## Testes finais

Comando:

`../../venv/Scripts/python.exe validar_compra_timeout_pg.py estoque.tests_correcao_automatica estoque.tests_compra_interface_finalizada estoque.tests_edicao_compra.EdicaoCompraTests estoque.tests_edicao_compra.EdicaoCompraBrowserTests`

**78 testes executados: 78 passaram, zero falhas.** Inclui 32 testes da correção automática e 46 testes da interface/edição reaproveitados. PostgreSQL 17.11 exclusivo em 127.0.0.1, settings independentes de .env, conexão fora do cluster recusada pelo runner. Banco de testes destruído e cluster encerrado.

Cobertura: substituição de produto; quantidade/unidade/preço; conversão e segunda correção com base registrada; pagamento registrado e parcial; parcelas abertas; devolução física com e sem dinheiro, integral e posterior; diferenças entre pago e corrigido; crédito e complemento a pagar; duas origens comprovadas; divergência sem rateio presumido; retificação restrita de registro automático; ID de item novo; alteração de unidade base; reenvio, clique duplo e duas requisições simultâneas; payload/estoque/pagamento alterados após prévia; permissões; rollback nas quatro etapas; histórico e reabertura; bloqueio das rotas antigas inseguras; edição posterior de observação sem mexer no crédito/estoque/financeiro.

Chrome real em 1280 e 1440 px no desktop e 320, 360 e 390 px no mobile. Prévia, cancelar, confirmar, reenvio, reabertura, erro de validação e rollback no navegador. Nos fluxos finais testados, os listeners de erro JavaScript instalados antes da carga permaneceram sem erros. Os testes aguardam os temporizadores de foco já existentes antes de digitar via CDP, evitando instabilidade do teste na abertura.

Layout: CSS, cabeçalhos, larguras/alturas da tabela e controles originais preservados nas cinco larguras. O painel da nova função acrescenta conteúdo à área existente de edição; a posição vertical da tabela pode acompanhar esse conteúdo. Não se afirma igualdade de pixels/posição vertical. No detalhe conciliado, o resumo identifica as contas reais vinculadas aos movimentos, incluindo contas não padrão, sem exibir alocações zeradas artificiais. A tabela desktop não foi substituída por cartões e não houve mudanças nas telas de listas por fornecedor.

Consistência da migração verificada por `makemigrations --check --dry-run` dentro do PostgreSQL descartável. Compilação Python e `git diff --check` passaram. Índice Git vazio; HEAD permanece aa5fdb6. A migração aditiva 0135 foi aplicada somente nos bancos de testes e NÃO em produção.

O log contém stack traces **esperados** das falhas sintéticas dos testes de rollback; os testes correspondentes passaram. O aviso preexistente de namespace estoque duplicado permanece. O erro intermitente Failed to fetch ficou fora do escopo.

## Limitações explícitas

- Cartão/fatura, vínculo apenas textual ou ambíguo, saldo incompatível com pagamentos e movimentos não reconhecidos são recusados sem gravação.
- Divergência do valor real com múltiplas origens ou pagamentos de contas a pagar não é rateada nem sobrescrita automaticamente. Múltiplas origens consistentes são preservadas normalmente.
- Devolução física não pode ser usada para substituir produtos, aumentar quantidade ou alterar preço; usar o motivo apropriado para esses casos.
- Crédito é apurado e registrado por compra. O detalhe identifica o momento da conciliação; pagamentos posteriores continuam visíveis no financeiro. Não há compensação automática em futuras compras.
- A troca de forma de pagamento em Nova Compra tem o erro preexistente `atualizarBotoesPagamento is not defined`, já documentado na etapa anterior; não foi corrigido nesta tarefa. A criação de rascunho da regressão é testada sem mudar esse campo. A edição de compras finalizadas preserva a forma existente.

## Arquivos e demonstração

Código: `estoque/services/correcao_compra.py`, integração em `views.py`/`services/edicao_compra.py`, modelos/migração 0135 e templates `compras_nova.html`/`compras_detalhe.html`. Testes novos e reaproveitados ficam em `tests_correcao_automatica.py` e `tests_compra_interface_finalizada.py`.

Abra `evidencias-correcao-automatica/demonstracao.html` para prévia, salvo, reabertura, devolução e comparação das tabelas original/corrigida nas cinco larguras. JSON `chrome-imperio.json`, log `testes-finais.log` e `postgresql-descartavel.json` comprovam os resultados. Todos os números nas imagens são fictícios.


## Pagamento real versus registro incorreto - complemento

O valor efetivamente pago inicia vazio. O valor registrado aparece separado, com aviso de que pode estar incorreto. O operador informa e confirma o pagamento real consultando nota e comprovante. O servidor exige essa confirmacao; nao infere pagamento a partir do valor antigo.

Credito positivo exige tambem confirmacao explicita de pagamento real a maior sem devolucao da diferenca. A previa e a auditoria distinguem valor registrado anteriormente, pagamento real confirmado, eventual retificacao do registro e credito real. Ausencia de confirmacao bloqueia sem gravar.

Dois exemplos ficticios, que nao afirmam qual foi o pagamento da compra real:

- Registro antigo R$ 442,20; nota corrigida e pagamento real R$ 438,00: retifica o registro automatico unico elegivel para R$ 438,00; credito zero, nenhum novo movimento de dinheiro.
- Pagamento real confirmado R$ 442,20; nota corrigida R$ 438,00; diferenca nao devolvida confirmada: preserva o pagamento e apura R$ 4,20 de credito.

Pagamentos efetivos vinculados a parcelas e origens ambiguas continuam protegidos. Divergencia que nao possa ser conciliada automaticamente com seguranca bloqueia toda a operacao e preserva os dados digitados.

Validacao final deste complemento: 78 testes, 119.333 segundos, zero falhas; PostgreSQL descartavel encerrado. Chrome nos cinco tamanhos, incluindo pagamento real a maior e registro errado sem credito ficticio.
