# Conferência de estoque: diagnóstico complementar e validação

Data: 18/10/2026. Base: `01ea870dc8a092ad7c8b43227ae68977eea69652`.
Worktree exclusiva: `.worktrees/inventario-confiabilidade-01ea870`, HEAD destacado.
Preparação de publicação autorizada: commit e push somente da correção;
deploy manual pelo responsável. Nenhuma migration executada.

## Validação complementar em PostgreSQL descartável

Resultado atual: **48 testes aprovados, zero falhas e zero ignorados**, em
PostgreSQL 17.11 local, incluindo os três testes de concorrência antes pendentes.
O cluster exclusivo foi encerrado ao terminar. Nenhuma conexão com produção
foi utilizada e nenhuma migration foi executada.

Os três testes de concorrência validaram:

1. Uma correção em andamento bloqueia a finalização concorrente; após a espera,
   a finalização com versão antiga é recusada.
2. Uma finalização em andamento bloqueia a correção concorrente; após o envio,
   a correção é recusada e a confirmação anterior permanece preservada.
3. Um novo OK aguarda uma venda em andamento e captura o saldo e a sequência
   corretos depois que a venda termina.

O novo teste com duas abas reais de Chrome validou login comercial, rotação
natural do cookie CSRF, 403 na página antiga e recuperação. Os 66 UN digitados
permaneceram na tela; o GET de recuperação manteve 36 UN no banco fictício.
Somente o clique explícito posterior confirmou 66 UN. Cookies de sessão
comercial e contagem, autenticação, rascunho comercial no servidor e dados em
localStorage/sessionStorage permaneceram preservados; a recuperação não
rotacionou a sessão comercial nem fez reenvio automático.

A revisão acrescentou uma proteção de leitura: o último OK retornado é
limitado à versão da rodada já lida. Assim, uma confirmação concorrente não
mistura quantidade/versão antigas com o histórico novo. Um teste específico
simula essa ordem de leitura e confirma a consistência.

Comando da seleção completa, a partir desta worktree:

```powershell
& '..\..\venv\Scripts\python.exe' -B -m inventario.validar_confiabilidade_postgresql inventario.tests_confiabilidade inventario.tests_sessoes inventario.tests_mobile_protecao inventario.tests_ciclo_vida inventario.tests_sessoes_browser
```

O launcher cria um diretório e cluster novos a cada execução. Aceita somente
`127.0.0.1`, porta sorteada, usuário/banco fictícios e diretório de dados
verificado pelo próprio servidor. A guarda é aplicada às conexões psycopg2,
inclusive as abertas pelos testes em threads. O runner cria as tabelas dos
modelos e o observador de teste diretamente, sem executar migrations.

Evidências locais da execução final:

- Após remover a alteração de configuração, a versão preparada para publicação
  passou novamente os **48 testes**, sem falhas ou ignorados, em **36,017 s**.
  Evidências: `%TEMP%/inventario-confiabilidade-pg-6s28uqdy/tests.log` e
  `validation.json`, com `failures=0`, `migrations_executed=false` e `state=stopped`.
- `%TEMP%/inventario-confiabilidade-pg-z_mb387x/tests.log`: 48 testes em 36,483 s, `OK`.
- `%TEMP%/inventario-confiabilidade-pg-z_mb387x/validation.json`: falhas 0,
  `migrations_executed=false`, `state=stopped`, versão e porta do cluster.
- A primeira seleção dirigida, em outro cluster novo
  `%TEMP%/inventario-confiabilidade-pg-rfazlekg`, passou 14 testes e também foi encerrada.

## Revisão do diff para eventual publicação

Revisão sobre `01ea870`. Os seis arquivos
de execução alterados são exclusivamente:

- `inventario/erros.py`: códigos de recusa, respostas e logs sem credenciais.
- `inventario/middleware.py`: resposta estruturada para recusas da contagem e
  classificação de CSRF após a recusa pelo middleware padrão do Django.
