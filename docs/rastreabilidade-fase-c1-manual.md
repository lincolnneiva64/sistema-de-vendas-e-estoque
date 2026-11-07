# Fase C.1 — ajustes manuais pela tela de Vendas

Integração local somente em `conferencia_estoque_corrigir`. Não instrumenta a
gravação de vendas, compras, consumo, devoluções, offline ou chamadas diretas ao
serviço manual. As regularizações individuais/em lote mantêm seus escritores e
ação específicos, sem um segundo complemento manual.

## Transação, identidade e dados

O serviço `conferir_ou_ajustar_estoque()` recebe um callback interno opcional,
habilitado apenas pelo endpoint Corrigir estoque. Executa-o antes de sair de
sua transaction.atomic, conservando o select_for_update do produto. Não muda
quantidades, validações, exigência de motivo, marcação de conferido ou payload
da resposta. O botão de confirmação sem correção não habilita o callback.

`estoque/services/rastreabilidade_manual.py::registrar()` busca o observador exato
com produto, sequência corrente via subquery, saldo original, saldo posterior,
origem `ajuste_manual` e referência exata do novo movimento manual. A sequência
é lida sob o bloqueio já adquirido e após o único UPDATE de quantidade.

O complemento usa ação `ajuste`, documento `movimentacao_estoque_manual` e seu ID.
O UUID da operação é derivado deterministicamente do ID desse documento; ordem 1.
OneToOne e unicidade operação/ordem permanecem. Produto, instante e saldos não
são duplicados: diferença assinada vem dos valores brutos do observador.

O executor é o usuário retornado pela autorização existente de estoque, inclusive
quando diferente do usuário da sessão comercial. O operador declarado vem do
header X-Operador-Venda ou do campo operador existente no JSON. É declaração,
não autenticação; não se infere FK por nome. O nome antigo do histórico manual
continua sendo o executor, preservando o comportamento da resposta existente.
O operador fica exclusivamente no campo próprio, respeitando o limite de 150
caracteres. Nenhuma validação comercial nova foi acrescentada.

O ajuste informa diretamente um saldo na unidade base. Assim, quantidade_entrada
é o saldo-alvo, conversao é nenhuma e fator é nulo. O fator do cadastro não é
copiado como se tivesse participado de uma conversão inexistente. A unidade é
capturada do produto bloqueado; cadastros antigos sem unidade são explicitamente
marcados no campo de unidade como Não informada, sem assumir UN.

Para saldo anterior NULL, o comportamento legado do serviço e de seu histórico
é preservado. A auditoria usa o antes=NULL verdadeiro do observador e seu delta
permanece desconhecido. Apenas esse novo evento recebe a atribuição correta com
o identificador existente; não há alteração de movimentos históricos.

Sem mudança efetiva não há complemento. O histórico manual sem alteração continua
sendo criado como antes. Repetir o mesmo saldo-alvo não duplica observador ou
complemento; uma alteração efetiva posterior constitui outro evento independente.
Falhas antes/depois do complemento ou em CHECK de banco revertem estoque,
marcação de conferido, histórico, sequência, observador e complemento na transação.
Não se classifica a redução como perda, furto ou quebra.

## Evidência imutável e migration autorizada

O proprietário autorizou um JSON opcional para congelar motivo/observação, pois
o registro manual original é editável. `0012_evidencia_complementar_movimento`
adiciona somente `evidencia` ao complemento, default dict e db_default {}. Não
preenche motivos antigos, não cria movimentos e não altera migrations 0009–0011.
Os registros existentes recebem a representação vazia de evidência não capturada,
sem modificar seus campos anteriores; escritores antigos podem omitir a coluna.

Conforme a condição final do proprietário, o JSON guarda exclusivamente as chaves
`motivo` e `observacao`, copiadas do histórico no momento do ajuste. Operador,
unidade, saldos, datas e demais metadados não são repetidos nesse JSON. As proteções
de imutabilidade da tabela já existente também protegem a nova coluna. Editar o
histórico manual não modifica a evidência congelada.
Não há trigger nova nem mudança de trigger.

