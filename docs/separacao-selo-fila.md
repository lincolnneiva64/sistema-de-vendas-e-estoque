# Texto do selo na fila — ajuste local

Referência publicada: `395b98e`. Alteração exclusivamente de apresentação do
texto do selo, reutilizando os dados operacionais e de andamento já calculados.

| Situação | Selo |
| --- | --- |
| Checklist não iniciado, sem ocorrência real | Aguardando separação |
| Checklist iniciado, faltando conferências, sem ocorrência real | Em separação |
| Falta, não encontrado ou revisão que exija providência | Separação com pendências |
| Concluído, sem pendências operacionais | Separação concluída — nota pronta |

Uma ocorrência real tem prioridade sobre os textos de espera/andamento.
Revisão posterior à conclusão mantém a sinalização de pendência. A ausência
de conferência não é apresentada como falta de mercadoria. Os contadores
continuam incluindo produtos a conferir na quantidade de pendências.

Implementação: `estoque/views.py` calcula somente o texto de apresentação
`texto_selo_fila`, usando os indicadores de início, conclusão e ocorrências
existentes. O template `estoque/templates/estoque/separacao_vendas_fila.html`
substitui apenas a variável de texto no selo existente. As classes do selo,
cores, CSS, botões, avisos e três contadores não foram alterados. Nenhuma
consulta adicional, novo componente ou alteração da classificação compartilhada.

Não houve mudança na gravação da separação, estoque, financeiro, eventos ou
snapshots. GET permanece somente de leitura, sem corrigir status persistido.
Checklist e edição da venda não foram modificados.

## Validação

69 testes aprovados em SQLite temporário, em 40,393 s: os 68 anteriores,
com verificações adicionais dos textos dos selos nos testes de andamento,
e um caso novo de conclusão sem ocorrências. Os testes verificam os quatro
estados, incluindo conclusão com ocorrências históricas resolvidas, nova
revisão após conclusão, falta real, produto não encontrado, revisão global,
início legado sem timestamp e recarga com dez conferências preservadas.
Selo, aviso informativo e contadores verificados em conjunto.

GET monitorado sem INSERT/UPDATE/DELETE, com comparação integral dos registros
de separação, itens históricos/comerciais, venda, produtos, eventos, conta a
receber e movimentos financeiros antes/depois das consultas.

Matriz visual adicional aprovada em 19,693 s: 18 pares Antes/Depois, com 36
capturas em 1440, 390 e 320 px. A versão Antes usa função de agrupamento e
template de `395b98e`, obtidos do Git local, com os mesmos dados fictícios.
Verificados o texto exato do selo, as cores do selo, altura dos cartões,
dimensões/estilos dos botões, contadores, recarga e ausência de overflow.
CSS, cabeçalhos, botões e faixa dos três indicadores idênticos à referência.

Prévia: `amostras/separacao-selo-fila/index.html`; medidas na mesma pasta,
em `medidas.json`. Logs: `amostras/selo-fila-testes.local.log` e
`amostras/selo-fila-comparacao.local.log`. Runner visual local:
`amostras/comparar_selo_fila.py`. Prévias, runners e logs são artefatos locais.

Configuração de teste isolada sem .env, SQLite privado temporário, tabelas
criadas diretamente dos models e executor de migrations bloqueado. Navegador
Edge/Chromium em loopback com perfil temporário. Celular emulado, sem aparelho
físico. Não foi executada a suíte completa da aplicação nem nova rodada
PostgreSQL, pois o ajuste não muda operações de banco ou transações.

`git diff --check` aprovado. Sem acesso à produção ou à nota real #1033,
migrations, commit, push ou deploy. Trabalhos locais não relacionados
preservados. Implementação e prévias aguardam aprovação da publicação.

## Publicação aprovada

Ajuste local aprovado pelo responsável. Revisão integral confirmou os quatro
textos, sua coerência com avisos/contadores e a prioridade das ocorrências reais
e revisões. Template altera somente a variável de texto do selo; cores, layout,
botões, checklist, históricos e classificação operacional foram preservados.

Conferidos os registros: 69 testes aprovados em 40,393 s e 18 comparações
visuais em 1440, 390 e 320 px aprovadas em 19,693 s. Nenhuma reexecução de
suíte foi necessária. `git diff --check` aprovado.

Autorizados commit e push sem force na branch
`backup/wip-pedidos-base-2026-06-04`, com a mensagem
`Corrige status do selo na fila de separacao [skip render]`, somente dos quatro
arquivos de implementação, testes e deste relatório. Prévias, logs, runners,
temporários e bancos ficam fora do commit. Trabalhos locais não relacionados
preservados.

Sem workflows de deploy versionados ou hooks locais ativos. Mantido o marcador
`[skip render]`, documentado para impedir auto-deploy:
https://render.com/docs/deploys#skipping-an-auto-deploy.
Sem acesso à produção, migrations ou comando de deploy.