- `inventario/services.py`: distinguir motivos sem afrouxar autorização.
- `inventario/views.py`: validar sessão, retornar último OK coerente e consulta de retomada.
- `inventario/templates/inventario/mobile.html`: mostrar pendência, último OK e recuperação.
- `static/inventario/contagem.js`: preservar digitação, consultar antes de reenviar e tratar incerteza.

`sistema/settings.py` está idêntico à base: foi removido o registro global de
`CSRF_FAILURE_VIEW` que existia na candidata anterior. O middleware de inventário
somente reavalia respostas 403 de métodos inseguros, sem aprovação prévia de CSRF,
usando as mesmas regras do Django para identificar uma recusa de CSRF. Não
executa novamente a view nem libera requisições recusadas. Fora do inventário,
o tratamento padrão permanece intacto. Os settings novos são exclusivamente
de testes isolados e nunca são carregados pela configuração de produção.

Os demais arquivos do diff são testes, seus runners/settings isolados e este
relatório. Nenhum arquivo inesperado foi encontrado; `git diff --check` passou.
O template `inventario/templates/inventario/detalhe.html`, os CSS de contagem e
revisão, os modelos, migrations e os diretórios comerciais `estoque`/`offline`
coincidem com a base. A melhoria da largura da tabela gerencial está excluída.
`INVENTARIO_AJUSTES_HABILITADOS=False` permanece intacto, assim como o bloqueio
correspondente no serviço de ajuste real. Nenhuma configuração Render ou Neon
foi lida ou alterada na preparação da publicação.

## Diagnóstico

O diagnóstico anterior por SELECT no Neon registrou dois OKs para o produto:
6 UN e 36 UN, ambos com referência de 68 UN. Não havia gravação de 66 UN.
Havia renovações posteriores ao OK de 36 UN. Nenhuma nova conexão com o banco
operacional foi usada durante a implementação ou os testes.

No código publicado, renovar o acesso troca seu hash e invalida as sessões
anteriores. Isso permanece obrigatório: a tela antiga não ganha autorização
com o token antigo. A credencial vigente deve ser apresentada novamente.

A mensagem genérica de acesso era causada por qualquer resposta não JSON,
incluindo CSRF, autenticação recusada ou erro intermediário de servidor.
Um teste complementar reproduziu login em outra aba: o cookie da contagem
permanece intacto, mas o login rotaciona CSRF e a página antiga recebe 403.
Isso é uma hipótese reproduzida, não uma conclusão sobre o primeiro 403 real.

Não há conector, CLI ou credenciais do Render disponíveis nesta sessão.
Para fechar a causa original, faltam horário/rota do POST recusado e o motivo
correspondente no log `django.security.csrf` ou de acesso do Render.
Não compartilhar links completos, cookies, hashes ou tokens para esse diagnóstico.

## Correção implementada

- Falhas específicas: `csrf_invalido`, `sessao_ausente`, `acesso_expirado`,
  `acesso_revogado`, `credencial_renovada`, `funcionario_inativo` e recusa
  de permissão. Respostas JSON para as requisições de contagem e HTML para
  navegação convencional; CSRF fora do inventário usa a resposta original.
- Quando o registro da sessão já expirou, não é possível recuperar seu acesso
  com segurança; a mensagem diferencia sessão ausente/expirada de um acesso
  expirado identificado em uma sessão ainda legível.
- Log novo `inventario_recusa` contém código, nome da rota e método;
  não inclui URL com convite, cookies, token ou quantidade.
- Último OK vem de `ConfirmacaoContagem`, inclusive quando existe rascunho
  posterior. A entrada digitada não substitui essa referência na interface.
- Ao editar, a tela mostra, por exemplo, “66 UN não confirmados; último OK
  conhecido: 36 UN”. Após falha, a referência é marcada como conhecida,
  sem afirmar que representa o estado mais recente do servidor.
- Após falha, novo envio fica bloqueado até a consulta do estado. A consulta
  “Verificar acesso e último OK” usa GET autenticado, atualiza CSRF e versões,
  preserva a digitação e nunca salva, confirma ou reenvia quantidade.
