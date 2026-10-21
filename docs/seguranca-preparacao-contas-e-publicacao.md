# Segurança — implementação e preparação das contas

Worktree: `.worktrees/auditoria-autenticacao-ab01055`, baseada em `ab01055a581d62333ad0ae3f48e3b9e9fe7313b5`.
Documento de revisão; nenhuma conexão ou preparação de contas de produção foi executada.

## Estrutura existente preservada

- Usuário padrão `django.contrib.auth.models.User`, backend padrão `ModelBackend`, grupos e permissões do Django; nenhuma tabela nova ou migration.
- Senhas são verificadas por hash. Mantidos os validadores de semelhança com usuário, comprimento mínimo, senha comum e senha apenas numérica.
- Sessões no banco, identificadas por cookie opaco. Não foi trocada a SECRET_KEY, o backend, o nome do cookie nem a tabela de sessões.
- Em produção (`DEBUG=False`), a configuração existente exige HTTPS e cookies de sessão/CSRF Secure. HttpOnly e SameSite continuam com os padrões do Django.
- `SESSION_COOKIE_AGE` configura a duração em segundos; padrão 2.592.000 (30 dias). Sessão persiste ao fechar o navegador. Uma variável de ambiente já definida prevalece sobre o padrão; conferir seu valor antes de publicar. `SESSION_SAVE_EVERY_REQUEST`, padrão false, pode renovar o prazo a cada requisição se explicitamente configurado.
- Sair exige POST e CSRF. A ação encerra a sessão comercial; não apaga IndexedDB, filas ou operações offline. A sessão separada de contagem continua independente. Senha alterada ou conta inativada invalida o acesso correspondente.

## Política implementada

O middleware exige autenticação para o backend comercial, incluindo os aliases `/estoque/`. Páginas HTML levam ao login preservando o destino; comandos e APIs recebem JSON 401/403. Não há liberação por selecionar um funcionário, ocultar botões, conhecer IDs ou declarar um cabeçalho de acesso LOCAL.

Administradores completos são usuários **ativos, staff e superuser**; o aplicativo usa a semântica padrão de superuser, sem qualquer privilégio baseado no texto “Lincoln” ou “Roseli”. As duas contas receberão exatamente esses mesmos atributos. Não há criação automática de usuários, senhas, permissões ou grupos na inicialização, build ou deploy.

Usuários comuns, inclusive staff sem superuser, não recebem acesso total. As permissões explicitamente mapeadas em `sistema/acesso.py` permitem consultas e ações específicas. Exemplos:

| Função | Permissões para o caminho mapeado |
|---|---|
| Abrir Vendas | `view_venda`, `view_cliente`, `view_produto` |
| Gravar nova venda / recibo online | `add_venda`, `view_cliente`, `view_produto` |
| Editar venda pelo gravador | `change_venda`, `view_cliente`, `view_produto` |
| Receber conta | GET: consulta de conta; POST: `add_recebimentocontareceber`, `change_contareceber` |
| Baixar conta a pagar | `add_pagamentocontapagar`, `change_contapagar` |
| Nova despesa | `add_despesadiaria`, `add_movimentofinanceiro` |
| Conferência física e ajuste | Mantidas as permissões próprias já verificadas pelo módulo |

Rotas comerciais não mapeadas para delegação continuam exclusivas de administradores completos. Isso é intencional: nenhuma ação desconhecida é liberada automaticamente. Para delegar funções adicionais a usuários comuns, a política deve ser revisada explicitamente; não se deve conceder superuser como atalho. Em especial, não atribuir administração de usuários/grupos a operadores comuns.

## Funcionários e atalhos

- Inventário mantém convites, validade, revogação, atribuições, CSRF e cookie separado de contagem. Um token de contagem não autentica o usuário comercial.
- Recolhimento mantém sua autorização individual e as permissões/recuperação de sessão existentes. A consulta do comprovante fica vinculada à autorização validada, não ao simples conhecimento do ID.
- Entregas e separações que antes usavam apenas IDs agora recebem URL assinada pelos mesmos auxiliares/botões de envio. O acesso permite somente a tarefa, seus itens e seus comprovantes. Links de consulta de cliente não liberam a rota inteira nem autorizam escrita.
- Os novos links expiram em 24 horas por padrão (`OPERACIONAL_LINK_MAX_AGE`). Alterar senha/inativar o emissor revoga esses links; perder a permissão de delegação também. Quando o funcionário está indicado no link, sua inativação ou perda da habilitação invalida o acesso.
- Não há novo cadastro de autorização nem botão de revogação individual desses novos links. A expiração e as condições acima são os mecanismos disponíveis nesta implementação. O recolhimento e inventário conservam seus mecanismos de revogação anteriores.
- **Links antigos de entrega/separação sem token precisam ser reenviados pelos controles atuais depois da publicação, antes de retomar o trabalho.** Não podem continuar anonimamente abertos e ao mesmo tempo proteger os dados. Planejar a transição fora de uma contagem/entrega em andamento. Atalhos comerciais antigos continuam com a mesma URL, mas solicitam login quando a sessão estiver ausente.
- URLs por token são credenciais temporárias: não publicar, fotografar ou encaminhar a terceiros. Não incluir seus valores em relatórios ou novos logs.
- Não foram alterados os módulos JavaScript offline, o indicador, o protocolo de sincronização, UUIDs, recibos ou filas. O adaptador LOCAL existente continua restrito ao loopback autorizado e desabilitado no Render/proxy; os dois GETs usados por sua página reutilizam seu cookie assinado existente, sem criar autorização nova.

