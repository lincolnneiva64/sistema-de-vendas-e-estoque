# Extrato consolidado — avaliação local

Base: `b99c6d87c1555329d3aa37315013e3f86af4c4c2`.
Worktree: `.worktrees/conferencia-tipografia-b99c6d8`.
Preserva a tipografia e a projeção do saldo inicial da etapa anterior.
Implementação aprovada para commit e push controlados. Deploy manual pelo proprietário;
sem migrations ou mudanças de configuração. Ajustes reais continuam desabilitados.

## Edições de vendas

O avaliador continua exigindo snapshots completos, consistentes, na mesma unidade
documental e histórica e sem edição do alvo. Edições de outros produtos só são
atribuídas quando o nome exato consta independentemente em outro SKU atual ou
removido da venda, excluindo nomes coincidentes com o alvo.

Dois textos conhecidos de `alteracao_separacao_atendida` podem encerrar uma
sequência de edições atribuídas a outros produtos:

- `Alteracoes apos separacao atendidas.`
- `Ajustes da separacao aplicados pela edicao da venda.`

O resumo não fornece identidade de item. Exige edição atribuída anterior ao seu
horário; cada resumo adicional exige nova edição desde o resumo anterior. Resumo
isolado, texto desconhecido, edição do alvo, nome ambíguo, evento não reconhecido,
evidência truncada ou conversão ausente continuam impedindo certificação. Não se
usa a conversão atual. O ajuste limita-se à classificação da evidência; a lógica
comercial e as operações de estoque não são alteradas.

## Ajustes manuais

`MovimentacaoEstoqueManual` integra o UNION cronológico e a paginação global de
50 registros, sem carregamento por linha. A data é a do registro manual. Cor
roxa e nome explícito distinguem a fonte. Entrada, saída e saldo físico ficam
indisponíveis porque o registro não congela unidade histórica. Antes/depois brutos,
motivo e observação ficam nos detalhes e numa página GET de consulta protegida
pelo mesmo `gestor()` do extrato, limitada ao produto da rodada/conferência.

Somente um observador com origem `ajuste_manual`, referência textual exata
`Movimentação manual #ID`, mesmo produto e mesmos valores brutos antes/depois é
representado pelo documento manual. O documento também precisa estar dentro do
intervalo. Referência desconhecida, valores divergentes ou documento fora do
período não são mesclados. Não há associação aproximada por data, quantidade ou
nome; nenhuma fonte é somada como movimento adicional.

Os manuais continuam obstáculos para reconstrução/continuidade. O modo completo
passa a considerar sua data mínima. Sem compra datada, o padrão de 30 dias permanece
identificado, com link explícito para ampliar ao histórico completo. Intervalos e
paginação de compras existentes permanecem disponíveis.

## OK terminal

O resumo físico existente foi movido para o último cartão do extrato, sem duplicar
os números. Aparece depois da última página de movimentos; nas anteriores, um aviso
informa sua localização. Mantém data/hora do OK, esperado, contado, diferença e
unidade congelada da conferência. Se o fim do período for anterior ao OK, isso é
identificado explicitamente. Sem OK, permanece a indicação de ausência de confirmação.
O cartão não integra entradas/saídas, cálculos ou contagem de movimentos e não
grava uma nova confirmação. A/B usam os mesmos dados; B permanece o padrão.

## Verificação real indispensável, somente SELECT

Foi reutilizada a configuração protegida existente, sem copiar credenciais.
TLS, identidade e `transaction_read_only=on` confirmados. Uma guarda adicional
recusou qualquer SQL que não começasse por SELECT. Foram 63 SELECTs, incluindo
identidade, com rollback e fechamento. Nenhuma escrita de produção.

