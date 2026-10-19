# Login inesperado no recolhimento mobile

**Estado atual:** após autorização explícita do usuário para implementar delegação
por tarefa, foi preparada a solução de [link individual sem login do funcionário](autorizacao-individual-recolhimento.md).
Ela supera a limitação de exigir sessão Django do funcionário descrita nas etapas
anteriores abaixo. Permanece sem publicação e sem alteração da locação real.

Preparação isolada em `correcao-login-recolhimento-20261019`, baseada no HEAD
`4785a58`. As alterações não commitadas da árvore original não foram transportadas
nem modificadas. Não houve commit, push, deploy ou alteração de dados reais.

## Causa identificada no código

O formulário de confirmação faz POST para
`/locacoes/tarefas-operacionais/<tarefa_id>/conferencia-recolhimento/`.
O identificador da tarefa não é necessariamente o número da locação.
Na versão original, `locacoes.views.conferencia_recolhimento` verifica
`request.user.is_authenticated` e, quando falso, retorna HTTP 302 para
`/offline/login/?next=...`. O navegador segue esse redirecionamento e mostra
o template `offline/login.html`, com o texto relatado.

Essa condição acontece **antes** de obter a tarefa ou chamar
`ConferenciaRecolhimentoLocacao.registrar`. Portanto, a tentativa específica que
entrou nesse ramo não gravou o recolhimento. Isso não exclui uma tentativa anterior
bem-sucedida, seguida de novo clique ou retorno à página.

O GET do checklist permite consulta sem sessão autenticada. Selecionar um
funcionário ou abrir o link recebido pelo WhatsApp não autentica o usuário Django.
Assim, o problema pode ocorrer tanto com sessão expirada quanto com cookie ausente,
outro navegador/WebView ou uma página aberta sem login. Sem os prints, URL final,
horário e logs do ambiente do incidente, não é possível distinguir essas hipóteses
nem afirmar que o registro real da locação 90 existe ou falta.

Atualização após resposta do usuário: incidente em **19/10/2026, aproximadamente
16h01 em Belém (19h01 UTC)**. Não há URL no print nem logs HTTP exportados.

Não há middleware de redirecionamento nesse caminho. O service worker examinado
não intercepta POSTs nem encaminha esse fluxo ao login; para navegações de checklist
usa a rede e somente adota o shell offline em falha de rede. O JavaScript do
checklist bloqueia gravações quando offline, sem enviar o navegador ao login.

## Comunicação ao cliente

Após a gravação, o link “Enviar checklist pelo WhatsApp” é gerado por
`_whatsapp_comprovante_recolhimento_url` com destino direto `https://wa.me/...`.
A mensagem inclui dados da conferência persistida e o comprovante
`/locacoes/conferencias-recolhimento/<conferencia_id>/checklist-recolhimento/`.
Esse GET já era acessível sem autenticação; nenhuma nova exposição foi introduzida.
O botão não precisa obter outra autorização para abrir o WhatsApp. Os testes
seguem o link do comprovante com o cookie de sessão removido e verificam que a
consulta não grava eventos nem cria outra conferência. Nenhuma mensagem real foi enviada.

## Correção

- Sessão ausente: HTTP 401 explicativo na própria operação, sem `Location` para o
  login e sem reenvio automático. A recuperação usa, por ação explícita, o login
  já existente, a mesma conta e o mesmo mecanismo de sessão. Não há senha nova,
  identidade inferida pelo nome do funcionário ou autenticação alternativa.
- Falha CSRF nessa rota: HTTP 403 contextual; as demais rotas conservam o handler
  padrão. As três permissões operacionais continuam obrigatórias.
- O formulário inclui um token assinado com tarefa e última conferência existente.
  No POST, a tarefa é bloqueada no PostgreSQL antes de comparar esse estado e gravar.
  Reenvio do formulário antigo leva ao comprovante já existente, sem registrar
  novamente, inclusive se a primeira operação foi parcial. O token é obrigatório
  independentemente de `interface_simplificada`; formulários antigos sem token
  precisam ser reabertos. Esse token não concede autenticação ou permissões.
- O navegador bloqueia submissões consecutivas e mostra “Confirmando recolhimento...”.
  O bloqueio do servidor também cobre duas requisições simultâneas que contornem a UI.
- A página operacional recebe `Cache-Control: no-store`, evitando reaproveitamento
  HTTP de formulários com estado antigo. Cookies e sessão Django seguem o fluxo normal.

