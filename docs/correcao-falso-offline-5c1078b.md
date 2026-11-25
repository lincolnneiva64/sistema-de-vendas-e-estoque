# Falso offline após 5c1078b — correção local de 25/11/2026

## Diagnóstico confirmado

Comparado `5c1078b` com `736ee30`. A suspensão introduziu no Service Worker um ramo
que interceptava **todos os GETs** e executava `fetch(event.request, {cache:'no-store'})`.
Qualquer rejeição dessa chamada em navegação era substituída por HTTP 503 contendo
apenas “Offline. Conecte-se ao servidor para continuar. Nenhuma gravação local
disponível.”. Essa frase vinha do worker, não de uma resposta do Django.

A resposta era texto puro, sem aplicação, indicador ou rotina de reconexão. Foi
reproduzido no Edge conectado ao Django habitual: provocar falha de rede na navegação
gera a mesma tela; voltar a rede não recupera a página já substituída. Antes da
suspensão, a interceptação de navegação era restrita a rotas específicas; o ramo
novo também envolvia navegação de links individuais e recursos de outras páginas.

Isso comprova a regressão de apresentação/recuperação. Não comprova qual falha de
rede originou o incidente no Render. As consultas públicas GET feitas nesta sessão
à inicial, vendas, health e worker do host configurado expiraram; não foi possível
obter o erro original de produção. O Render estar Live e a conexão do computador
funcionar não garantem sucesso de uma chamada específica ao serviço.

“Reconectando” pertence ao módulo offline anterior; o adaptador exclusivamente
online publicado não usa essa palavra. A observação é compatível com páginas ou
módulos antigos ainda carregados, mas o estado de cache dos aparelhos reais não
foi inspecionado nem alterado.

## Correção pequena, preservando a suspensão

- Com `OFFLINE_SUSPENDED=True`, o worker retorna do handler de fetch **sem**
  `respondWith`. O navegador faz a navegação/requisição normal, mantendo redirects,
  parâmetros, tokens, cookies e respostas HTTP. Não há mais página terminal
  sintetizada pelo worker nem fallback de vendas/checklists para cache.
- O worker continua instalado. Instalação/ativação suspensas continuam sem abrir,
  enumerar ou apagar caches. IndexedDB, operações e filas não são tocados.
- Na página já aberta, o indicador mostra Offline quando o navegador detecta
  ausência de rede. Uma falha de health com rede disponível informa “Servidor
  indisponível”, sem afirmar falsamente que a internet caiu.
- O health tolera até 15 segundos, em vez de 5, antes de falhar. Não é janela de
  estabilidade: retorno de rede/foco e a sondagem existente recuperam Online após
  uma resposta válida. Não há espera de 15 minutos.
- As gravações continuam exigindo confirmação do servidor. Sem confirmação, não
  há conclusão offline, comando novo em fila nem reenvio automático. O protocolo
  de envio, UUID/hash/recibo e a consulta de resultado desconhecido permanecem iguais.

Em nova navegação sem rede, o navegador apresenta seu aviso nativo de conexão.
Em uma página já aberta, o indicador e a validação de gravação informam a falha.
Erros HTTP/autorização do servidor continuam visíveis como erros reais, não são
transformados em Offline. As rotas e as regras de autorização dos checklists não
foram alteradas.

## Verificações direcionadas

14 testes automatizados aprovados:

- Fluxo online local e autenticado, proteção de resposta perdida, consulta GET,
  ausência de reenvio e preservação do armazenamento legado.
- Worker 5c1078b reproduzindo a resposta terminal e worker corrigido liberando
  navegação de vendas, checklists, links com token/querystring, health e POSTs.
- Instalação/ativação suspensas sem tocar em caches; falha de health não classificada
  como ausência de internet; recuperação imediata após resposta válida.
- Bloqueio de sincronização e de módulos offline antigos permanece ativo.
- Link operacional válido passa à conferência existente; link já concluído preserva
  o redirect ao comprovante. Essas verificações usaram sessão e consultas em memória,
  sem criar sessão, autorização ou conferência real no Neon.

No Edge com o servidor habitual em `127.0.0.1:8000`:

- Reproduzido o defeito com o worker anterior à correção local.
- Com worker atualizado, vendas abriu e indicou Online.
- Queda/retorno recuperou a indicação Online na página aberta.
- Navegação sem rede deixou de gerar a mensagem antiga; voltar a rede permitiu
  abrir vendas novamente.
- Links operacionais inválidos/inexistentes chegaram ao Django, mantendo as suas
  respostas de segurança em vez de uma tela falsa de offline.

Foram usadas somente navegações GET no sistema habitual. Nenhuma venda ou conferência
foi gravada. Não foi aberto um link válido real de funcionário, pois esse GET pode
gravar sessão no servidor. Não foi enviada mensagem de WhatsApp.

Havia dois servidores locais disputando a porta 8000. Os processos foram identificados
e consolidados em uma instância da pasta principal, com o comando habitual e DEBUG
somente no processo, conforme autorização anterior. Isso elimina a ambiguidade local;
não é apontado como causa do incidente do Render.

## Escopo e publicação futura

Código funcional alterado somente em:

- `static/offline/service-worker.js`
- `static/offline/online-only.js`

Testes em `offline/tests_suspension_js.py` e `offline/tests_navegacao_online.py`.
Scripts de verificação em `diagnostics/` são artefatos locais; não publicar essa pasta.
Nenhuma regra de venda, estoque, financeiro, recebimento Pix ou rotina da 1496 foi
alterada. As mudanças anteriores da pasta principal e da pasta fixa foram preservadas.

Correção **somente local**, ainda não publicada. Depois de eventual publicação
autorizada, carregar a página inicial com conexão para atualizar o worker e reabrir
vendas/checklists. Não limpar dados do site ou IndexedDB. Não forçar recarga de uma
montagem em andamento nem repetir uma operação sem consultar seu resultado.

Se uma falha nativa de rede/HTTP continuar em produção após a atualização, investigar
esse erro e os logs correspondentes: o novo worker não o mascara como Offline.

Sem migrations, commit, push, deploy ou alterações em dados reais nesta correção.

## Preparação na pasta fixa

Integrada por trechos em `.worktrees/publicacao-receber-css-0fe2331`, branch
`preparacao/publicacao-marco-zero`, base `5c1078b`. Os arquivos de código e testes
ficaram idênticos aos aprovados na pasta principal; as dependências relevantes e
a classe dos testes de suspensão também foram conferidas. Os 14 testes já aprovados
foram aproveitados, sem repetição, porque a integração não mudou o código testado.
Diff revisado e `git diff --check` aprovado.

Manifesto exclusivo desta correção — cinco arquivos:

```text
static/offline/service-worker.js
static/offline/online-only.js
offline/tests_suspension_js.py
offline/tests_navegacao_online.py
docs/correcao-falso-offline-5c1078b.md
```

O `docs/git-publicacao.md` não rastreado já existente na pasta fixa foi preservado
sem alteração e está fora desta seleção. Não adicionar `diagnostics/`, rotinas da
regularização financeira da 1496 ou quaisquer outras modificações da pasta principal.

Comandos para execução posterior pelo responsável, na pasta fixa. Nenhum foi
executado durante esta preparação:

```powershell
git add -- static/offline/service-worker.js static/offline/online-only.js offline/tests_suspension_js.py offline/tests_navegacao_online.py docs/correcao-falso-offline-5c1078b.md
git diff --cached
git commit -m "Corrige falso offline na navegacao e na reconexao"
$aprovado = git rev-parse HEAD
git push origin "${aprovado}:refs/heads/preparacao/publicacao-marco-zero"
```

Antes de push autorizado, conferir o SHA aprovado, os gatilhos do Render e a
configuração efetiva `OFFLINE_SUSPENDED=True`. O documento preexistente fora da
seleção pode continuar aparecendo como não rastreado; não o incluir por conveniência
para limpar o status. Após eventual publicação, conferir o mesmo SHA em GitHub e
no deploy Live e carregar a página inicial com conexão para atualizar o worker.
Não limpar IndexedDB ou filas e não forçar recarga durante montagem em andamento.
