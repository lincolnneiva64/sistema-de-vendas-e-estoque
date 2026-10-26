"""A fila resume o início e lista somente trabalho restante e ocorrências reais."""
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from estoque import views
from estoque.models import (Produto, ItemVenda, SeparacaoVenda, SeparacaoVendaItem,
    Venda, EventoVenda, ContaReceber, MovimentoFinanceiro)
from estoque.services.separacao_vendas import sincronizar_checklist_aberto_com_venda
from estoque.tests_separacao_operacional import OperacionalFixtures


class AndamentoFilaFixtures(OperacionalFixtures):
    def checklist(self, total=3, conferidos=0):
        sep, _, linha = self.create("4", "UN")
        linhas = [linha]
        for n in range(1, total):
            p = Produto.objects.create(nome=f"Produto da fila {n:02d}", quantidade=20,
                preco_compra=5, preco_vista=10, preco_prazo=10, unidade_compra="UN", unidade_venda_1="UN")
            linhas.append(ItemVenda.objects.create(venda=sep.venda, produto=p,
                quantidade=4, unidade="UN", preco_unitario=10, valor_total=40))
        sincronizar_checklist_aberto_com_venda(sep)
        itens = list(sep.itens.order_by("pk"))
        for item in itens[:conferidos]:
            self.assertEqual(self.save(sep, item, status="conferido", value="4").status_code, 200)
        sep.refresh_from_db()
        return sep, itens, linhas

    def retrato(self):
        return [list(m.objects.order_by("pk").values()) for m in (SeparacaoVenda,
            SeparacaoVendaItem, Venda, ItemVenda, Produto, EventoVenda, ContaReceber, MovimentoFinanceiro)]

    def consultar(self, sep):
        antes = self.retrato()
        with CaptureQueriesContext(connection) as consultas:
            r = self.client.get(reverse("estoque:separacao_vendas_fila"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(antes, self.retrato())
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in consultas))
        sep.refresh_from_db()
        views._montar_grupos_rota_separacao([sep])
        self.assertEqual(sep.total_itens, sep.total_resolvidos + sep.total_pendencias)
        return r