## Preparar Lincoln e Roseli ANTES de publicar a proteção

Esta etapa futura altera **apenas as contas de autenticação**, e precisa de execução explicitamente autorizada pelo responsável. Não foi feita agora.

O comando novo pode ser executado **nesta worktree local com o Python já existente**, apontando explicitamente para o banco correto. Ele não depende de estar publicado no Render. Não requer instalar um servidor PostgreSQL ou Docker no computador. É necessário somente o cliente Python/psycopg2 já utilizado pelo projeto e acesso técnico autorizado ao Neon.

1. Conferir os nomes individuais exatos que serão usados. Exemplos abaixo: `lincoln` e `roseli`. Se as contas já tiverem outros nomes, usar esses nomes reais; não criar duplicatas nem promover alguém apenas porque seu nome de exibição é semelhante.
2. Abrir PowerShell **interativo e privado**, nesta worktree. Não usar tarefa automática, build, transcrição de terminal, gravação de tela ou comando que coloque senha/URL do banco nos argumentos. A configuração privada existente do projeto deve estar disponível; não mudar a SECRET_KEY da aplicação.
3. Somente quando a conexão de produção estiver autorizada, fornecer a URL do Neon por entrada oculta, em memória, sem salvá-la na worktree:

```powershell
$pythonSeguranca = '..\..\venv\Scripts\python.exe'
$urlPrivada = Read-Host 'URL PostgreSQL Neon autorizada (entrada oculta)' -AsSecureString
$env:DATABASE_URL = [System.Net.NetworkCredential]::new('', $urlPrivada).Password
try {
    & $pythonSeguranca manage.py preparar_administrador --username lincoln
    & $pythonSeguranca manage.py preparar_administrador --username roseli
} finally {
    Remove-Item Env:DATABASE_URL
    $urlPrivada = $null
}
```

Esse primeiro comando apenas verifica existência, ativação, flags administrativas e senha utilizável. Não testa uma senha digitada nem altera a conta. Se retornar “conta ainda não preparada”, a preparação será necessária. O comando recusa SQLite de fallback.

4. Para criar ou ativar cada conta, repetir o mesmo bloco privado trocando as duas linhas de verificação por:

```powershell
& $pythonSeguranca manage.py preparar_administrador --username lincoln --ativar
if ($LASTEXITCODE -ne 0) { throw 'Lincoln ainda não está preparado. Não publicar.' }
& $pythonSeguranca manage.py preparar_administrador --username roseli --ativar
if ($LASTEXITCODE -ne 0) { throw 'Roseli ainda não está preparada. Não publicar.' }
```

O comando pede a confirmação do nome e duas entradas ocultas da nova senha. **Cada titular escolhe e digita sua própria senha, longa, exclusiva e diferente da outra.** Não enviar senhas nesta conversa. O comando valida a senha antes da gravação, aplica `is_active/is_staff/is_superuser=True` em transação e confirma a autenticação pelo backend. Se entrada oculta não for possível, aborta. Nenhuma senha é exibida ou incluída em argumento, arquivo ou mensagem de sucesso. Para conta existente, isso redefine a senha e invalida suas sessões anteriores; não fazer uma redefinição desnecessária se a conta já está preparada e o titular consegue autenticar.

5. Executar novamente as duas verificações sem `--ativar`. Devem aprovar ambas. A preparação de uma conta não prova que a outra esteja pronta.
6. **Cada titular entra com sua própria conta no `/admin/login/` do endereço atual**, em perfis/navegadores separados. Esse login já existe na versão publicada. Confirmar que cada um consegue acessar o admin com a sua identidade e privilégios completos. Não é necessário publicar `/entrar/` para realizar esta prova.
7. Somente após os dois testes reais serem confirmados, autorizar a publicação da proteção. Depois do deploy autorizado, validar também `/entrar/`, painel, Vendas, Financeiro, Inventário e Sair para ambas as contas, além dos novos links operacionais.

