# Produtos exibidos na fila conforme o andamento — ajuste local

Referência publicada: `47e063a`. A alteração se limita à seleção e descrição dos
produtos exibidos na fila; a classificação operacional compartilhada e seus
três contadores não foram modificados.

## Comportamento

- Antes do início, produtos apenas aguardando conferência não são listados.
  O aviso existente informa “Aguardando separação — 30 produtos”, mantendo
  30 produtos, 0 resolvidos e 30 pendências. São conferências por fazer,
  não uma afirmação de falta de mercadoria.
- Após o início, o aviso existente informa “Em separação — faltam conferir
  10 produtos”. A lista mostra os produtos restantes e ocorrências que exigem
  atenção. A antiga repetição dos nomes dentro do aviso foi removida.
- “Ainda não conferido” não mostra quantidade separada zero como se já tivesse
  ocorrido conferência. Mantém a quantidade solicitada. Não encontrado, falta
  confirmada e revisão continuam distinguíveis e visíveis.
- A identificação do início considera a data registrada e os resultados
  persistidos. Uma situação legada sem data de início, mas com dez produtos
  conferidos, permanece em andamento após a recarga. Nenhum marco é gravado
  ou corrigido por GET.
- Uma revisão real não é ocultada apenas porque o status físico do item ainda
  é pendente. Produtos OK com nova revisão continuam aparecendo para atenção.
- Conclusão e histórico de ocorrências resolvidas permanecem inalterados,
  com o histórico recolhido por padrão.

## Escopo e preservação

Implementação em `estoque/views.py` e no template existente
`estoque/templates/estoque/separacao_vendas_fila.html`. Nenhum novo componente,
CSS, campo ou migration. Cabeçalhos de rota, faixa dos três indicadores e
marcação dos botões continuam idênticos aos da referência publicada. O cartão
encolhe naturalmente quando deixa de renderizar produtos, sem mudar tamanho,
ordem, alinhamento ou estilo dos botões. Checklist e seus controles não mudaram.

Nenhuma alteração nos serviços de classificação, reconhecimento histórico,
estoque, financeiro ou gravação comercial. A montagem continua somente de
leitura, sem consultas adicionais por produto. Snapshots e eventos preservados.

## Testes e comparação

68 testes aprovados em SQLite temporário, em 38,919 s: os 59 anteriores e nove
novos em `estoque/tests_separacao_andamento_fila.py`. Os nove testes pontuais
foram repetidos após o ajuste final de texto/classe do aviso, passando em 1,638 s.
Não são 77 testes distintos.

Cobertura nova: três e trinta produtos não iniciados, sem nomes individuais;
vinte produtos com dez conferidos e recargas preservando quantidades/resultados;
marco legado ausente; marco de início com itens ainda pendentes; não encontrado,
falta e conferência pendente distintos; alteração posterior; revisão global;
conclusão com ocorrências históricas resolvidas; contadores consistentes.
GET monitorado sem INSERT/UPDATE/DELETE e comparação integral de separações,
snapshots, vendas, itens, produtos, eventos, contas a receber e movimentos
financeiros antes/depois das consultas.

Uma matriz visual adicional passou em 15,188 s, em 1440, 390 e 320 px:
18 pares Antes/Depois (36 capturas), abrangendo seis cenários. A coluna Antes
usa a função de agrupamento da view e o template originais de `47e063a`, obtidos
do Git local, com os mesmos dados fictícios. CSS, cabeçalhos, contadores e
marcação dos botões foram comparados integralmente. Estilos e dimensões dos
botões permaneceram iguais, sem overflow horizontal. Recarga no navegador
mantém os dez produtos restantes; retratos do banco permanecem iguais após
todas as consultas Antes/Depois.

Alturas medidas dos cartões, em pixels arredondados:

| Cenário | Largura | Antes | Depois |
| --- | ---: | ---: | ---: |
| 30 produtos, não iniciado | 1440 | 1904 | 159 |
| 30 produtos, não iniciado | 390 | 3475 | 382 |
| 30 produtos, não iniciado | 320 | 4315 | 471 |
| 20 produtos, 10 conferidos | 1440 | 952 | 735 |
| 20 produtos, 10 conferidos | 390 | 1624 | 1151 |
| 20 produtos, 10 conferidos | 320 | 1951 | 1497 |

Prévia: `amostras/separacao-andamento-fila/index.html`, com imagens lado a lado
e medições em `medidas.json`. Runners locais: `amostras/testar_andamento_fila.py`,
`amostras/testar_andamento_fila_pontual.py` e `amostras/comparar_andamento_fila.py`.
Prévias, runners e logs são artefatos locais, fora da entrega de código.

Configuração isolada sem .env, SQLite temporário privado, tabelas criadas dos
models e executor de migrations bloqueado. Navegador Edge/Chromium em loopback
com perfil temporário. Celular emulado, sem aparelho físico. Não foi executada
a suíte completa da aplicação nem nova rodada PostgreSQL: não há mudança de
schema, transação ou operação de banco nesta alteração de apresentação.

`git diff --check` aprovado. Sem acesso à produção, vendas reais, migrations,
commit, push ou deploy. Trabalhos locais não relacionados preservados.
Implementação local e prévia aguardam aprovação.

## Publicação aprovada

Implementação e comparações Antes/Depois aprovadas pelo responsável. Revisão
integral confirmou os dez critérios: resumo antes do início, lista somente do
trabalho restante e ocorrências reais após o início, recarga preservando os
resultados, produtos OK fora da lista ativa, revisões/faltas visíveis, conclusão
preservada, três contadores consistentes, layout original, GET sem escrita e
integridade de estoque, financeiro, histórico e correções anteriores.

Conferidos os registros dos 68 testes aprovados (incluindo os nove novos),
repetição dos nove testes pontuais após o ajuste final e as 18 comparações em
1440, 390 e 320 px. Não foi necessário repetir suítes nesta publicação.
Verificação de fonte reconfirmou CSS, cabeçalhos, botões e faixa de contadores
idênticos aos de 47e063a. `git diff --check` aprovado.

Autorizados commit e push sem force na branch
`backup/wip-pedidos-base-2026-06-04`, com a mensagem
`Simplifica exibicao de produtos na fila de separacao [skip render]`, somente
dos quatro arquivos de implementação, testes e deste relatório.
Prévias, runners, temporários, logs e bancos não integram o commit.
Trabalhos locais não relacionados preservados.

Sem workflows de deploy versionados ou hooks locais ativos. Mantido o marcador
`[skip render]`, documentado para impedir auto-deploy:
https://render.com/docs/deploys#skipping-an-auto-deploy.
Nenhum acesso à produção, execução de migrations ou comando de deploy.
