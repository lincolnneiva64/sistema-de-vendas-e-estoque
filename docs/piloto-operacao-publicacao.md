# Operação de publicação — candidata, não publicada

Não executar os comandos abaixo no Neon de produção nesta etapa. O usuário informou o deploy Live `29edbfd518bed75941492589ab85d378ab0ba9d7`; seu delta de Locações foi integrado à candidata. A publicação continua dependendo da homologação e da autorização posterior. O checkout está isolado, sem novo commit e sem branch remota alterada.

## Identificação obrigatória

Registrar serviço Render, URL HTTPS canônica, branch vinculada, tipo de runtime, build/start/pre-deploy configurados e o SHA completo do deploy marcado Live (ID e horário). A branch remota ou o último commit local não comprovam o SHA Live. Em 17/10/2026, a consulta GitHub retornou zero deployments, statuses e check-runs para f07eba0. A conta Render ainda não foi conectada.

Base histórica da candidata: f07eba0e07e70afc8e6552ae6369df02283e9610. Correções incorporadas por patch, sem commit: cfb5b7865fc8d751b0823e4c59253445f88790ee (autenticação/rascunhos), 3a84a36cd752938c3607e314d0fc330cb59b1290 (vendas locais), o estado anterior da worktree de inventário e o delta de Locações de 29edbfd518bed75941492589ab85d378ab0ba9d7. A integração de Locações passou em 30 testes direcionados, preservando as 165 execuções anteriores. Se Live mudar novamente, comparar e testar somente as diferenças afetadas antes de promover.

Nos conflitos, foram mantidas a proteção dos rascunhos por identidade, a recuperação de sessão de contas autenticadas, o transporte local limitado ao loopback e todos os controles do inventário. O login obrigatório em `/vendas/` introduzido por cfb5b78 foi excluído da candidata por conflitar com a decisão operacional explícita do usuário. Páginas online anônimas não recebem rascunhos autenticados. O endpoint local continua recusado no Render. Cache do service worker foi versionado para `offline-pilot-shell-v40-piloto-integrado`; não limpa IndexedDB.

## Build e migrations

Na candidata, `build.sh` instala dependências e coleta estáticos, sem migration. O CMD Docker também não executa migration. O comando de inicialização deve iniciar apenas a aplicação após a etapa de schema. Se o serviço usar start command customizado, remover de lá qualquer migration duplicada mediante autorização. Não alterar agora as configurações remotas.

Aplicar schema em uma única execução controlada, após backup e janela aprovados. Render oferece pre-deploy em serviços pagos; se indisponível, usar execução administrativa equivalente, única e autorizada. Pre-deploy não resolve sozinho concorrência com o servidor antigo: controlar escritores e monitorar locks na janela.

Primeiro somente inspecionar, na conexão operacional explicitamente aprovada:

```sh
python manage.py showmigrations --plan
python manage.py migrate --plan
```

Confrontar com o inventário real de `django_migrations`. A candidata acrescenta inventario 0001–0005 e também offline 0004–0005 da linha de Vendas locais, se ainda não aplicadas. Locacoes 0016 integra o código Live informado e deve estar aplicada na produção; uma cópia de homologação anterior pode ainda precisar dela, conforme o plano real da cópia. As demais pendências não devem ser presumidas nem aplicadas sem revisão. Não executar regressões, `flush`, `--fake` ou migrations de teste em produção.

Exemplo de execução futura, com limites a confirmar no ensaio representativo:

```sh
PGOPTIONS='-c lock_timeout=2s -c statement_timeout=120s' python manage.py migrate --noinput
```

Abortar ao atingir timeout ou erro, verificar transações longas, reprogramar e reaplicar após diagnóstico; não liberar o código novo com schema parcial. Defaults e expansão/validação de offline são transacionais em etapas distintas. O observador afeta toda mudança de saldo assim que instalado, inclusive nos processos antigos. Confirmar proprietário confiável da função SECURITY DEFINER, search_path fixo, trigger ativo e grants do escritor novo antes da promoção. Falha de diário deve continuar abortando a venda/compra; não esconder a exceção nem apagar auditoria.

## Backup e reversão

Antes de qualquer schema: capturar backup consistente em formato custom do PostgreSQL, guardar manifest de SHAs e migrations e os privilégios/roles que o provedor permitir exportar. Usar mecanismo protegido de credenciais, sem URL de banco em logs. O dump não inclui anexos/storage externo e pode não permitir exportar roles gerenciadas do provedor; documentar e testar o reprovisionamento das permissões da aplicação. Restaurar numa instância isolada e comparar contagens, totais de estoque, financeiro, migrations e históricos. Confirmar retenção e acesso ao ponto de recuperação com o responsável.

