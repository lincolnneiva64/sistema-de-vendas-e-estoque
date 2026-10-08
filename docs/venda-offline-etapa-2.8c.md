# Etapa 2.8C — revisão controlada de conflitos

Base: `627dc29`. Worktree: `.worktrees/offline-2-8c`; branch: `offline-2-8c`.
Implementação preparada para revisão, sem staging, commit ou push.

## Fluxo e identidade

O diagnóstico de um `criar_venda` com receipt comercial de conflito oferece
“Revisar venda”. A navegação abre `/offline/revisao/?operation_id=UUID`, uma
tela própria, sem executar o formulário online de vendas. O shell compartilhado
contém apenas estrutura e ambiente; cliente, itens, ator e operação ficam no IDB.

Somente conflito comercial válido, no ator/ambiente/dispositivo original, pode
iniciar uma revisão. Não são elegíveis confirmado, pendente, erro,
resultado_desconhecido ou UUID divergente. Ao iniciar/reabrir online, a consulta
autenticada existente valida UUID, hash, ator, ambiente, dispositivo e tipo.
Ausência, incompatibilidade, autenticação recusada ou resposta indeterminada
bloqueiam a abertura. Uma revisão existente pode ser retomada offline para edição.

A original nunca é escrita pelo módulo de revisões: UUID, payload, hash, receipt,
status, histórico e sequência originais permanecem preservados. Resultados
oficiais descobertos são registrados em `metadata` sob uma chave de observação
separada. Confirmação da original bloqueia a revisão e apresenta o link oficial.
A observação usa transação com leitura da versão persistida: resposta antiga não
regride confirmação nem apaga vínculos já conhecidos.

## Montagem e persistência

O draft de revisão usa chave específica em `metadata`, composta por ambiente,
ator, dispositivo e UUID original. O draft normal mantém sua chave e conteúdo.
Uma chave de versão independente sobrevive ao descarte e impede uma aba antiga
de ressuscitar/sobrescrever a versão descartada. As transações relêem identidade,
dispositivo, original e revisão. Duas abas retomam o mesmo draft; escrita antiga
falha e conclusão simultânea reconhece a mesma operação.

Os dados comerciais são copiados por whitelist. Não se reutiliza o restaurador
normal, pois o comando não preserva fator, unidades alternativas ou prazo padrão
original do cliente. Esses campos não são inventados. O catálogo preparado é
exibido apenas como referência com sua data. Produto ausente continua visível.
Escolher produto não preenche automaticamente preço ou unidade; trocar cliente
não recalcula vencimento. Origem de recebimento exige edição explícita.

Editáveis: cliente, itens/produto, quantidade, unidade, preço, pagamento,
vencimento e caixa/banco. Data da venda e operador são preservados e não têm
controles editáveis. Total é apenas calculado para referência; custo, estoque e
saldo não são editáveis. Campos incompletos podem ser salvos no draft e são
validados ao concluir.

## Conclusão e protocolo

Concluir exige conexão e uma NOVA consulta oficial da original. Se não continuar
em conflito comercial, se já houver substituta registrada ou se a consulta
falhar, o draft é preservado e não nasce operação. A condição online é conferida
novamente dentro da transação de conclusão.

O UUID próprio do draft de revisão é promovido para identidade da nova operação
somente na conclusão. UUID original nunca é reutilizado. A transação reserva
nova sequência e adiciona operação `pendente`, vínculo local/marcador concluído
e revisão de versão atomicamente. Depois disso, retries usam exatamente o mesmo
UUID, hash, payload, sequence e created_at. Novo UUID não nasce a cada retry.

O envelope versão 1 permanece intacto. A extensão opcional `payload.revisao`
integra o hash do comando e contém exatamente:

- `original_operation_id`;
- `original_hash`;
- `relacao: revisao_de_conflito`;
- `revisada_em`, timestamp com timezone.

O novo UUID é o `operation_id` do envelope; ator, ambiente e dispositivo são os
campos existentes do comando. O serviço comercial recebe somente dados comerciais,
sem `revisao`. Comandos anteriores sem esse campo continuam válidos e seus hashes
não mudam.

## Backend e migration

`offline.0002_revisaovendaoffline` cria `RevisaoVendaOffline`. Original e substituta
são `OneToOneField` com `PROTECT`, além de relação, hash original, ator, ambiente,
motivo original, data da revisão e data de registro. Não migra nem atualiza dados
de operações existentes. Não adiciona campo ou estado em `Venda`.

O processamento idempotente registra a nova operação e bloqueia a original com
`select_for_update` antes de efeitos comerciais. Verifica identidade, hash do
comando persistido, conflito comercial, ausência de substituta, data e operador
originais. Lock + unicidade impedem duas substitutas para a mesma origem, inclusive
em dispositivos diferentes. Relação, operação, efeitos e receipt ficam na mesma
transação externa. Falha técnica desfaz tudo; conflito comercial desfaz efeitos
comerciais e conserva relação/receipt da substituta rejeitada.

