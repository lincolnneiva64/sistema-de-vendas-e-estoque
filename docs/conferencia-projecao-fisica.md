# Conferência: contagem física, projeção decimal e aplicação auditada

Implementação exclusivamente local em 05/11/2026. Sem migrations necessárias:
utiliza as contagens existentes e os JSONs de evidência já protegidos da auditoria.
Nenhum banco operacional foi consultado. Alterações locais anteriores preservadas.

## Regra implementada

O bloqueio comparava `contado_base × fator == unidades físicas`. Frações como
23/6 e 85/12 não passam nessa igualdade quando o estoque tem três casas decimais.
A validação agora exige que a projeção seja exatamente aquela produzida por
`converter()` a partir das embalagens e dos avulsos originais, usando somente a
relação congelada na conferência. Não existe tolerância configurável.

Para UN discretas, ficam separados:

- Embalagens e avulsos originais e total inteiro em UN.
- Contagem com nove casas e projeção operacional com três casas.
- Resíduo exato: `total_UN − projeção × fator`, também registrado como numerador
  e denominador em PCT, sem depender de uma divisão decimal finita.
- Referência esperada, equivalência inteira, compatibilidade com a projeção
  canônica e eventual resíduo da própria referência.
- Diferença física quando a referência a permite; diferença decimal sempre
  preservada separadamente. Referências não canônicas são apresentadas com ≈.

Um total esperado só recebe equivalência inteira sem aproximação quando sua
projeção canônica reproduz o valor registrado. O arredondamento para a exibição
aproximada não autoriza aplicar o ajuste: a autorização depende da contagem
original, confirmação, cadastro, cadeia documental e bloqueios transacionais.
Projeções incapazes de distinguir unidades inteiras continuam bloqueadas;
contagens antigas exatamente representáveis em fatores grandes são preservadas.
Unidades contínuas mantêm suas proteções anteriores.

## Aplicação e caso Amstel

Mantido o serviço existente: prévia assinada, confirmação expressa, bloqueios,
revalidação, transação, idempotência, observador e auditoria. Não há aplicação
automática ao confirmar a contagem. Movimentos posteriores só são aceitos quando
a cadeia existente os comprova; o resultado continua sendo saldo atual + diferença.

Na evidência assinada da proposta/aplicação, `representacao_fisica` congela a regra
`projecao_fisica_v1`, os números acima e a natureza da regularização. A confirmação
original e os registros anteriores não são reescritos. Propostas antigas que
divirjam da nova evidência precisam de nova revisão; não são alteradas em lugar.

Para Amstel, 85 UN ficam preservadas, com projeção 7,083 PCT e resíduo 0,004 UN.
A referência 7,084 PCT tem equivalência aproximada de 85 UN, com resíduo 0,008 UN.
Sua diferença de −0,001 PCT não é apresentada como falta física comprovada.
Se o administrador confirmar a atualização e não houver outros bloqueios, ela é
registrada como **normalização decimal**, inclusive na observação do movimento
manual. O valor anterior e a diferença permanecem auditáveis. Não é criada uma
venda ou um consumo artificial.

Coca-Cola: esperado 24 UN, encontrado 23 UN, falta 1 UN; projeção 3,833 PCT.
Fanta: esperado 21 UN, encontrado 20 UN, falta 1 UN; projeção 3,333 PCT.
Os detalhes retêm a diferença decimal, sem apresentá-la como −1,002 UN físicas.

## Apresentação

O painel, a prévia de atualização e a referência histórica reutilizam a composição
em pacotes + avulsos = total de UN. O resíduo fica identificado, não contado como
falta/sobra física; percentuais físicos usam inteiros quando comprováveis.
Estimativas ao custo continuam baseadas na diferença decimal registrada.
Produtos sem relação física mantêm a apresentação simples.

Aplicado, pendente e bloqueado permanecem separados. Encerramento administrativo
não equivale a regularização e não aplica o estoque. As proteções de aplicação
desabilitada, permissões, conflitos e auditoria não foram removidas.

Prévia estática: `diagnostics/conferencia-projecao/previa.html` (abrir no navegador
habitual). Contém apenas dados fictícios, não executa ajustes e não precisa de
login ou servidor. Scripts e tokens de formulário foram retirados, com envio de
formulários bloqueado por CSP. Links e botões não constituem um fluxo funcional.
Capturas: `comparacao-1366.png`, `comparacao-390.png`, `comparacao-320.png`.

## Arquivos desta etapa

