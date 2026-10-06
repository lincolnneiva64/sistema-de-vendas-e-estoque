from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.test import TestCase

from estoque.models import Cliente, EntregaRota, EntregaRotaItem, EventoVenda, Venda
from .tests import fixtures


class DeliverySyncedHistoryTests(TestCase):
    def setUp(self):
        self.user, _, task, _ = fixtures()
        self.sale = Venda.objects.create(data_venda=task.data_agendada, cliente=Cliente.objects.create(nome='Histórico'))
        self.route = EntregaRota.objects.create(tipo='unitaria')
        self.item = EntregaRotaItem.objects.create(rota=self.route, venda=self.sale)
        self.client.force_login(self.user)
        self.url = f'/entregas/{self.route.pk}/checklist/'

    def event(self, text='Nota sincronizada', **kwargs):
        fields = dict(venda=self.sale, tipo_evento='observacao_offline', canal='offline',
                      usuario='Lincoln', descricao=f'Rota #{self.route.pk}, bloco #{self.item.pk}: {text}')
        fields.update(kwargs)
        return EventoVenda.objects.create(**fields)

    def history(self, response):
        return next(item for item in response.context['itens_entrega'] if item.pk == self.item.pk).observacoes_sincronizadas

    def test_no_history_keeps_new_observation_target(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.history(response), [])
        self.assertNotContains(response, 'data-synced-history=')
        self.assertContains(response, f'data-checklist-task="{self.item.pk}"', count=1)

    def test_synced_text_user_and_local_datetime(self):
        text = 'Portão lateral: tocar campainha\n<script>alert("x")</script> & obrigado 😀'
        event = self.event(text)
        instant = datetime(2026, 10, 5, 19, 41, tzinfo=ZoneInfo('America/Sao_Paulo'))
        EventoVenda.objects.filter(pk=event.pk).update(criado_em=instant)
        response = self.client.get(self.url)
        self.assertEqual(self.history(response)[0]['texto'], text)
        self.assertContains(response, '05/10/2026 19:41')
        self.assertContains(response, '— Lincoln')
        self.assertContains(response, '&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; obrigado 😀')
        self.assertNotContains(response, '<script>alert("x")</script>')
        self.assertContains(response, 'data-synced-history=', count=1)
        self.assertContains(response, 'Observações já sincronizadas (1)')

    def test_multiple_events_newest_first_and_tie_breaker(self):
        first = self.event('Antiga')
        second = self.event('Recente')
        third = self.event('Mais recente no empate')
        instant = datetime(2026, 10, 5, 19, 41, tzinfo=ZoneInfo('America/Sao_Paulo'))
        EventoVenda.objects.filter(pk=first.pk).update(criado_em=instant - timedelta(days=1))
        EventoVenda.objects.filter(pk__in=[second.pk, third.pk]).update(criado_em=instant)
        response = self.client.get(self.url)
        self.assertEqual([note['texto'] for note in self.history(response)], ['Mais recente no empate', 'Recente', 'Antiga'])
        self.assertContains(response, 'class="check-offline-history-text"', count=3)
        self.assertContains(response, 'Observações já sincronizadas (3)')

    def test_other_types_sales_routes_blocks_and_channels_excluded(self):
        self.event('Visível')
        other_sale = Venda.objects.create(data_venda=self.sale.data_venda)
        excluded = [
            self.event('Outro tipo', tipo_evento='checklist_cliente_enviado'),
            self.event('Outra venda', venda=other_sale),
            self.event(descricao=f'Rota #{self.route.pk}0, bloco #{self.item.pk}: Outra rota'),
            self.event(descricao=f'Rota #{self.route.pk}, bloco #{self.item.pk}0: Outro bloco'),
            self.event('Outro canal', canal='whatsapp_checklist'),
            self.event(descricao='Sem contexto verificável'),
        ]
        response = self.client.get(self.url)
        self.assertEqual([note['texto'] for note in self.history(response)], ['Visível'])
        for event in excluded:
            self.assertNotContains(response, event.descricao)

    def test_each_block_has_its_own_history(self):
        other_sale = Venda.objects.create(data_venda=self.sale.data_venda, cliente=Cliente.objects.create(nome='Outra venda'))
        other_item = EntregaRotaItem.objects.create(rota=self.route, venda=other_sale)
        pending_item = EntregaRotaItem.objects.create(rota=self.route, venda=self.sale, is_pendencia=True)
        self.event('Primeira venda')
        self.event(venda=other_sale, descricao=f'Rota #{self.route.pk}, bloco #{other_item.pk}: Segunda venda')
        self.event(descricao=f'Rota #{self.route.pk}, bloco #{pending_item.pk}: Mesma venda em outro bloco')
        response = self.client.get(self.url)
        histories = {item.pk: [note['texto'] for note in item.observacoes_sincronizadas] for item in response.context['itens_entrega']}
        self.assertEqual(histories, {self.item.pk: ['Primeira venda'], other_item.pk: ['Segunda venda'],
                                    pending_item.pk: ['Mesma venda em outro bloco']})
        self.assertContains(response, 'data-synced-history=', count=3)

    def test_missing_user_read_only_and_no_new_persistence(self):
        for username in (None, ''):
            with self.subTest(username=username):
                EventoVenda.objects.all().delete()
                self.event('Sem usuário', usuario=username)
                response = self.client.get(self.url)
                self.assertEqual(self.history(response)[0]['usuario'], username)
                self.assertContains(response, 'Sem usuário', count=1)
                self.assertNotContains(response, '— None')
                self.assertNotContains(response, '— Lincoln')
                self.assertEqual(EventoVenda.objects.count(), 1)
                self.assertContains(response, f'data-checklist-task="{self.item.pk}"', count=1)