## Testes e desempenho

Após restringir o JSON exclusivamente aos dois textos: 79 testes aprovados em
20,401 s, cluster descartável inventario-confiabilidade-pg-k05ngp66, encerrado.
Cobertura adicional de aspas, acentos, emoji, barras, apóstrofo, tabulação e quebra
de linha; igualdade exata com os textos existentes; JSON sem chaves extras;
textos vazios; edição posterior do manual sem alterar a cópia; imutabilidade e
rollback antes/depois do complemento e por falha real de CHECK. Incluídos os
endpoints antigos, migration aditiva e regularizações individual/em lote.
A migration 0012 continua sendo somente AddField opcional com default vazio;
não foi criada outra migration nem reescrita auditoria anterior.

Na validação anterior desta fase: 162 testes aprovados em 51,066 s, PostgreSQL descartável
inventario-confiabilidade-pg-h9yg5sr3, encerrado pelo runner protegido. Foram 17
testes novos (incluindo migration), 31 testes existentes de endpoints e 114
regressões anteriores de auditoria, limites, regularizações e fluxos comerciais.

Cobrem 23→20 UN, aumentos, motivo vazio, textos longos, unidade discreta/contínua/
especial/ausente, saldo anterior desconhecido, ausência de mudança, reenvios,
eventos sucessivos, operador diferente do executor, autorização temporária com
executor diferente da sessão, CSRF, permissões, falhas antes/depois da gravação,
CHECK real com rollback, imutabilidade e compatibilidade das regularizações.
A migration real foi aplicada no banco descartável com registro legado intacto
e inserção posterior pelo modelo histórico, omitindo evidencia, aceita pelo default.

```
venv/Scripts/python.exe -B -X utf8 -m inventario.validar_confiabilidade_postgresql inventario.tests_rastreabilidade_manual estoque.tests_estoque_manual_endpoints inventario.tests_rastreabilidade_lote inventario.tests_limites_auditoria inventario.tests_detalhe_movimento inventario.tests_rastreabilidade_individual estoque.tests_estoque_manual inventario.tests_integracao inventario.tests_confirmacao_unica inventario.tests_ajustes.PreparacaoTests inventario.tests_ajustes.AplicacaoTests inventario.tests_escritas_lote
```

Comparação do endpoint com/sem callback, somente em memória: cinco amostras por
variante, alternadas, mesma pré-condição restaurada por savepoint. Saldo conhecido:
13→15 consultas, mediana 9,593→11,456 ms (+1,863 ms). As duas consultas novas são
SELECT do observador com subquery e INSERT do complemento; SELECT FOR UPDATE e
sua ordem permaneceram iguais. Não é previsão de latência em produção. O caso
de saldo original NULL requer atribuição adicional, fora dessa medição.

As fixtures das migrations anteriores usam modelos históricos para testar seus
respectivos esquemas e restauram as versões posteriores antes dos próximos testes.
Não foi alterado código comercial para contornar falhas. O ERROR de CHECK emitido
no teste de rollback foi provocado intencionalmente; a suíte terminou OK.

Autodetector sem divergência modelo/migration e git diff --check aprovado. Avisos
preexistentes: namespace estoque duplicado e diretório staticfiles ausente.

## Limites antes de publicação futura

A migration 0012 deve preceder o escritor em uma publicação autorizada. Evidência
de auditoria não deve ser removida em rollback operacional. Cadastros sem unidade
e saldos desconhecidos continuam identificados como desconhecidos; a auditoria
não reconstrói esses dados. O operador declarado não passa a ser identidade
autenticada nem fornece permissões.

Sem acesso a Neon/Render, bancos ou saldos reais, commit, push ou deploy. Trabalhos
locais de locações, inventário e melhorias visuais preservados. Não se avançou
para compras ou vendas nem se ativou obrigatoriedade global de complementos.
