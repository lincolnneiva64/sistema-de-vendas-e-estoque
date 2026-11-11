# Proteção de preços no cadastro e nas compras

## Revisão final de regressões — 11/11/2026

Correções restritas encontradas nesta revisão:

- A finalização chamava `Produto.save(update_fields=quantidade)`, que executa
  `full_clean()` de todos os preços. Um cadastro já abaixo do custo bloqueava
  inclusive uma compra legítima com revisão adiada. A entrada agora atualiza
  exclusivamente quantidade/data, com a linha bloqueada e dentro da mesma
  transação. Custos, auditoria e aprovação de preços continuam nos serviços
  existentes. O teste reproduziu o bloqueio antes da correção.
- A atualização da fila descartava os produtos concluídos, impedindo voltar
  para consultá-los. Além disso, o botão “Próximo” de um item já concluído não
  executava o avanço. Agora o histórico da sessão permanece somente para
  consulta, com campos desabilitados; os pendentes recebem dados e assinaturas
  atuais do servidor, e avançar novamente não repete o POST.

Verificações finais: **129 testes aprovados e dois ignorados**, sem somar
reexecuções. Cadastro/decisões/revisão posterior/pré-revisão: 54 aprovados e
dois de concorrência PostgreSQL ignorados; contratos históricos de grupos:
64 aprovados; Chrome com perfil temporário: 11 aprovados, incluindo grupos
em desktop/mobile, voltar/avançar e adiamento após recusa de preço inválido.
Os contratos históricos de grupos usam o middleware original do settings de
teste; os demais usam também os middlewares atuais de acesso e LOCAL.

Regressões específicas acrescentadas: corrigir custo e/ou preços de cadastro
antigo inválido, independente e vinculado; compra com custo aumentado e preço
inválido anterior, à vista e com três boletos; reenvio sem duplicar estoque ou
contas; equivalência entre custo principal/fracionado e o custo usado na venda;
voltar ao item concluído e avançar sem reenvio; adiar após recusa de “manter”.

Limitações: a verificação adicional de `CompraUnidadesTests` apresentou quatro
falhas e dois erros: cinco casos recebem 403 com os fixtures antigos e o
middleware atual; um teste chama a finalização sem a decisão obrigatória de
preços. Essa suíte adicional não foi declarada aprovada nem alterada nesta
revisão. Não houve teste de concorrência PostgreSQL. Permanecem o aviso de
namespace `estoque` duplicado e mensagens de Broken pipe ao fechar abas.

Execução reproduzível em `diagnostics/revisao_final_custos_runner.py`, com
SQLite exclusivo em TEMP e `SemMigrationsRunner`, que cria as tabelas
diretamente. Resultados em `diagnostics/revisao_final_custos_backend_final.log`,
`diagnostics/revisao_final_custos_grupos_final.log` e
`diagnostics/revisao_final_custos_browser_final.log`; a suíte adicional está
em `diagnostics/revisao_final_custos_fluxos.log`. Chrome precisou executar fora
do sandbox para inicializar; continuou usando servidor local e perfil novo.

Sem acesso ao Neon, dados reais, migrations, commit, push ou deploy. Arquivos
de locações e offline preservados.

Complemento da [validação preventiva na venda](vendas-validacao-custo-preventiva.md).
Esta parte não estava implementada pela entrega da venda. Foi auditada e
corrigida separadamente. Nenhum dado operacional foi consultado ou saneado.

## Diagnóstico e correções

| Caminho | Proteção que já existia | Lacuna e correção |
| --- | --- | --- |
| Cadastro novo | `Produto.clean()` e `save()` verificavam preços principais positivos e custo | A opção legada `permitir_prejuizo` podia liberar preços principais abaixo do custo. Agora isso é recusado mesmo com a opção marcada, conforme decisão expressa do usuário. |
| Edição normal | O formulário reutilizava `Produto.clean()`; grupos tinham versão, consentimento, auditoria e bloqueios | Produtos independentes não passavam pela proteção dos fracionados. `ProdutoForm` e o formulário do admin agora reutilizam o validador individual existente, antes da gravação. |
| Fracionados | O serviço de grupos já comparava ambos os preços fracionados com o custo da unidade de venda | A regra foi extraída para `validar_precos_individuais`, utilizada também nos cadastros e nas compras independentes. Não foi criado um segundo algoritmo de comparação. |
| Alteração de custo | Edição normal verificava os preços principais; compras exigiam decisão assinada sobre revisão/adiamento | Uma revisão declarada concluída podia conservar preços abaixo do novo custo. O estado final é validado antes de confirmar a compra, dentro da transação existente. |
| Revisão após compra | Vínculos, assinatura da prévia, versão, consentimento e custos desatualizados já eram verificados | A escrita de preços independentes usava atualização direta; “manter preços” podia encerrar uma pendência inválida. Ambos os caminhos agora validam os preços antes de gravar/concluir. |

