# Quantidade insuficiente na separação — etapa 1

## Diagnóstico e escopo

A fila abre `separacao_venda_detalhe.html` por “Abrir checklist”; não possui
um segundo editor de quantidade. O checklist usava apenas a quantidade já
separada no campo, por isso o primeiro acesso ficava vazio. As alterações pelos
controles eram gravadas automaticamente, sem um botão de confirmação que
recolhesse o editor de quantidade insuficiente.

No código local auditado, `_passo_quantidade_separacao` já devolve 1 para UN/PC;
não foi reproduzido passo 0,25 em UN. Esse passo é legítimo, por exemplo, para
CX com conversão de quatro unidades. Não foi consultado cadastro de produção
para inferir a unidade do produto citado. A regra existente foi preservada e
testada: UN/PC = 1; KG/MIL = 0,001; CX sem conversão = 0,5; CX com fator 4 =
0,25. Nenhuma regra global de quantidade, unidade ou conversão foi alterada.

## Comportamento implementado

- Ao selecionar Quantidade insuficiente, se não houver quantidade encontrada
  positiva, o campo parte da quantidade solicitada, somente como rascunho.
- Uma quantidade encontrada existente continua sendo reutilizada.
- Menos/mais e digitação mantêm o rascunho até “Confirmar quantidade”.
  Enter no campo também confirma, evitando submeter o formulário inteiro.
- Botão reaproveita o estilo existente. Confirmação aceita pelo servidor
  recolhe o editor e devolve foco ao resumo do item. Falha mantém o editor
  aberto. Quantidade igual à solicitada é bloqueada no fluxo de falta:
  o sistema não a reduz silenciosamente para o máximo permitido.
- O resumo orienta a confirmação. Ao voltar a ajustar o valor, mensagens de
  validação antigas desse fluxo são removidas.
- Falta confirmada mantém status `quantidade_insuficiente` no item e
  `com_pendencia` na separação; o resumo informa quanto falta.

A alteração funcional se limita ao template do checklist. Não há mudança em
views, consultas, models, sincronização, regras de cálculo, botão Aplicar ou
comportamento existente de reabrir checklist concluído. O fluxo próprio de
Peso separado em KG e seu botão OK foram preservados.

## Validação

17 testes aprovados em 8,865 s:

- Quatro testes novos: passos por unidade/conversão, confirmação e persistência,
  rejeição de quantidade inteira inválida e matriz de navegador.
- Um teste existente de peso localizado em KG.
- Doze testes existentes de quantidade insuficiente e unidade menor UN de
  `SeparacaoVendaFase1Tests`, incluindo validação de frações equivalentes.

Matriz de navegador Edge/Chromium: UN, CX e MIL em 1440, 390 e 320 px (nove
fluxos). Confere valor inicial 4 sem gravação, bloqueio de confirmar esse
rascunho, menos/mais, ausência de autosave, confirmação explícita por mouse e
toque, digitação e Enter, recolhimento, abertura do editor, recarga da página,
persistência, status com pendência e exibição da falta na fila. Sem overflow
horizontal. Comparação de registros de Produto, Venda, ItemVenda, Compra,
ContaPagar e MovimentoFinanceiro sem diferenças após cada fluxo.

Somente dados fictícios. Runner local:
`venv/Scripts/python.exe -B amostras/testar_separacao_insuficiente.py`.
Configuração isolada sem .env; SQLite temporário privado, tabelas criadas
diretamente dos models e executor de migrations bloqueado. Navegador com
perfil temporário e servidor de teste em loopback. Não foram executadas
migrations nem utilizados bancos manuais/operacionais.

Capturas em `amostras/separacao-insuficiente/editor-{1440,390,320}.png`.
Limitações: emulação de celular, sem dispositivo físico; não foi feita nova
execução da suíte inteira da separação, cujas falhas preexistentes constam em
`docs/separacao-peso-fracionado-validacao.md`.

Arquivos da entrega: template `estoque/templates/estoque/separacao_venda_detalhe.html`,
testes `estoque/tests_separacao_insuficiente.py` e este relatório. Prévia e
runner são artefatos locais em `amostras`. `git diff --check` aprovado.
Validação sem acesso à produção, migrations ou deploy.

## Publicação aprovada

Implementação e prévia aprovadas pelo responsável. Revisão final confirma os
seis pontos solicitados e os 17 testes aprovados. Código e testes permanecem
iguais aos da rodada final; nenhuma verificação adicional foi necessária.
Autorizados commit e push sem force na branch
`backup/wip-pedidos-base-2026-06-04`, com `[skip render]`, exclusivamente do
template, testes e deste relatório. Prévias, temporários, logs e bancos
permanecem fora do commit. Nenhum deploy autorizado.