class AndamentoFilaTests(AndamentoFilaFixtures, TestCase):
    def test_tres_produtos_nao_iniciados_sem_listagem(self):
        sep, itens, linhas = self.checklist(3)
        r = self.consultar(sep)
        self.assertContains(r, "Aguardando separação — 3 produtos")
        self.assertEqual((sep.total_itens, sep.total_resolvidos, sep.total_pendencias), (3, 0, 3))
        self.assertTrue(sep.checklist_nao_iniciado_fila)
        self.assertFalse(sep.itens_com_pendencia)
        for linha in linhas:
            self.assertNotContains(r, linha.produto.nome)

    def test_trinta_produtos_nao_iniciados_sem_listagem(self):
        sep, itens, linhas = self.checklist(30)
        r = self.consultar(sep)
        self.assertContains(r, "Aguardando separação — 30 produtos")
        self.assertEqual((sep.total_itens, sep.total_resolvidos, sep.total_pendencias), (30, 0, 30))
        self.assertFalse(sep.itens_com_pendencia)
        self.assertNotContains(r, 'class="pend-item"')
        self.assertNotContains(r, "Quantidade insuficiente")

    def test_vinte_produtos_dez_conferidos_e_recarga_preservam_resultados(self):
        sep, itens, linhas = self.checklist(20, 10)
        antes = list(sep.itens.order_by("pk").values())
        for _ in range(2):
            r = self.consultar(sep)
            self.assertContains(r, "Em separação — faltam conferir 10 produtos")
            self.assertFalse(sep.checklist_nao_iniciado_fila)
            self.assertEqual((sep.total_itens, sep.total_resolvidos, sep.total_pendencias), (20, 10, 10))
            self.assertEqual(len(sep.itens_com_pendencia), 10)
            self.assertContains(r, "Ainda não conferido", count=10)
            for linha in linhas[:10]:
                self.assertNotContains(r, linha.produto.nome)
            for linha in linhas[10:]:
                self.assertContains(r, linha.produto.nome, count=1)
        self.assertEqual(antes, list(sep.itens.order_by("pk").values()))
        self.assertEqual(list(sep.itens.order_by("pk").values_list("quantidade_separada", flat=True))[:10], [Decimal("4")]*10)

    def test_resultados_salvos_com_marco_legado_ausente_comprovam_inicio(self):
        sep, _, _ = self.checklist(20, 10)
        SeparacaoVenda.objects.filter(pk=sep.pk).update(iniciado_em=None, status="enviada")
        r = self.consultar(sep)
        self.assertFalse(sep.checklist_nao_iniciado_fila)
        self.assertContains(r, "Em separação — faltam conferir 10 produtos")
        self.assertEqual(len(sep.itens_com_pendencia), 10)

    def test_marco_de_inicio_com_itens_pendentes_nao_volta_a_nao_iniciado(self):
        sep, _, _ = self.checklist(3)
        SeparacaoVenda.objects.filter(pk=sep.pk).update(iniciado_em=timezone.now())
        r = self.consultar(sep)
        self.assertFalse(sep.checklist_nao_iniciado_fila)
        self.assertContains(r, "Em separação — faltam conferir 3 produtos")
        self.assertEqual(len(sep.itens_com_pendencia), 3)

    def test_nao_encontrado_falta_e_ainda_nao_conferido_sao_distintos(self):
        sep, itens, _ = self.checklist(3)
        self.save(sep, itens[0], status="nao_encontrado")
        self.save(sep, itens[1], status="quantidade_insuficiente", value="3")
        r = self.consultar(sep)
        self.assertEqual(len(sep.itens_com_pendencia), 3)
        self.assertContains(r, "Quantidade insuficiente — falta 1", count=1)
        self.assertContains(r, "Ainda não conferido", count=1)
        self.assertContains(r, "Não encontrado", count=1)
        self.assertEqual(sum(i["aguardando_conferencia"] for i in sep.itens_com_pendencia), 1)
        self.assertEqual(sep.total_pendencias, 3)
        self.assertEqual(sep.itens_com_pendencia[0]["status"], "nao_encontrado")

    def test_alteracao_posterior_mantem_produto_conferido_na_lista_de_atencao(self):
        sep, _, linhas = self.checklist(3, 3)
        linhas[0].quantidade = 5
        linhas[0].save(update_fields=["quantidade"])
        EventoVenda.objects.create(venda=sep.venda, tipo_evento="quantidade_item_alterada",
            descricao=f"Quantidade alterada na nota: {linhas[0].produto.nome}. De 4.000 UN para 5.000 UN.")
        r = self.consultar(sep)
        self.assertEqual(len(sep.itens_com_pendencia), 1)
        self.assertFalse(sep.itens_com_pendencia[0]["aguardando_conferencia"])
        self.assertContains(r, "Necessita conferência")
        self.assertNotContains(r, "Separação concluída — nota pronta")

    def test_revisao_real_nao_e_ocultada_por_itens_ainda_nao_conferidos(self):
        sep, _, _ = self.checklist(3)
        SeparacaoVenda.objects.filter(pk=sep.pk).update(revisao_pendente=True)
        self.consultar(sep)
        self.assertEqual(len(sep.itens_com_pendencia), 3)
        self.assertTrue(all(not i["aguardando_conferencia"] for i in sep.itens_com_pendencia))

    def test_concluido_com_historico_resolvido_permanece_recolhido(self):
        sep, _, _, _ = self.legado(extras=11)
        r = self.consultar(sep)
        self.assertEqual((sep.total_itens, sep.total_resolvidos, sep.total_pendencias), (13, 13, 0))
        self.assertContains(r, "Separação concluída — nota pronta")
        self.assertFalse(sep.itens_com_pendencia)
        self.assertEqual(len(sep.itens_resolvidos), 2)
        self.assertContains(r, '<details class="pend-list pend-resolvidos">')
