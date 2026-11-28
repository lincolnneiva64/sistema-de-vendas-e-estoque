"""Participação comercial por ID de cadastro; nenhuma inferência de identidade."""
from decimal import Decimal
from heapq import nsmallest

from django.conf import settings
from django.db.models import Case, CharField, Count, Max, Min, Q, Sum, Value, When
from django.db.models.functions import TruncMonth
from django.utils import timezone

from .relatorios_produtos import vendas_comerciais, itens_comerciais


ZERO = Decimal('0.00')
CENTAVO = Decimal('0.01')
# Confirmação comercial por ID; nomes antigos e novos não mudam a classificação.
CLIENTE_BALCAO_ID = 1126


def coletivos_confirmados():
    return {CLIENTE_BALCAO_ID, *getattr(settings, 'RELATORIOS_CLIENTES_COLETIVOS_CONFIRMADOS', ())}


def resumo_balcao_clientes(inicio, fim, resumo):
    """Duas consultas estreitas; reutiliza os custos já validados no resumo geral.

    Faturamento/operações vêm das notas, nunca de um join com os itens. Custos
    e cobertura vêm dos mesmos itens e validações do Resumo de Vendas.
    """
    confirmados = coletivos_confirmados()
    notas = list(vendas_comerciais().filter(data_venda__range=(inicio, fim))
        .values('cliente_id', 'cliente__nome').annotate(faturamento=Sum('total'),
            operacoes=Count('pk'), sem_total=Count('pk')-Count('total')).order_by())
    grupos = {chave: dict(chave=chave, nome=nome, faturamento=ZERO, operacoes=0,
        sem_total=0, receita_itens=ZERO, receita_coberta=ZERO, cmv_conhecido=ZERO,
        itens=0, com_custo=0, itens_sem_valor=0) for chave, nome in (
            ('balcao', 'Venda de Balcão'), ('cadastrados', 'Clientes cadastrados'),
            ('outros_coletivos', 'Outras origens coletivas'),
            ('sem_cliente', 'Sem cliente vinculado'), ('total', 'Total geral'))}

    def grupo_id(pk):
        return ('balcao' if pk == CLIENTE_BALCAO_ID else 'sem_cliente' if pk is None
                else 'outros_coletivos' if pk in confirmados else 'cadastrados')

    for nota in notas:
        nota['faturamento'] = nota['faturamento'] or ZERO
        for chave in (grupo_id(nota['cliente_id']), 'total'):
            for campo in ('faturamento', 'operacoes', 'sem_total'):
                grupos[chave][campo] += nota[campo]
    total = grupos['total']['faturamento']
    if total != resumo['faturamento'] or grupos['total']['operacoes'] != resumo['quantidade']:
        return dict(disponivel=False, inicio=inicio, fim=fim,
            motivo='Os grupos não conferem com o Resumo de Vendas. Consulte novamente.')

    validacoes = resumo['rentabilidade']['validacoes_custo']
    itens = itens_comerciais().filter(venda__data_venda__range=(inicio, fim)).values(
        'pk', 'venda__cliente_id', 'valor_total')
    for item in itens:
        custo = validacoes.get(item['pk'], {}).get('custo')
        for chave in (grupo_id(item['venda__cliente_id']), 'total'):
            grupo = grupos[chave]
            grupo['itens'] += 1
            grupo['itens_sem_valor'] += int(item['valor_total'] is None)
            grupo['receita_itens'] += item['valor_total'] or ZERO
            if custo is not None:
                grupo['com_custo'] += 1
                grupo['cmv_conhecido'] += custo
                grupo['receita_coberta'] += item['valor_total'] or ZERO

    for grupo in grupos.values():
        vazio = grupo['operacoes'] == 0
        grupo['participacao'] = (grupo['faturamento']/total*100).quantize(CENTAVO) if total > ZERO else None
        grupo['divergencia'] = grupo['receita_itens']-grupo['faturamento']
        grupo['completa'] = vazio or (grupo['itens'] > 0
            and grupo['com_custo'] == grupo['itens'] and not grupo['itens_sem_valor']
            and not grupo['sem_total'] and grupo['divergencia'] == ZERO)
        grupo['cmv'] = grupo['cmv_conhecido'] if grupo['completa'] else None
        grupo['lucro'] = grupo['faturamento']-grupo['cmv_conhecido'] if grupo['completa'] else None
        grupo['margem'] = (grupo['lucro']/grupo['faturamento']*100).quantize(CENTAVO) if (
            grupo['completa'] and grupo['faturamento'] > ZERO) else None
        grupo['lucro_coberto'] = grupo['receita_coberta']-grupo['cmv_conhecido'] if grupo['com_custo'] else None
        grupo['margem_coberta'] = (grupo['lucro_coberto']/grupo['receita_coberta']*100).quantize(CENTAVO) if (
            grupo['com_custo'] and grupo['receita_coberta'] > ZERO) else None
        grupo['cobertura'] = (grupo['receita_coberta']/grupo['receita_itens']*100).quantize(CENTAVO) if grupo['receita_itens'] > ZERO else None

    geral = grupos['total']
    rentabilidade = resumo['rentabilidade']
    if (geral['com_custo'] != rentabilidade['com_custo']
            or geral['cmv_conhecido'] != rentabilidade['cmv']
            or geral['itens'] != rentabilidade['quantidade']):
        return dict(disponivel=False, inicio=inicio, fim=fim,
            motivo='A cobertura dos custos mudou durante a consulta. Consulte novamente.')
    maiores = nsmallest(5, (n for n in notas if n['cliente_id'] is not None
                            and n['cliente_id'] not in confirmados),
                       key=lambda n: (-n['faturamento'], n['cliente_id']))
    for posicao, nota in enumerate(maiores, 1):
        nota['posicao'] = posicao
        nota['participacao'] = (nota['faturamento']/total*100).quantize(CENTAVO) if total > ZERO else None
    return dict(disponivel=True, inicio=inicio, fim=fim, balcao=grupos['balcao'],
        total=geral, maiores=maiores,
        grupos=[grupos[k] for k in ('balcao', 'cadastrados', 'outros_coletivos', 'sem_cliente', 'total')
                if k in ('balcao', 'cadastrados', 'total') or grupos[k]['operacoes']])