O cálculo fracionado usa primeiro o custo fracionado explícito. Na ausência de
valor positivo, usa o custo principal dividido pelo fator, com o mesmo
arredondamento decimal em centavos. `custo_fracionado_produto` é compartilhado
pelo serviço de cadastro e pelo helper de custo da venda.

Foram preservadas as regras anteriores de igualdade: o cadastro principal
normal exige preço maior que o custo; a compatibilidade legada com igualdade
não permite valores inferiores. Os preços fracionados e a venda aceitam
igualdade. Não houve criação nem ativação automática de exceção comercial.

## Revisão pendente não é aprovação

Continua possível receber a compra com seu custo real e escolher revisar depois.
Isso mantém a pendência e os preços anteriores, sem liberar prejuízo, alterar o
histórico de compras ou inventar um custo menor. A venda permanece protegida
pela conferência preventiva e pelo backend.

Não é possível concluir a revisão com preços inválidos, nem corrigir somente
um preço deixando outro preço aplicável abaixo do custo. A recusa preserva os
dados, as assinaturas/autorias anteriores e a pendência; a transação desfaz
eventuais efeitos parciais. Corrigir custo e todos os preços necessários na
mesma confirmação continua permitido.

Produtos independentes são validados individualmente. As regras de igualdade
de apresentação/conversão dos grupos não são aplicadas a produtos independentes
de unidades diferentes. Integrantes não selecionados e não comprados não são
incluídos indevidamente na aprovação. Configurações de grupos ainda pendentes
podem conservar valores legados sem atestá-los como preços válidos; os comandos
efetivos de preços e de conclusão continuam sujeitos à validação.

## Arquivos desta parte

Código:

```text
estoque/models.py
estoque/forms.py
estoque/admin.py
estoque/services/precos_vinculados.py
estoque/services/precos_compra.py
estoque/views.py
```

Testes:

```text
estoque/tests_protecao_custos_cadastro.py
estoque/tests_precos_seletivos.py
estoque/tests_precos_vinculados_interfaces.py
estoque/tests_precos_vinculados_servico.py
```

Os três arquivos de testes históricos receberam somente os dados de decisão e
a prévia assinada que o endpoint já exigia. As verificações de consentimento,
versão, rollback e preservação dos demais produtos continuam presentes.

## Resultado da verificação isolada

170 testes distintos aprovados nesta conferência, sem somar reexecuções:

| Parte | Testes | Ambiente |
| --- | ---: | --- |
| Cadastro/compras, incluindo 12 novos testes | 48 | SQLite temporário, política atual de acesso e middleware LOCAL |
| Contratos históricos de grupos e preços seletivos | 78 | SQLite temporário, configuração isolada original dessas suítes |
| Venda: prévia de custo, gravação e navegador | 44 | SQLite temporário, política atual de acesso e Chrome com perfil novo |
| Total | 170 | Sem banco operacional |

Os 78 testes históricos verificam os contratos dos serviços e suas proteções
próprias; não constituem uma verificação do middleware global de acesso, para o
qual seus fixtures antigos não têm as permissões atuais. A proteção das rotas
não foi flexibilizada. Os testes novos do cadastro/admin/compra e os de venda
foram executados com a política atual.

Cobertura nova: cadastro novo e edição, quatro preços, opção legada marcada,
alteração de custo, custo fracionado explícito e calculado, arredondamento,
admin, compra independente, aprovação conjunta de custo/preços, adiamento,
“manter preços” inválidos, correção parcial recusada e preservação de estoque,
financeiro e auditoria.

O setup reproduzível está no relatório da venda: settings independentes
`sistema.settings_test_compras`, app `inventario` habilitado apenas no processo
de teste, banco exclusivo em TEMP e executor
`inventario.test_runner_confiabilidade.SemMigrationsRunner`. O schema é criado
diretamente; não foi executado comando ou executor de migrations. Para os
contratos históricos de grupos usa-se o middleware original do settings
isolado; para os demais acrescentam-se os middlewares atuais de acesso e LOCAL.

Não houve teste de concorrência PostgreSQL. Permanecem os avisos preexistentes
de namespace `estoque` duplicado e Broken pipe durante encerramento de abas de
teste. Não houve falhas finais nas execuções descritas acima.

## Preservação e limites

Não houve acesso ao Neon, modificação da venda #1278, envio das operações antigas,
limpeza de IndexedDB ou alteração de histórico de compras. Os trabalhos de
locações e a alteração local de `sales-closures.js` foram preservados. Sem
migrations, commit, push ou deploy.

Os cadastros existentes não foram corrigidos em lote. Uma venda offline continua
dependendo da referência que foi preparada: alterações posteriores de custo no
servidor não podem ser conhecidas sem conexão, e a validação final do backend
continua necessária.
