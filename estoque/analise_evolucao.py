"""Comparações puras de totais diários; sem consultas ou operações comerciais."""
from calendar import monthrange
from bisect import bisect_left, bisect_right
from datetime import date, timedelta
from decimal import Decimal


ZERO = Decimal('0.00')
DIAS = ('Segundas-feiras', 'Terças-feiras', 'Quartas-feiras', 'Quintas-feiras',
        'Sextas-feiras', 'Sábados', 'Domingos')


def periodos_historicos(inicio, fim, hoje, modo, dia_semana):
    """Only the interval edges may clip a calendar week/month."""
    fim = min(fim, hoje)
    chave = (inicio if modo == 'diario' else inicio-timedelta(days=inicio.weekday())
             if modo == 'semanal' else inicio.replace(day=1))
    while chave <= fim:
        ultimo = (chave if modo == 'diario' else
                  date.fromordinal(min(date.max.toordinal(), chave.toordinal()+6)) if modo == 'semanal' else
                  chave.replace(day=monthrange(chave.year, chave.month)[1]))
        a, b = max(chave, inicio), min(ultimo, fim)
        if modo != 'diario' or dia_semana == -1 or chave.weekday() == dia_semana:
            yield a, b, a != chave or b != ultimo or ultimo >= hoje
        proximo = ultimo.toordinal()+1
        if proximo > fim.toordinal():
            break
        chave = date.fromordinal(proximo)


def referencia_historica(a, b, parcial, modo, dia_semana, comparacao):
    """Never clamp an invalid equivalent date or compare unequal partials."""
    if modo != 'mensal':
        dias = 7 if modo == 'semanal' or dia_semana != -1 else 1
        if a.toordinal() <= dias:
            return None
        return a-timedelta(days=dias), b-timedelta(days=dias)
    try:
        ano, mes = (a.year-1, a.month) if comparacao == 'ano_anterior' else (
            (a.year-1, 12) if a.month == 1 else (a.year, a.month-1))
        anterior_a = date(ano, mes, a.day if parcial else 1)
        anterior_b = date(ano, mes, b.day if parcial else monthrange(ano, mes)[1])
        return anterior_a, anterior_b
    except ValueError:
        return None


