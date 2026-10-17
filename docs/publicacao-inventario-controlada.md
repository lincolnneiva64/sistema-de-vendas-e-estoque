# Candidata de publicação controlada — 17/10/2026

## Escopo e estado

Base obrigatória: `29edbfd518bed75941492589ab85d378ab0ba9d7`. Preparação exclusivamente na candidata integrada existente, preservando a pasta principal e todas as outras worktrees. Destino futuro: `refs/heads/backup/wip-pedidos-base-2026-06-04` em `https://github.com/lincolnneiva64/sistema-de-vendas-e-estoque`; a referência remota foi consultada e permanece em 29edbfd. Não confundir o remoto local original desta candidata com esse destino GitHub.

O usuário determinou nesta etapa concluir a preparação local, **sem migrations em produção, push ou deploy**. O SHA Live 29edbfd é uma confirmação fornecida pelo usuário; não foi novamente verificado no painel Render nesta sessão. Render e Neon não foram conectados por decisão do usuário. Falta confirmar os dados remotos indicados abaixo antes de obter a autorização final de execução.

Inclui inventário, autenticação/rascunhos e vendas locais já integrados e homologados; conserva integralmente o código de Locações de 29edbfd. Vendas permanece sem login obrigatório. `INVENTARIO_AJUSTES_HABILITADOS=False` permanece fixo no servidor e controla a interface; contagens não aplicam ajustes automáticos.

## Exclusões da publicação e do piloto

- Produto inconsistente identificado na homologação: **ID 501**. Excluído da seleção do inventário no servidor e na interface por `INVENTARIO_PRODUTOS_EXCLUIDOS_PILOTO=(501,)`, sem alterar cadastro/saldo nem ocultá-lo em Produtos, Vendas ou Compras. Categoria e fornecedor ignoram esse produto; requisição manual que tente incluí-lo é recusada. Três testes direcionados aprovados em PostgreSQL descartável, sem refazer as 195 execuções anteriores.
- Não incluir no commit os três módulos `inventario/demo_access.py`, `demo_local.py`, `demo_distribuicao.py` nem `tests_demo_access.py`, específico dessa demonstração. Permanecem apenas na cópia local.
- Não incluir `.env`, secrets, dumps, logs, perfis, banco de demonstração, launchers de `.git` ou artefatos de homologação. `dados.json` e o backup vazio `db.sqlite3.bkp`, herdados da base e sem referências no código, são removidos apenas da árvore de publicação; suas cópias locais são preservadas. Isso não remove arquivos de commits históricos.
- `.dockerignore` também impede copiar credenciais/artefatos locais e módulos de demonstração para uma imagem. Os settings e runners de teste preservados são exclusivamente de teste e não são carregados pelo WSGI/start normal.

## Verificado por leitura no banco operacional configurado

Conexão TLS e transação `read_only=on`. Identidade SQL: branch `br-billowing-wind-amvlraaj`, endpoint `ep-fancy-cell-amotua1w`, banco `neondb`, role `neondb_owner`. **A associação desse ID ao nome `production` ainda requer confirmação no painel/API Neon.** Nenhuma escrita, migration ou backup novo de produção foi executado nesta preparação.

Plano observado, exatamente sete migrations:

1. `inventario.0001_initial`
2. `inventario.0002_observador_quantidade`
3. `inventario.0003_movimentoobservado_origem_and_more`
4. `inventario.0004_acessoinventario_ciclo_inventario_ciclo_and_more`
5. `inventario.0005_acessoinventario_enviada_em_and_more`
6. `offline.0004_local_sale_principal`
7. `offline.0005_validate_local_sale_principal`

Locações 0016 já consta aplicada. Não existe observador de inventário instalado nesse banco. A role consultada é proprietária de Produtos e possui CREATE no schema e TRIGGER na tabela. Não havia transação aberta há mais de 30 segundos no instante da consulta; isso não garante ausência de bloqueios na janela futura. Evidência local sem credenciais: `.git/publicacao-preflight.json`.

