"""Consulta operacional somente leitura, com paginação antes do carregamento."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from estoque.models import Cliente, MovimentoFinanceiro
from .models import ConfiguracaoLocacao, EventoLocacao, FaixaPrecoLocacao, Locacao, PagamentoLocacao, TarefaOperacionalLocacao


class ConsultaOperacionalTests(TestCase):
    hoje = date(2026, 10, 25)

    def setUp(self):
        ConfiguracaoLocacao.obter()
        self.faixa, _ = FaixaPrecoLocacao.objects.get_or_create(codigo=FaixaPrecoLocacao.CENTRO_PERTO, defaults={"nome": "Centro teste", "preco_jogo_diaria": 10})
        self.clock = patch("locacoes.views.timezone.localdate", return_value=self.hoje)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def criar(self, dias=0, status="reservada", hora=time(9), **campos):
        entrega = self.hoje + timedelta(days=dias)
        dados = dict(tipo_pessoa="avulsa", pessoa_avulsa_nome="Exemplo sintético", endereco_entrega="Rua fictícia",
                     faixa_preco=self.faixa, data_entrega=entrega, horario_entrega=hora,
                     data_evento=entrega, horario_evento=time(18), data_prevista_devolucao=entrega,
                     horario_previsto_devolucao=hora, status=status, total=30, saldo_devedor=30)
        dados.update(campos)
        return Locacao.objects.create(**dados)

    def consultar(self, **filtros):
        return self.client.get(reverse("locacoes:lista"), filtros)

    def ids(self, **filtros):
        resposta = self.consultar(**filtros)
        self.assertEqual(resposta.status_code, 200)
        return [l.pk for l in resposta.context["locacoes"]]

    def test_entregas_padrao_inclui_realizadas_e_ordena_mais_recentes(self):
        futura = self.criar(2)
        hoje_tarde = self.criar(hora=time(16))
        atrasada = self.criar(-1)
        hoje_cedo = self.criar(hora=time(8))
        empate = self.criar(hora=time(8))
        entregue = self.criar(status="entregue")
        self.assertEqual(self.ids(), [futura.pk, hoje_tarde.pk, entregue.pk, empate.pk, hoje_cedo.pk, atrasada.pk])
        resposta = self.consultar()
        self.assertEqual(resposta.context["aba"], "entregas")
        self.assertContains(resposta, '>Entregas</a>')
        self.assertContains(resposta, '>Recolhimentos</a>')

    def test_recolhimentos_priorizam_atrasados_hoje_e_proximos_com_horario(self):
        futuro = self.criar(2, status="entregue")
        hoje = self.criar(status="entregue", hora=time(14))
        tarde = self.criar(status="entregue", hora=time(18))
        atrasado = self.criar(-2, status="pendente_devolucao")
        self.criar(status="reservada")
        self.assertEqual(self.ids(aba="recolhimentos"), [atrasado.pk, hoje.pk, tarde.pk, futuro.pk])

    def test_quitacao_nao_conclui_recolhimento(self):
        locacao = self.criar(status="entregue", saldo_devedor=0, total_pago=30, status_financeiro="quitada")
        self.assertEqual(self.ids(aba="recolhimentos", financeiro="quitadas"), [locacao.pk])
        self.assertEqual(self.ids(aba="recolhimentos", situacao="realizadas"), [])

    def test_tarefa_reagendada_define_ordem_e_periodo(self):
        reagendada = self.criar(-2)
        outra = self.criar(1)
        TarefaOperacionalLocacao.objects.create(locacao=reagendada, tipo="entrega", data_agendada=self.hoje + timedelta(days=3), horario_agendado=time(7))
        self.assertEqual(self.ids(), [reagendada.pk, outra.pk])
        self.assertEqual(self.ids(data_inicio="2026-10-28", data_fim="2026-10-28"), [reagendada.pk])

    def test_realizadas_usam_confirmacao_recente_sem_incluir_pendentes(self):
        antiga = self.criar(-6, status="entregue")
        recente = self.criar(-10, status="entregue")
        self.criar(status="reservada")
        for locacao, dias in ((antiga, -3), (recente, -1)):
            evento = EventoLocacao.objects.create(locacao=locacao, tipo="entregue", descricao="Exemplo")
            instante = timezone.make_aware(datetime.combine(self.hoje + timedelta(days=dias), time(12)))
            EventoLocacao.objects.filter(pk=evento.pk).update(criado_em=instante)
        self.assertEqual(self.ids(situacao="realizadas"), [recente.pk, antiga.pk])

    def test_recolhimentos_concluidos_com_e_sem_avaria_mais_recentes_primeiro(self):
        antiga = self.criar(-4, status="devolvida")
        recente = self.criar(-1, status="devolvida_com_avaria")
        self.criar(status="entregue")
        self.assertEqual(self.ids(aba="recolhimentos", situacao="realizadas"), [recente.pk, antiga.pk])

    def test_encerramento_administrativo_nao_e_conclusao_fisica(self):
        locacao = self.criar(status="entregue")
        TarefaOperacionalLocacao.objects.create(locacao=locacao, tipo="recolhimento", status="resolvida_admin", data_agendada=self.hoje)
        self.assertEqual(self.ids(aba="recolhimentos"), [])
        self.assertEqual(self.ids(aba="recolhimentos", situacao="realizadas"), [])
        self.assertEqual(self.ids(visao="historico"), [locacao.pk])

    def test_legado_sem_atividade_nao_atrapalha_fila_mas_existe_no_historico(self):
        antiga = self.criar(-60)
        recente = self.criar()
        self.assertEqual(self.ids(), [recente.pk])
        self.assertEqual(set(self.ids(visao="historico")), {antiga.pk, recente.pk})

    def test_historico_completo_inclui_todos_os_status_e_datas(self):
        locacoes = [self.criar(-60 if i % 2 else 1, status=status) for i, (status, _) in enumerate(Locacao.STATUS_CHOICES)]
        self.assertEqual(set(self.ids(visao="historico")), {l.pk for l in locacoes})
        self.assertEqual(set(self.ids(aba="historico")), {l.pk for l in locacoes})

    def test_historico_mais_recentes_primeiro_e_desempate_estavel(self):
        a, b = self.criar(), self.criar()
        instante = timezone.now()
        Locacao.objects.filter(pk__in=[a.pk, b.pk]).update(criado_em=instante)
        self.assertEqual(self.ids(visao="historico"), [b.pk, a.pk])

    def test_filtros_cliente_cadastrado_avulso_status_datas_financeiro(self):
        cliente = Cliente.objects.create(nome="Cliente cadastrado")
        alvo = self.criar(tipo_pessoa="cliente", cliente=cliente, saldo_devedor=0, status_financeiro="quitada")
        outro = self.criar(pessoa_avulsa_nome="Outro contratante")
        self.assertEqual(self.ids(cliente="cadastrado", financeiro="quitadas"), [alvo.pk])
        self.assertEqual(self.ids(cliente="Outro", financeiro="com_saldo"), [outro.pk])
        cancelada = self.criar(status="cancelada", pessoa_avulsa_nome="Localizável no histórico")
        self.assertEqual(self.ids(visao="historico", cliente="Localizável", status="cancelada", data_inicio="2026-10-25", data_fim="2026-10-25"), [cancelada.pk])
        self.assertContains(self.consultar(cliente="Outro"), "Filtros ativos")

    def test_paginacao_no_banco_preserva_filtros_e_contexto(self):
        for i in range(31):
            self.criar(pessoa_avulsa_nome=f"Cliente paginação {i}")
        resposta = self.consultar(visao="historico", cliente="paginação", financeiro="com_saldo")
        self.assertEqual(len(resposta.context["locacoes"]), 25)
        self.assertEqual(resposta.context["pagina"].paginator.count, 31)
        self.assertIn("visao=historico", resposta.context["url_proxima"])
        self.assertIn("financeiro=com_saldo", resposta.context["url_proxima"])
        self.assertEqual(len(self.ids(visao="historico", cliente="paginação", pagina=2)), 6)
        self.assertEqual(len(self.ids(visao="historico", pagina="invalida")), 25)

    def test_data_invalida_nao_causa_erro_nem_ignora_restricao_silenciosamente(self):
        self.criar()
        resposta = self.consultar(data_inicio="2026-13-40")
        self.assertContains(resposta, "Revise as datas")
        self.assertEqual(resposta.context["locacoes"], [])

    def test_consultar_nao_altera_locacoes_tarefas_pagamentos_ou_financeiro(self):
        self.criar()
        self.criar(status="entregue", saldo_devedor=0)
        modelos = (Locacao, TarefaOperacionalLocacao, PagamentoLocacao, EventoLocacao, MovimentoFinanceiro)
        antes = {m: list(m.objects.order_by("pk").values()) for m in modelos}
        for filtros in ({}, {"aba": "recolhimentos"}, {"situacao": "realizadas"}, {"visao": "historico"}):
            self.consultar(**filtros)
        for m in modelos:
            self.assertEqual(list(m.objects.order_by("pk").values()), antes[m])

    def test_historico_carrega_apenas_uma_pagina_de_relacoes(self):
        for _ in range(40):
            self.criar()
        with CaptureQueriesContext(connection) as queries:
            resposta = self.consultar(visao="historico")
        self.assertEqual(len(resposta.context["locacoes"]), 25)
        self.assertTrue(any("LIMIT 25" in q["sql"].upper() for q in queries))

    def test_destaque_entrega_depende_da_operacao_e_nao_do_financeiro(self):
        import re
        futura = self.criar(2, saldo_devedor=0, status_financeiro="quitada")
        atrasada = self.criar(-2, status="saiu_para_entrega")
        entregue = self.criar(status="entregue", saldo_devedor=30)
        for filtros in ({}, {"aba": "recolhimentos"}, {"visao": "historico"}):
            resposta = self.consultar(**filtros)
            linhas = re.findall(r'<tr([^>]*)>(.*?)</tr>', resposta.content.decode(), re.S)
            for atributos, conteudo in linhas:
                if 'data-label=' not in conteudo:
                    continue
                pendente = not filtros and (f'#{futura.pk}</a>' in conteudo or f'#{atrasada.pk}</a>' in conteudo)
                self.assertEqual('entrega-pendente' in atributos, bool(pendente))
                if not filtros and f'#{futura.pk}</a>' in conteudo:
                    self.assertIn('Aguardando entrega', conteudo)
                    self.assertNotIn('entrega-atrasada', atributos)
                if not filtros and f'#{atrasada.pk}</a>' in conteudo:
                    self.assertIn('entrega-atrasada', atributos)
                    self.assertIn('Entrega atrasada', conteudo)
                if f'#{entregue.pk}</a>' in conteudo:
                    self.assertNotIn('entrega-pendente', atributos)

    def test_cenarios_sinteticos_77_98_99_101_102_sem_trocar_filtros(self):
        self.criar(pk=77, dias=-2, pessoa_avulsa_nome="Cliente fict?cio 77")
        self.criar(pk=98, dias=2, pessoa_avulsa_nome="Cliente fict?cio 98")
        self.criar(pk=99, dias=-4, status="devolvida", pessoa_avulsa_nome="Cliente fict?cio 99")
        self.criar(pk=101, dias=-3, status="entregue", pessoa_avulsa_nome="Cliente fict?cio 101")
        recente = self.criar(pk=102, dias=-10, status="entregue", pessoa_avulsa_nome="Cliente fict?cio 102")
        evento = EventoLocacao.objects.create(locacao=recente, tipo="entregue", descricao="Entrega fict?cia")
        EventoLocacao.objects.filter(pk=evento.pk).update(criado_em=timezone.make_aware(datetime.combine(self.hoje - timedelta(days=1), time(12))))
        self.assertEqual(self.ids(), [98, 102, 77, 101, 99])
        self.assertEqual(self.ids(situacao="pendentes"), [77, 98])
        self.assertEqual(self.ids(aba="recolhimentos"), [102, 101])
        self.assertEqual(self.ids(aba="recolhimentos", situacao="realizadas"), [99])
        self.assertEqual(set(self.ids(visao="historico")), {77, 98, 99, 101, 102})
        self.assertEqual(self.ids(data_inicio="2026-10-24", data_fim="2026-10-24"), [102])
        resposta = self.consultar()
        self.assertContains(resposta, 'value="todas" selected')
        self.assertContains(resposta, "Cliente fict?cio 102")
        item = next(l for l in resposta.context["locacoes"] if l.pk == 102)
        self.assertEqual(item.indicador_operacional["texto"], "Entrega realizada")
        self.assertFalse(item.indicador_operacional["atraso"])
