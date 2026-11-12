# Situação da atualização e contagem original

Correção exclusivamente de apresentação, em worktree isolada baseada em
`ecd0486`. A diferença comparável continua vindo de `comparacao_atual`, sem
recalcular ou usar a diferença histórica como substituta.

- Diferença zero sem aplicação: **Contagem OK · Marco pendente**, inclusive
  quando houver ressalva documental; não recebe estilo de ajuste bloqueado.
- Marco aplicado sem correção: **Marco aplicado**.
- Correção aplicada registrada no marco: **Ajuste aplicado**, mesmo quando a
  diferença atual já for zero.
- Diferença comparável não zero sem aplicação: **Correção pendente**.
- Projeção desconhecida: a coluna Contado preserva o texto original do OK e
  sua unidade, identificado como contagem original, com **Projeção não comprovada**
  separadamente. Diferença e estimativa continuam não comparáveis.

As ressalvas detalhadas permanecem no painel recolhido. Sem alterações em CSS,
cálculos, serviços, estoque, confirmações, movimentos ou regras do marco zero.

Validação: cinco testes de apresentação aprovados (0,132 s), sem banco,
com Engine de templates e contextos fictícios. Cobrem também sete colunas,
popover recolhido e preservação dos dados de entrada. Comando, a partir da worktree:

```powershell
../../venv/Scripts/python.exe -X utf8 -m unittest inventario.tests_situacao_apresentacao.SituacaoApresentacaoTests -v
```

`git diff --check` aprovado. Não foi consultada a conferência operacional nº 42
nem executado teste em navegador. Sem commit, push, migrations ou deploy.