- Se o link foi renovado, abrir o link vigente em outra aba do mesmo navegador
  e confirmar a entrada estabelece uma nova sessão. A tela original pode então
  consultar o acesso e retomar. Outra atribuição é recusada pela consulta e
  continua sujeita à autorização do backend em todo POST.
- Se a gravação ocorreu e apenas a resposta se perdeu, a consulta reconhece
  o OK salvo, sem duplicar confirmação. Se os números divergem, exige decisão
  explícita do funcionário antes de gravar novamente.
- CSRF, validade, revogação, ciclo, atribuição, controle de versão e transações
  permanecem obrigatórios. Nenhuma extensão automática de acesso foi incluída.

## Testes isolados

O runner exclusivo cria tabelas a partir dos modelos em SQLite temporário e
um observador de estoque de teste, sem executar o sistema de migrations.
Gera somente permissões e dados fictícios. Não usa `.env` copiado, credenciais
de produção, banco real nem perfil de navegador do usuário.

Na raiz desta worktree, utilizando o Python do venv compartilhado:

```powershell
& '..\..\venv\Scripts\python.exe' -B manage.py test inventario.tests_confiabilidade inventario.tests_sessoes inventario.tests_mobile_protecao inventario.tests_ciclo_vida inventario.tests_sessoes_browser --settings=inventario.test_settings_confiabilidade --verbosity=2
```

| Cenário | Evidência validada |
|---|---|
| 36 → 66 | Dois OKs preservados, referência 68, diferença −2 no painel; estoque fictício permanece 68 |
| CSRF inválido e login em outra aba | 403 específico, nenhuma alteração; consulta recupera CSRF e permite envio explícito |
| Renovação com página antiga | Credencial antiga recusada; novo convite em outra aba permite retomar os 66 digitados |
| Resposta perdida depois do commit | Servidor guarda 66, tela indica incerteza; consulta reconhece 66 sem duplicar OK |
| Versão antiga e outro OK de 40 | POST recusado; consulta mostra último OK de 40 e preserva o rascunho de 66 |
| Erro HTML de proxy, HTTP 502 | Quantidade preservada e resposta identificada como inesperada, sem presumir perda da sessão |
| Ausência de sessão, expiração, revogação | Códigos distintos e nenhuma alteração de contagem |
| Link de outra atribuição | Consulta recusa adotar o rascunho e mantém o valor digitado |
| Rascunho posterior a OK | Consulta distingue 66 guardados sem OK e histórico de 36 confirmado |
| Contexto comercial em outra aba | Login, cookie, comando pendente e armazenamentos preservados em Chrome fictício |

Resultado da seleção SQLite inicial: 46 testes, 43 passaram e três ignorados por
exigirem PostgreSQL descartável. Os dez testes novos incluem dois testes
com navegador Chrome em viewport de celular.

Uma regressão adicional incluindo `inventario.tests_browser` encontrou uma
falha de largura da tabela do gestor em `test_gestor_tela_revisao_e_link_limitado`,
linha 117. A mesma falha foi reproduzida com os arquivos originais do `01ea870`
nesta própria worktree, restaurando em seguida as alterações. A correção
presente não altera o layout gerencial. Os outros três testes desse módulo
passaram. Avisos existentes: namespace `estoque` duplicado e ausência do
diretório de arquivos estáticos coletados no ambiente de teste.

## Limites e retomada posterior

Os três cenários de locks concorrentes do PostgreSQL foram validados na etapa
complementar acima. Cookies em Safari ou WebView reais não foram testados.
Rascunhos são mantidos na página aberta; não há promessa de
recuperação se o sistema móvel encerrar a aba. O aviso de saída ajuda quando
o navegador o suporta. Nenhum rascunho é reenviado automaticamente.

Produção continua sem esta correção. A conferência nº 1, seus quatro produtos
confirmados, atribuições, sessões e dados comerciais não foram escritos nesta
etapa. A correção real de 36 para 66 deve ocorrer posteriormente pelo fluxo
do funcionário, após validar e disponibilizar a correção.