- `inventario/conversao_fisica.py`: representação e validação da projeção.
- `inventario/ajustes.py`: evidência congelada e identificação da normalização.
- `inventario/apresentacao.py`, `painel.py`, `views.py`: composição física e estados.
- `inventario/saldos_apresentacao.py`: composição na referência histórica do OK.
- Templates em `inventario/templates/inventario/`: `comparacao_fisica.html`,
  `representacao_detalhes.html`, `detalhe.html`, `ajustes.html`,
  `previa_produtos_ajuste.html`, `situacao_atualizacao.html`,
  `historico_conferencia.html`.
- CSS em `static/inventario/`: `revisao.css`, `ajustes.css`, `historico_b.css`.
- Testes: `inventario/tests_projecao_fisica.py`, `tests_projecao_apresentacao.py`,
  `tests_ajustes.py`, `tests_ajustes_modal.py`, `tests_relacao_fisica.py`.
- Este documento. Arquivos alterados anteriormente de locações e da reconstrução
  do histórico não foram substituídos ou descartados.

## Resultados

Todos os clusters abaixo foram criados pelo runner protegido
`inventario.validar_confiabilidade_postgresql`, restritos a loopback, porta,
usuário, banco e diretório temporários verificados. Encerrados ao terminar, sem
executar migrations. Resumo em `diagnostics/conferencia-projecao/resultados.json`.

- Rodada funcional final: **91 testes aprovados**, 31,944 s, sem skips. Inclui
  fatores 6/12/24, pacotes, avulsos, resíduo, falta/sobra, aplicação, original
  preservado, idempotência, rollback, conflitos, vendas posteriores, concorrência
  da projeção e encerramento com pendências.
- Apresentação: **2 testes aprovados**, 2,663 s. HTML real com dados fictícios,
  Chrome somente no arquivo estático, em 1366/390/320 px; sem transbordamento
  horizontal inclusive com detalhes abertos. Nenhum servidor de prévia ou senha.
- Verificação complementar dos limites e concorrência: **17 testes aprovados**,
  4,109 s, após preservar também as contagens exatas em fatores grandes. Essa
  rodada se sobrepõe à funcional; não são 17 cenários inteiramente distintos.
- Regressões comerciais: **6 testes aprovados**, 0,685 s, com autenticação adicionada
  somente à fixture em memória. Cobrem compra/entrada, venda fracionada, snapshot,
  remoção/devolução e consumo próprio/cancelamento. A primeira tentativa sem essa
  autenticação encontrou HTTP 401, antes do fluxo comercial; não se removeu a
  exigência de login e não se alteraram os testes comerciais do repositório.
- Regressão ampliada: **184 de 187 aprovados**. As três falhas abaixo também foram
  reproduzidas com módulos e templates do HEAD `127b8d8` carregados somente em
  memória, sem substituir arquivos locais (3 testes, mesmas 3 falhas):
  - `HistoricoSeguroTests.test_historico_somente_leitura_sem_previa_documental`:
    espera o texto antigo `Revisão por`, ausente na interface.
  - `AtualizacaoResultadoTests.test_lote_mesmo_resultado_uma_confirmacao_sem_reaplicacao`:
    espera ausência do elemento `revisar-atualizacao`, que continua presente.
  - `AtualizacaoResultadoTests.test_saldo_aplicado_historico_independente_do_atual`:
    mesma expectativa antiga de ausência de `revisar-atualizacao`.
  Essas pendências preexistentes não foram corrigidas ampliando o escopo.

## Limites antes da publicação

Esta etapa não altera a precisão global nem reescreve compras/vendas/consumos/
devoluções. Seus arredondamentos futuros continuam sujeitos à representação
operacional existente de três casas. A contagem inteira desta conferência é a
evidência preservada, não uma garantia de eliminar deriva futura dos outros fluxos.
Alterar essa representação global exigiria outro diagnóstico e autorização.

As referências reais não foram consultadas: produtos ainda podem ser bloqueados
por falta de documentos, conflito, cadastro alterado ou aplicação desabilitada.
A prévia estática não valida o clique administrativo no sistema operacional;
aprovação funcional ocorreu somente nos testes descartáveis. Sem commit, push,
deploy ou publicação. Aprovação visual do usuário ainda pendente.

Para repetir os testes específicos (sempre em um novo cluster descartável):

```powershell
venv/Scripts/python.exe -X utf8 -m inventario.validar_confiabilidade_postgresql inventario.tests_projecao_fisica inventario.tests_ajustes.PrecisaoFisicaTests
venv/Scripts/python.exe -X utf8 -m inventario.validar_confiabilidade_postgresql inventario.tests_projecao_apresentacao.ApresentacaoFisicaTests
```
