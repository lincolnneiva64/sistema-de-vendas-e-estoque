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
from .models import Inventario, RodadaContagem
from .selecao import itens_incluidos

SALT = 'inventario.encerramento-administrativo.v1'


def permitir(usuario):
    services.permitir(usuario, 'gerenciar_inventario')
    services.permitir(usuario, 'revisar_inventario')


def resumo(inventario):
    itens = list(itens_incluidos(inventario).prefetch_related('decisao', Prefetch('rodadas',
        queryset=RodadaContagem.objects.order_by('numero').prefetch_related('confirmacoes'),
        to_attr='rodadas_encerramento')).order_by('pk'))
    if not itens:
        raise ValueError('A conferencia nao possui produtos participantes.')
    evidencias=[]; aplicados=0; pendentes=0
    for item in itens:
        rodadas=item.rodadas_encerramento
        r=rodadas[-1] if rodadas else None
        if not r or r.encerrada_em or sum(x.encerrada_em is None for x in rodadas)!=1 or not r.ok_em:
            raise ValueError(f'{item.cadastro["nome"]}: falta OK valido na rodada atual. O encerramento foi recusado.')
        valores=converter(item.cadastro,r.embalagens,r.avulsos)
        if valores[2]!=r.contado or valores[3]!=r.contado_base or r.esperado is None or r.sequencia_ok is None:
            raise ValueError(f'{item.cadastro["nome"]}: contagem inconsistente. O encerramento foi recusado.')
        confirmacoes=list(r.confirmacoes.all())
        conf=max(confirmacoes,key=lambda c:c.versao_entrada,default=None)
        campos=('ok_em','esperado','contado_base','contado','sequencia_ok','embalagens','avulsos')
        if not conf or conf.versao_entrada>=r.versao or any(getattr(conf,c)!=getattr(r,c) for c in campos):
            raise ValueError(f'{item.cadastro["nome"]}: OK sem confirmacao original compativel.')
        decisao=getattr(item,'decisao',None)
        aplicados+=int(decisao is not None)
        pendentes+=int(decisao is None and r.contado_base!=r.esperado)
        evidencias.append(dict(item=item.pk,produto=item.produto_id,nome=item.cadastro['nome'],
            rodada=r.pk,numero=r.numero,versao=r.versao,confirmacao=conf.pk,
            esperado=r.esperado,contado=r.contado_base,decisao=decisao.pk if decisao else None))
    dados=dict(total=len(itens),aplicados=aplicados,sem_ajuste=len(itens)-aplicados,
        diferencas_pendentes=pendentes,rodadas=evidencias,ciclo=inventario.ciclo,versao=inventario.versao)
    dados['assinatura']=hashlib.sha256(json.dumps(dados,cls=DjangoJSONEncoder,sort_keys=True).encode()).hexdigest()
    return dados


def previa(usuario,inventario):
    permitir(usuario)
    services.exigir_nao_cancelada(inventario)
    if inventario.finalizada_em:
        raise ValueError('Conferencia ja finalizada.')
    dados=resumo(inventario)
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
