"""Explicit administrative attribution, stored in the existing inventory journal.

Only the signed, scoped acknowledgement supplies a missing legacy record link.
Neither the original event nor any stock/count/document row is rewritten.
"""
from collections import defaultdict
from django.core import signing
from django.db import DatabaseError, transaction

from estoque.models import EventoVenda
from . import services
from .evidencia_remocoes import LEGADO, comprovar
from .models import EventoInventario

ACAO = 'remocao_legada_reconhecida'
SALT = 'inventario-remocao-legada-v1'


def congelar(dados):
    return {k: (v.isoformat() if hasattr(v, 'isoformat') else
                str(v) if hasattr(v, 'as_tuple') else v) for k, v in dados.items()}


def contexto(r):
    return dict(inventario=r.item.inventario_id, rodada=r.pk, produto=r.item.produto_id,
                versao=r.versao, ok_em=r.ok_em.isoformat() if r.ok_em else None,
                sequencia_ok=r.sequencia_ok)


def carregar(lote):
    lote.reconhecimentos_legados = defaultdict(list)
    if any(LEGADO.fullmatch(e.get('descricao', '')) for es in lote.eventos.values() for e in es):
        for registro in EventoInventario.objects.filter(rodada_id__in=lote.rodadas, acao=ACAO):
            lote.reconhecimentos_legados[registro.rodada_id].append(registro)


def mapa(r, reconhecimentos, eventos, registros, venda_id):
    resultado = {}
    usados = set()
    por_evento = {e['pk']: congelar(e) for e in eventos}
    por_registro = {x['id']: congelar(x) for x in registros}
    for registro in reconhecimentos:
        try:
            dados = registro.dados
            p = signing.loads(dados['selo'], salt=SALT)
            if (p != dados['reconhecimento'] or p['contexto'] != contexto(r)
                    or registro.inventario_id != r.item.inventario_id or registro.rodada_id != r.pk
                    or p['responsavel'] != registro.usuario_id
                    or not all(str(p[c]).strip() for c in ('documento', 'evidencia', 'motivo'))):
                raise ValueError
            if p['registro']['venda_id'] != venda_id:
                continue
            if (p['evento'] != por_evento.get(p['evento']['pk'])
                    or p['registro'] != por_registro.get(p['registro']['id'])):
                raise ValueError
            evento_id, registro_id = p['evento']['pk'], p['registro']['id']
            if evento_id in resultado or registro_id in usados:
                raise ValueError
            resultado[evento_id] = registro_id
            usados.add(registro_id)
        except (signing.BadSignature, KeyError, TypeError, ValueError):
            raise ValueError('Reconhecimento legado ausente, alterado ou desatualizado. Solicite revisão administrativa.') from None
    return resultado


def sugestoes(r, lote):
    """Reuse request-local data, without per-product queries or automatic approval."""
    saida = []
    if not r.ok_em or r.encerrada_em:
        return saida
    vendas = {int(m.referencia[7:]) for m in lote.movimentos[r.item.produto_id]
              if m.referencia.startswith('Venda #') and m.referencia[7:].isdigit()}
    for venda_id in sorted(vendas):
        eventos = lote.eventos[venda_id]
        registros = lote.registros_removidos[venda_id]
        try:
            aprovados = mapa(r, lote.reconhecimentos_legados[r.pk], eventos, registros, venda_id)
        except ValueError:
            aprovados = {}
        for e in eventos:
            if e['tipo_evento'] != 'item_removido_da_nota' or not LEGADO.fullmatch(e.get('descricao', '')) or e['pk'] in aprovados:
                continue
            candidatos = []
            for registro in registros:
                try:
                    comprovar(venda_id, r.item.produto_id,
                        {r.item.cadastro['nome'].strip().casefold(), r.item.produto.nome.strip().casefold()},
                        lote.itens[venda_id], [e], registros, legados={e['pk']: registro['id']})
                    candidatos.append(registro)
                except ValueError:
                    pass
            previa = signing.dumps(dict(contexto=contexto(r), evento=congelar(e),
                registros=[congelar(x) for x in candidatos]), salt=SALT+'-previa', compress=True)
            saida.append(dict(evento=e, venda=venda_id, candidatos=candidatos, previa=previa))
    return saida