**Impedimento de publicação:** se faltar acesso técnico seguro, a entrada oculta não funcionar, uma conta não autenticar ou as permissões diferirem, não publicar a proteção. Não há bypass de login ou senha provisória embutida para resolver isso.

## Limites e revisão

### Validação desta entrega

- Regressão consolidada: **100 testes aprovados em 8,463 s**, no PostgreSQL descartável `inventario-confiabilidade-pg-z7c5mwy7`. Abrange segurança, sessões de inventário, endpoints de estoque manual, edição de vendas e rollback, pendências, recibos online, recolhimento e vendas LOCAL.
- Após ampliar a verificação de funcionário, o teste de link de separação passou novamente: gravação do item, conclusão, retorno à tarefa e ausência de sessão administrativa.
- Chrome: login/saída e apresentação em 1440, 390 e 320 px aprovados, com fila IndexedDB preservada byte a byte. Fluxo LOCAL no Chrome aprovado, incluindo recarregamento e rascunho.
- Sem migrations. Clusters temporários encerrados ao terminar. Nenhum banco real acessado.
- Avisos do ambiente de teste: namespace `estoque` duplicado já existente e ausência de `staticfiles` coletado na worktree; não impediram os testes.

- Nenhuma conta real foi consultada, criada ou alterada pelo agente. O responsável confirmou posteriormente que `lincoln` e `roseli` estão preparadas com administração completa e que ambas as senhas foram testadas. Não executar novamente `--ativar` nem redefinir senhas para publicar.
- Os testes usam contas e dados fictícios em PostgreSQL exclusivo, sem migrations. A política é exercitada com CSRF real e sessões de administradores, usuário comum e funcionário por capacidade.
- A auditoria anterior foi preservada como documentação (`auditoria-autenticacao-reproducao-ab01055.py.txt`), sem continuar como teste que espera que vulnerabilidades permaneçam abertas.
- Não foi implementado bloqueio de tentativas de login/MFA nesta etapa; Django mantém suas validações e senhas por hash. Não há alegação de auditoria exaustiva de toda a infraestrutura.
- Sair remove a sessão no servidor, mas preserva dados locais necessários ao offline. Usar perfis individuais em dispositivos compartilhados.
- Sem commit, push ou deploy nesta entrega.

## Conferência de liberação — 21/10/2026

- `git fetch origin` concluído. HEAD da worktree e remoto de produção coincidem em `ab01055a581d62333ad0ae3f48e3b9e9fe7313b5`. Não houve merge, reset ou alteração da branch principal divergente.
- Regressão final: 102 testes aprovados em 18,572 s (incluindo Chrome); sete verificações adicionais de entrega/recolhimento aprovadas em 1,279 s. Total: 109 testes distintos. A expectativa antiga de ausência de cookie no recolhimento foi substituída por verificações de ausência de autenticação administrativa, comprovante restrito à sessão autorizada e recusa de acesso comercial.
- Entrega de venda: link assinado abre e salva sua rota, sem permitir outra rota ou financeiro. Entrega de locação: abre, registra e retorna ao comprovante sem login administrativo. Separação, convites de inventário e recolhimento individual também exercitados.
- `git diff --check` aprovado. Não há alterações de migrations, dependências, build, serviços financeiros/de estoque ou JavaScript offline.
- Sem acesso ao serviço Render: seu SHA Live, variáveis, grupos vinculados e comandos configurados continuam pendentes de conferência no painel, somente leitura. Consultar documentação pública não comprova o estado do serviço.

### Pendências antes da autorização de publicação

1. No Render, conferir o serviço, repositório, branch de produção, último SHA Live e comandos de build/start/pre-deploy. Registrar o deploy anterior para eventual reversão.
2. Em Environment e grupos vinculados, procurar somente `SESSION_COOKIE_AGE`: ausente usa 2592000; se definida, deve corresponder a 2592000 para cumprir a decisão de 30 dias. Conferir também `SESSION_SAVE_EVERY_REQUEST` (padrão false), `DEBUG=False` e eventual `OPERACIONAL_LINK_MAX_AGE` (padrão 86400). Não revelar `SECRET_KEY`, DATABASE_URL ou outros segredos. Nenhuma variável nova é obrigatória se os padrões forem adequados. Não alterar SECRET_KEY.
3. Não salvar configurações agora: mudanças no Render podem disparar deploy. Se for necessário ajustar uma variável, fazer isso somente com autorização e coordenar o deploy; a opção Save only, quando disponível, adia seu uso até o próximo deploy.
4. O build existente chama `publicar_recolhimento` em todo build. Ele verifica o plano em transação READ ONLY e termina sem gravar quando não há pendências. Se somente locacoes.0017 estiver pendente, ele pode aplicá-la automaticamente; outras pendências bloqueiam o build. Esta melhoria não exige essa migration. Conferir nos logs do deploy anterior a ausência de pendências e impedir publicação se houver dúvida ou plano inesperado. Não executar o comando, migrations ou consultas reais nesta preparação.
5. Planejar reenvio dos links antigos de entrega/separação sem assinatura pelos controles atuais, sem tarefas em andamento. Não recriar convites válidos de inventário ou autorizações válidas de recolhimento desnecessariamente.

