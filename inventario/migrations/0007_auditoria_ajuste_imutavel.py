from django.db import migrations


SQL = """
CREATE FUNCTION public.inventario_auditoria_ajuste_fn() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  RAISE EXCEPTION 'Auditoria de ajuste imutavel: use movimentacao compensatoria';
END; $$;
CREATE TRIGGER inventario_aplicacao_imutavel BEFORE UPDATE OR DELETE
ON public.inventario_aplicacaoajuste FOR EACH ROW EXECUTE FUNCTION public.inventario_auditoria_ajuste_fn();
CREATE TRIGGER inventario_evento_ajuste_imutavel BEFORE UPDATE OR DELETE
ON public.inventario_eventoajuste FOR EACH ROW EXECUTE FUNCTION public.inventario_auditoria_ajuste_fn();
CREATE FUNCTION public.inventario_proposta_congelada_fn() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'Proposta auditada nao pode ser apagada';
  END IF;
  IF (to_jsonb(NEW) - 'estado') IS DISTINCT FROM (to_jsonb(OLD) - 'estado')
     OR NOT (OLD.estado = 'aprovado' AND NEW.estado IN ('aplicado','rejeitado')) THEN
    RAISE EXCEPTION 'Evidencia congelada ou transicao invalida de proposta';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER inventario_proposta_congelada BEFORE UPDATE OR DELETE
ON public.inventario_propostaajuste FOR EACH ROW EXECUTE FUNCTION public.inventario_proposta_congelada_fn();
"""

REVERSE = """
DROP TRIGGER inventario_proposta_congelada ON public.inventario_propostaajuste;
DROP FUNCTION public.inventario_proposta_congelada_fn();
DROP TRIGGER inventario_aplicacao_imutavel ON public.inventario_aplicacaoajuste;
DROP TRIGGER inventario_evento_ajuste_imutavel ON public.inventario_eventoajuste;
DROP FUNCTION public.inventario_auditoria_ajuste_fn();
"""


class Migration(migrations.Migration):
    dependencies = [('inventario', '0006_propostas_aplicacoes_ajuste')]
    operations = [migrations.RunSQL(SQL, reverse_sql=REVERSE)]
