# Piloto offline — fase 1

## Escopo

Somente observações operacionais vinculadas a uma tarefa de locação existente.
O servidor cria `EventoLocacao(tipo="observacao_offline")` e uma auditoria
`offline.OperacaoSincronizacao`. Não chama métodos de entrega, recolhimento,
pagamento, conferência, baixa ou mudança de status. Os testes comparam todos os
campos da tarefa e locação antes/depois e as contagens dos modelos físicos e financeiros.

## Arquitetura implementada

- `/offline/`: estrutura estática, sem dados pessoais embutidos ou CDN.
- `/offline/login/`: autenticação Django online; nunca cacheada pelo worker.
- `/service-worker.js`: cache explícito de quatro recursos do piloto. Nenhum POST
  ou endpoint API é interceptado. O restante do sistema continua dependendo do servidor.
- IndexedDB `vendas-offline-pilot`, versão 1: `metadata`, `operations`, `history`,
  `snapshots`. Stores existentes são preservados; não existe exclusão automática da fila.
- `device_id` aleatório persistente. Sequência alocada numa transação local.
- Comando imutável: UUID, dispositivo, usuário, ambiente, tipo, versão, tarefa,
  payload, data e sequência. SHA-256 sobre JSON canônico com chaves ordenadas,
  UTF-8 e sem espaços. O hash cobre a identidade e o conteúdo completos.
- Estados locais: pendente, enviando, resultado_desconhecido, confirmada,
  conflito e erro. A transação IndexedDB deve concluir antes de mostrar sucesso.
- Health-check a cada 30 segundos, timeout de 5 segundos incluindo leitura do
  corpo, janela de 900 segundos e lacuna máxima de 60 segundos. Relógio monotônico
  e relógio de parede verificam lacunas/suspensão; a janela não é persistida.
- Reconexão apenas mede estabilidade. Clique e confirmação explícita iniciam o envio.
- Uma operação por vez. Web Locks mantém um único remetente entre abas da origem.
  Sem Web Locks, gravação/exportação continuam disponíveis e sincronização fica
  desabilitada, com diagnóstico; não há fallback inseguro baseado em localStorage.
- Timeout/resposta ambígua mantém o UUID como resultado desconhecido. Novo envio
  só ocorre após estabilidade e nova confirmação. Não há Background Sync.
- O servidor usa UNIQUE(operation_id) + get_or_create dentro de atomic(), bloqueia
  tarefa/locação e cria evento e resultado na mesma transação. Reenvio igual
  retorna o resultado persistido; UUID com comando diferente recebe HTTP 409.
- Tarefa ausente, alterada ou finalizada/locação encerrada produz conflito
  persistido, sem evento nem alteração da entidade. O payload permanece disponível.
- Fila/histórico exportáveis em JSON sem cookies, CSRF ou senhas. O arquivo contém
  observações e identidade do usuário; deve ser guardado com acesso restrito.

## Preparação para revisão local

1. Revisar `offline/migrations/0001_initial.py`: cria somente a tabela de auditoria.
   Nenhuma migration operacional foi alterada. A implementação não aplicou migrations
   no banco operacional; os testes aplicam a migration em banco descartável.
2. No ambiente de revisão, aplicar a migration com `manage.py migrate offline`.
3. Configurar `OFFLINE_ENVIRONMENT_ID` com um identificador público e único para
   cada banco/ambiente (ex.: `deposito-revisao-v1`). Sem configuração, usa o host e
   porta da requisição. Manter o identificador estável e não compartilhar entre bancos.
4. Autorizar o usuário com `offline.registrar_observacao` no Django Admin.
   Superusuários já possuem essa permissão. Não basta escolher um operador numa tela.
5. Abrir `/offline/` em HTTPS ou localhost, autenticar online e preparar os dados.
   O snapshot contém até 200 tarefas pendentes, parciais ou não realizadas,
   ordenadas por agendamento; não é uma implementação de planejamento de rotas.
6. Acessos por IP HTTP da rede não oferecem o mesmo contexto seguro de localhost.
   Usar HTTPS para revisar pelo celular. Domínios/portas diferentes possuem filas diferentes.

## Testes automatizados

Resultado desta implementação: suíte conjunta com 16 testes, **15 aprovados e
1 pulado** (concorrência PostgreSQL, sem banco isolado disponível). Chrome real
validou Service Worker com rede bloqueada, refresh, fechamento/reabertura do
processo, UUID preservado, falha de gravação IndexedDB sem falso sucesso, janela
controlada de 15 minutos, ausência de envio automático, bloqueio entre abas,
resposta perdida após commit, timeout real de 5 segundos, interrupção dos envios
seguintes, histórico, sessão expirada e conflito com conteúdo preservado.

`manage.py check` passou com o aviso antigo de namespace `estoque` duplicado;
`makemigrations offline --check --dry-run` não detectou mudanças pendentes;
`git diff --check` não encontrou erros de whitespace.

Reinício físico do computador e espera de 15 minutos de relógio real permanecem
no roteiro manual. Concorrência PostgreSQL não foi validada nesta máquina.

API, em banco SQLite descartável:

```powershell
.\venv\Scripts\python.exe manage.py test offline.tests --settings=offline.test_settings --noinput
```

Navegador Chrome real, perfil temporário e banco descartável:

```powershell
.\venv\Scripts\python.exe manage.py test offline.tests_browser --settings=offline.test_settings --noinput
```

O executor não precisa de Playwright/Selenium. Usa o protocolo DevTools com a
biblioteca padrão Python. O Chrome roda headless, oculto, e o teste fecha somente
seu processo/perfil. `OFFLINE_TEST_CHROME` pode indicar outro executável.
`OFFLINE_BROWSER_TESTS=0` desabilita explicitamente esses testes.