def nome_periodo(inicio, fim):
    meses = ('janeiro', 'fevereiro', 'março', 'abril', 'maio', 'junho',
             'julho', 'agosto', 'setembro', 'outubro', 'novembro', 'dezembro')
    if (inicio.year, inicio.month) == (fim.year, fim.month):
        return f'{meses[inicio.month - 1]} de {inicio.year}'
    return f'{inicio:%d/%m/%Y} a {fim:%d/%m/%Y}'


def resumo_comercial(grupos, diferenca, impactos, aumentos, desempenho):
    """Escolhe destaques sobre os valores já conciliados, sem inferir causas."""
    anterior = nome_periodo(desempenho['referencia_inicio'], desempenho['referencia_fim'])
    atual = nome_periodo(desempenho['inicio'], desempenho['fim'])
    descricoes = {
        'ambos': 'Compraram nos dois períodos',
        'ausentes': f'Compraram em {anterior}, mas não em {atual}',
        'somente_atual': f'Compraram em {atual}, mas não em {anterior}',
        'coletivos': 'Cadastros coletivos confirmados',
        'sem_cliente': 'Vendas sem cliente identificado',
    }
    for chave, grupo in grupos.items():
        grupo['descricao'] = descricoes[chave]
        grupo['rotulo'] = descricoes[chave]
        grupo['valor_mudanca'] = abs(grupo['diferenca'])
    queda = min((g for g in grupos.values() if g['diferenca'] < ZERO),
                key=lambda g: g['diferenca'], default=None)
    alta = max((g for g in grupos.values() if g['diferenca'] > ZERO),
               key=lambda g: g['diferenca'], default=None)

    def destaque(grupo, cadastros):
        if grupo is not None:
            return dict(tipo='grupo', rotulo=grupo['descricao'], valor=abs(grupo['diferenca']))
        if cadastros:
            cadastro = cadastros[0]
            return dict(tipo='cadastro', id=cadastro['id'], rotulo=cadastro['nome'],
                        valor=abs(cadastro['diferenca']))
        return None

    foco = queda or alta
    if foco is not None and foco['chave'] == 'coletivos':
        investigar = 'Confira as notas dos cadastros coletivos antes de atribuir essa mudança a clientes individuais.'
    elif foco is not None and foco['chave'] == 'sem_cliente':
        investigar = 'Confira as notas sem cliente identificado antes de atribuir essa mudança a clientes.'
    elif queda is not None and queda['chave'] == 'ausentes':
        investigar = 'Comece pelas ausências com maior valor comprado anteriormente e confira o histórico abaixo. A ausência neste intervalo ainda precisa ser investigada.'
    elif queda is not None or impactos:
        investigar = 'Confira as notas e os dias de compra dos clientes que compraram menos, para distinguir menos compras de compras de menor valor.'
    elif alta is not None or aumentos:
        investigar = 'Confira as notas e os produtos dos clientes que estão comprando mais para entender o que sustentou o aumento.'
    else:
        investigar = 'Acompanhe os próximos períodos equivalentes antes de concluir que há uma tendência de mudança.'
    return dict(periodo_anterior=anterior, periodo_atual=atual,
                movimento='queda' if diferenca < ZERO else 'alta' if diferenca > ZERO else 'igual',
                valor_mudanca=abs(diferenca), ambos=grupos['ambos'],
                ausentes=grupos['ausentes'], somente_atual=grupos['somente_atual'],
                principal=queda if diferenca < ZERO else alta if diferenca > ZERO else None,
                problema=destaque(queda, impactos), positivo=destaque(alta, aumentos),
                houve_movimentos=bool(queda or alta or impactos or aumentos), investigar=investigar)


