# Rodape das contas a pagar — preparado para revisao

Base: `b65b06baf3c4cc82df5e3122a1276372182a0a57`, versao atual de
`origin/backup/wip-pedidos-base-2026-06-04` consultada nesta implementacao.
Worktree isolada: `.worktrees/contas-pagar-rodape` (HEAD destacado).
Sem commit, push, deploy, migrations novas ou alteracoes em dados operacionais.

## Comportamento

- Rodape desktop em `tfoot`, com valores alinhados pela mesma grade das linhas.
- Celular: resumo apos o ultimo cartao em grade de duas colunas, sem nova rolagem horizontal.
- Quantidade e totais de todos os resultados filtrados, sem paginacao na pagina atual.
- Soma a lista final usada na renderizacao, apos todos os filtros e a deduplicacao.
- Original usa o total ja calculado pela pagina. Pago soma o principal registrado;
  juros sao somados separadamente, como nas colunas existentes. Pagamentos cancelados
  ficam excluidos. Saldo usa `valor_em_aberto`, preservando ajustes ja registrados.
- Uma conta quitada legada sem historico nao recebe um pagamento presumido: ha aviso.
- Sem contas, o rodape e omitido e a mensagem existente de nenhum resultado e mantida.
- O fluxo existente de baixa recarrega a pagina com os filtros ativos, recalculando o rodape.

Os cartoes superiores conservam o escopo existente: visao geral do fornecedor/busca,
e juros por data de pagamento. O novo rodape identifica o seu escopo como soma das
contas listadas. Por isso, com filtros de datas, situacao ou atalhos, cartoes e rodape
podem apresentar conjuntos diferentes; as regras dos cartoes nao foram alteradas.

## Validacao

PostgreSQL **17.11**, cluster novo em diretorio temporario e porta dinamica em loopback.
O runner recusa conexoes fora desse cluster e bloqueia o executor de migrations.
O schema sintetico foi criado diretamente dos modelos. Servidor encerrado ao final.

- **8 testes novos passaram**: fornecedor com tres contas, parciais, quitadas,
  abertas e canceladas; compra/vencimento, busca/documento e atalhos; juros,
  pagamentos cancelados e deduplicacao; baixa real sintetica e nova consulta;
  legado, consulta sem mutacoes financeiras e resultado vazio; navegador.
- **14 testes existentes passaram** na primeira execucao: filtros (4), periodo (3)
  e encargos (7). Os quatro problemas iniciais dos testes novos foram corrigidos
  (formatacao/localizacao, texto e saldo da fixture de pagamento). Depois foram
  repetidos somente os oito testes novos, todos aprovados.
- Nova execucao apenas do teste de navegador aprovou as capturas finais.
- Chrome real em **1440, 390 e 320 px**: rodape depois da ultima conta, valores
  esperados, tres botoes Pagar/Baixar e dois Historicos; sem overflow horizontal.
  Alinhamento com a grade de valores validado no desktop.
- Exemplo sintetico Ademar: **3 contas; Original R$ 585,00; Pago R$ 160,50;
  Juros R$ 0,00; Em aberto R$ 424,50**.
- `git diff --check`: aprovado. Nenhum arquivo de migration alterado.

Evidencias em `diagnostics/contas-pagar-rodape/`:
`regressao-inicial.log`, `testes-novos.log`, `validacao-testes-novos.json`,
`tests.log` e `validation.json` (capturas finais), `layout-*.json` e `rodape-*.png`.
Os avisos de namespace duplicado e diretorio staticfiles ausente ja existem
no ambiente de teste e nao impediram os testes.

Para executar os testes desta pagina em outro cluster descartavel:

```powershell
..\..\venv\Scripts\python.exe validate_contas_pagar_local.py
```

O runner local reutiliza o bootstrap com guarda de conexao ja existente em
`offline/validate_gelo_postgresql.py`; usa os binarios PostgreSQL locais existentes.
O runner e os diagnosticos sao recursos de validacao, separados do codigo da pagina.