def analisar_historico(dias, inicio, fim, hoje, modo, dia_semana=-1, comparacao='anterior', *, inicio_registros=None):
    """One daily aggregate, prefix sums and bounded visible DOM; no sales fetched.

    No documents is unknown, not certified zero. Null totals also interrupt
    comparisons. A documented zero remains a valid numeric reference.
    """
    periodos = list(periodos_historicos(inicio, fim, hoje, modo, dia_semana))
    linhas = sorted(dias, key=lambda d: d['data_venda'])
    datas, valores, registros, ausentes = [], [ZERO], [0], [0]
    for d in linhas:
        datas.append(d['data_venda'])
        valores.append(valores[-1]+(d['faturamento'] or ZERO))
        registros.append(registros[-1]+d['quantidade'])
        ausentes.append(ausentes[-1]+d['sem_valor'])

    def total(a, b):
        i, j = bisect_left(datas, a), bisect_right(datas, b)
        if registros[j] == registros[i]:
            return None, 'Sem histórico registrado neste período.'
        if ausentes[j] != ausentes[i]:
            return None, 'Há documentos sem total: faturamento incompleto.'
        return valores[j]-valores[i], ''

    def cobertura(a, b):
        # A sale on the first/last day alone does not prove the days between.
        # Only explicit daily records (with valid totals) support equivalence;
        # a day without documents may be closed or missing, never assumed zero.
        i, j = bisect_left(datas, a), bisect_right(datas, b)
        return j-i == (b-a).days+1 and ausentes[j] == ausentes[i]

    def indicadores(a, b, valor, situacao):
        i, j = bisect_left(datas, a), bisect_right(datas, b)
        quantidade = registros[j]-registros[i]
        if not quantidade:
            return None
        corrente = a.year == hoje.year and a.month == hoje.month and b == hoje
        base_a = max(a, inicio_registros) if inicio_registros else a
        base_b = b-timedelta(days=1) if corrente else b
        dias_base = (base_b-base_a).days+1
        base_valida = dias_base > 0 and cobertura(base_a, base_b)
        encerrados = total(base_a, base_b)[0] if base_valida else None
        media = encerrados/dias_base if base_valida else None
        dados = dict(quantidade=quantidade,
            ticket_medio=(valor/quantidade).quantize(Decimal('.01')) if valor is not None else None,
            media_diaria=media.quantize(Decimal('.01')) if media is not None else None,
            dias_media=dias_base if base_valida else 0,
            media_inicio=base_a.isoformat() if base_valida else None,
            media_fim=base_b.isoformat() if base_valida else None,
            dias_encerrados=corrente,
            situacao='Parcial em andamento' if corrente and a.day == 1 and base_valida and
                (not inicio_registros or inicio_registros <= a) else situacao,
            previsao=None, equivalente_teorico=None)
        if not corrente:
            ultimo = a.replace(day=monthrange(a.year, a.month)[1])
            if (ultimo < hoje and b == ultimo and inicio_registros and
                    a.replace(day=1) < inicio_registros <= b and a <= inicio_registros and
                    base_valida and valor is not None):
                dados['equivalente_teorico'] = dict(
                    valor=(media*ultimo.day).quantize(Decimal('.01')),
                    rotulo='Equivalente teórico', dias_calendario=ultimo.day, dias_base=dias_base,
                    inicio=base_a.isoformat(), fim=base_b.isoformat(),
                    metodologia='Ritmo diário do intervalo comprovado × dias do calendário do mês. Equivalência matemática; não representa vendas realizadas nem previsão de um mês já encerrado.')
            return dados
        previsao = dict(valor=None, motivo='', diferenca=None, variacao=None, anterior=None,
            anterior_inicio=None, anterior_fim=None, motivo_comparacao='',
            metodologia='Realizado (incluindo hoje) + média dos dias encerrados × dias após hoje até o fim do mês. O restante de hoje não é estimado.')
        dados['previsao'] = previsao
        if a.day != 1 or (inicio_registros and inicio_registros > a):
            previsao['motivo'] = 'Inclua o mês desde o dia 1 com cobertura comprovada.'
        elif not base_valida:
            previsao['motivo'] = 'Sem dias encerrados suficientes ou com lacunas documentais.'
        elif valor is None:
            previsao['motivo'] = 'Há documentos sem total registrado.'
        else:
            restantes = monthrange(hoje.year, hoje.month)[1]-hoje.day
            previsao.update(valor=(valor+media*restantes).quantize(Decimal('.01')),
                realizado=valor, encerrados=encerrados, hoje=total(hoje, hoje)[0], dias_restantes=restantes)
            if a > date.min:
                anterior_b = a-timedelta(days=1)
                anterior_a = anterior_b.replace(day=1)
                previsao.update(anterior_inicio=anterior_a.isoformat(), anterior_fim=anterior_b.isoformat())
                anterior = total(anterior_a, anterior_b)[0]
                if anterior is not None and cobertura(anterior_a, anterior_b):
                    previsao.update(anterior=anterior,
                        diferenca=(previsao['valor']-anterior).quantize(Decimal('.01')),
                        variacao=variacao(previsao['valor'], anterior))
                else:
                    previsao['motivo_comparacao'] = 'Períodos não equivalentes: mês anterior completo sem cobertura comprovada.'
        return dados

    itens = []
    for a, b, parcial in periodos:
        valor, motivo = total(a, b)
        inicio_incompleto = (modo == 'mensal' and inicio_registros is not None and
                            a < inicio_registros <= b)
        cobertura_atual = cobertura(a, b) if modo == 'mensal' else True
        parcial = parcial or inicio_incompleto or (modo == 'mensal' and valor is not None and not cobertura_atual)
        referencia = referencia_historica(a, b, parcial, modo, dia_semana, comparacao)
        item = dict(inicio=a.isoformat(), fim=b.isoformat(), valor=valor, parcial=parcial,
            status=f'Parcial — registros desde {inicio_registros:%d/%m}' if inicio_incompleto else
                'Parcial — cobertura não comprovada' if modo == 'mensal' and valor is not None and not cobertura_atual else
                'Parcial' if parcial else 'Completo', motivo=motivo, variacao=None,
            diferenca=None, comparacao=None,
            rotulo=a.strftime('%d/%m/%Y') if modo == 'diario' else
                a.strftime('%m/%Y') if modo == 'mensal' and not parcial else f'{a:%d/%m/%Y} a {b:%d/%m/%Y}')
        if valor is not None:
            if modo == 'mensal' and not cobertura_atual:
                item['motivo'] = 'Períodos não equivalentes — cobertura diária do mês selecionado não comprovada.'
            elif referencia is None:
                item['motivo'] = 'Períodos não equivalentes — referência sem os mesmos dias no calendário.'
            elif modo == 'diario' and a == hoje:
                item['motivo'] = 'Hoje está em andamento; não há horário equivalente comprovado.'
            else:
                anterior_a, anterior_b = referencia
                anterior, motivo_anterior = total(anterior_a, anterior_b)
                if anterior is None:
                    item['motivo'] = 'Períodos não equivalentes — referência: '+motivo_anterior
                elif modo == 'mensal' and not cobertura(anterior_a, anterior_b):
                    item['motivo'] = 'Períodos não equivalentes — cobertura diária da referência não comprovada.'
                else:
                    item.update(diferenca=(valor-anterior).quantize(Decimal('.01')),
                        variacao=variacao(valor, anterior),
                        motivo='Percentual não calculável: faturamento anterior zero ou negativo.' if anterior <= ZERO else '',
                        comparacao=dict(tipo='parcial' if parcial else 'completa',
                            rotulo='Parcial equivalente' if parcial else 'Completa vs completa',
                            inicio=a.isoformat(), fim=b.isoformat(), atual=valor, anterior=anterior,
                            anterior_inicio=anterior_a.isoformat(), anterior_fim=anterior_b.isoformat(),
                            inclui_hoje=a <= hoje <= b))
        if modo == 'mensal':
            item['indicadores'] = indicadores(a, b, valor, item['status'])
            comp = item['comparacao']
            if comp:
                item['indicadores_referencia'] = indicadores(date.fromisoformat(comp['anterior_inicio']),
                    date.fromisoformat(comp['anterior_fim']), comp['anterior'], comp['rotulo'])
        itens.append(item)
    completos = [i['valor'] for i in itens if not i['parcial'] and i['valor'] is not None]
    numeros = [ZERO, *(i['valor'] for i in itens if i['valor'] is not None)]
    return dict(historico=True, inicio=inicio, fim=min(fim, hoje), modo=modo, dia_semana=dia_semana,
        titulo='Todos os dias' if modo == 'diario' and dia_semana == -1 else DIAS[dia_semana] if modo == 'diario' else
            'Semanas' if modo == 'semanal' else 'Meses', itens=itens,
        comparacao_mensal=comparacao, inicio_registros=inicio_registros, cobertura_mensal=modo == 'mensal',
        media=(sum(completos, ZERO)/len(completos)).quantize(Decimal('.01')) if completos else None,
        quantidade_completos=len(completos), minimo=min(numeros), maximo=max(numeros),
        comparavel=any(i['variacao'] is not None for i in itens),
        comparacao_disponivel=any(i['comparacao'] for i in itens))