Recusa da origem gera receipt explícito `revisao_origem_bloqueada`, com
`conflict_kind: tecnico`, sem venda e sem elegibilidade para nova revisão. Se uma
confirmação tardia bloquear a substituta na sincronização, a tela reconsulta a
original para recuperar sua venda oficial. Uma substituta com conflito comercial
pode ser revisada, formando original → revisão 1 → revisão 2.

GET de resultado continua sem efeitos comerciais e acrescenta `revisions`, cadeia
de vínculos e receipts validada por identidade/hash. Receipts históricos não são
alterados. Uma substituta confirmada resolve apenas a apresentação da origem e
do contador, mantendo `conflito` no armazenamento/histórico.

## Sincronização e comunicação ao cliente

Abrir, editar e concluir localmente não criam Venda, ItemVenda, estoque ou financeiro.
Mesmo conectada, a revisão só cria operação local. O sincronizador manual existente
mantém Web Lock e janela de 900000 ms. Uma substituta em resultado_desconhecido usa
o fluxo 2.8B existente: consulta antes de retry, sem POST após recuperação do receipt.

Após confirmação: “Venda corrigida confirmada. Se você enviou ao cliente informações
da versão anterior, envie novamente a nota correta.” Não há envio automático,
registro de WhatsApp ou inferência de entrega.

## Assets e publicação posterior

Novo namespace `/offline/assets/2-8c/`, grafo coerente de 13 módulos e cache
`offline-pilot-shell-v25-2-8c`. Rotas antigas de assets continuam disponíveis.
O worker prepara o shell genérico de revisão para refresh offline; APIs e conteúdo
comercial não entram no cache compartilhado. Aplicar migration no backend antes
de disponibilizar a nova versão dos assets. Nenhum deploy foi realizado.

## Validação

Os testes novos cobrem imutabilidade da original e histórico, draft normal
coexistente, elegibilidade, consultas de abertura/conclusão, falhas/identidades,
catálogo alterado/ausente, campos explícitos, offline/refresh/reabertura,
descarte, rollback IDB, duas abas/conclusões, confirmação tardia antes e depois
da conclusão local, cadeia, reconciliação 2.8B e ausência de efeitos antes do envio.
Teste PostgreSQL adicional cobre dois dispositivos concorrentes; exige
`OFFLINE_TEST_DATABASE_URL` apontando para banco isolado.

Resultados finais em SQLite isolado em arquivo e Chrome real, fora do sandbox:

- Direcionados finais: **42 testes em 149,150 s; 41 aprovados, nenhum erro/falha,
  1 PostgreSQL pulado**. Incluem novos cenários, lookup/sync 2.8A/B e os dois
  cenários de cache que agora verificam exatamente v25-2-8c.
- Regressão completa `manage.py test offline`: **142 testes em 462,824 s;
  138 aprovados, nenhuma falha/erro, 4 PostgreSQL pulados**. Inclui os 117 casos
  anteriores, 6 casos de histórico de checklist e 19 casos novos desta etapa.
- `makemigrations offline --check --dry-run`: modelo/migration coerentes.
- Comparação com 627dc29: core.js integral e monitor de conexão de app.js
  preservados. Novos vínculos não alteram a regra de reconciliação 2.8B.

A revisão detectou e corrigiu uma corrida da apresentação da confirmação tardia:
uma atualização nova podia terminar antes de uma consulta retida, descartando a
apresentação antiga que havia recuperado o receipt. Evidência mostrou confirmação
persistida com link ainda oculto. Atualizações concorrentes agora aguardam a mesma
consulta; teste mantém a resposta retida e dispara duas notificações antes da
liberação. Não há retry/supressão de erro no teste ou cliente.

Pulados por falta de `OFFLINE_TEST_DATABASE_URL` para PostgreSQL isolado:

- offline.tests.PostgreSQLConcurrencyTests.test_concurrent_same_uuid_creates_one_event
- offline.tests_sale_creation.SaleCreationPostgreSQLTests.test_mesmo_uuid_concorrente_cria_uma_venda_e_um_financeiro
- offline.tests_sale_creation.SaleCreationPostgreSQLTests.test_mesmo_uuid_concorrente_conteudo_diferente_preserva_vencedor
- offline.tests_sale_revisions.SaleRevisionPostgreSQLTests.test_two_devices_create_only_one_replacement_sale

Concorrência real PostgreSQL permanece sem revalidação; a restrição de unicidade,
o lock da origem e o teste específico estão implementados. Migration foi aplicada
somente nos bancos de teste. Avisos preexistentes: namespace estoque duplicado e
ausência de staticfiles no worktree. Logs no TEMP: offline-28c-directed-complete.log,
offline-28c-regression-complete.log e offline-28c-lookup-race-proven.log.
