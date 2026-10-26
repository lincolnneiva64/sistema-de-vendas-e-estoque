# Classificação operacional da separação — correção final local

## Resultado

Fila, resumo da rota, checklist e revisão de edição usam a mesma classificação
operacional de leitura. Situação exibida, cor do cartão e totais da rota seguem
as pendências atuais, independentemente do status histórico persistido.
Nenhum status é corrigido por GET e nenhum checklist concluído é reaberto.

Os indicadores principais são Produtos, Resolvidos e Pendências. Cada linha do
checklist entra uma única vez; resolvidos + pendências = produtos. Conferidos
corretamente e ajustes comprovados entram em resolvidos, salvo nova revisão.
Produtos ainda não conferidos também entram em pendências. Uma revisão global
ou acréscimo sem vínculo seguro mantém os produtos aguardando revisão, explica
o motivo e impede conclusão; não acrescenta linhas fictícias ao total.

Sem ação pendente e com marco de conclusão, a mensagem é “Separação concluída —
nota pronta”. Ajustes atendidos acrescentam discretamente “Nota ajustada após a
separação — alterações resolvidas.” Com ação pendente, a mensagem é “Separação
com pendências”, acompanhada dos produtos e motivos. Isso não declara entrega,
pagamento ou encerramento financeiro.

## Evidência histórica

O serviço existente reconhece reduções anteriores à implantação, sem exigir
novo salvamento. A análise começa no registro da ocorrência física do item,
sem descartar eventos apenas porque antecedem a última revisão/atendimento.
Exige vínculo do item, nome/unidade únicos no checklist legado, quantidade
separada confirmada e válida, cadeia coerente de reduções desde a solicitação
original até a quantidade separada, quantidade atual correspondente e ausência
de troca, remoção/reinclusão ou evento incompatível posterior.

Snapshots legados não contêm o ID original do produto. Por isso homônimos,
eventos incompletos, mudanças de unidade e cadeias ambíguas impedem inferência.
Igualdade numérica e atendimento global genérico não são provas de resolução
ou de complementação física. A pendência explica a ausência de comprovação.

O salvamento atual continua utilizando os IDs de eventos por item e a identidade
anterior capturada pelo serviço de vendas. Isso preserva linhas homônimas
identificadas por ID, sem estender essa confiança aos eventos antigos em texto.
Reenvio sem alteração não produz prova inexistente nem duplica eventos.
Eventos originais e snapshots não são reescritos. Quando há salvamento posterior,
o registro estruturado associa os IDs das evidências reconhecidas.

Falta ativa informa “Quantidade insuficiente — falta 1”; ajuste comprovado,
“Quantidade ajustada na venda — resolvido”; situações incompatíveis ou sem
prova suficiente mostram “Necessita conferência” e o motivo. Solicitado original,
separado e quantidade atual continuam disponíveis no histórico/detalhamento.
O histórico é carregado em lote, sem consulta individual por produto; o teste
existente de leitura cacheada continua cobrindo uma consulta inicial e zero
consultas nas repetições de preparação dos itens.

## Consultas e preservação

Removida a sincronização da montagem da fila. GET da fila, checklist e revisão
não altera separações, snapshots, eventos, itens comerciais, estoque ou contas.
A sincronização necessária permanece nas operações de escrita existentes.
Não foram criados campos, migrations, telas ou botões. O selo do checklist
também acompanha as confirmações por AJAX, sem mudar Enter, foco ou confirmação.

Confirmação física registrada como conferido continua distinta de ajuste
comercial. Não existe nesta entrega um novo procedimento para registrar
complementação de checklist histórico: um atendimento genérico nunca é
apresentado como material adicional fisicamente separado. Não se inventa prova
de complementação onde o histórico não a registra.

## Verificação

59 testes aprovados em SQLite temporário privado, em 38,461 s: os 44 testes
das etapas anteriores, com expectativas de texto atualizadas, e 15 novos
(14 de servidor e uma matriz de navegador). Os 55 testes de servidor passaram
também em PostgreSQL descartável, em 11,132 s; cluster encerrado ao final.
São 59 testes distintos, com repetição dos testes de servidor entre bancos.

Cobertura: legado comprovado sem salvar; igualdade sem evento, inclusive em
reenvio; atendimento posterior; homônimos e ambiguidades; unidade diferente;
correção parcial; aumento posterior; acréscimo e revisão global; conferência
física registrada versus atendimento genérico; total de 13 linhas sem duplicar;
fila/rota/checklist/edição consistentes; GET monitorado sem INSERT/UPDATE/DELETE;
sincronização mantida no POST; auditoria sem duplicação; snapshots integrais;
estoque, financeiro e registros comerciais preservados pelas consultas;
peso em KG confirmado mas com quantidade comercial ainda divergente bloqueia
a indicação de nota pronta, preservando o atalho de ajuste já existente.

Edge/Chromium em 1440, 390 e 320 px: 27 capturas dos cenários resolvido, parcial
e sem prova, em fila/checklist/edição. Sem overflow horizontal. As matrizes
anteriores verificam teclado/Enter, confirmação, passos de quantidade,
expansão/recolhimento, consulta protegida e ausência de reprocessamento.
Após os últimos ajustes de cor e singular/plural, a matriz nova foi repetida
para regenerar as prévias. Galeria local: `amostras/separacao-final/index.html`.

