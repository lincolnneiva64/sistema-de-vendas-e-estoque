"""Synthetic release rehearsal. No operational data or external databases."""
import json
import os
from pathlib import Path
import statistics
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest import skipUnless

from django.db import connection, close_old_connections, transaction, DatabaseError
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, Client
from estoque.models import Produto
from .tests import produto
from . import tests_escritores_antigos as escritores
from estoque.tests_edicao_compra import EdicaoFixture
from .models import MovimentoObservado


@skipUnless(connection.vendor == 'postgresql', 'Disposable PostgreSQL only')
class EnsaioPilotoTests(TransactionTestCase):
    roles = escritores.EscritoresAntigosTests.roles
    role = escritores.EscritoresAntigosTests.role

    def test_volume_roles_edicao_concorrencia_e_backup_restaurado(self):
        started = time.perf_counter()
        count = 5000
        Produto.objects.bulk_create([Produto(nome=f'Ensaio {i}', codigo=f'PILOTO-{i}',
            quantidade=10000, unidade_compra='UN', unidade_venda_1='UN',
            preco_compra=10, preco_vista=15, preco_prazo=16, vende_fracionado=False)
            for i in range(count)], batch_size=500)
        ids = list(Produto.objects.order_by('pk').values_list('pk', flat=True))
        for _ in range(10):
            with connection.cursor() as c:
                c.execute('UPDATE estoque_produto SET quantidade=quantidade+1')
        self.assertEqual(MovimentoObservado.objects.count(), count*10)
        seeded = time.perf_counter()
        latencies = []
        with self.roles() as (old, new, observer):
            def writer(worker):
                close_old_connections()
                times=[]
                try:
                    for step in range(100):
                        tick=time.perf_counter()
                        with transaction.atomic(), connection.cursor() as c:
                            c.execute('SET LOCAL lock_timeout = %s', ['2s'])
                            c.execute(f'SET LOCAL ROLE "{old if worker%2 else new}"')
                            c.execute('UPDATE estoque_produto SET quantidade=quantidade+1 WHERE id=%s', [ids[step%16]])
                        times.append((time.perf_counter()-tick)*1000)
                    return times
                finally:
                    close_old_connections()
            with ThreadPoolExecutor(max_workers=8) as pool:
                for times in pool.map(writer,range(8)):
                    latencies.extend(times)
        self.assertEqual(MovimentoObservado.objects.count(), count*10+800)
        with connection.cursor() as c:
            c.execute('SELECT count(*) FROM inventario_sequenciaestoque s WHERE s.sequencia <> (SELECT count(*) FROM inventario_movimentoobservado m WHERE m.produto_id=s.produto_id)')
            self.assertEqual(c.fetchone()[0],0)
        # Real existing purchase edit endpoint on top of the loaded observer.
        fixture=EdicaoFixture();fixture.client=Client();fixture.setUp()
        response=fixture.client.post(fixture.url,fixture.payload(**{'quantidade[]':['12'], 'valor_cobrado':'120', 'compra_valor_nota_boleto':'120', 'compra_parcela_valor_1':'120'}))
        self.assertEqual(response.status_code,302)
        fixture.produto.refresh_from_db();self.assertEqual(fixture.produto.quantidade,22)
        self.assertTrue(MovimentoObservado.objects.filter(produto=fixture.produto,origem='edicao_compra').exists())
        # Restore a real custom-format dump into the launcher's other empty disposable database.
        database=connection.settings_dict
        self.assertEqual(database['HOST'],'127.0.0.1')
        self.assertEqual(database['NAME'],'test_inventario_disposable')
        binary=Path(os.environ['TEMP'])/'offline-phase1-pg'/'pgsql'/'bin'
        queries=['SELECT count(*), sum(quantidade) FROM estoque_produto',
            'SELECT count(*), sum(sequencia) FROM inventario_sequenciaestoque',
            'SELECT count(*) FROM inventario_movimentoobservado',
            'SELECT count(*), sum(valor_original), sum(valor_em_aberto) FROM estoque_contapagar']
        with connection.cursor() as c:
            expected=[]
            for sql in queries:
                c.execute(sql);expected.append(c.fetchone())
        with tempfile.TemporaryDirectory(prefix='piloto-backup-') as temp:
            dump=Path(temp)/'piloto.dump'
            common=['-h','127.0.0.1','-p',str(database['PORT']),'-U',database['USER']]
            flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
            subprocess.run([str(binary/'pg_dump.exe'),*common,'-Fc','--no-owner','--no-acl','-f',str(dump),database['NAME']],check=True,capture_output=True,creationflags=flags)
            subprocess.run([str(binary/'pg_restore.exe'),*common,'--exit-on-error','--no-owner','--no-acl','-d','inventario_disposable',str(dump)],check=True,capture_output=True,creationflags=flags)
            import psycopg2
            restored=psycopg2.connect(host='127.0.0.1',port=database['PORT'],user=database['USER'],dbname='inventario_disposable')
            with restored.cursor() as c:
                for sql,values in zip(queries,expected):
                    c.execute(sql);self.assertEqual(c.fetchone(),values)
            restored.close()
            dump_size=dump.stat().st_size
        report={'synthetic_products':count,'initial_journal_rows':count*10,'parallel_workers':8,
            'concurrent_transactions':len(latencies),'errors':0,'p50_ms':statistics.median(latencies),
            'p95_ms':sorted(latencies)[int(len(latencies)*.95)],'max_ms':max(latencies),
            'seed_seconds':seeded-started,'total_seconds':time.perf_counter()-started,
            'dump_bytes':dump_size,'restored_checks_match':True}
        out=Path('ensaio-volume.json');out.write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report))

    def test_install_timeout_preserva_escritor_e_reaplicacao(self):
        latest=MigrationExecutor(connection).loader.graph.leaf_nodes()
        MigrationExecutor(connection).migrate([('inventario','0001_initial')])
        p=produto(nome='Escritor durante DDL')
        Produto.objects.bulk_create([Produto(nome=f'Catalogo legado {i}',codigo=f'DDL-{i}',quantidade=10,
            preco_compra=10,preco_vista=15,preco_prazo=16,unidade_compra='UN',vende_fracionado=False)
            for i in range(4999)],batch_size=500)
        locked,release=Event(),Event()
        def writer():
            close_old_connections()
            try:
                with transaction.atomic(), connection.cursor() as c:
                    c.execute('UPDATE estoque_produto SET quantidade=quantidade+1 WHERE id=%s',[p.pk])
                    locked.set();self.assertTrue(release.wait(15))
            finally:
                close_old_connections()
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(writer);self.assertTrue(locked.wait(10))
                try:
                    with connection.cursor() as c:c.execute("SET lock_timeout='100ms'")
                    with self.assertRaises(DatabaseError):
                        MigrationExecutor(connection).migrate([('inventario','0002_observador_quantidade')])
                finally:
                    release.set()
                future.result(15)
            p.refresh_from_db();self.assertEqual(p.quantidade,11)
        finally:
            with connection.cursor() as c:c.execute("SET lock_timeout='5s'")
            MigrationExecutor(connection).migrate(latest)
            with connection.cursor() as c:c.execute("SET lock_timeout='0'")
        Produto.objects.filter(pk=p.pk).update(quantidade=12)
        self.assertEqual(MovimentoObservado.objects.get(produto=p).depois,12)