## Validação reproduzível

Na worktree, usar o Python do venv existente:

```powershell
& ../../venv/Scripts/python.exe -m offline.validate_pickup_postgresql
```

O launcher cria um cluster PostgreSQL novo em pasta temporária, limitado a loopback,
e bloqueia conexões fora dele. Cria tabelas diretamente dos modelos e permissões de
teste; não executa migrations e bloqueia explicitamente `MigrationExecutor.migrate`.
O cluster é encerrado ao terminar. `PICKUP_PG_BIN` permite indicar outros binários locais.

Evidências da suíte inicial: `diagnostics/pickup-session/baseline-36-validation.json` e
`diagnostics/pickup-session/baseline-36-tests.log`: **36 testes passaram** em PostgreSQL 17.11;
cluster encerrado ao término. `git diff --check` também passou.
Os cenários incluem confirmação única,
dupla submissão, dois POSTs simultâneos, recolhimento parcial, sessão expirada,
CSRF inválido, permissões insuficientes e comprovante ao cliente. Há cenário com
10 mesas e 40 cadeiras exclusivamente fictícias.

Chrome foi usado com perfil temporário, viewport mobile; os novos cenários também
configuram user agent Android e toque. Isso é emulação mobile em Chrome desktop,
não execução num aparelho Android físico. Não foi feita validação em produção.

## Pendente para concluir o diagnóstico do caso real

Consultar somente em leitura a conferência, o evento `recolhimento_usuario_confirmou`,
o status da tarefa de recolhimento da locação 90 e os logs HTTP no horário do incidente.
É necessário correlacionar o ID da tarefa e o horário, sem reenviar o POST. A confirmação
do estado real e da causa da ausência de sessão depende dessas evidências do ambiente.

## Consulta posterior dos registros disponíveis

A configuração local não contém `DATABASE_URL`; usa SQLite local por padrão.
Não foi identificada conexão disponível ao PostgreSQL operacional nem conector
de logs desse ambiente. Os arquivos `db.sqlite3` (modificado em 27/09/2026) e
`manual-checklist-flow.sqlite3` (modificado em 05/08/2026) foram abertos por SQLite
com `mode=ro` e `PRAGMA query_only=ON`, sem inicializar o Django. Nenhum contém
a locação 90, conferências, tarefas ou eventos com esse `locacao_id`. A ausência
nessas bases antigas **não comprova ausência do registro em produção**.

A busca nos logs locais disponíveis não encontrou o caso pelo nome, rota
`/locacoes/90/` ou registros de 19/10/2026 entre 16h00–16h09 local / 19h00–19h09 UTC.
Isso não substitui os logs HTTP do servidor operacional.

Para finalizar a verificação sem gravar dados, um operador com acesso já autorizado
ao banco correto pode executar `docs/consulta-somente-leitura-locacao-90.sql`.
O arquivo contém somente consultas dentro de transação explicitamente `READ ONLY`,
com limites de espera e `ROLLBACK`. Não foi executado no banco operacional.
Não é necessário abrir o formulário ou pressionar qualquer botão de confirmação.

Interpretação: uma conferência persistida é evidência de registro físico; conferir
quantidades, situação, responsável e horário. `recolhimento_usuario_confirmou`
correlaciona a conferência com o usuário autenticado. Conferência completa, tarefa
confirmada e locação devolvida devem ser coerentes; parcial deve manter as
quantidades pendentes. Status administrativo isolado não prova checklist físico.
Ausência de evento de auditoria isoladamente também não prova ausência da conferência.
Se houver inconsistência, registrar o achado sem repetir o recolhimento.

Nos logs do ambiente, buscar inicialmente **15h50–16h15 de Belém / 18h50–19h15 UTC
em 19/10/2026**, usando o ID real da tarefa retornado pela consulta (não presumir 90).
Correlacionar POST da conferência, status e `Location` do 302, GET do comprovante
ou `?conferencia=...` e GET de `/offline/login/`. Um 302 sozinho pode ser sucesso ou
autenticação; é preciso observar o destino. Sem log de sessão adequado, HTTP sozinho
não distingue cookie ausente de sessão expirada. Não exportar cookies ou senhas.

## Arquivos preparados nesta worktree