def modo_padrao(inicio, fim, hoje):
    segunda = inicio.toordinal() + (-inicio.weekday()) % 7
    ultimo = min(fim, hoje-timedelta(days=1)).toordinal()
    return 'semanal' if ultimo-segunda+1 >= 14 else 'diario'


def variacao(atual, anterior):
    if anterior <= ZERO:
        return None
    return ((atual-anterior)/anterior*100).quantize(Decimal('0.01'))


def referencia_equivalente(inicio, fim, hoje):
    atual_fim = min(fim, hoje)
    atual_inicio = atual_fim.replace(day=1)
    if inicio > atual_inicio or inicio > atual_fim:
        return None, 'Inclua o mês selecionado desde o dia 1 até o último dia da comparação.'
    if atual_inicio == date.min:
        return None, 'Mês anterior fora do calendário disponível.'
    anterior_fim = atual_inicio-timedelta(days=1)
    anterior_inicio = anterior_fim.replace(day=1)
    if atual_fim.day > anterior_fim.day:
        return None, 'O mês anterior não possui o mesmo número de dias transcorridos.'
    return (atual_inicio, atual_fim, anterior_inicio, anterior_inicio.replace(day=atual_fim.day)), ''


def analisar(dias, inicio, fim, hoje, modo, dia_semana, *, referencia_dias=None):
    valores = {}
    for linha in dias:
        dia = linha['data_venda']
        if modo == 'semanal' and dia > hoje:
            continue
        if modo == 'diario' and dia.weekday() != dia_semana:
            continue
        chave = (dia if modo == 'diario' else dia-timedelta(days=dia.weekday())
                 if modo == 'semanal' else dia.replace(day=1))
        valores[chave] = valores.get(chave, ZERO) + linha['faturamento']
    if modo == 'diario':
        ordinal = inicio.toordinal() + (dia_semana-inicio.weekday()) % 7
        chave = date.fromordinal(ordinal) if ordinal <= fim.toordinal() else None
    else:
        chave = inicio-timedelta(days=inicio.weekday()) if modo == 'semanal' else inicio.replace(day=1)
    itens = []
    while chave and chave <= fim:
        ultimo = (chave if modo == 'diario' else
                  date.fromordinal(min(date.max.toordinal(), chave.toordinal()+6)) if modo == 'semanal' else
                  chave.replace(day=monthrange(chave.year, chave.month)[1]))
        a, b = max(inicio, chave), min(fim, ultimo)
        if modo == 'semanal' and a <= hoje:
            b = min(b, hoje)
        parcial = a != chave or b != ultimo or ultimo >= hoje
        valor = valores.get(chave, ZERO)
        anterior = itens[-1] if itens else None
        motivo = ('Primeira ocorrência no intervalo.' if anterior is None else
                  'Período atual parcial.' if parcial else
                  'Período anterior parcial.' if anterior['parcial'] else
                  'Valor anterior zero ou negativo.' if anterior['valor'] <= ZERO else '')
        percentual = variacao(valor, anterior['valor']) if not motivo else None
        rotulo = (a.strftime('%d/%m/%Y') if modo == 'diario' else
                  f'{a:%d/%m/%Y} a {b:%d/%m/%Y}')
        status = ('Hoje (parcial)' if modo == 'diario' and chave == hoje else
                  'Parcial' if parcial else 'Completo')
        itens.append({'inicio': a.isoformat(), 'fim': b.isoformat(), 'rotulo': rotulo,
                      'valor': valor, 'parcial': parcial, 'status': status,
                      'variacao': percentual, 'motivo': motivo})
        proximo = chave.toordinal() + (7 if modo in {'diario', 'semanal'} else (ultimo-chave).days+1)
        chave = date.fromordinal(proximo) if proximo <= fim.toordinal() else None
    completos = [item['valor'] for item in itens if not item['parcial']]
    media = (sum(completos, ZERO)/len(completos)).quantize(Decimal('0.01')) if completos else None
    resultado = {
        'modo': modo, 'dia_semana': dia_semana, 'itens': itens,
        'titulo': DIAS[dia_semana] if modo == 'diario' else 'Semanas' if modo == 'semanal' else 'Meses',
        'media': media, 'quantidade_completos': len(completos),
        'minimo': min([ZERO, *(i['valor'] for i in itens)]),
        'maximo': max([ZERO, *(i['valor'] for i in itens)]),
        'comparavel': any(i['variacao'] is not None for i in itens),
    }
    if modo == 'semanal':
        comparar_semanas(resultado, dias, inicio, hoje, referencia_dias)
    return resultado