Concorrência real: configurar `OFFLINE_TEST_DATABASE_URL` através de variável de
ambiente segura apontando **exclusivamente para PostgreSQL de testes**, cujo usuário
possa criar/remover o banco `test_...`, e executar `offline.tests` com as mesmas
settings isoladas. O teste de duas requisições simultâneas é pulado em SQLite.
Não usar conexão do banco operacional.

As settings de testes substituem o banco operacional. Para SQLite, o executor
ignora somente o SQL da migration antiga `estoque.0124` que aumenta um varchar
em PostgreSQL (SQLite não impõe esse tamanho). Todas as outras migrations,
inclusive a nova infraestrutura, são executadas. Nenhuma migration antiga é editada.

O relógio dos testes do navegador é controlado pelo executor DevTools. Não existe
botão, parâmetro de URL ou redução da janela no código de produção. O teste de
tempo simula sucessos a cada 30 segundos; a observação de 15 minutos reais faz
parte do roteiro manual abaixo.

## Roteiro manual

1. Autenticar online em `/offline/login/?next=/offline/`.
2. Preparar os dados; aguardar sucesso e confirmar diagnóstico de persistência.
3. Desligar a internet ou usar Network → Offline no DevTools.
4. Escolher uma tarefa e escrever “Ligar antes de chegar”. Salvar.
5. Anotar o UUID mostrado; atualizar a página e conferir texto/UUID na fila.
6. Fechar completamente o navegador e reabrir `/offline/` na mesma origem/perfil.
7. Reiniciar o computador, preservando dados do site, e repetir a conferência.
8. Voltar à rede. Conferir que não houve envio imediato.
9. Aguardar 15 minutos com testes contínuos. Interromper a conexão no meio e
   verificar que a contagem volta a zero. Suspender por mais de 60 segundos e
   verificar novamente. Uma janela sem lacunas deve habilitar Sincronizar agora.
10. Conferir que, mesmo habilitado, nenhum evento foi criado sem clique.
11. Clicar, confirmar e conferir status confirmada, UUID, evento e resultado.
12. Exportar o histórico e conferir que não há cookies, senha ou token CSRF.
13. Reenvio proposital em revisão técnica: ler a operação confirmada do IndexedDB,
    enviar novamente somente o comando e seu payload_hash para
    `/api/offline/observations/` com CSRF atual. A API deve devolver o mesmo
    record_id/completed_at; verificar que há um único evento e uma auditoria.
14. Para resposta perdida, usar o executor automatizado, que permite o commit e
    descarta a resposta antes de chegar ao aplicativo. Conferir resultado_desconhecido;
    após nova janela e clique, deve confirmar o mesmo evento, sem duplicá-lo.
15. Criar duas observações, derrubar comunicação durante a primeira e verificar
    que a segunda não é enviada e ambas permanecem recuperáveis.
16. Expirar a sessão antes do clique: deve pedir autenticação e preservar a fila.
17. Abrir duas abas e tentar sincronizar juntas: somente uma detém o Web Lock.
18. Cancelar/finalizar a tarefa por um fluxo online autorizado antes de sincronizar
    uma observação antiga: deve aparecer conflito com o conteúdo preservado.
19. Comparar status/saldos da tarefa e locação e estoque/financeiro antes/depois:
    somente a observação e auditoria devem ser criadas.

## Limitações e riscos restantes

- Nenhuma operação comercial/física foi habilitada offline.
- Permissão do piloto permite consultar o conjunto limitado de tarefas; não há
  segmentação por motorista/rota nesta fase.
- O snapshot não é atualizado automaticamente; o botão de preparação atualiza-o.
- Não existe login offline nem verificação online de revogação enquanto sem rede.
  O snapshot e a fila pertencem ao perfil do navegador; usar perfil de acesso
  controlado. Sincronização exige o mesmo usuário autenticado e autorizado.
- Limpeza do perfil, modo privado, perda do disco ou limpeza dos dados do site
  podem remover IndexedDB. Persistência reduz despejo automático, não substitui
  backup. Exportação é manual; não existe importação/restauração automática.
- Conflitos não têm resolução automática nem edição de comandos enviados.
- Cache de recursos usa versão explícita; futuras alterações devem atualizar
  a versão do worker, mantendo compatibilidade com operações antigas.
- A validação PostgreSQL depende de banco isolado disponível e é requisito antes
  de publicação; aprovação em SQLite não comprova locks em produção.
- Continua existente o aviso de namespace `estoque` duplicado, anterior ao piloto.
- Não houve commit, push, publicação ou migração do banco operacional.

## Arquivos desta fase

Alterados: `.env.example`, `sistema/settings.py`, `sistema/settings_sqlite.py`,
`sistema/urls.py` e `estoque/templates/estoque/base.html`.

Criados:

- `offline/__init__.py`, `apps.py`, `models.py`, `services.py`, `views.py`, `urls.py`.
- `offline/migrations/__init__.py` e `0001_initial.py` (única migration nova).
- `offline/templates/offline/login.html`.
- `offline/tests.py`, `tests_browser.py`, `browser_support.py`, `test_settings.py`, `test_runner.py`.
- `static/offline/core.js`, `app.js`, `service-worker.js`, `pilot.html`, `pilot.css`.
- Este documento.

A alteração preexistente em `estoque/templates/estoque/separacao_vendas_fila.html`
e os `.bak`/`.txt` existentes foram preservados e não pertencem à implementação.