| Produto/documento | Antes | Depois | Resultado do avaliador novo |
|---|---:|---:|---|
| Trigo #223, compra #116 | — | — | Não certificado: edição do próprio produto na referência posterior |
| Trigo #223, compra #167 | 0,200 FD | 1,200 FD | Reconstruído e corroborado: vendas #610/#698 |
| Trigo #223, compra #174 | 0,200 FD | 2,200 FD | Reconstruído e corroborado: vendas #698/#822 |
| Trigo #223, venda #698 | 1,200 FD | 0,200 FD | Snapshot registrado, trecho corroborado |
| Trigo #223, venda #822 | 2,200 FD | 1,200 FD | Snapshot registrado, trecho corroborado |
| Trigo #223, venda #845 | 1,200 FD | 0,200 FD | Snapshot registrado, compatível com anterior |
| Trigopan #1118, compra #116 | 2 UN | 102 UN | Reconstruído e corroborado |
| Bril #1119, compra #116 | 4 UN | 44 UN | Reconstruído e corroborado |

Esses valores são resultados da verificação, nunca constantes no aplicativo. O OK
do trigo permaneceu esperado 0,200 FD e contado 0 FD. A conversão KG/FD não comprovada
da venda #529 e outros eventos sem atribuição continuam bloqueados. Corroboração
documental não certifica integralmente movimentações antigas não auditadas.

## Validação

Revisão final para publicação: **92 testes aprovados em 33,420 s**, incluindo Chrome
A/B em 1366/390/320 px, no cluster descartável `inventario-confiabilidade-pg-2z3xw2t2`,
encerrado. Sem acesso ao Neon nesta revisão. Rota manual validada como GET gerencial,
com recusa de POST, acesso anônimo/contador e documento de outro produto. Diff limitado
aos 18 arquivos de inventário, estilos, testes e documentação; artifacts excluídos.

**92 testes aprovados**, em 31,964 s, cluster PostgreSQL descartável
`inventario-confiabilidade-pg-9759hyo4`, encerrado, sem executor de migrations:

```
python -B -m inventario.validar_confiabilidade_postgresql inventario.tests_extrato_consolidado inventario.tests_saldos_historicos inventario.tests_historico inventario.tests_painel inventario.tests_periodos_historico inventario.tests_saldo_inicial
```

Inclui as 77 regressões anteriores e 15 testes consolidados: cenários fictícios
equivalentes ao Trigopan, Bril, trigo FD com três compras, saco de trigo com
conversão ausente e produto sem compra; edições alheias, resumos isolados/invertidos,
alvo alterado, nome ambíguo, devoluções/cancelamentos, manuais, deduplicação,
permissões/GET sem escrita, OK na última página e referência fora do período.

A fixture de snapshots agora usa a data comercial no fuso local: a execução após
00h UTC revelou uma dependência do horário nos testes, corrigida sem afrouxar o
critério de abertura. Chrome validou A/B em 1366/390/320 px, documentos de compra,
venda e manual, detalhes fechados, números sem overflow e um único OK terminal.
`git diff --check` aprovado. Avisos preexistentes: namespace estoque duplicado e
pasta staticfiles ausente no ambiente descartável; os estáticos foram servidos
pelo servidor de testes.

Após o último refinamento CSS, os três testes Chrome foram repetidos e aprovados
em 20,767 s, no cluster descartável `inventario-confiabilidade-pg-8x4wmz5c`, encerrado.

Prévia fictícia: `artifacts/extrato/extrato-b-{1366,390,320}.png`. Capturas e scripts
pontuais em artifacts não integram os arquivos da aplicação.

## Arquivos

- `inventario/evidencia_saldos.py`, `saldos_historicos.py`: atribuição de eventos.
- `inventario/extrato.py`, `historico.py`, `periodos_historico.py`: fontes e contexto.
- `inventario/views.py`, `urls.py`: consulta GET do registro manual, com guarda existente.
- Templates `historico.html`, `historico_conferencia.html`, `movimento_manual.html`.
- CSS `historico.css`, `historico_b.css`.
- Testes `tests_extrato_consolidado.py`, `tests_historico.py`,
  `tests_saldos_historicos.py`, `tests_saldo_inicial.py`.
- Este documento e a documentação anterior de tipografia/saldo inicial.

Models, settings, serviços comerciais, autenticação e contagem cega preservados.
Conferência nº 1, compras, vendas e estoque de produção intactos.