| Arquivo | Finalidade |
| --- | --- |
| `locacoes/views.py` | Erro de sessão explícito, token de confirmação e serialização do POST |
| `locacoes/pickup_security.py` | Respostas operacionais e tratamento contextual de CSRF |
| `locacoes/templates/locacoes/conferencia_recolhimento.html` | Token e bloqueio de duplo envio |
| `locacoes/templates/locacoes/recolhimento_sessao.html` | Explicação e recuperação explícita da sessão existente |
| `sistema/settings.py` | Configuração do handler CSRF |
| `offline/test_pickup_checklist.py` | Ajuste das regressões para o fluxo corrigido |
| `offline/test_pickup_session.py` | Sessão, concorrência, segurança e comunicação mobile |
| `offline/test_pickup_recovery.py` | Recuperação pelo login existente com preservação dos campos e confirmação explícita |
| `offline/validate_pickup_postgresql.py` | Launcher PostgreSQL descartável sem migrations |
| `docs/incidente-login-recolhimento-mobile.md` | Causa, evidências e limites da investigação |
| `docs/consulta-somente-leitura-locacao-90.sql` | Consulta de histórico no ambiente correto, sem gravação |

Na etapa de consulta local, foram alterados somente os dois documentos acima.
A complementação operacional abaixo foi feita posteriormente. A worktree original
e a locação operacional não foram modificadas.

## Complementação: autorização e recuperação efetiva do fluxo

O cadastro `Funcionario` possui as flags `pode_receber_checklist` e
`pode_operar_sistema`, mas não tem vínculo com um usuário Django. As flags e o
parâmetro `funcionario` do link não concedem sessão ou permissões. O GET público
serve para consulta; gravar exige usuário Django autenticado no mesmo domínio e
navegador, com `add_conferenciarecolhimentolocacao`, `change_locacao` e
`change_tarefaoperacionallocacao` do app `locacoes`. O mesmo cookie de sessão atende
o POST, sem outro login. Não existe autorização delegada assinada no link de WhatsApp.

A primeira correção eliminava o redirecionamento, mas ainda não assegurava recuperar
os campos após expiração. A complementação agora:

- Avisa antes do preenchimento quando a consulta foi aberta sem sessão autenticada;
  oferece explicitamente o login já existente, sem inventar outra identidade.
- Em sessão ausente ou erro CSRF, guarda somente os campos operacionais permitidos
  na sessão do servidor, nunca senha ou cookie, por até 30 minutos.
- “Restabelecer a sessão existente” abre o login habitual no mesmo navegador. Se a
  sessão segue válida (por exemplo, apenas token CSRF inválido), a LoginView existente
  retorna ao checklist sem solicitar senha. Se expirou, é necessário autenticar a
  conta habitual. O login Django conserva o rascunho da sessão anônima ao trocar sua chave.
- Após autenticação e verificação das três permissões, recupera campos para revisão,
  com CSRF atual. Não faz POST automaticamente. A recuperação é vinculada ao caminho
  e ao estado assinado da tarefa; se houve conferência posterior, mostra o comprovante
  existente em vez de reaproveitar quantidades antigas.
- O funcionário revisa e confirma uma vez; a conferência registrada oferece o link
  `wa.me` para compartilhar normalmente, sem outra autenticação no sistema.

Executados **somente cinco testes complementares**, todos aprovados em novo
PostgreSQL descartável, migrations bloqueadas e Chrome em emulação mobile:
login existente preservando campos, permissões mantidas, histórico alterado
impedindo replay, expiração seguida de login/revisão/confirmação/WhatsApp e
recuperação CSRF com sessão válida sem nova senha. Evidências complementares em
`diagnostics/pickup-session/tests.log` e `validation.json`. O cluster foi encerrado.
Comando usado: `python -m offline.validate_pickup_postgresql offline.test_pickup_recovery`.
Os 36 testes anteriores foram aproveitados, sem repetir a suíte inteira.

**Conclusão operacional:** o fluxo foi comprovado para uma conta autorizada; não
há pedido adicional de login na confirmação enquanto a sessão é válida. Não é
possível garantir a conclusão pela Roseli somente porque ela recebeu o link:
sua conta, permissões e sessão no navegador real não puderam ser verificadas sem
acesso ao ambiente. Se ela possui apenas o link de WhatsApp, permanece sem autorização
de gravação. Essa lacuna é de acesso legítimo, não deve ser resolvida retirando
segurança ou convertendo o nome/telefone do funcionário em autenticação. Nenhuma
conta, senha ou permissão operacional real foi criada ou alterada. Não houve commit
ou deploy; a produção ainda não recebeu a correção.
