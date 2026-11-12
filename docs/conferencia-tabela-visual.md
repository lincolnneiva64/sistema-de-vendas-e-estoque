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


## Equivalências visuais e saldo aplicado — 08/11/2026

Abaixo de Estoque atual e Contado, a tabela apresenta a equivalência quando há
unidades diferentes e fator positivo finito. Estoque atual usa o cadastro atual;
Contado usa a conversão congelada na conferência. O total em UN já mostrado
pela composição física não é repetido. Não há novas consultas por linha.

O arredondamento visual em KG exige embalagem FD/CX/PCT/DZ, destino KG e
composição explícita N/1Kg cujo número corresponda ao fator cadastrado.
Avulsos registrados com peso fracionado impedem esse arredondamento. Nome sem
fator/unidades compatíveis, granel e composições de 1,5Kg/0,250Kg preservam
as casas decimais. Usa-se o inteiro mais próximo (meio para cima), somente no
texto secundário, com sinal de aproximação e equivalência exata no atributo title.
Valores principais, fatores, contagens, diferenças e estimativas permanecem intactos.

Saldo aplicado aparece na coluna de atualização apenas com estado Aplicado,
saldo_aplicado disponível e unidade_aplicada. Nunca usa a contagem como fallback.

Validação desta etapa: 16 testes locais sem banco e medição do HTML renderizado
no Chrome em 320/390/1366px, com estados aplicado/bloqueado, equivalências abaixo
dos valores principais e Editar contagem preservado, sem rolagem horizontal.
Nenhum acesso a banco operacional, migration, commit, push ou deploy.


### Coluna Atualização do estoque

Aplicado mostra apenas o saldo e a unidade registrados pela aplicação; bloqueado
ou pendente destaca diretamente a quantidade física já existente, sem o prefixo
Contagem encontrada. Quantidades aplicadas e contadas compartilham a mesma
tipografia, cor e alinhamento; a equivalência permanece menor abaixo.
Sem contagem disponível, nenhum número é acrescentado. A equivalência abaixo usa
o valor que está sendo mostrado: saldo aplicado ou contagem, nunca um pelo outro.
Conversão congelada e regras visuais anteriores são reutilizadas; se o saldo já
estiver na unidade de destino, não se repete a unidade nem se aplica fator novamente.
Status, Ver detalhes e motivos preservados, sem consultas adicionais ou mudança
de regras. Validação: 20 testes de apresentação, sem banco.
