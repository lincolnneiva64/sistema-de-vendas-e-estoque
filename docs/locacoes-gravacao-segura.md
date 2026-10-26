# Gravação de locações e pagamentos — etapa 2

## Criação com sinal

A view `nova` envolve a criação da reserva e o pagamento inicial em uma única
transação externa. As funções atuais e suas transações internas são reutilizadas.
Mensagens de sucesso e redirecionamentos ocorrem depois de sair dessa transação.
Erros de validação ou falhas inesperadas revertem reserva, itens, eventos,
pagamento e movimento financeiro. O formulário vinculado ao POST preserva os
valores e mostra o aviso de falha existente; falhas inesperadas são registradas
no logger sem adicionar dados do formulário ao texto do log.

## Pagamento direto sem alteração de estrutura

`PagamentoLocacao.recibo_token` já é um UUID único no banco. Ele passa a ser
gerado antes da gravação, ao abrir Registrar pagamento, e funciona também como
identidade daquele envio. Não muda o formato nem a utilização pública do recibo.
O formulário recebe um token assinado com UUID, locação e usuário. O POST valida
a assinatura e os vínculos. Não se usa valor/data como identificação da operação.

Dentro de `Locacao.registrar_pagamento`, o bloqueio `select_for_update` da locação
precede a busca desse UUID e qualquer baixa. Se já existir pagamento com esse
UUID e os mesmos dados, retorna-se o pagamento original, mesmo após quitação.
Se os dados foram alterados, o envio é rejeitado. Um novo formulário recebe outro
UUID e permite pagamento legítimo com mesmo valor. A unicidade existente no banco
é a garantia adicional contra criação duplicada.

O token permanece no formulário após erro, permitindo repetir uma tentativa
sem trocar sua identidade. Um POST sem token, adulterado ou de outra locação/
usuário não grava. Formulários abertos antes desta atualização precisarão ser
reabertos, com mensagem explicativa. CSRF, permissões e validações existentes
permanecem. O argumento opcional não modifica as chamadas do recebimento unificado.

## Validação

Rodada final: **98 testes aprovados, sem skips**, incluindo:

- 15 testes novos de atomicidade, reenvio e concorrência;
- 65 testes existentes de consulta e formulário, com navegador em desktop/mobile;
- 18 testes existentes de pagamentos e recibos.

Inclui criação com/sem sinal e sem endereço, rollback após movimento financeiro,
falha de validação, reenvio de parcial e quitação, tokens inválidos, reenvio com
dados diferentes, dois pagamentos iguais legítimos, meios de pagamento e
consistência de valor e quantidade de movimentos.
Na revisão final, o formulário antigo sem token foi recusado sem pagamento ou
movimento; a reabertura e gravação funcionaram no mesmo cliente, preservando
cookies e sessão, sem qualquer limpeza.

Os dois testes de concorrência usam conexões separadas no PostgreSQL: dois POSTs
com a mesma identidade retornam o mesmo recibo; duas identidades distintas geram
dois pagamentos. No SQLite esses testes são explicitamente ignorados, pois ele
não oferece `select_for_update`.

Usado o runner local `validar_compra_timeout_pg.py`, que inicia um cluster novo,
em porta exclusiva de loopback, verifica diretório/porta e restringe conexões.
`DATABASES["default"]["TEST"]["MIGRATE"] = False` foi definido somente no processo
de teste; o esquema temporário foi criado dos models, sem executar migrations.
A faixa de preço exigida pelos testes legados de pagamentos foi criada como
fixture sintética no processo de teste, em substituição ao seed de migration.
O PostgreSQL descartável foi encerrado pelo runner ao final.

Rótulos executados: `locacoes.tests_gravacao_segura`,
`locacoes.tests_formulario_erros`, `locacoes.tests_consulta_operacional`,
`locacoes.tests_consulta_abas.ConsultaAbasTests`,
`locacoes.tests_consulta_ux.ConsultaLocacoesUxTests` e
`locacoes.tests.LocacoesPagamentosTermoTests`.

Não foram alterados campos, índices ou constraints. Não é necessária migration.
Sem acesso a produção, dados reais, commit, push ou deploy.
