"""Close counted inventory administratively; never execute or reconcile stock adjustments."""
import hashlib
import json
from django.core import signing
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone
from . import services
from .conversao import converter
from .conversao_fisica import cadastro_contagem, carregar
from .models import Inventario, RodadaContagem, PropostaAjuste, EventoInventario
from .selecao import itens_incluidos

SALT = 'inventario.encerramento-administrativo.v1'


def permitir(usuario):
    services.permitir(usuario, 'gerenciar_inventario')
    services.permitir(usuario, 'revisar_inventario')


def resumo(inventario, *, resumo_confirmacao=None):
    itens = list(itens_incluidos(inventario).prefetch_related('decisao', Prefetch('rodadas',
        queryset=RodadaContagem.objects.order_by('numero').prefetch_related('confirmacoes'),
        to_attr='rodadas_encerramento')).order_by('pk'))
    if not itens:
        raise ValueError('A conferencia nao possui produtos participantes.')
    carregar(itens)
    from .marco_fisico import recibos
    marcos = {a.proposta.rodada_id: a for a in recibos(PropostaAjuste.objects.filter(
        rodada__item__inventario=inventario, estado='aplicado'))}
    administrativas = {r.pk for item in itens for r in item.rodadas_encerramento
        if any(c.acesso_id is None for c in r.confirmacoes.all())}
    eventos_admin = {e.dados.get('confirmacao_id'): e for e in EventoInventario.objects.filter(
        rodada_id__in=administrativas, acao='contagem_admin_confirmada')}
    evidencias=[]; aplicados=0; pendentes=0
    for item in itens:
        rodadas=item.rodadas_encerramento
        r=rodadas[-1] if rodadas else None
        if not r or r.encerrada_em or sum(x.encerrada_em is None for x in rodadas)!=1 or not r.ok_em:
            raise ValueError(f'{item.cadastro["nome"]}: falta OK valido na rodada atual. O encerramento foi recusado.')
        if r.pk not in marcos:
            try:
                valores=converter(cadastro_contagem(item),r.embalagens,r.avulsos)
            except ValueError as exc:
                raise ValueError(f'{item.cadastro["nome"]} (produto #{item.produto_id}): {exc} '
                                 'Revise a contagem e a relação física antes de finalizar.') from exc
            if valores[2]!=r.contado or valores[3]!=r.contado_base or r.esperado is None or r.sequencia_ok is None:
                raise ValueError(f'{item.cadastro["nome"]}: contagem inconsistente. O encerramento foi recusado.')
        confirmacoes=list(r.confirmacoes.all())
        from .marco_fisico import ok_referencia
        try:
            if r.pk in marcos:
                # Already committed receipts are not pending administrative observations.
                # Preserve their original interpretation without replaying the application.
                conf=marcos[r.pk].proposta.confirmacao
                campos=('ok_em','esperado','contado_base','contado','sequencia_ok','embalagens','avulsos')
                if conf.rodada_id!=r.pk or any(getattr(conf,c)!=getattr(r,c) for c in campos):
                    raise ValueError('Rodada atual incompatível com o recibo aplicado.')
            else:
                conf=ok_referencia(r, confirmacoes, eventos_admin)
        except ValueError as exc:
            raise ValueError(f'{item.cadastro["nome"]}: {exc}') from exc
        decisao=getattr(item,'decisao',None)
        aplicados+=int(r.pk in marcos)
        pendentes+=int(r.pk not in marcos and r.contado_base!=r.esperado)
        evidencias.append(dict(item=item.pk,produto=item.produto_id,nome=item.cadastro['nome'],
            rodada=r.pk,numero=r.numero,versao=r.versao,confirmacao=conf.pk,
            esperado=r.esperado,contado=r.contado_base,decisao=decisao.pk if decisao else None))
    dados=dict(total=len(itens),aplicados=aplicados,sem_ajuste=len(itens)-aplicados,
        diferencas_pendentes=pendentes,rodadas=evidencias,ciclo=inventario.ciclo,versao=inventario.versao)
    if resumo_confirmacao is None:
        from .confirmacao_estoque import consultar
        resumo_confirmacao = consultar(inventario)
    dados.update(resumo_confirmacao)
    dados['assinatura']=hashlib.sha256(json.dumps(dados,cls=DjangoJSONEncoder,sort_keys=True).encode()).hexdigest()
    return dados


def previa(usuario,inventario, *, resumo_confirmacao=None):
    permitir(usuario)
    services.exigir_nao_cancelada(inventario)
    if inventario.finalizada_em:
        raise ValueError('Conferencia ja finalizada.')
    dados=resumo(inventario, resumo_confirmacao=resumo_confirmacao)
    dados['token']=signing.dumps(dict(usuario=usuario.pk,inventario=inventario.pk,assinatura=dados['assinatura']),salt=SALT)
    return dados


@transaction.atomic
def finalizar(usuario,inventario_id,token,confirmacao):
    permitir(usuario)
    if confirmacao!='sim':
        raise ValueError('Confirme expressamente o encerramento sem aplicar ajustes.')
    try:
        entrada=signing.loads(token or '',salt=SALT,max_age=1800)
    except (signing.BadSignature,TypeError,ValueError):
        raise ValueError('Confirmacao invalida ou expirada. Atualize a pagina.')
    if (entrada.get('usuario'),entrada.get('inventario'))!=(usuario.pk,inventario_id):
        raise ValueError('Confirmacao nao pertence a esta conferencia ou usuario.')
    inventario=Inventario.objects.select_for_update().get(pk=inventario_id)
    services.exigir_nao_cancelada(inventario)
    if inventario.finalizada_em:
        return dict(ja_finalizada=True)
    dados=resumo(inventario)
    if dados['assinatura']!=entrada.get('assinatura'):
        raise ValueError('A conferencia mudou. Atualize a pagina e revise os totais antes de finalizar.')
    inventario.finalizada_em=timezone.now()
    inventario.finalizada_por=usuario
    inventario.finalizada_por_acesso=None
    inventario.finalizacao_versao=inventario.versao
    inventario.versao+=1
    inventario.save(update_fields=['finalizada_em','finalizada_por','finalizada_por_acesso','finalizacao_versao','versao'])
    # The explicit administrative acknowledgement replaces further employee submissions,
    # preserving their original records, cycles and every existing stock restriction.
    auditoria=json.loads(json.dumps(dados,cls=DjangoJSONEncoder))
    services.evento(inventario,'conferencia_finalizada',usuario=usuario,origem='encerramento_administrativo',
        finalizada_em=inventario.finalizada_em.isoformat(),
        estoque_alterado=False,envios_preservados=True,**auditoria)
    return dados
