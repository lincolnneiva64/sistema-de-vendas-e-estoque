# Tabela da conferência — apresentação compacta

Etapa exclusivamente visual, local. Preservadas todas as alterações anteriores.
Nenhuma alteração em cálculos, conversões, serviços, auditoria, modelos ou migrations.

## Apresentação

- Composição principal em pacotes + avulsos e total menor abaixo, sem a expressão
  longa com sinal de igualdade. Os grupos número/unidade não se quebram isolados.
- Saldo atual preservado; botão Editar contagem permanece abaixo do contado.
- Linguagem operacional: Sem diferença, Diferença de arredondamento, Falta/Sobra
  em UN, Ajuste pendente, Ajuste aplicado, Ajuste bloqueado ou Sem ajuste necessário.
- Os rótulos traduzem textos/estados existentes; não recalculam nem autorizam nada.
  Somente o estado existente Bloqueado recebe o rótulo Ajuste bloqueado.
- Ver detalhes abre um popover nativo acessível por clique, toque e teclado,
  com identificação do produto, botão Fechar, Escape e fechamento ao clicar fora.
  Altura limitada e rolagem própria; abrir o painel não aumenta a linha da tabela.
  Os valores técnicos, justificativas e evidências continuam no painel. O link
  para a auditoria fecha o popover e conserva a abertura do histórico existente.
- Mantidos tabela no desktop e blocos responsivos no celular. A aprovação em
  navegador real ainda está pendente, conforme a limitação abaixo.

## Arquivos desta etapa

- `inventario/templates/inventario/detalhe.html`: reutiliza a linha extraída.
- `inventario/templates/inventario/linha_tabela.html`: linha compacta, mesmas ações
  e atributos usados pelos scripts existentes.
- `inventario/templates/inventario/quantidade_tabela.html`: composição e total.
- `inventario/templates/inventario/detalhes_tabela.html`: painel fora do fluxo da linha.
- `inventario/templates/inventario/situacao_atualizacao.html`: estados curtos e acesso
  aos detalhes, sem declarar que uma aplicação ausente foi uma tentativa recusada.
- `inventario/templates/inventario/representacao_detalhes.html`: parâmetro opcional
  para abrir seu conteúdo técnico dentro do painel; demais usos preservados.
- `inventario/templates/inventario/editar_contagem_admin.html`: omite apenas o aviso
  repetitivo de contagem bloqueada na tabela compacta; aviso preservado nos detalhes.
- `inventario/templatetags/__init__.py` e `conferencia_visual.py`: filtros de texto
  e agrupamento visual com escape de HTML, sem operações numéricas.
- `static/inventario/revisao.css`: colunas, espaçamento, grupos e popover limitado.
- `static/inventario/revisao.js`: fecha o popover ao seguir o link da auditoria.
- `inventario/tests_tabela_visual.py` e este documento.

## Validação

**8 testes aprovados, sem banco**, com Engine de templates e contextos fictícios:

```powershell
venv/Scripts/python.exe -X utf8 -m unittest inventario.tests_tabela_visual.TabelaVisualTests -v
```

Cobrem Amstel, Coca-Cola, Fanta, CX, DZ, pacotes sem avulsos, ausência de diferença,
sobra, ajuste aplicado/pendente/bloqueado, composição e total separados, conservação
dos dados de entrada, ausência de jargão na linha visível, dados técnicos preservados,
alvos/identificadores acessíveis de abertura e fechamento e escape de HTML.
`git diff --check` aprovado. Não foram repetidas suítes financeiras/comerciais.

As regras de CSS desktop/mobile e a estrutura do popover foram verificadas no código;
isso não substitui teste real de geometria, foco, toque ou fechamento em navegador.
Node não está disponível para uma checagem independente de sintaxe do JavaScript.
A alteração de JavaScript foi limitada a fechar o popover pelo link do histórico.

## Inspeção visual pendente — porta 8000

O servidor existente está ouvindo em 127.0.0.1:8000. Não foi possível confirmar seu
banco de destino a partir do comando do processo, que não informa `--settings`.
Não foram abertas páginas dinâmicas nem reiniciado o servidor, para não provocar
acesso involuntário ao Neon. Não foram criados navegadores, perfis, usuários, senhas
ou outra prévia nesta etapa. Capturas da etapa funcional anterior não representam
este novo layout.

Após confirmar que o servidor habitual utiliza somente banco local, abrir a mesma
conferência na porta 8000, sem confirmar contagens ou ajustes, e conferir:

1. Amstel: Diferença de arredondamento, composição e 85 UN no total.
2. Coca-Cola/Fanta: Falta 1 UN, com totais físicos em linha secundária.
3. Produtos sem diferença, aplicados, CX e DZ: valores e ações preservados.
4. Desktop e celular: alinhamento, grupos de quantidade e ausência de sobreposição.
5. Ver detalhes por clique/toque/teclado; Fechar, Escape e clique fora; a linha não
   deve crescer. Abrir a auditoria deve manter o comportamento anterior.

A nova biblioteca de tags requer recarga do Django; se o servidor ainda não a
reconhecer, recarregar o servidor habitual somente após confirmar seu banco local.
Sem migrations, commit, push ou deploy. Aprovação visual permanece pendente.