Limitações: celular emulado, sem aparelho físico; não foi executada a suíte
completa da aplicação. Históricos insuficientes permanecem para revisão humana.
Não foi consultada a venda real #1031 nem qualquer ambiente de produção.

Runners locais: `amostras/testar_separacao_final.py`,
`amostras/testar_separacao_final_browser.py` e
`amostras/validar_separacao_final_pg.py`. Configurações isoladas sem .env,
tabelas criadas diretamente dos models e executor de migrations bloqueado.
PostgreSQL com guardas de host, porta, usuário e diretório exclusivo.
Prévia, runners, logs e bancos são artefatos locais, fora da entrega de código.

Arquivos de implementação: `estoque/services/pendencias_separacao.py`,
`estoque/views.py` e templates existentes `separacao_vendas_fila.html`,
`separacao_venda_detalhe.html`, `venda_editar_revisao.html`.
Testes novos em `estoque/tests_separacao_operacional.py`; expectativas de texto
ajustadas em `estoque/tests.py`, `estoque/tests_separacao_consulta.py`,
`estoque/tests_separacao_insuficiente.py` e `estoque/tests_separacao_resolucoes.py`.

Implementação somente local. Sem migrations, commit, push ou deploy.
Trabalhos locais não relacionados preservados. Publicação depende de aprovação.

## Correção de escopo visual — layout original 1353c06

A reorganização visual inicial não foi aprovada. Esta seção substitui a proposta
visual anterior para fila e checklist. A lógica operacional, reconhecimento
histórico, cálculos, consultas sem escrita e proteções comerciais foram mantidos.

Os três indicadores voltaram ao ponto original da faixa `.sep-counts`, imediatamente
antes dos botões, usando as mesmas classes e estilos. Removidas as novas linhas
permanentes de resumo, ajuste e motivo global dos cabeçalhos da fila/checklist.
O selo original recebe o texto operacional; o aviso verde já existente foi
preservado na mesma posição e com seus textos anteriores. Histórico permanece
recolhido e os motivos detalhados ficam no item expandido do checklist.

Nenhuma mudança de CSS, media queries, marcação dos botões ou cabeçalho das rotas
em relação a 1353c06. Estados usam a paleta já existente, conforme a classificação
operacional correta. O resumo de edição da venda não foi reorganizado nesta revisão.

Comparação local de 18 pares (36 capturas), nos cenários resolvido, parcial e sem
prova, para fila/checklist em 1440, 390 e 320 px. A coluna Antes utiliza templates
obtidos por `git show 1353c06`, com os mesmos dados fictícios e classificação
funcional local; não simula o backend antigo nem consulta produção.

Conferidos automaticamente: CSS integral idêntico, cabeçalho de rota idêntico,
marcação/estilos/dimensões dos botões idênticos, ordem dos controles preservada,
três indicadores no ponto original, detalhes recolhidos e ausência de overflow.
Medições adicionais confirmaram posições horizontais e dimensões dos controles
idênticas; no desktop e no checklist, as posições verticais também coincidiram.
Na fila mobile, a faixa passou a ter uma linha a menos pela redução autorizada
de cinco para três indicadores: cartão e botões ficaram 38,72 px mais acima,
sem mudança de padding, margem, tamanho dos botões ou regras de posicionamento.
Não foi acrescentado espaço vazio artificial para compensar a linha retirada.

59 testes de regressão aprovados em SQLite temporário, em 39,217 s. Comparação
visual aprovada em 14,290 s. Nenhuma alteração funcional de servidor nesta revisão;
não foi necessária outra execução de PostgreSQL. A rodada anterior de 55 testes
de servidor está registrada acima e não é apresentada como nova execução.

Nova prévia para aprovação: `amostras/separacao-layout-original/index.html`.
Medições: `medidas.json` e `resumo.json` na mesma pasta. Runner de comparação:
`amostras/comparar_layout_separacao.py`. Prévias anteriores não representam a
proposta visual atual. Celular emulado; sem avaliação em aparelho físico.

Sem acesso à produção/#1031, migrations, commit, push ou deploy.

## Publicação aprovada

Implementação funcional e última comparação Antes/Depois aprovadas pelo
responsável. Revisão integral confirmou os dez critérios solicitados, incluindo
layout original preservado, três contadores, conclusão sem pendências,
reconhecimento histórico comprovado, classificação compartilhada, GET sem
escrita e preservação de snapshots, estoque, financeiro e regressões anteriores.

Conferidos os registros finais: 59 testes SQLite aprovados após o ajuste visual
(39,217 s), 55 testes PostgreSQL descartável aprovados (11,132 s, cluster
encerrado) e comparação de layout aprovada em 1440, 390 e 320 px (14,290 s).
Não foi necessário repetir as suítes. Nova verificação de fonte confirmou CSS,
cabeçalhos de rota e marcação dos botões iguais aos de 1353c06.

Autorizados commit e push sem force na branch
`backup/wip-pedidos-base-2026-06-04`, exclusivamente dos 11 arquivos de código,
testes e deste relatório. Prévias, runners, logs, bancos, temporários e trabalhos
locais não relacionados permanecem fora do commit.

Mensagem: `Unifica classificacao e simplifica pendencias da separacao [skip render]`.
Sem workflows de deploy versionados ou hooks locais ativos. O bloqueio de
auto-deploy por `[skip render]` foi reconfirmado na documentação oficial:
https://render.com/docs/deploys#skipping-an-auto-deploy.
Não houve acesso à produção/#1031, execução de migrations ou comando de deploy.
