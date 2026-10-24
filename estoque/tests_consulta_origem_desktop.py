"""Read-only desktop grouping and provenance, isolated PostgreSQL fixtures."""
from datetime import timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase, RequestFactory
from django.urls import reverse
from django.utils import timezone
from estoque.models import Compra, Fornecedor, ListaCompraFornecedor
from estoque.services.compras_origem_consulta import anotar_compras_listas_desktop, anotar_origens_compras
from estoque.views import _resposta_lista_fornecedor_salva

class ConsultaOrigemDesktopTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser('origem-consulta','test@example.test','test-only'))
        self.supplier=Fornecedor.objects.create(nome='Fornecedor Origem')
        self.url=reverse('estoque:compras_listas_fornecedor')

    def lista(self,status='aberta',**kwargs):
        return ListaCompraFornecedor.objects.create(fornecedor=self.supplier,status=status,
            data_lista=timezone.localdate(),data_inicio_periodo=timezone.localdate(),data_fim_periodo=timezone.localdate(),**kwargs)

    def compra(self,lista=None,**kwargs):
        return Compra.objects.create(fornecedor=self.supplier,lista_fornecedor=lista,
            data_compra=timezone.localdate(),status=kwargs.pop('status','rascunho'),**kwargs)

    def ids(self,params=None):
        response=self.client.get(self.url,params or {})
        self.assertEqual(response.status_code,200)
        return {row.pk for row in response.context['listas_desktop']}

    def test_default_only_open_sent_without_proven_purchase(self):
        open_row=self.lista();sent=self.lista('enviada');done=self.lista('finalizada');cancel=self.lista('cancelada')
        linked=self.lista();self.compra(linked)
        legacy=self.lista();self.compra(observacao=f'Gerada a partir da Lista de Compras #{legacy.pk}')
        self.assertEqual(self.ids(),{open_row.pk,sent.pk})
        self.assertEqual(self.ids({'aba':'compras'}),{linked.pk,legacy.pk})
        self.assertEqual(self.ids({'aba':'todas'}),{open_row.pk,sent.pk,done.pk,cancel.pk,linked.pk,legacy.pk})
        self.assertEqual(self.ids({'aba':'invalida'}),{open_row.pk,sent.pk})

    def test_field_wins_over_conflicting_observation_and_newer_legacy(self):
        first=self.lista();second=self.lista()
        direct=self.compra(first,observacao=f'Gerada a partir da Lista de Compras #{second.pk}')
        self.compra(observacao=f'Gerada a partir da Lista de Compras #{first.pk}')
        rows=anotar_compras_listas_desktop([first,second])
        self.assertEqual(rows[0].compra_gerada.pk,direct.pk)
        self.assertIsNone(rows[1].compra_gerada)
        anotar_origens_compras([direct]);self.assertEqual(direct.origem_lista_id,first.pk)

    def test_exact_historical_id_no_prefix_collision(self):
        short=self.lista(pk=125);long=self.lista(pk=1250)
        generated=self.compra(observacao='Gerada a partir da Lista de Compras #1250\nObservacao adicional')
        rows=anotar_compras_listas_desktop([short,long])
        self.assertIsNone(rows[0].compra_gerada)
        self.assertEqual(rows[1].compra_gerada.pk,generated.pk)
        anotar_origens_compras([generated]);self.assertEqual(generated.origem_lista_id,long.pk)

    def test_mentions_nonexistent_and_ambiguous_markers_do_not_prove_origin(self):
        first=self.lista();second=self.lista()
        cases=[self.compra(observacao=f'Conferir Lista de Compras #{first.pk}'),
            self.compra(observacao='Gerada a partir da Lista de Compras #999999'),
            self.compra(observacao=f'Gerada a partir da Lista de Compras #{first.pk}\nGerada a partir da Lista de Compras #{second.pk}'),
            self.compra(observacao=f'Gerada a partir da Lista de Compras #{first.pk}abc')]
        anotar_origens_compras(cases)
        self.assertTrue(all(row.origem_lista_id is None for row in cases))
        self.assertEqual(self.ids({'aba':'compras'}),set())
        self.assertEqual(self.ids(),{first.pk,second.pk})

    def test_cancelled_purchase_is_still_a_recorded_origin(self):
        row=self.lista();purchase=self.compra(row,status='cancelada',cancelada=True)
        self.assertEqual(self.ids(),set())
        response=self.client.get(self.url,{'aba':'compras'})
        self.assertEqual(response.context['listas_desktop'][0].compra_gerada.pk,purchase.pk)
        self.assertContains(response,'Cancelada')

    def test_latest_legacy_purchase_is_used_without_duplicate_list_rows(self):
        row=self.lista();self.compra(observacao=f'Gerada a partir da Lista de Compras #{row.pk}')
        latest=self.compra(observacao=f'Gerada a partir da Lista de Compras #{row.pk}')
        response=self.client.get(self.url,{'aba':'compras'})
        self.assertEqual(len(response.context['listas_desktop']),1)
        self.assertEqual(response.context['listas_desktop'][0].compra_gerada.pk,latest.pk)

    def test_all_filters_and_tabs_preserve_query(self):
        selected=self.lista('cancelada');self.lista('finalizada')
        other=Fornecedor.objects.create(nome='Outro Fornecedor')
        excluded=self.lista('cancelada');excluded.fornecedor=other;excluded.save(update_fields=['fornecedor'])
        old=self.lista('cancelada');old.data_lista=timezone.localdate()-timedelta(days=30);old.save(update_fields=['data_lista'])
        params={'aba':'todas','status':'cancelada','fornecedor':'Origem','data_inicio':timezone.localdate().isoformat(),'data_fim':timezone.localdate().isoformat()}
        self.assertEqual(self.ids(params),{selected.pk})
        response=self.client.get(self.url,params)
        from urllib.parse import urlsplit,parse_qs
        for tab in response.context['abas_desktop']:
            query=parse_qs(urlsplit(tab['url']).query)
            for name in ['status','fornecedor','data_inicio','data_fim']:self.assertEqual(query[name],[params[name]])
        self.assertEqual(self.ids({**params,'aba':'ativas'}),set())

    def test_mobile_collection_preserves_existing_rules(self):
        row=self.lista('finalizada');cancel=self.lista('cancelada');direct=self.lista();self.compra(direct)
        legacy=self.lista();self.compra(observacao=f'Gerada a partir da Lista de Compras #{legacy.pk}')
        for tab in ['ativas','compras','todas']:
            response=self.client.get(self.url,{'aba':tab})
            self.assertEqual({r.pk for r in response.context['listas_mobile']},{row.pk,direct.pk})
            response=self.client.get(self.url,{'aba':tab,'mobile':'historico'})
            self.assertEqual({r.pk for r in response.context['listas_mobile']},{cancel.pk,legacy.pk})

    def test_provenance_read_is_batched_and_does_not_write(self):
        row=self.lista();direct=self.compra(row);legacy=self.compra(observacao=f'Gerada a partir da Lista de Compras #{row.pk}')
        before=list(Compra.objects.values());lists_before=list(ListaCompraFornecedor.objects.values())
        with self.assertNumQueries(1):anotar_origens_compras([direct,legacy])
        with self.assertNumQueries(1):anotar_compras_listas_desktop([row])
        self.assertEqual(list(Compra.objects.values()),before)
        self.assertEqual(list(ListaCompraFornecedor.objects.values()),lists_before)

    def test_purchase_origin_labels_links_and_filters_keep_totals(self):
        row=self.lista();direct=self.compra(row);manual=self.compra()
        legacy=self.compra(observacao=f'Gerada a partir da Lista de Compras #{row.pk}')
        url=reverse('estoque:compras_lista')
        response=self.client.get(url)
        purchases={p.pk:p for p in response.context['compras']}
        self.assertEqual(purchases[direct.pk].origem_lista_id,row.pk)
        self.assertEqual(purchases[legacy.pk].origem_lista_id,row.pk)
        self.assertIsNone(purchases[manual.pk].origem_lista_id)
        self.assertTrue(all(p.total_lista==Decimal('0.00') for p in purchases.values()))
        self.assertContains(response,f'Gerada da Lista #{row.pk}',count=2)
        self.assertContains(response,'Compra direta',count=1)
        response=self.client.get(url,{'compra':str(manual.pk)})
        self.assertEqual([p.pk for p in response.context['compras']],[manual.pk])

    def test_cancelled_shortcut_and_tab_survive_edit_return(self):
        row=self.lista('cancelada')
        response=self.client.get(self.url)
        self.assertContains(response,'?aba=todas&amp;status=cancelada')
        request=RequestFactory().post('/',{'retorno_consulta':self.url+'?aba=todas&status=cancelada&externo=1'})
        response=_resposta_lista_fornecedor_salva(request,row)
        self.assertEqual(response.url,self.url+'?aba=todas&status=cancelada')
