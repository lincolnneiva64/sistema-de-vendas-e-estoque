-- Read-only. Review and authorize the target connection before running.
-- No import, update, creation of employees, balance correction or schema change.
BEGIN READ ONLY;

-- Totals and coverage of the selection methods.
SELECT count(*) AS produtos_ativos,
       count(*) FILTER (WHERE categoria IS NULL OR btrim(categoria)='') AS sem_categoria,
       count(*) FILTER (WHERE quantidade IS NULL) AS sem_saldo
FROM estoque_produto WHERE ativo AND NOT excluido;

-- Products requiring registration/conversion review before the selected pilot.
SELECT id, nome, unidade_compra, unidade_venda_1, unidade_venda_2,
       fator_conversao, vende_fracionado
FROM estoque_produto
WHERE ativo AND NOT excluido AND (
    coalesce(nullif(btrim(unidade_venda_1),''),nullif(btrim(unidade_compra),'')) IS NULL
    OR (vende_fracionado AND (
        coalesce(btrim(unidade_venda_2),'')=''
        OR upper(btrim(unidade_venda_2))=upper(coalesce(nullif(btrim(unidade_venda_1),''),btrim(unidade_compra)))
        OR coalesce(fator_conversao,0)<=0))
    OR quantidade IS NULL
);

-- Legacy supplier text does not constitute an active ProductSupplier relation.
SELECT count(*) AS com_fornecedor_texto_sem_vinculo_ativo
FROM estoque_produto p
WHERE p.ativo AND NOT p.excluido AND coalesce(btrim(p.fornecedor),'')<>''
  AND NOT EXISTS (SELECT 1 FROM estoque_produtofornecedor pf WHERE pf.produto_id=p.id AND pf.ativo);

SELECT id,nome,telefone_whatsapp_normalizado
FROM estoque_funcionario WHERE ativo ORDER BY nome;
ROLLBACK;
