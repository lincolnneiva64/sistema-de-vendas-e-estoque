from django.test import TestCase
from django.urls import reverse

from . import services, views
from .models import EventoInventario, AcessoInventario, RodadaContagem
from .selecao import itens_incluidos, RETIRADO
from .tests_selecao_explicita import DadosSelecao


class DistribuicaoEscopoTests(DadosSelecao, TestCase):
    def distribuir(self, indices, manter=False, funcionario=None, versao=None):
        self.inv.refresh_from_db()
        return services.distribuir_produtos(self.user, self.inv.pk,
            (funcionario or self.funcionario).pk, [self.rodadas[n].pk for n in indices], 24,
            self.inv.versao if versao is None else versao, manter)

    def test_padrao_retira_na_distribuicao_e_finaliza_sem_pendencias(self):
        self.client.force_login(self.user)
        url = reverse("inventario:detalhe", args=[self.inv.pk])
        antes = self.client.get(url)
        self.assertContains(antes, "Manter produtos não selecionados para outra distribuição")
        self.assertContains(antes, 'id="form-atribuicao"')
        resposta = self.client.post(reverse("inventario:detalhe", args=[self.inv.pk]), {
            "acao": "acesso", "funcionario": self.funcionario.pk, "versao": self.inv.versao,
            "rodadas": [r.pk for r in self.rodadas[:2]]})
        self.assertTrue(resposta.context["link"])
        self.assertNotContains(resposta, 'id="form-selecao-participantes"')
        self.assertEqual(resposta.context["rodadas_disponiveis"], 0)
        self.assertNotContains(resposta, 'id="form-atribuicao"')
        self.assertNotContains(resposta, 'name="manter_nao_selecionados"')
        acesso = self.inv.acessos.get()
        self.assertEqual(itens_incluidos(self.inv).count(), 2)
        evento = EventoInventario.objects.get(inventario=self.inv, acao=RETIRADO)
        self.assertEqual(evento.usuario, self.user)
        self.assertEqual(evento.dados["produto_id"], self.produtos[2].pk)
        self.assertEqual(self.inv.itens.count(), 3)
        self.ok(acesso, 0)
        self.ok(acesso, 1)
        self.assertIsNotNone(self.enviar(acesso).finalizada_em)
        self.assertEqual(views.resumo_painel(views.linhas_revisao(self.inv))["pendentes"], 0)
        resultado = self.client.get(url)
        self.assertEqual(resultado.context["resumo_painel"]["total"], 2)
        self.assertEqual(resultado.context["resumo_painel"]["pendentes"], 0)
        self.assertEqual({linha["item"].produto_id for linha in resultado.context["linhas"]},
                         {p.pk for p in self.produtos[:2]})
        csv = self.client.get(reverse("inventario:relatorio", args=[self.inv.pk])).content.decode("utf-8")
        self.assertNotIn(self.produtos[2].nome, csv)
        for participante in self.produtos[:2]:
            self.assertIn(participante.nome, csv)

    def test_reserva_multiplos_funcionarios_preserva_oks_envios(self):
        ana, _ = self.distribuir([0], manter=True)
        self.ok(ana, 0)
        self.assertIsNone(self.enviar(ana).finalizada_em)
        ana.refresh_from_db()
        envio = ana.enviada_em
        antes = list(self.itens[0].rodadas.values())
        bruno, _ = self.distribuir([1], funcionario=self.outro)
        self.assertEqual(set(itens_incluidos(self.inv).values_list("pk", flat=True)), {i.pk for i in self.itens[:2]})
        self.assertEqual(list(self.itens[0].rodadas.values()), antes)
        ana.refresh_from_db()
        self.assertEqual(ana.enviada_em, envio)
        self.ok(bruno, 1)
        self.assertIsNotNone(self.enviar(bruno).finalizada_em)

    def test_reserva_sem_nova_atribuicao_continua_pendente(self):
        acesso, _ = self.distribuir([0], manter=True)
        self.ok(acesso, 0)
        self.assertIsNone(self.enviar(acesso).finalizada_em)
        self.assertEqual(itens_incluidos(self.inv).count(), 3)
        self.assertFalse(EventoInventario.objects.filter(acao=RETIRADO).exists())

    def test_evidencia_no_nao_selecionado_reverte_distribuicao_inteira(self):
        RodadaContagem.objects.filter(pk=self.rodadas[2].pk).update(embalagens=0)
        antes = EventoInventario.objects.count()
        with self.assertRaises(ValueError):
            self.distribuir([0])
        self.assertFalse(AcessoInventario.objects.exists())
        self.assertEqual(EventoInventario.objects.count(), antes)
        self.assertEqual(itens_incluidos(self.inv).count(), 3)

    def test_versao_antiga_repeticao_e_finalizada_nao_alteram(self):
        versao = self.inv.versao
        acesso, _ = self.distribuir([0])
        eventos = EventoInventario.objects.count()
        with self.assertRaises(ValueError):
            self.distribuir([0], versao=versao)
        self.ok(acesso, 0)
        self.enviar(acesso)
        with self.assertRaises(ValueError):
            self.distribuir([1])
        self.assertEqual(EventoInventario.objects.filter(acao=RETIRADO).count(), 2)
        self.assertEqual(self.inv.acessos.count(), 1)

    def test_get_historico_nao_infere_exclusao(self):
        self.atribuir([0])
        self.client.force_login(self.user)
        antes = EventoInventario.objects.count()
        self.client.get(reverse("inventario:detalhe", args=[self.inv.pk]))
        self.assertEqual(itens_incluidos(self.inv).count(), 3)
        self.assertEqual(EventoInventario.objects.count(), antes)

    def test_sem_versao_ou_produto_estrangeiro_nao_cria_nem_retira(self):
        with self.assertRaises(ValueError):
            services.distribuir_produtos(self.user, self.inv.pk, self.funcionario.pk,
                [self.rodadas[0].pk], 24, None)
        with self.assertRaises(ValueError):
            services.distribuir_produtos(self.user, self.inv.pk, self.funcionario.pk,
                [999999], 24, self.inv.versao)
        self.assertFalse(self.inv.acessos.exists())
        self.assertEqual(itens_incluidos(self.inv).count(), 3)
