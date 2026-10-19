-- Execute apenas no PostgreSQL correto, por uma conexao ja autorizada.
-- Nao usa Django, nao executa migrations e nao confirma operacoes.
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL lock_timeout = '2s';

SELECT current_database() AS banco,
       current_setting('transaction_read_only') AS somente_leitura;

SELECT id, status, cliente_id, pessoa_avulsa_nome,
       data_entrega, data_prevista_devolucao
FROM locacoes_locacao WHERE id = 90;

-- Historico completo: nao restringir ao incidente, pois pode haver gravacao anterior.
SELECT id, tarefa_id, situacao, responsavel,
       criado_em AT TIME ZONE 'America/Belem' AS criado_em_belem,
       previsto_mesas, previsto_cadeiras,
       boa_mesas, boa_cadeiras, quebrada_mesas, quebrada_cadeiras,
       perdida_mesas, perdida_cadeiras, descartada_mesas, descartada_cadeiras,
       pendente_mesas, pendente_cadeiras
FROM locacoes_conferenciarecolhimentolocacao
WHERE locacao_id = 90 ORDER BY criado_em, id;

SELECT id, status, confirmado_por,
       confirmado_em AT TIME ZONE 'America/Belem' AS confirmado_em_belem,
       tentativa_em AT TIME ZONE 'America/Belem' AS tentativa_em_belem,
       resolvida_em AT TIME ZONE 'America/Belem' AS resolvida_em_belem,
       motivo_resolucao
FROM locacoes_tarefaoperacionallocacao
WHERE locacao_id = 90 AND tipo = 'recolhimento' ORDER BY id;

SELECT id, tipo, responsavel,
       criado_em AT TIME ZONE 'America/Belem' AS criado_em_belem, descricao
FROM locacoes_eventolocacao
WHERE locacao_id = 90 ORDER BY criado_em, id;

ROLLBACK;