def comportamento_cliente(cadastro):
    """Descreve notas observadas; não presume visitas ou causas comerciais."""
    if cadastro.get('id') == CLIENTE_BALCAO_ID:
        return 'Origem comercial coletiva: reúne consumidores sem cadastro individual.'
    antes, agora = cadastro['notas_anterior'], cadastro['notas_atual']
    subiu = cadastro['diferenca'] > ZERO
    if not antes:
        return ('Compras somente no período atual. Investigue o histórico do cadastro; '
                'isso não comprova que seja um cliente novo.')
    if not agora:
        return ('Sem compras no período atual. Confira as últimas notas e acompanhe '
                'os próximos períodos; isso não comprova perda definitiva.')
    if agora == antes:
        return (('Aumentou' if subiu else 'Reduziu') + ' o valor comprado mantendo a mesma quantidade de notas. '
                'Investigue quais produtos ou quantidades explicam a mudança.')
    if agora > antes:
        return ('Registrou mais notas. Investigue o aumento da frequência de compras registradas '
                'e acompanhe se continua nos próximos períodos.' if subiu else
                'Reduziu o valor comprado apesar de registrar mais notas. Investigue a mudança no valor médio das compras.')
    return ('Aumentou o valor comprado apesar de registrar menos notas. Investigue a mudança no valor médio das compras.'
            if subiu else 'Continuou comprando, com menos notas e menor valor comprado. Confira as notas, os dias com compras e os produtos para investigar a redução.')