@transaction.atomic
def reconhecer(usuario, inventario_id, rodada_id, previa, registro_id, documento, evidencia, motivo, confirmacao=False):
    services.permitir(usuario, 'revisar_inventario')
    campos = dict(documento=str(documento or '').strip(), evidencia=str(evidencia or '').strip(), motivo=str(motivo or '').strip())
    if not confirmacao or any(not v or len(v) > 4000 for v in campos.values()):
        raise ValueError('Informe documento independente, evidência do vínculo e motivo; confirme a verificação. Nome e horário não bastam.')
    try:
        dados = signing.loads(previa, salt=SALT+'-previa', max_age=1200)
        registro_id = int(registro_id)
    except (signing.BadSignature, TypeError, ValueError):
        raise ValueError('Prévia inválida ou expirada. Atualize a página.') from None
    from . import ajustes, revisao_historico
    from .contexto_ajustes import carregar as carregar_lote
    r, produto = ajustes.bloquear(inventario_id, [rodada_id])[0]
    if dados.get('contexto') != contexto(r) or not r.ok_em or r.encerrada_em:
        raise ValueError('Contagem alterada; atualize a página.')
    with carregar_lote([r], travar=True) as lote:
        # Match the submitted preview against freshly locked source records.
        venda_id = None
        for v, eventos in lote.eventos.items():
            if any(congelar(e) == dados.get('evento') for e in eventos):
                venda_id = v
                break
        if venda_id is None:
            raise ValueError('Evento indisponível ou alterado.')
        try:
            with transaction.atomic():
                evento = EventoVenda.objects.select_for_update(nowait=True).filter(
                    pk=dados['evento']['pk'], venda_id=venda_id).values('pk', 'tipo_evento', 'descricao', 'criado_em').get()
        except DatabaseError:
            raise ValueError('Evento em alteração concorrente. Atualize a página e revise novamente.') from None
        registro = next((x for x in lote.registros_removidos[venda_id] if x['id'] == registro_id), None)
        if (congelar(evento) != dados['evento'] or not registro
                or congelar(registro) not in dados.get('registros', [])):
            raise ValueError('Evento ou remoção alterados desde a prévia. Atualize a página.')
        reconhecimentos = lote.reconhecimentos_legados[r.pk]
        existentes = mapa(r, reconhecimentos, lote.eventos[venda_id], lote.registros_removidos[venda_id], venda_id)
        if evento['pk'] in existentes:
            if existentes[evento['pk']] != registro_id:
                raise ValueError('Evento já vinculado a outra remoção. Não substituir a auditoria.')
            return next(x for x in reconhecimentos if x.dados['reconhecimento']['evento']['pk'] == evento['pk'])
        if registro_id in existentes.values():
            raise ValueError('Registro de remoção já utilizado em outro reconhecimento.')
        comprovar(venda_id, r.item.produto_id,
            {r.item.cadastro['nome'].strip().casefold(), r.item.produto.nome.strip().casefold()},
            lote.itens[venda_id], [evento], lote.registros_removidos[venda_id], legados={evento['pk']: registro_id})
        payload = dict(contexto=contexto(r), evento=congelar(evento), registro=congelar(registro),
            responsavel=usuario.pk, **campos)
        auditoria = EventoInventario.objects.create(inventario_id=inventario_id, rodada=r, usuario=usuario,
            acao=ACAO, dados=dict(reconhecimento=payload, selo=signing.dumps(payload, salt=SALT, compress=True)))
        lote.reconhecimentos_legados[r.pk].append(auditoria)
        # Recording cannot bypass snapshots, cancellations, signals or later movements.
        revisao_historico.carregar(lote)
        revisao_historico.validar(r, produto, lote)
        return auditoria
