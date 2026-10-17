# Homologação Neon — resultado de 17/10/2026

**Homologação técnica de banco e fluxos aprovada. Não houve publicação.**

## Destino confirmado antes das migrations

- Branch confirmada pelo próprio servidor: `br-square-rain-amumrc37`.
- Endpoint confirmado pelo próprio servidor: `ep-shiny-bonus-ammbame5`.
- Host exclusivo: `ep-shiny-bonus-ammbame5.c-5.us-east-1.aws.neon.tech`.
- Autenticação como `neondb_owner` em `neondb`, com TLS ativo no cliente. O proxy Neon termina TLS antes do backend; a proteção foi conferida pela conexão cliente, não pelo TLS interno do PostgreSQL.
- Preflight em transação somente de leitura. Nenhuma conexão de produção foi carregada ou usada.

## Schema aplicado somente na homologação

Aplicadas exclusivamente as oito migrations previstas e aprovadas:

- `inventario.0001_initial` a `inventario.0005_acessoinventario_enviada_em_and_more`.
- `locacoes.0016_locacao_horario_previsto_devolucao` (a cópia ainda estava sem a alteração já publicada no Live informado).
- `offline.0004_local_sale_principal` e `offline.0005_validate_local_sale_principal`.

Plano posterior sem pendências. Observador ativo, `SECURITY DEFINER`, `search_path=pg_catalog`, proprietário `neondb_owner`, EXECUTE de PUBLIC revogado. Sem grants faltantes para o usuário de homologação nas tabelas e sequências novas. Constraint de identidade de operações offline validada contra os dados da cópia. Ajustes de inventário continuam desabilitados no settings e recusados no servidor.

## Preservação e recuperação

Backup custom da branch antes das migrations e fingerprints dos dados comerciais existentes. Comparação final das mesmas colunas originais de **72 tabelas e 17.978 registros**, sem divergências, incluindo Produtos, Vendas, Compras, financeiro e Locações. A nova coluna opcional de Locações não é tratada como alteração dos campos originais.

Backup restaurado em PostgreSQL 17.11 novo, em loopback; os mesmos 17.978 registros coincidiram. Cluster encerrado. O dump usou `--no-owner --no-acl`: a restauração comprova dados/schema dessa cópia, não o reprovisionamento de roles gerenciadas do Neon nem backup atual de produção. Não foi usado restore sobre a branch ou sobre produção.

## Fluxos exercitados na cópia

Os ensaios usaram transação com rollback, sem `flush` nem execução de TestCase/TransactionTestCase sobre os dados copiados:

- Produtos, Vendas, Compras e Locações abrem sem nova exigência de login. Horário opcional de devolução presente.
- Inventário exige autorização específica; sessão do funcionário não ganha acesso comercial.
- Seleção de dois produtos válidos do cadastro copiado, distribuição para dois funcionários sintéticos, geração de links HTTPS e mensagem WhatsApp sem envio.
- Contagem cega, recusa de POST sem CSRF, OK, correção do OK com histórico preservado, envios separados, conclusão geral e recusa de edição posterior.
- POST de ajuste recusado mesmo para superusuário com CSRF válido, sem escrever saldo.
- Reabertura, renovação, expiração e revogação de links.
- Venda sintética pelo endpoint comercial com baixa, financeiro e diário do observador; todos os registros sintéticos, sessões e contagens revertidos. Zero inventários persistidos após o ensaio.

As 165 execuções anteriores e os 30 testes locais de integração foram reaproveitados, não repetidos como suíte nesta branch. Ajustes nos instrumentos de homologação (Origin do cliente HTTPS, comparação de timestamps em UTC e captura de saída do PostgreSQL no Windows) foram resolvidos; execução final aprovada sem alteração funcional do aplicativo.

## Cadastros e limites restantes

A cópia tem 510 produtos ativos, nenhum saldo nulo nessa seleção e três funcionários ativos. **Um produto tem unidade/conversão inválida para contagem**: não foi corrigido automaticamente; selecionar apenas produtos validados para o piloto. Não foram encontrados fornecedores legados com texto preenchido sem vínculo ativo na relação conferida.

Os links foram verificados com HTTPS em requisições locais à aplicação conectada à homologação. **Não foi homologado domínio público nem abertura/POST em celular real**; isso depende de ambiente público autorizado. Antes de produção, confirmar backup/recuperação atual do banco operacional e os comandos efetivos de migration/build/start, definir janela e obter autorização de publicação. O observador acrescenta escrita a toda alteração de saldo e pode bloquear a transação comercial se seu diário falhar; ajustes desabilitados não removem esse efeito.

Sem commit, push, deploy ou migration em produção. Pasta principal, implementação original e outras worktrees preservadas.

## Evidências locais não publicáveis

- `.git/homologacao-conexao-verificada.json`: identidade SQL, TLS e plano inicial.
- `.git/homologacao-migrations.json`: aplicação e preservação imediata de saldos/contagens.
- `.git/homologacao-evidencias/backup.json` e `antes.dump`: backup da homologação.
- `.git/homologacao-evidencias/restauracao.json`: restauração local aprovada.
- `.git/homologacao-evidencias/fluxos.json`: resultado dos fluxos e rollback.
- `.git/homologacao-evidencias/preservacao-final.json`: igualdade final das 72 tabelas e proteções/grants.

Os arquivos de conexão e o backup não devem ser enviados ao Git ou pelo chat. O arquivo de conexão permanece em `%LOCALAPPDATA%/Codex/homologacao-inventario.env`.
