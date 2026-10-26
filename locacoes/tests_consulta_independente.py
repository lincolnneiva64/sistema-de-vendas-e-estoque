"""Independência do período financeiro e da consulta operacional na mesma página."""
from urllib.parse import parse_qs, urlparse

from django.test import TestCase

from . import tests_resumo_financeiro as resumo


class ConsultaIndependenteTests(resumo.DadosResumo, TestCase):
    def test_filtros_operacionais_nao_alteram_indicadores(self):
        atual = self.criar()
        self.pagar(atual, 20)
        antiga = self.criar("2026-08-01", total=200, status="entregue")
        self.pagar(antiga, 30)
        original = self.consultar().context["resumo_financeiro"]
        for filtros in ({"cliente": "Sem correspondência"}, {"status": "cancelada"},
                        {"financeiro": "quitadas"}, {"data_inicio": "2030-01-01"},
                        {"data_fim": "2020-01-01"}, {"aba": "recolhimentos"},
                        {"situacao": "realizadas"}, {"visao": "historico"}):
            with self.subTest(filtros=filtros):
                resposta = self.consultar(**filtros)
                self.assertEqual(resposta.context["resumo_financeiro"], original)

    def test_periodo_financeiro_nao_altera_linhas_ordem_ou_paginacao(self):
        for _ in range(30):
            self.criar()
        original = self.consultar(pagina=2)
        ids = [l.pk for l in original.context["locacoes"]]
        resposta = self.consultar(pagina=2, resumo_inicio="2020-01-01", resumo_fim="2020-12-31", resumo_periodo="escolha")
        self.assertEqual([l.pk for l in resposta.context["locacoes"]], ids)
        self.assertEqual(resposta.context["pagina"].number, 2)
        self.assertEqual(resposta.context["resumo_financeiro"]["quantidade"], 0)
        self.assertEqual(resposta.context["preservar_operacional"]["pagina"], "2")

    def test_datas_invalidas_independentes(self):
        self.criar()
        resposta = self.consultar(data_inicio="invalida")
        self.assertIsNotNone(resposta.context["resumo_financeiro"])
        self.assertEqual(resposta.context["locacoes"], [])
        resposta = self.consultar(resumo_inicio="invalida")
        self.assertIsNone(resposta.context["resumo_financeiro"])
        self.assertEqual(len(resposta.context["locacoes"]), 1)
        self.assertContains(resposta, "Resumo indisponível")

    def test_links_preservam_resumo_e_limpar_afeta_somente_operacional(self):
        for _ in range(26):
            self.criar()
        resposta = self.consultar(cliente="fictício", financeiro="com_saldo", status="reservada", resumo_periodo="escolha")
        links = [a["url"] for a in resposta.context["abas"]] + [resposta.context["url_historico"], resposta.context["url_proxima"]]
        for link in links:
            parametros = parse_qs(urlparse(link).query)
            self.assertEqual(parametros["resumo_inicio"], ["2026-10-01"])
            self.assertEqual(parametros["resumo_fim"], ["2026-10-31"])
            self.assertEqual(parametros["cliente"], ["fictício"])
            self.assertEqual(parametros["status"], ["reservada"])
        limpar = parse_qs(urlparse(resposta.context["url_limpar"]).query)
        self.assertEqual(limpar["resumo_periodo"], ["escolha"])
        self.assertNotIn("cliente", limpar)
        self.assertNotIn("financeiro", limpar)
        self.assertNotIn("data_inicio", limpar)

    def test_ordem_resumo_filtros_abas_historico_tabela(self):
        self.criar()
        html = self.consultar().content.decode()
        self.assertLess(html.index('class="top"'), html.index('class="cardx resumo-financeiro"'))
        self.assertLess(html.index('class="cardx resumo-financeiro"'), html.index('id="filtros-locacoes"'))
        self.assertLess(html.index('id="filtros-locacoes"'), html.index('<nav class="consulta-operacional"'))
        self.assertLess(html.index('<nav class="consulta-operacional"'), html.index('class="historico-consulta"'))
        self.assertLess(html.index('class="historico-consulta"'), html.index('class="table-responsive resultados-locacoes"'))
        self.assertContains(self.consultar(), 'id="filtros-resumo"')