class PublicHttpsTests(TestCase):
    def test_link_whatsapp_https_proxy_csrf_and_no_production_demo_bypass(self):
        from django.test import override_settings
        from django.contrib.auth import get_user_model
        from estoque.models import Funcionario
        from . import services
        from urllib.parse import urlsplit, parse_qs
        from django.conf import settings
        self.assertFalse(settings.INVENTARIO_AJUSTES_HABILITADOS)
        self.assertNotIn('inventario.demo_access.AcessoDiretoDemoMiddleware',settings.MIDDLEWARE)
        with override_settings(ALLOWED_HOSTS=['piloto.example.test'],SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO','https'),
                CSRF_TRUSTED_ORIGINS=['https://piloto.example.test']):
            browser=Client(enforce_csrf_checks=True,HTTP_HOST='piloto.example.test',HTTP_X_FORWARDED_PROTO='https')
            self.assertEqual(browser.get('/vendas/').status_code,200)
            self.assertEqual(browser.get('/inventario/').status_code,403)
            user=get_user_model().objects.create_superuser('gestor-https','','teste')
            employee=Funcionario.objects.create(nome='Funcionario HTTPS',telefone_whatsapp='11999999999')
            p=produto();inv=services.criar_inventario(user,'HTTPS','manual',[p.pk]);round=inv.itens.get().rodadas.get()
            browser.force_login(user)
            url=f'/inventario/sessao/{inv.pk}/';browser.get(url)
            response=browser.post(url,{'acao':'acesso','versao':inv.versao,'funcionario':employee.pk,'horas':24,'rodadas':[round.pk],
                'csrfmiddlewaretoken':browser.cookies['csrftoken'].value},HTTP_ORIGIN='https://piloto.example.test')
            self.assertEqual(response.status_code,200)
            link=response.context['link'];self.assertTrue(link.startswith('https://piloto.example.test/inventario/acesso/'))
            message=parse_qs(urlsplit(response.context['whatsapp_url']).query)['text'][0]
            self.assertIn(link,message)