def resumir_clientes(linhas, desempenho, coletivos_confirmados=()):
    """Particiona valores sem sobreposição; ausência usa notas, nunca receita."""
    if not desempenho.get('comparavel'):
        return dict(disponivel=False, motivo=desempenho.get('motivo') or
                    'Sem dias encerrados comparáveis no intervalo selecionado.')

    confirmados = {CLIENTE_BALCAO_ID, *coletivos_confirmados}
    cadastros = {}
    for linha in linhas:
        if linha['sem_total']:
            return dict(disponivel=False, motivo='Há notas sem total na consulta por cadastro. Consulte novamente.')
        cadastro = cadastros.setdefault(linha['cliente_id'], dict(
            id=linha['cliente_id'], nome=linha['cliente__nome'],
            anterior=ZERO, atual=ZERO, notas_anterior=0, notas_atual=0,
            dias_anterior=0, dias_atual=0))
        periodo = linha['periodo']
        cadastro[periodo] = linha['faturamento']
        cadastro['notas_' + periodo] = linha['notas']
        cadastro['dias_' + periodo] = linha['dias']

    total_anterior = sum((c['anterior'] for c in cadastros.values()), ZERO)
    total_atual = sum((c['atual'] for c in cadastros.values()), ZERO)
    # Uma alteração entre consultas não pode gerar atribuições inconsistentes.
    if (total_anterior != desempenho['anterior'] or total_atual != desempenho['atual']
            or total_atual - total_anterior != desempenho['diferenca']):
        return dict(disponivel=False, motivo=
            'Os totais por cadastro não coincidem com a análise geral. Consulte novamente antes de interpretar a participação.')

    def pontos(valor):
        return (valor / total_anterior * 100).quantize(CENTAVO) if total_anterior > ZERO else None

    grupos = {
        chave: dict(chave=chave, rotulo=rotulo, classificacao=classificacao,
                    cadastros=0, anterior=ZERO, atual=ZERO, notas_anterior=0, notas_atual=0)
        for chave, rotulo, classificacao in (
            ('ambos', 'Presentes nos dois períodos', 'Pendente'),
            ('ausentes', 'Ausentes no período atual', 'Pendente'),
            ('somente_atual', 'Presentes somente no período atual', 'Pendente'),
            ('coletivos', 'Cadastros coletivos confirmados', 'Confirmada'),
            ('sem_cliente', 'Sem cliente identificado', 'Sem vínculo'),
        )
    }
    for cadastro in cadastros.values():
        pk = cadastro['id']
        if pk is None:
            grupo = 'sem_cliente'
            cadastro['classificacao'] = 'Sem vínculo'
        elif pk in confirmados:
            grupo = 'coletivos'
            cadastro['classificacao'] = 'Coletivo confirmado'
        else:
            cadastro['classificacao'] = 'Classificação pendente'
            grupo = ('ambos' if cadastro['notas_anterior'] and cadastro['notas_atual'] else
                     'ausentes' if cadastro['notas_anterior'] else 'somente_atual')
        grupos[grupo]['cadastros'] += int(pk is not None)
        grupos[grupo]['anterior'] += cadastro['anterior']
        grupos[grupo]['atual'] += cadastro['atual']
        grupos[grupo]['notas_anterior'] += cadastro['notas_anterior']
        grupos[grupo]['notas_atual'] += cadastro['notas_atual']
        cadastro['diferenca'] = cadastro['atual'] - cadastro['anterior']
    for grupo in grupos.values():
        grupo['diferenca'] = grupo['atual'] - grupo['anterior']
        grupo['pontos'] = pontos(grupo['diferenca'])
    grupos['coletivos']['balcao_presente'] = CLIENTE_BALCAO_ID in cadastros

    diferenca = sum((g['diferenca'] for g in grupos.values()), ZERO)
    if diferenca != desempenho['diferenca']:
        return dict(disponivel=False, motivo='Conciliação por cadastro indisponível. Consulte novamente.')
    # Apenas as cinco maiores contribuições negativas; nomes não viram chaves ou links.
    impactos = nsmallest(5, (c for c in cadastros.values()
                          if c['id'] is not None and c['id'] not in confirmados and c['diferenca'] < ZERO),
                        key=lambda c: (c['diferenca'], c['id']))
    aumentos = nsmallest(5, (c for c in cadastros.values()
                           if c['id'] is not None and c['id'] not in confirmados and c['diferenca'] > ZERO),
                        key=lambda c: (-c['diferenca'], c['id']))
    ausentes = sorted((c for c in cadastros.values() if c['id'] is not None
                       and c['id'] not in confirmados and c['notas_anterior'] and not c['notas_atual']),
                      key=lambda c: (-c['anterior'], c['id']))
    for cadastro in impactos + aumentos + ausentes:
        cadastro['variacao_percentual'] = (
            (cadastro['diferenca'] / cadastro['anterior'] * 100).quantize(CENTAVO)
            if cadastro['anterior'] != ZERO else None)
        cadastro['comportamento'] = comportamento_cliente(cadastro)
        # Apresentação curta apenas para mudanças que exigem interpretação.
        antes, agora = cadastro['notas_anterior'], cadastro['notas_atual']
        contraste = (antes == agora or (cadastro['diferenca'] > ZERO and agora < antes)
                     or (cadastro['diferenca'] < ZERO and agora > antes))
        cadastro['leitura_comercial'] = (cadastro['comportamento'].split('. ', 1)[0] + '.'
                                         if antes and agora and contraste else '')
    return dict(disponivel=True, grupos=list(grupos.values()), impactos=impactos,
                grupos_visiveis=[g for g in grupos.values() if g['anterior'] or g['atual']
                    or g['cadastros'] or g['notas_anterior'] or g['notas_atual']],
                sem_cliente=grupos['sem_cliente'],
                aumentos=aumentos, ausentes=ausentes,
                comercial=resumo_comercial(grupos, diferenca, impactos, aumentos, desempenho),
                anterior=total_anterior, atual=total_atual, diferenca=diferenca,
                pontos=pontos(diferenca),
                pendentes=sum(g['cadastros'] for k, g in grupos.items()
                              if k in ('ambos', 'ausentes', 'somente_atual')),
                coletivos=grupos['coletivos']['cadastros'],
                cadastro_1126_pendente=1126 in cadastros and 1126 not in confirmados)