## Recuperação: procedimento e verificação pendente

O backup restaurado e comparado na homologação **não comprova backup atual de produção nem sua janela de PITR**. Antes de qualquer alteração operacional, confirmar no Neon o ID/nome da branch production, retenção e ponto recuperável atual, ou criar backup consistente atual e provar restauração isolada; registrar horário/LSN, schema, versão, proprietário/grants e configurações. Guardar dados de mídia/storage separadamente. Não enviar credenciais ou dumps ao Git.

Em falha de aplicação, suspender contagens e reimplantar o SHA 29edbfd compatível, mantendo tabelas e históricos adicionais; não usar `migrate inventario zero`. Se o observador estiver saudável, conservá-lo para não perder o diário de movimentos feitos por escritores antigos.

Se a falha for no observador, voltar apenas o código não resolve: pausar brevemente escritores e o piloto, preservar evidências e corrigir/reverter o componente específico sob procedimento autorizado. Não desativá-lo silenciosamente e continuar contagens. Se houver necessidade de recuperar dados, restaurar o ponto/backup **em nova branch/banco**, manter a produção original preservada e reconciliar operações comerciais posteriores ao ponto de recuperação (vendas, compras, pagamentos, cancelamentos e movimentos) antes de qualquer troca autorizada. Nunca restaurar um backup antigo por cima das vendas novas ou recalcular saldos sem essa reconciliação.

## Ordem futura, somente após confirmação e autorização

1. Confirmar SHA Live 29edbfd e configuração efetiva de runtime, build, start, pre-deploy e auto-deploy no Render; confirmar o destino remoto ainda em 29edbfd. Se divergir, interromper antes de push/migration.
2. Confirmar branch production e recuperação conforme acima. Atualizar o preflight somente de leitura e comparar o plano inteiro com as sete migrations listadas; qualquer diferença exige revisão.
3. Reservar janela, monitorar locks/escritores e revisar grants da role efetivamente usada pelo Render. Instalação de FKs/trigger toma locks; toda alteração de saldo passa a escrever sequência/diário na mesma transação. Falha de privilégio/constraint/storage do diário aborta Venda/Compra mesmo com ajustes desabilitados. Isso é uma proteção deliberada, não uma garantia de impacto zero.
4. Aplicar schema uma única vez, em execução administrativa controlada com `lock_timeout=2s` e `statement_timeout=120s`, na conexão operacional validada; abortar na primeira falha, sem `--fake` e sem liberar código com schema parcial. Verificar trigger, SECURITY DEFINER, search_path fixo, grants, constraint offline validada, saldos/contagens e ausência de migrations pendentes.
5. Publicar apenas o SHA candidato aprovado para a branch indicada. Build e CMD Docker desta candidata não executam migrations; confirmar que comandos customizados do Render também não as repetem. Controlar auto-deploy para não subir código antes do schema nem uma revisão diferente da aprovada.
6. Confirmar SHA Live/logs. Abrir Vendas, Compras, Produtos e Locações sem criar registros comerciais fictícios. Validar gestor autorizado, produto 501 ausente apenas do inventário, ajustes recusados e poucas contagens reais autorizadas. Conferir link HTTPS e POST/CSRF pelo celular; testar OK individual, correção, envio e histórico. Não enviar mensagens a funcionários sem instrução específica; fornecer link para validação pelo responsável.

## Bloqueios reais para execução em produção

- Verificação atual de SHA/comandos/auto-deploy do Render e acesso aos logs.
- Confirmação do nome production para o ID SQL e comprovação de backup/ponto recuperável operacional atual.
- Autorização final de migrations, push e deploy, revogada para esta etapa de preparação local.

Depois de publicar, a verificação por celular depende da participação do responsável com o aparelho. Esse teste ainda não foi realizado em domínio público e não deve ser declarado aprovado antes disso.