### Arquivos da integração proposta

19 arquivos de código, templates e testes, mais este procedimento (20 arquivos). Os três artefatos da auditoria histórica ficam fora da integração operacional.

```text
estoque/templates/estoque/base.html
estoque/templates/estoque/vendas_layout_teste.html
estoque/templates/estoque/includes/sessao_usuario.html
estoque/views.py
estoque/management/commands/preparar_administrador.py
inventario/tests_sessoes.py
locacoes/pickup_links.py
locacoes/services.py
locacoes/templates/locacoes/lista.html
locacoes/views.py
offline/test_pickup_checklist.py
offline/test_pickup_links.py
offline/tests_online_sales.py
sistema/settings.py
sistema/urls.py
sistema/acesso.py
sistema/acesso_funcionarios.py
sistema/test_seguranca.py
sistema/test_seguranca_browser.py
docs/seguranca-preparacao-contas-e-publicacao.md
```

### Publicação futura, somente após autorização

1. Exclusivamente nesta worktree, executar novamente `git fetch origin` e comparar o remoto com a base acima. Se avançou, parar; não integrar automaticamente nem mexer na branch principal.
2. Revisar `git diff` e os arquivos novos; adicionar explicitamente somente os 20 caminhos do manifesto. Conferir `git diff --cached --name-only`, `git diff --cached --check` e `git diff --cached`. Não usar `git add .`.
3. Criar commit descritivo sem `[skip render]`. Confirmar que o commit tem como pai a base aprovada e que nenhum arquivo necessário ou estranho entrou. Não são necessários migrations, criação de usuários ou alteração de permissões.
4. Enviar diretamente da worktree: `git push origin HEAD:refs/heads/backup/wip-pedidos-base-2026-06-04`, sem force. A rejeição por avanço do remoto exige interrupção; não contornar. Confirmar SHA com `git ls-remote origin refs/heads/backup/wip-pedidos-base-2026-06-04`.
5. Acompanhar Render; se autodeploy não iniciar, solicitar/acionar o deploy autorizado desse mesmo SHA no painel. Não considerar push como publicação concluída. Exigir status Live e SHA idêntico, sem migrations inesperadas nos logs.
6. Fazer validação de produção sem operações comerciais: janela anônima deve redirecionar painel/Vendas/clientes/financeiro ao login, e API de leitura deve negar acesso. Cada titular testa login, seus atalhos e consulta dos módulos. Conferir no navegador duração do cookie comercial de 2592000 segundos, Secure/HttpOnly, persistência ao fechar/abrir e Sair. Não divulgar valores dos cookies. Não efetuar vendas, recebimentos, ajustes ou sincronização forçada para esse teste.
7. Conferir abertura dos links operacionais válidos e bloqueio de acesso comercial pelo mesmo navegador de funcionário. Não gravar contagens/entregas/recolhimentos fictícios em produção. As gravações já foram testadas no banco descartável. Monitorar erros 500, recusas indevidas e ciclos de redirecionamento.

### Reversão se houver problema

- Parar o uso da função afetada; guardar status HTTP/horários sem credenciais. Não alterar contas, senhas, vendas, saldos, filas ou banco para tentar reparar um problema de código.
- O deploy anterior não contém esta proteção global: voltar a ele pode reabrir acesso anônimo. Não fazer rollback público automático. Se não houver contenção externa suficiente, suspender o serviço enquanto a correção é preparada, em vez de expor novamente dados comerciais.
- Se a reversão for autorizada com essa contenção, Render > Deploys > deploy anterior bem-sucedido > Rollback. Confirmar o SHA Live e conferir variáveis/grupos: a reversão pode reutilizar configurações do deploy anterior. O rollback pelo painel desativa autodeploy; conferir isso antes de retomá-lo.
- Nenhum rollback de banco é necessário para esta melhoria e não se deve restaurar fotografia antiga do estoque. As contas administrativas permanecem. Para alinhar o Git, preparar posteriormente um commit de reversão na linha atual, com nova revisão/autorização e push normal; nunca reset/force nem alteração da branch local divergente.

Referências oficiais: [variáveis e grupos no Render](https://render.com/docs/configure-environment-variables), [reversão no Render](https://render.com/docs/rollbacks).