def comparar_semanas(analise, dias, inicio, hoje, referencia_dias):
    """Compare matching weekdays, never the preceding visible (clipped) bar.

    None means the external reference was not loaded; an empty aggregate means
    it was consulted and had no sales. Reference totals never enter the cards.
    Each comparison visits at most seven dates, independent of sales volume.
    """
    valores = {d['data_venda']: d['faturamento'] for d in dias}
    valores.update({d['data_venda']: d['faturamento'] for d in referencia_dias or []
                    if d['data_venda'] < inicio})
    for item in analise['itens']:
        a = date.fromisoformat(item['inicio'])
        b = min(date.fromisoformat(item['fim']), hoje)
        item.update(diferenca=None, comparacao=None, variacao=None)
        if a > b:
            item['motivo'] = 'Semana futura: ainda não há dias transcorridos.'
            continue
        if a.toordinal() <= 7:
            item['motivo'] = 'Semana anterior fora do calendário disponível.'
            continue
        anterior_a, anterior_b = a-timedelta(days=7), b-timedelta(days=7)
        if referencia_dias is None and anterior_a < inicio:
            item['motivo'] = 'Semana anterior fora do intervalo consultado.'
            continue
        atual = sum((valores.get(a+timedelta(days=n), ZERO) for n in range((b-a).days+1)), ZERO)
        anterior = sum((valores.get(anterior_a+timedelta(days=n), ZERO)
                        for n in range((anterior_b-anterior_a).days+1)), ZERO)
        item['diferenca'] = (atual-anterior).quantize(Decimal('.01'))
        item['variacao'] = variacao(atual, anterior)
        item['motivo'] = 'Percentual não calculável: faturamento anterior zero ou negativo.' if anterior <= ZERO else ''
        item['comparacao'] = dict(
            tipo='parcial' if item['parcial'] else 'completa',
            rotulo='Parcial equivalente' if item['parcial'] else 'Completa vs completa',
            inicio=a.isoformat(), fim=b.isoformat(), atual=atual, anterior=anterior,
            anterior_inicio=anterior_a.isoformat(), anterior_fim=anterior_b.isoformat(),
            inclui_hoje=a <= hoje <= b,
        )
    analise['comparavel'] = any(i['variacao'] is not None for i in analise['itens'])
    analise['comparacao_disponivel'] = any(i['comparacao'] is not None for i in analise['itens'])