def classificar_frequencia(dias, meses, primeira, ultima):
    """Padrão observado, nunca um rótulo permanente de cliente habitual/perdido."""
    amplitude = (ultima - primeira).days if primeira and ultima else 0
    if meses >= 2 and amplitude >= 28:
        if dias >= 4:
            return 'recorrentes', 'Compras recorrentes observadas'
        if 2 <= dias <= 3:
            return 'ocasionais', 'Compras ocasionais observadas'
    return 'insuficientes', 'Histórico insuficiente para avaliar a frequência'


def historico_ausencias(resultado, desempenho):
    """Uma agregação adicional apenas para ausentes; não altera valores do recorte."""
    ausentes = resultado['ausentes']
    if not ausentes:
        return
    inicio = desempenho['inicio']
    antes = Q(data_venda__lt=inicio)
    # Hoje pode comprovar uma compra posterior, mas nunca integra a comparação.
    hoje = timezone.localdate()
    linhas = (vendas_comerciais().filter(cliente_id__in=[c['id'] for c in ausentes],
                                        data_venda__lte=hoje)
        .values('cliente_id').annotate(
            dias=Count('data_venda', distinct=True, filter=antes),
            meses=Count(TruncMonth('data_venda'), distinct=True, filter=antes),
            primeira=Min('data_venda', filter=antes), ultima=Max('data_venda', filter=antes),
            anterior_recorte=Max('data_venda', filter=Q(data_venda__lt=desempenho['referencia_inicio'])),
            posterior=Min('data_venda', filter=Q(data_venda__gt=desempenho['fim']))).order_by())
    por_id = {l['cliente_id']: l for l in linhas}
    contagem = dict(recorrentes=0, ocasionais=0, insuficientes=0)
    prioridades = []
    for cadastro in ausentes:
        h = por_id.get(cadastro['id'], dict(dias=0, meses=0, primeira=None, ultima=None,
                                            anterior_recorte=None, posterior=None))
        chave, descricao = classificar_frequencia(h['dias'], h['meses'], h['primeira'], h['ultima'])
        h.update(chave=chave, descricao=descricao)
        cadastro['historico'] = h
        contagem[chave] += 1
        if chave == 'recorrentes' and h['posterior'] is None and cadastro['anterior'] > ZERO:
            prioridades.append(cadastro)
    resultado['historico_ausencias'] = dict(contagem=contagem, prioridades=prioridades[:3],
                                            limite_posterior=hoje)


def participacao_clientes(desempenho):
    """Uma agregação dos recortes + uma do histórico se há ausentes; zero se incomparáveis."""
    if not desempenho.get('comparavel'):
        return resumir_clientes([], desempenho)
    atual = (desempenho['inicio'], desempenho['fim'])
    anterior = (desempenho['referencia_inicio'], desempenho['referencia_fim'])
    linhas = (vendas_comerciais().filter(
        Q(data_venda__range=atual) | Q(data_venda__range=anterior))
        .annotate(periodo=Case(When(data_venda__range=atual, then=Value('atual')),
                              default=Value('anterior'), output_field=CharField()))
        .values('cliente_id', 'cliente__nome', 'periodo')
        .annotate(faturamento=Sum('total'), notas=Count('pk'),
                  dias=Count('data_venda', distinct=True), sem_total=Count('pk')-Count('total'))
        .order_by())
    confirmados = coletivos_confirmados()
    resultado = resumir_clientes(linhas, desempenho, confirmados)
    if resultado['disponivel']:
        historico_ausencias(resultado, desempenho)
    return resultado
