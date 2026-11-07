# Fase B.2 — rastreabilidade da regularização física em lote

Integração local do complemento no escritor de lote existente. Nenhuma migration
nova, alteração de modelos ou mudança de fórmula, precisão ou regras de estoque.
Migrations 0009–0011, observador e triggers permanecem intactos.

## Ponto da integração

`estoque_manual_lote.gravar()` continua bloqueando produtos por PK, atualizando
quantidades e identificando movimentos com as mesmas consultas e verificações.
Cada resultado agora também devolve o objeto `movimento` que já foi encontrado
por produto + sequência e conferido contra os saldos. Não há SELECT adicional.

`ajustes._gravar_lote()` chama `rastreabilidade_regularizacao.registrar_lote()`
após o bulk_create das aplicações e antes da transição final das propostas e
eventos. Usa a mesma transação externa de aplicar/atualizar e os mesmos locks.

A preparação de dados é compartilhada com o piloto individual. Confere produto,
sequência, antes/depois, origem `ajuste_manual` e referência exata do manual para
cada aplicação. Todos os detalhes são preparados antes de um único bulk_create.
Não usa ignore_conflicts nem atribui movimentos por horário ou posição da lista:
uma associação trocada falha na verificação e reverte toda a operação.

## Identidade e evidência

Cada aplicação mantém UUID próprio, usado como operação do complemento com ordem
1. O documento aponta para a aplicação imutável; esta preserva lote comum, proposta,
decisão, manual e confirmação original. Referências textuais também conservam
conferência, proposta e rodada. Não se duplica o lote em um novo campo.

Mantidos executor e nome registrado pelo manual; não há seleção independente de
outro operador neste fluxo. Não se infere FK por nome e não se confunde o contador
físico com o executor. Unidade e conversão usam a evidência congelada, com a mesma
semântica do piloto: quantidade de entrada é a contagem física de origem; delta
assinado é derivado dos saldos do observador.

Produtos sem diferença e bloqueados continuam excluídos pela seleção/validação
existente. Se um produto selecionado ficar bloqueado depois da prévia, o lote
inteiro continua recusado. O preparador não produz complemento para saldo igual.

Qualquer erro na identificação, no INSERT de um complemento ou depois dele
reverte todos os produtos, movimentos, aplicações, complementos e eventos criados
na transação. Reenvios retornam as aplicações existentes antes de chegar ao
escritor; não há complementação retroativa de lotes antigos.

## Validação

110 testes aprovados em 41,900 s no PostgreSQL descartável
`inventario-confiabilidade-pg-kqp1sb0h`. Outros 4 testes preexistentes de
`inventario.tests_escritas_lote` aprovados em 7,148 s no cluster
`inventario-confiabilidade-pg-nxsvxeyy`. Ambos encerrados pelo runner protegido.

Cobertura: lote misto com seis produtos (CX, KG, M³, UN/FD, DZ e FD), conversão
de 1.200.000.000 UN, normalização decimal, vínculos exatos, lote compartilhado,
sem diferença, bloqueios, erro real no segundo INSERT, erro após inserir todos,
movimentos trocados, rollback integral, reenvio, legado sem backfill, imutabilidade,
duas aplicações concorrentes e escrita posterior aguardando os locks.

Incluídas regressões do piloto individual, limites, cadeia real de migrations
0009 → 0010 → 0011, ajustes manuais, compras/vendas de teste e confirmação única.
O teste do piloto que antes esperava lote sem complemento foi atualizado para
exigir dois complementos e continuar garantindo que o manual comum não os cria.

```
venv/Scripts/python.exe -B -X utf8 -m inventario.validar_confiabilidade_postgresql inventario.tests_rastreabilidade_lote inventario.tests_limites_auditoria inventario.tests_detalhe_movimento inventario.tests_rastreabilidade_individual estoque.tests_estoque_manual inventario.tests_integracao inventario.tests_confirmacao_unica inventario.tests_ajustes.PreparacaoTests inventario.tests_ajustes.AplicacaoTests
venv/Scripts/python.exe -B -X utf8 -m inventario.validar_confiabilidade_postgresql inventario.tests_escritas_lote
```

## Consultas e desempenho

Comparação com o corpo anterior de `_gravar_lote()` carregado em memória, sem a
chamada de complementação, no mesmo banco, produtos e prévia. Cinco amostras por
variante, ordem alternada, estado restaurado por savepoint entre execuções.
Medição inclui validação/aplicação; não inclui o commit externo. Não é benchmark
de produção e não permite prever latência em outro ambiente.

| Produtos | Consultas antes | Consultas depois | Mediana antes | Mediana depois |
|---|---:|---:|---:|---:|
| 2 | 35 | 36 | 37,854 ms | 39,739 ms |
| 20 | 53 | 54 | 98,077 ms | 99,921 ms |

Acréscimo constante de um INSERT, sem SELECT por complemento e sem novos
SELECT FOR UPDATE. O teste compara a sequência exata desses bloqueios entre as
variantes. O crescimento de consultas do fluxo anterior com o número de produtos
foi preservado; não foi introduzido nem otimizado nesta etapa.

Autodetector confirmou ausência de migration pendente por divergência de modelo.
`git diff --check` aprovado. Avisos preexistentes: namespace `estoque` duplicado
e diretório `staticfiles` ausente. Não se declara aprovação de toda a suíte.

## Limites e segurança

Não foram instrumentados compras, vendas, devoluções, consumo, sincronização
offline ou manuais comuns. A obrigatoriedade não é global: abrange apenas as
regularizações físicas individuais e em lote agora integradas. A futura publicação
continua exigindo migrations 0009–0011 antes do escritor, sem remover a auditoria
em um retorno operacional. Lotes máximos de 500 não foram cronometrados aqui;
a comparação de desempenho usou 2 e 20 produtos.

Não houve acesso a Neon/Render, banco/estoque real, commit, push ou deploy.
Alterações locais anteriores de inventário, visuais, locações e outros módulos
preservadas. Nenhuma incompatibilidade estrutural identificada no escopo testado.