O ensaio local comprovou pg_dump/pg_restore entre dois bancos exclusivos do mesmo cluster, comparando produtos, quantidades, contador/diário e contas a pagar. Não é backup do Neon, nem comprovação das permissões reais.

Se houver problema após schema e antes do piloto, retornar ao SHA Live registrado somente se sua compatibilidade tiver sido ensaiada. Manter tabelas e dados adicionais. Não executar `migrate inventario zero`, nem reverter Estoque para remover dependências, nem restaurar dump antigo sobre vendas/compras mais recentes. Se o trigger for a causa, rollback de código não o remove: parar a conferência e coordenar procedimento de banco que preserve o diário e demarque qualquer intervalo sem observação. Restaurar backup em banco separado e reconciliar operações posteriores antes de uma troca de banco. Nunca declarar cadeia íntegra se o observador foi desativado durante movimento comercial.

## HTTPS, domínio e celular

Criar/renovar usa `request.build_absolute_uri`; WhatsApp recebe exatamente essa URL. A candidata reconhece `X-Forwarded-Proto: https` somente quando `RENDER=true`. Essa confiança pressupõe que a entrada passe pelo proxy Render, que define o cabeçalho. Não habilitar essa regra em outro proxy sem validar seu tratamento dos cabeçalhos.

Configurar ALLOWED_HOSTS apenas com os domínios aceitos e CSRF_TRUSTED_ORIGINS com as origens HTTPS corretas; DEBUG=False, cookies seguros, SECRET_KEY operacional preservada. Não usar settings de teste, middleware de acesso direto da demo, gestor_demo ou banco fictício na implantação. CHECKLIST_BASE_URL não controla links do inventário.

O ensaio sintético validou HTTPS, Host, CSRF e mensagem WhatsApp num domínio `.example.test`, com painel anônimo recusado e Vendas aberta. Falta provar no domínio público real: gerar link de uma atribuição autorizada, conferir HTTPS/domínio sem loopback ou portas locais, abrir no celular fora do notebook, confirmar entrada com CSRF e retorno de contagem, expiração, revogação e encerramento. Não enviar tokens em relatórios ou canais públicos.

## Cadastros mínimos

Executar somente após autorização a consulta read-only `piloto-preflight-cadastros.sql`, preferencialmente na cópia restaurada. Selecionar uma relação pequena: produto ativo/não excluído, saldo não nulo, unidade principal e fator/avulsos coerentes; categoria preenchida quando usada; vínculos ativos ProdutoFornecedor quando usado fornecedor. O texto legado de fornecedor não é vínculo. Conferir funcionários ativos e telefone/DDD, sem criar cadastro paralelo; responsável real com permissões de gerenciar/revisar e sem aprovar ajuste.

Manter `INVENTARIO_AJUSTES_HABILITADOS=False` e comprovar recusa do POST de aprovação mesmo para usuário privilegiado. Não confundir esse bloqueio com ferramentas legadas do sistema. Não fazer purgas físicas de produtos com diário. Funcionário e operação comercial devem usar perfis/aparelhos separados: sessões de link continuam intencionalmente restritas e não recebem administração.

## Liberação e validação posterior

Com evidências anteriores aprovadas e autorização expressa: versionar a candidata revisada, publicar o SHA exato após migration controlada e coletar os estáticos. Manter auto-deploy sob controle para não trocar a release aprovada por um push posterior. Não enviar pastas secrets, dados, logs, credenciais ou perfis.

Validar Vendas/Produtos/Compras/Locações sem novo login na navegação comum; observar funcionamento comercial e financeiro com operações autorizadas e rastreáveis. Validar uma conferência pequena: seleção, distribuição, links, correção do OK, histórico, envio por funcionário e conclusão geral. Monitorar latência/locks/erros SQL e armazenamento do diário. Suspender a ampliação ao primeiro problema comercial, vazamento ou ajuste indevido; não usar saldo real para teste destrutivo nem desfazer vendas por SQL.

Fontes operacionais consultadas: https://render.com/docs/deploys ; https://render.com/tutorials/web-service-vs-static-site/web-services ; https://docs.djangoproject.com/en/6.0/ref/settings/#secure-proxy-ssl-header ; https://www.postgresql.org/docs/current/app-pgdump.html . As instruções do provedor não comprovam a configuração deste serviço, que ainda precisa ser lida no Render.
