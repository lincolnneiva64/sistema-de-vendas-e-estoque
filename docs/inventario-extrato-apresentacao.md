# Extrato: alternativas visuais e seleção de períodos

Base: `c3b79eda5fbfc6a3f992f5cf12932f10f5cc4931`.
Worktree isolada: `.worktrees/conferencia-visual-c3b79ed`.
Publicação do código autorizada após revisão e validação final. Versão B aprovada como padrão e A preservada como alternativa. Sem deploy pelo agente ou migrations; deploy manual pelo responsável no Render.

## Apresentações

A mantém a tabela contínua de cinco colunas, com linhas menores, saldos maiores
e números alinhados à direita. B mantém a identidade de cartões, com marcadores
ligados por uma linha do tempo, data lateral no desktop e saldo em bloco próprio.
As duas apresentam compras verdes, vendas azuis e devoluções/lacunas amarelas,
sempre com rótulos; divergências físicas ficam vermelhas. No celular, as colunas
são identificadas por rótulos sem rolagem horizontal.

Ambas usam o mesmo HTML, a mesma consulta paginada e o mesmo avaliador histórico.
B é o padrão ao abrir o extrato (inclusive para parâmetro visual desconhecido); A continua disponível por `visual=a`. B acrescenta somente `historico_b.css`; o parâmetro `visual=a|b` e os links de
comparação preservam filtros e página. Não há duplicação de lógica de saldos.
Fonte, responsável/fornecedor, saldo anterior, validações e continuidade ficam
em “Detalhes e origem”, fechado por padrão. Snapshot/reconstrução e alertas de
saldo pontual/lacuna permanecem visíveis, sem propagar saldos pela lacuna.

“Ver evolução do estoque” segue disponível em cada linha da tabela gerencial,
abrindo diretamente o extrato. O nome do produto e a investigação administrativa
continuam disponíveis. A comparação física usa esperado, contado e diferença do
OK e sua data, independentemente do período selecionado. Sobras recebem sinal
`+` somente na apresentação.

## Refinamento final da versão B

Cartões com margens e preenchimentos moderadamente menores e linhas internas
mais próximas. Saldos continuam em 29 px no desktop e 24 px no celular; a data,
o dia da semana, a operação e as quantidades são preservados. Cores, detalhes
recolhidos, comparação física, filtros e paginação permanecem disponíveis.
A mudança funcional deste ajuste se restringe ao padrão visual; o seletor de
períodos e o avaliador de saldos não foram alterados.

Validação final deste refinamento: **69 testes aprovados**, em 28,715 s, cluster
descartável `inventario-confiabilidade-pg-y_biqfrj`, encerrado. Chrome conferiu B
como padrão pelo acesso direto, alternância para A, detalhes fechados, links,
alinhamento e prévias de ambas em 1366/390/320 px. Teste adicional confirma o
padrão B, A explícita e tratamento de parâmetro visual desconhecido sem mudar
o período. `git diff --check` aprovado. As capturas `extrato-b-{1366,390,320}.png`
contêm a versão refinada, com dados fictícios.

Arquivos deste refinamento: `inventario/historico.py`,
`static/inventario/historico_b.css`, `inventario/tests_historico.py`,
`inventario/tests_periodos_historico.py` e este documento. As alterações
anteriores da worktree continuam preservadas.

## Períodos

- Padrão: última compra recebida datada até a referência da conferência.
- Contexto: última compra e duas compras anteriores, contando documentos distintos
  (mesmo quando uma compra tem vários itens do mesmo produto). Menos de três
  documentos disponíveis são indicados explicitamente.
- Personalizado: datas inclusivas no fuso local; o limite superior é o menor
  entre o início do dia seguinte à data final e a referência da conferência.
  Datas ausentes, inválidas, invertidas ou inteiramente posteriores à referência
  geram erro e nenhum registro; não mudam silenciosamente para outro período.
- Completo: desde o primeiro registro datado disponível nas fontes do extrato,
  incluindo a data comercial para não omitir documentos antigos. Não implica
  cobertura integral nem comprovação de saldo inicial.
- Compra específica: preserva seleção de compras, paginação das opções e término
  na próxima compra ou referência da conferência, inclusive nos links antigos.

