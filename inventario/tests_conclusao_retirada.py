"""Recuperação de conferências abertas sem alterar contagens já enviadas."""
from unittest.mock import patch

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from . import services
from .models import AcessoInventario, ConfirmacaoContagem, EventoInventario, RodadaContagem
from .selecao import RETIRADO, salvar_selecao
from .tests_selecao_explicita import DadosSelecao


class ConclusaoAposRetiradaTests(DadosSelecao, TestCase):
    def preparar_envios(self, multiplos=False):
        if multiplos:
            acessos = [self.atribuir([0]), self.atribuir([1], self.outro)]
            self.ok(acessos[0], 0)
            self.ok(acessos[1], 1)
        else:
            acessos = [self.atribuir([0, 1])]
            self.ok(acessos[0], 0)
            self.ok(acessos[0], 1)
        for acesso in acessos:
            self.enviar(acesso)
        self.inv.refresh_from_db()
        self.assertIsNone(self.inv.finalizada_em)
        return acessos

    def evidencias(self):
        return (list(AcessoInventario.objects.order_by("pk").values()),
                list(RodadaContagem.objects.order_by("pk").values()),
                list(ConfirmacaoContagem.objects.order_by("pk").values()))

    def test_envios_preservados_finalizacao_auditada_contadores_historico_csv(self):
        self.preparar_envios()
        antes = self.evidencias()
        self.client.force_login(self.user)
        resposta = self.client.post(reverse("inventario:detalhe", args=[self.inv.pk]), {
            "acao": "selecao", "versao": self.inv.versao,
            "participantes": [i.pk for i in self.itens[:2]],
            "motivo": "Produto não participou; retirada autorizada pelo responsável"}, follow=True)
        self.assertEqual(resposta.status_code, 200)
        self.inv.refresh_from_db()
        self.assertIsNotNone(self.inv.finalizada_em)
        self.assertEqual(self.inv.finalizada_por, self.user)
        self.assertIsNone(self.inv.finalizada_por_acesso_id)
        self.assertEqual(self.evidencias(), antes)
        evento = EventoInventario.objects.get(inventario=self.inv, acao="conferencia_finalizada")
        self.assertEqual(evento.usuario, self.user)
        self.assertIsNone(evento.acesso_id)
        self.assertEqual(evento.dados["origem"], "retirada_explicita")
        self.assertEqual(len(evento.dados["rodadas"]), 2)
        self.assertEqual(resposta.context["resumo_painel"]["total"], 2)
        self.assertEqual(resposta.context["resumo_painel"]["pendentes"], 0)
        self.assertEqual(self.inv.itens.count(), 3)
        self.assertEqual(len(resposta.context["linhas"]), 2)
        csv = self.client.get(reverse("inventario:relatorio", args=[self.inv.pk])).content.decode("utf-8")
        self.assertNotIn(self.itens[2].cadastro["nome"], csv)
        for item in self.itens[:2]:
            self.assertIn(item.cadastro["nome"], csv)

    def test_multiplos_funcionarios_sem_reenvio(self):
        self.preparar_envios(multiplos=True)
        antes = self.evidencias()
        self.selecionar([i.pk for i in self.itens[:2]])
        self.inv.refresh_from_db()
        self.assertIsNotNone(self.inv.finalizada_em)
        self.assertEqual(self.evidencias(), antes)

    def test_sem_ok_informa_pendencias_e_mantem_aberta(self):
        acesso = self.atribuir([0])
        self.ok(acesso, 0)
        self.enviar(acesso)
        self.atribuir([1], self.outro)
        self.client.force_login(self.user)
        self.inv.refresh_from_db()
        resposta = self.client.post(reverse("inventario:detalhe", args=[self.inv.pk]), {
            "acao": "selecao", "versao": self.inv.versao,
            "participantes": [i.pk for i in self.itens[:2]], "motivo": "Retirada autorizada"}, follow=True)
        self.inv.refresh_from_db()
        self.assertIsNone(self.inv.finalizada_em)
        mensagens = " ".join(str(m) for m in get_messages(resposta.wsgi_request))
        self.assertIn(self.itens[1].cadastro["nome"], mensagens)
        self.assertIn("aguardando OK", mensagens)
        self.assertIn("aguardando envio", mensagens)
        self.assertEqual(resposta.context["resumo_painel"]["pendentes"], 1)

    def test_ok_sem_envio_e_participante_sem_atribuicao_impedem(self):
        acesso = self.atribuir([0])
        self.ok(acesso, 0)
        resultado = self.selecionar([i.pk for i in self.itens[:2]])
        self.assertIsNone(resultado.finalizada_em)
        self.assertTrue(any("aguardando envio" in p for p in resultado.pendencias_conclusao))
        self.assertTrue(any("aguardando atribuição" in p for p in resultado.pendencias_conclusao))
        self.enviar(acesso)
        self.inv.refresh_from_db()
        self.assertIsNone(self.inv.finalizada_em)

    def test_retirar_contado_bloqueia_lote_sem_apagar_evidencias(self):
        self.preparar_envios()
        antes = self.evidencias()
        with self.assertRaises(ValueError):
            self.selecionar([self.itens[0].pk])
        self.assertEqual(self.evidencias(), antes)
        self.assertFalse(EventoInventario.objects.filter(acao=RETIRADO).exists())
        self.inv.refresh_from_db()
        self.assertIsNone(self.inv.finalizada_em)

    def test_repeticao_nao_duplica_e_noop_nao_finaliza(self):
        self.preparar_envios()
        self.selecionar([i.pk for i in self.itens])
        self.assertFalse(EventoInventario.objects.filter(acao="conferencia_finalizada").exists())
        self.selecionar([i.pk for i in self.itens[:2]])
        self.inv.refresh_from_db()
        data = self.inv.finalizada_em
        versao = self.inv.versao
        for _ in range(2):
            with self.assertRaises(ValueError):
                self.selecionar([i.pk for i in self.itens[:2]])
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.finalizada_em, data)
        self.assertEqual(self.inv.versao, versao)
        self.assertEqual(EventoInventario.objects.filter(acao="conferencia_finalizada").count(), 1)
        self.assertEqual(EventoInventario.objects.filter(acao=RETIRADO).count(), 1)

    def test_retirada_e_finalizacao_revertem_juntas_em_falha(self):
        self.preparar_envios()
        versao = self.inv.versao
        registrar = services.evento
        def falhar(inventario, acao, *args, **kwargs):
            if acao == "conferencia_finalizada":
                raise RuntimeError("Falha de auditoria simulada")
            return registrar(inventario, acao, *args, **kwargs)
        with patch.object(services, "evento", side_effect=falhar):
            with self.assertRaises(RuntimeError):
                self.selecionar([i.pk for i in self.itens[:2]])
        self.inv.refresh_from_db()
        self.assertIsNone(self.inv.finalizada_em)
        self.assertEqual(self.inv.versao, versao)
        self.assertFalse(EventoInventario.objects.filter(acao=RETIRADO).exists())

    def test_rodada_invalida_ou_ciclo_antigo_impedem(self):
        self.preparar_envios()
        acesso = self.inv.acessos.get()
        AcessoInventario.objects.filter(pk=acesso.pk).update(ciclo=self.inv.ciclo + 1)
        resultado = self.selecionar([i.pk for i in self.itens[:2]])
        self.assertIsNone(resultado.finalizada_em)
        self.assertTrue(any("outro ciclo" in p for p in resultado.pendencias_conclusao))
        RodadaContagem.objects.filter(pk=self.rodadas[0].pk).update(encerrada_em=self.rodadas[0].__class__.objects.get(pk=self.rodadas[0].pk).ok_em)
        _, pendencias = services.estado_conclusao(resultado)
        self.assertTrue(any("sem rodada atual única" in p for p in pendencias))
