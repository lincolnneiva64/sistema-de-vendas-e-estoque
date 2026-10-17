# Retomada do piloto — 17/10/2026

**Atualização posterior:** a conexão foi disponibilizada e a homologação técnica na branch Neon foi concluída. O resultado atual, incluindo migrations aplicadas, preservação dos dados, restauração e limites restantes, está em [homologacao-neon-resultado.md](homologacao-neon-resultado.md). As pendências de conexão abaixo são o registro histórico da retomada, já resolvidas.

## Pronto

- Candidata existente atualizada, sem recriação, em `secrets/integracao-piloto`. Preservados inventário, autenticação/rascunhos, vendas locais e demais correções integradas.
- Incorporado apenas o delta dos 13 arquivos do commit Live informado `29edbfd518bed75941492589ab85d378ab0ba9d7`. O template compartilhado de Vendas foi mesclado em três vias sem conflito. Os demais arquivos de Locações coincidem com o commit publicado. A aplicação reversa do patch completo foi verificada sem alterar fontes, comprovando a presença de seus hunks.
- Ajustes reais continuam bloqueados: `INVENTARIO_AJUSTES_HABILITADOS=False`, serviço e POST privilegiado recusados; sem bypass administrativo da demo e sem novo login obrigatório na navegação comercial.
- Build e Docker continuam sem migrations automáticas. Grafo sem conflitos e modelos sem migration faltante. Folhas: `estoque.0134`, `inventario.0005`, `offline.0005`, `locacoes.0016`.
- Histórico das 165 execuções anteriores preservado. Somente **30 testes direcionados adicionais**, todos aprovados, em PostgreSQL descartável com guarda de host/porta/diretório e Chrome: disponibilidade e alertas de Locações, integração de Vendas/Compras com o diário, vendas locais/identidade/replay, navegação compartilhada, HTTPS/CSRF sintético e bloqueio de ajustes no serviço e no POST. Cluster encerrado. Nenhuma suíte foi executada no Neon.
- Sem commit, push, deploy ou alteração de outra worktree/pasta principal. Índice preexistente da candidata preservado; alterações novas estão nos arquivos de trabalho. O estado Git continua sobre a base histórica `f07eba0`, não constitui um novo SHA publicado.

Evidência dos 30 testes: `%TEMP%/inventario-disposable-pg-5v91y8ep/tests.log` e `validation.json`. A primeira tentativa foi impedida pelo token restrito do `pg_ctl`, antes dos testes; a execução seguinte no cluster novo passou. Snapshot e manifesto da integração: `.git/retomada-29edbfd/`. Launcher dos testes direcionados: `.git/validar_retomada_locacoes.py`. Patch completo atualizado: `.git/retomada-29edbfd/piloto-integrado-atual.patch`; manifesto e hash em `.git/retomada-29edbfd/artefatos.json`. Esses arquivos são locais e não devem ser publicados.

## O que falta de fato

Executar a homologação já preparada na branch Neon `br-square-rain-amumrc37`: plano efetivo, instalação controlada, observador/grants e preservação dos saldos/cadastros, fluxo pequeno de conferência e compatibilidade comercial. Antes da liberação real, concluir as evidências de recuperação e domínio público/links e obter autorização de publicação. Os ensaios sintéticos anteriores não substituem essas verificações.

A conexão continua indisponível: nenhuma variável específica de homologação e nenhum arquivo de configuração externo nos locais previstos. Não foram lidas ou usadas credenciais de produção. Nenhuma migration foi executada no Neon. Não se presume que a branch criada seja backup restaurável homologado.

## Única próxima ação

Disponibilizar a conexão **exclusiva de homologação** no arquivo externo `%LOCALAPPDATA%/Codex/homologacao-inventario.env`, com `DATABASE_URL`, `NEON_BRANCH_ID=br-square-rain-amumrc37` e `NEON_ENDPOINT_ID` correspondente, já conferidos no painel Neon. Não enviar credenciais pelo chat. Expiração informada: **18/10/2026 às 10h28, horário de Brasília**; se necessário, renovar a homologação antes dessa disponibilização. Com isso, o operador pode assumir o ensaio restante usando `.git/homologar.py`, sem refazer a investigação ou as 165 execuções anteriores.