Sem compra recebida datada, o padrão é explicitamente os 30 dias anteriores à
referência. Vendas e devoluções são consultadas por produto mesmo sem compra;
snapshots válidos podem ser exibidos como referências pontuais. Não se inventa
compra ou saldo inicial. O JavaScript apenas mostra os campos pertinentes ao modo
e seleciona o modo ao trocar compra/datas. Sem JavaScript todos os campos seguem
disponíveis. O servidor valida o intervalo independentemente do navegador.

Todas as movimentações documentais elegíveis seguem acessíveis, com paginação
global de 50 registros; não há limite de vendas por compra. A seleção de vendas
continua considerando a data comercial, com as ressalvas já existentes para
documentos dos dias de fronteira. O fim exclusivo à meia-noite não inclui o dia
seguinte. Os horários documentais não certificam o momento físico.

## Integridade

`evidencia_saldos.py` e `saldos_historicos.py` permanecem idênticos à base.
Não houve mudança nas validações de snapshots, conversões, cancelamentos,
devoluções, edições, ajustes ou lacunas. `extrato.py` apenas recebe o produto
explicitamente para permitir consulta sem compra. `historico.py` delega a escolha
do intervalo ao novo seletor e conserva a comparação do OK. Sem alterações em
modelos, configurações, views, URLs, autenticação, contagem cega, regras comerciais,
finalização ou recontagem. Ajustes reais continuam desabilitados.

Nenhum acesso à produção nesta etapa. Conferência nº 1 e dados comerciais reais
não foram consultados nem alterados para construir as prévias. Fixtures e
capturas são exclusivamente fictícias em PostgreSQL local descartável.

## Testes e prévias

Revalidação antes da publicação autorizada: **69 testes aprovados**, em 29,972 s,
cluster PostgreSQL descartável `inventario-confiabilidade-pg-md37andt`, encerrado.
Chrome validou B como padrão e A como alternativa em 1366/390/320 px. Revisão
final limitada aos 13 arquivos de interface, períodos, testes e documentação;
capturas, galeria local, temporários, backups e credenciais excluídos do commit.
Nenhuma migration ou alteração de configuração necessária. Neon de produção não
acessado, conferência nº 1 e operações comerciais preservadas.

Python do venv, runner existente com identidade do cluster verificada e sem
executor de migrations:

```
python -B -m inventario.validar_confiabilidade_postgresql inventario.tests_saldos_historicos inventario.tests_historico inventario.tests_painel inventario.tests_periodos_historico
```

**68 testes aprovados**, em 28,684 s, cluster
`inventario-confiabilidade-pg-4galsqf1`, encerrado. Incluem as 58 regressões
anteriores e 10 casos de períodos/equivalência. Chrome validou A e B em
1366/390/320 px, mesmos documentos e saldos, links, cores, detalhes fechados,
filtros, paginação e ausência de sobreposição/rolagem horizontal nas quantidades.
A fixture do teste de equivalência foi depois reforçada com três compras
atravessando uma lacuna: caso repetido e aprovado em 0,321 s no cluster
`inventario-confiabilidade-pg-mogcliby`, encerrado, sem alteração da aplicação.

Avisos preexistentes: namespace `estoque` duplicado e pasta `staticfiles` ausente
no ambiente de teste; estáticos servidos e conferidos no Chrome. `git diff --check`
aprovado. A consulta do extrato segue testada para executar somente SELECT.

Em `artifacts/extrato/`, fora do conjunto de arquivos da aplicação:

- `comparar-a-b.html`: galeria local com A/B lado a lado e links aos originais.
- `extrato-a-{1366,390,320}.png` e `extrato-b-{1366,390,320}.png`.
- `periodos-{1366,390,320}.png`: formulário personalizado e comparação física fixa.
- `acesso-direto-{1366,390,320}.png`: atalho na tabela gerencial.

## Arquivos da alteração

- Python: `inventario/historico.py`, `inventario/extrato.py`,
  `inventario/periodos_historico.py`.
- Templates: `inventario/templates/inventario/detalhe.html`, `historico.html`,
  `historico_filtros.html`.
- Estáticos: `static/inventario/historico.css`, `historico_b.css`, `revisao.css`,
  `extrato.js`.
- Testes: `inventario/tests_historico.py`, `inventario/tests_periodos_historico.py`.
- Este documento.
