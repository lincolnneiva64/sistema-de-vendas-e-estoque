# Compras e boletos — primeira etapa

Base conferida: branch `backup/wip-pedidos-base-2026-06-04`, HEAD
`5b0f096bb231fd206f26149bb28c47907790727f`.

## Implementacao

O formulario original `compras_nova.html` oferece o mesmo painel de boletos
para compras diretas e compras por lista, quando o pagamento e a prazo.
Os rascunhos diretos armazenam a programacao em `Compra`/`ParcelaCompra`;
as listas continuam armazenando seus registros em `ListaCompraFornecedor`/
`ParcelaNotaListaCompraFornecedor`. As duas origens compartilham a validacao,
o salvamento do formulario e a rotina de criacao de Contas a Pagar.

O salvamento normal do rascunho e o salvamento anterior a revisao de precos
persistem os boletos sem lancar obrigacoes, pagamentos ou estoque. Valores,
vencimentos e observacoes sobrevivem a recarga e ao retorno da revisao.
Mudar um rascunho direto para pagamento imediato limpa seu plano de boletos.
O historico da nota da lista nao e eliminado quando o pagamento muda.

O backend exige de 1 a 12 boletos (minimo 2 em varios boletos), valores
positivos e finitos, vencimentos, observacoes de ate 120 caracteres e soma
exatamente igual ao valor financeiro da nota. Preserva-se a regra existente
das listas de permitir valor financeiro diferente do total dos produtos.
O primeiro vencimento e refletido em `Compra.data_vencimento`.

A criacao de contas bloqueia a compra dentro de uma transacao e compara o
conjunto existente com o plano. Um conjunto completo nao e duplicado;
um conjunto incompleto, cancelado ou divergente causa recusa e rollback.
A rota Nova Compra tambem reutiliza o token de fechamento em reenvios.
As protecoes de status e de entrada de estoque impedem que envios atrasados
recriem itens ou movimentem novamente compras finalizadas.

## Migration aditiva

`estoque/migrations/0133_parcelas_compra_direta.py` adiciona tres campos
de programacao em Compra e cria ParcelaCompra. A tabela nova possui
unicidade de numero por compra, numero entre 1 e 12 e valor positivo.
Nao ha conversao de dados, exclusao de registros ou alteracao das parcelas
existentes das listas ou das Contas a Pagar.

A migration foi aplicada somente nos bancos descartaveis de testes. Um
teste volta ao estado 0132, cria registros legados, aplica 0133 e verifica a
preservacao da compra finalizada, itens, snapshot, Conta a Pagar e parcela
da lista. Nenhuma migration foi aplicada ao banco operacional.

## Isolamento e verificacao

`sistema.settings_test_compras` e independente dos settings de producao e
nao carrega DATABASE_URL. Define SQLite em memoria e banco de testes em
diretorio temporario exclusivo. `CompraTestRunner` verifica modulo de
settings, alias, engine e caminho antes de criar o banco. O runner adapta
somente o ALTER COLUMN legado da migration 0124, pois SQLite nao limita
varchar; os arquivos de migrations anteriores nao foram alterados.

Comando da verificacao final (PowerShell):

```powershell
$env:COMPRA_TEST_CHROME = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
python -u manage.py test estoque.tests_compra_boletos estoque.tests_compra_pre_revisao.CompraPreRevisaoTests estoque.tests_compra_pre_revisao.CompraPreRevisaoBrowserTests estoque.tests_correcao_compra --settings=sistema.settings_test_compras --noinput --verbosity=1
```

Resultado: **47 testes passaram**, incluindo compras diretas com 2, 3 e
12 boletos, listas, rascunhos, valores personalizados, revisao desktop/mobile,
retorno e recarga, pagamento imediato, finalizacao repetida, intercalacao de
envios, rollback de parcelas e contas, unicidade e migration aditiva.
Chrome/Edge inicialmente encerraram a conexao dentro do sandbox. Os testes
headless foram executados com sucesso fora desse sandbox, mantendo servidor
local e o mesmo banco temporario protegido pelo runner.

Comando da regressao ampliada:

```powershell
python -u manage.py test estoque.tests.FechamentoCompraFinanceiroTests estoque.tests.ComprasListaFornecedorConferenciaTests estoque.tests.CorrecaoItensCompraTests estoque.tests.CorrecaoFinanceiroCompraTests estoque.tests.CorrecaoOrigemCompraTests estoque.tests_revisao_precos_posterior estoque.tests_precos_seletivos.PrecosSeletivosTests --settings=sistema.settings_test_compras --noinput --verbosity=1
```

Resultado final: **214 executados, 200 passaram, 13 falhas e 1 erro**.
Para comparar, views.py e o template original foram lidos com `git show HEAD`
e carregados somente em memoria em outro processo, com o mesmo banco isolado
e a mesma selecao de testes. O baseline apresentou 14 falhas e 1 erro;
todos os 14 casos que continuam falhando na implementacao tambem falharam
no baseline. A verificacao nao representa uma suite legada inteiramente verde.

Casos preexistentes ainda falhando:

- FechamentoCompraFinanceiroTests: baixa de parcela com juros; seis casos de
  cartao credito; revisao de preco com autorizacao; textos/layout do modal e
  da linha provisoria; avisos de visitas na tela de vendas.
- ComprasListaFornecedorConferenciaTests: resumo da conferencia, link
  externo e layout mobile de edicao da lista.

`makemigrations estoque --check --dry-run --settings=sistema.settings_test_compras`
retornou **No changes detected**; `git diff --check` passou. O aviso de namespace
`estoque` duplicado tambem aparece no baseline e nao foi tratado nesta etapa.

## Limites e proxima etapa

- Nao foi implementada a conversao financeira de compras ja finalizadas,
  nem a correcao da compra operacional numero 186.
- As telas antigas de correcao financeira de compras finalizadas continuam
  com as limitacoes apontadas na auditoria. O novo plano direto nao e
  sincronizado por essas telas; a correcao de multiplas contas diretas e a
  conciliacao de pagamentos exigem a segunda etapa.
- SQLite permite testar rollback, reenvios e intercalacao deterministica,
  mas nao comprova bloqueios reais de linhas do PostgreSQL. Essa verificacao
  precisa de PostgreSQL descartavel local, sem Neon, em etapa futura.
- A aplicacao do novo codigo em outro ambiente exige a migration 0133;
  nao houve publicacao nem aplicacao operacional nesta entrega.

Nao houve acesso a Neon/producao, alteracao do modulo de vendas/offline,
commit, push ou deploy. `separacao_vendas_fila.html` manteve a alteracao
preexistente, e os arquivos nao rastreados e backups anteriores foram
preservados. A segunda etapa depende de nova autorizacao.
