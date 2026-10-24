# Peso fracionado na separação — diagnóstico e validação

Base: `cd6256890a23bd1f1db8827941bb1868ca9c9972`, confirmado na branch remota `backup/wip-pedidos-base-2026-06-04` antes de criar a worktree isolada `.worktrees/separacao-peso-fracionado-cd62568`.

Registro da etapa de validacao anterior ao commit e push aprovados. Nenhum deploy. Nenhum acesso ao Neon. Somente dados fictícios em clusters novos PostgreSQL 17.11 em 127.0.0.1, protegidos pelo runner contra conexão fora do cluster. Todos os clusters foram encerrados e os bancos de testes destruídos. O erro intermitente Failed to fetch ficou fora do escopo.

## Causa comprovada no código original

1. JavaScript remove `step` do campo number depois da confirmação. Sem esse atributo, o navegador aplica passo padrão 1. O campo com valor 0.2 passa a apresentar `validity.stepMismatch=true`, inclusive escondido. O clique em Concluir aciona a validação nativa e a mensagem de revisão antes de enviar o formulário ao Django.
2. A confirmação já havia gravado `Decimal('0.200')` e status `conferido` no PostgreSQL. Não havia diferença numérica nesse caso; a validação HTML rejeitava a fração.
3. Digitar um peso positivo já pintava o item de verde antes do OK, embora o servidor ainda registrasse `pendente`. O OK do campo Peso separado é necessário para confirmar o peso digitado. A opção geral OK continua confirmando a quantidade solicitada pelo fluxo existente.
4. Django já aceitava 0,2, 0.2 e 0,200 como o mesmo Decimal de três casas. O JavaScript usava Number sem normalizar vírgula, e o campo number dependia do tratamento local do navegador. A resposta do servidor usa ponto.


## Correção aplicada

- Campo de peso em texto com teclado decimal, leitura explícita de vírgula ou ponto, valor positivo e até três casas decimais, equivalente no backend. Valores inválidos não são convertidos silenciosamente para um peso mínimo.
- `step` permanece explícito nos campos numéricos. Produtos UN, CX, PCT e FD continuam com seus controles e validações anteriores.
- O indicador confirmado depende da resposta aceita pelo servidor; nova edição torna o item pendente até confirmar novamente.
- Confirmações de um mesmo item são serializadas: clique duplo não duplica requisições e resposta antiga não substitui uma edição mais recente. Um novo OK durante a gravação aguarda e salva o peso novo.
- Concluir bloqueia dados ainda não confirmados ou alterados, informando o nome do produto e a ação necessária. O backend compara os valores submetidos com a confirmação persistida, sem gravar o formulário de conclusão como uma nova confirmação.
- Quantidades solicitadas, itens da venda, preços, estoque, compras e financeiro não foram alterados. Testes comparam os registros antes e depois do fluxo. Não houve mudanças em consultas, serviços de estoque/compras/financeiro ou migrações.

## Testes finais

Comando: `../../venv/Scripts/python.exe validar_compra_timeout_pg.py estoque.tests_separacao_peso_fracionado estoque.tests.SeparacaoVendaFase1Tests`

| Versão | Executados | Passaram | Falharam |
|---|---:|---:|---:|
| Original cd62568, suíte preexistente intacta | 110 | 102 | 8 |
| Corrigida, suíte preexistente e testes novos | 119 | 111 | 8 |

Os nove testes novos passaram. As oito falhas finais têm os mesmos nomes e divergências observadas no original. Duas expectativas antigas foram atualizadas: o formulário final deve transmitir os status realmente confirmados, em vez de reenviar pendente; a verificação do JavaScript acompanha agora o indicador de confirmação aceita pelo servidor. Nenhuma das oito falhas antigas foi ocultada ou alterada.

- Chrome real com viewport mobile em 320, 360 e 390 px.
- 18 fluxos completos: 0,2 / 0.2 / 0,200 / 0,08 / 0,5 / 1,25 em cada largura; digitar, bloquear antes de confirmar, OK, conferir Decimal persistido, concluir e reabrir.
- Pesos inválidos bloqueados sem requisição de salvamento no Chrome: zero, negativo, texto, NaN, mais de três casas e separadores duplicados. Backend também cobre Infinity.
- Clique duplo, alteração durante resposta retida e novo OK enfileirado: sem perda ou duplicidade de confirmação. A retenção controlada de uma resposta bem-sucedida testa concorrência; não investiga o erro Failed to fetch.
- 24 fluxos de regressão: UN, CX, PCT e FD, quantidade total ou insuficiente, nas três larguras.
- Ausência de erros JavaScript capturados desde a abertura da página nos testes mobile e ausência de rolagem horizontal na matriz de pesos.
- Testes backend cobrem persistência decimal exata, rejeição de entradas inválidas sem mutação, bloqueio de finalização não confirmada e confirmação repetida sem duplicar itens nem alterar a data de finalização.
- Na etapa de validacao: `git diff --check` e compilação Python passaram. Índice Git vazio e HEAD mantido em cd62568.


## Oito falhas preexistentes preservadas

As divergências abaixo foram reproduzidas no original e permanecem na versão corrigida. Registradas para transparência; sua causa funcional não foi investigada nem corrigida nesta tarefa.

| Teste (SeparacaoVendaFase1Tests) | Divergência observada nas duas versóes |
|---|---|
| `test_alteracao_posterior_do_item_e_detectada_e_nao_processa` | O aviso esperado de alteração após envio não aparece. |
| `test_edicao_quantidade_reconciliada_recalcula_status_persistido_da_separacao` | Quantidade observada 14.000; teste esperava 14.300. |
| `test_edicao_venda_remove_item_mas_mantem_com_pendencia_quando_resta_outra_pendencia` | Permanecem 3 itens; teste esperava 2. |
| `test_edicao_venda_remove_item_pendente_e_recalcula_separacao_para_separada` | Status permanece com_pendencia; teste esperava separada. |
| `test_itens_adicionados_removidos_e_quantidade_alterada_entram_no_checklist_de_revisao` | Conjunto de vínculos de itens difere: existe None onde o teste esperava o ID do item. |
| `test_nova_edicao_depois_da_revisao_reabre_revisao` | Rótulo esperado ALTERACAO APOS SEPARACAO ausente. |
| `test_revisao_da_alteracao_concluida_mostra_nota_pronta_revisada` | Item permanece conferido; teste esperava pendente. |
| `test_venda_enviada_para_separacao_sai_de_outras_vendas` | Redirecionamento relativo; teste esperava URL absoluta https://testserver. |
