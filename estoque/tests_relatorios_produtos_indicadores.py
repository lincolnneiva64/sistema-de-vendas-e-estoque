"""Frozen evidence and estimates on disposable PostgreSQL; never real stock."""
from datetime import date
from decimal import Decimal as D
from uuid import uuid4
import json
import os
import base64
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from inventario.models import DetalheMovimentoEstoque, MovimentoObservado
from inventario.tests import produto
from estoque.models import ItemVenda, Venda
from estoque.services.relatorios_produtos import historico_produto
from estoque.tests_relatorios import PostgreSQLIsoladoTests
from offline.browser_support import Chrome


class IndicadoresProdutoTests(PostgreSQLIsoladoTests, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.p = produto(nome='Embalagem histórica', quantidade=0, fator_conversao=99,
                        preco_compra=D('9999'), preco_vista=D('10000'), preco_prazo=D('10001'))
        cls.admin = get_user_model().objects.create_user('indicadores-admin',is_staff=True,is_superuser=True)

    def item(self, q, unidade='DZ', valor='100', custo_unitario='5', custo_total=None, custo_unidade=None, origem='preco_compra_atual'):
        v = Venda.objects.create(data_venda=date(2026,10,15),tipo_pagamento='Pix',total=D(valor))
        total = (D(q)*D(custo_unitario)).quantize(D('.01')) if custo_total is None and custo_unitario is not None else D(custo_total) if custo_total is not None else None
        return ItemVenda.objects.create(venda=v,produto=self.p,quantidade=D(q),unidade=unidade,
            preco_unitario=D('999'),valor_total=D(valor),custo_unitario_snapshot=D(custo_unitario) if custo_unitario is not None else None,
            custo_total_snapshot=total,custo_unidade_snapshot=custo_unidade or unidade,custo_origem_snapshot=origem)

    def evidencia(self, item, fator=12, base='UN', conversao='multiplicacao'):
        q = item.quantidade
        baixa = (q*D(fator) if conversao=='multiplicacao' else q/D(fator)).quantize(D('.001'))
        antes, depois = D('10000'), D('10000')-baixa
        ItemVenda.objects.filter(pk=item.pk).update(estoque_antes=antes,estoque_depois=depois,
                                                   estoque_movimentado=baixa,estoque_unidade_snapshot=base)
        m = MovimentoObservado.objects.create(produto=self.p,sequencia=MovimentoObservado.objects.filter(produto=self.p).count()+1,
            antes=antes,depois=depois,origem='venda',referencia=f'Venda #{item.venda_id}',registrado_em=timezone.now())
        return DetalheMovimentoEstoque.objects.create(movimento=m,operacao_id=uuid4(),ordem=1,acao='saida',
            documento_tipo='venda',documento_id=str(item.venda_id),item_referencia=str(item.pk),contexto='autenticado',
            executor=self.admin,executor_nome='Administrador fictício',
            unidade_base=base,quantidade_entrada=q,unidade_entrada=item.unidade,conversao=conversao,fator=D(fator))

    def resumo(self, **kw):
        return historico_produto(self.p.pk,date(2026,10,1),date(2026,10,31),hoje=date(2026,11,20),**kw)

    def test_exemplo_fisico_exato_decimal_arredondado_sem_mudar_originais(self):
        item=self.item('65.5',valor='2966.50');self.evidencia(item)
        self.item('4','UN',valor='16')
        antes=list(ItemVenda.objects.values())
        r=self.resumo()['indicadores_produto']
        self.assertEqual(r['fisica'],dict(embalagens=65,unidade='DZ',avulsas=10,total_un=790,equivalente=D('65.83'),fator=12))
        self.assertEqual(antes,list(ItemVenda.objects.values()))
        self.assertEqual(r['quantidades_vendidas'][0]['quantidade'],D('65.500'))
        self.client.force_login(self.admin)
        html=self.client.get(reverse('estoque:relatorios_produto',args=[self.p.pk]),
            dict(periodo='personalizado',data_inicial='2026-10-01',data_final='2026-10-31')).content.decode()
        for texto in ('65 DZ + 10 UN','65,83 DZ','790 UN'):
            self.assertIn(texto,html)

    def test_caixas_pacotes_fator_historico_independente_do_atual(self):
        for unidade, fator in [('PCT',6),('CX',24),('DZ',12)]:
            with self.subTest(unidade=unidade):
                self.p = produto(nome='Embalagem fictícia ' + unidade, quantidade=0, fator_conversao=99)
                i=self.item('1.5',unidade);self.evidencia(i,fator)
                dados=self.resumo(medida='quantidade',unidade=unidade)['indicadores_produto']
                self.assertEqual(dados['fisica']['total_un'],fator*3//2)
                self.assertEqual(dados['fisica']['avulsas'],fator//2)

    def test_sem_fator_imutavel_nao_inferir_de_custo_snapshot_estoque_ou_cadastro(self):
        self.item('65.5');self.item('4','UN',custo_unidade='DZ',custo_total='1.67')
        r=self.resumo()['indicadores_produto']
        self.assertIsNone(r['fisica'])
        self.assertEqual(len(r['quantidades_vendidas']),2)
        self.assertIsNone(r['cmv'])
        self.assertIsNone(r['lucro'])

    def test_fatores_divergentes_e_unidades_fracionarias_nao_inventam_inteiros(self):
        a=self.item('.333','PCT');self.evidencia(a,3)
        self.assertIsNone(self.resumo()['indicadores_produto']['fisica'])
        self.p = produto(nome='Embalagem com fatores divergentes', quantidade=0)
        a=self.item('1','PCT');self.evidencia(a,6)
        b=self.item('1','PCT');self.evidencia(b,24)
        self.assertIsNone(self.resumo()['indicadores_produto']['fisica'])

    def test_multiplos_eventos_do_mesmo_item_nao_atribuem_fator_antigo(self):
        item=self.item('1','PCT');self.evidencia(item,12);self.evidencia(item,24)
        self.assertIsNone(self.resumo()['indicadores_produto']['fisica'])

    def test_registro_de_estoque_em_outra_unidade_nao_prova_unidade_comercial(self):
        i=self.item('1','DZ');self.evidencia(i,2,base='CX',conversao='divisao')
        self.item('4','UN')
        self.assertIsNone(self.resumo()['indicadores_produto']['fisica'])

    def test_custo_congelado_valido_media_ponderada_lucro_e_margem(self):
        self.item('2',valor='30',custo_unitario='5')
        self.item('3',valor='60',custo_unitario='8')
        r=self.resumo()['indicadores_produto']
        self.assertEqual(r['linhas'][0]['preco_medio'],18)
        self.assertEqual(r['linhas'][0]['custo_medio'],D('6.80'))
        self.assertEqual(r['cmv'],34)
        self.assertEqual(r['lucro'],56)
        self.assertEqual(r['margem'],D('62.22'))
        self.assertEqual((r['com_custo'],r['itens']),(2,2))

    def test_custo_em_unidade_diferente_requer_prova_de_conversao(self):
        i=self.item('2','UN',valor='10',custo_unitario='12',custo_total='2',custo_unidade='DZ')
        self.assertIsNone(self.resumo()['indicadores_produto']['cmv'])
        self.evidencia(i,12,base='DZ',conversao='divisao')
        r=self.resumo()['indicadores_produto']
        self.assertEqual(r['cmv'],2)
        self.assertEqual(r['linhas'][0]['custo_medio'],1)
        self.assertEqual(r['lucro'],8)

    def test_custo_incompleto_inconsistente_ou_sem_origem_bloqueia_margem_total(self):
        self.item('2',custo_unitario='5')
        self.item('2',custo_unitario=None)
        self.item('2',custo_unitario='5',custo_total='9.99')
        self.item('2',custo_unitario='5',origem='')
        r=self.resumo()['indicadores_produto']
        self.assertEqual(r['com_custo'],1)
        self.assertEqual(r['custo_conhecido'],10)
        self.assertIsNone(r['cmv']);self.assertIsNone(r['lucro']);self.assertIsNone(r['margem'])

    def test_recortes_e_unidades_medias_separadas_financeiro_preservado(self):
        self.item('2','PCT',valor='20',custo_unitario='5')
        self.item('3','KG',valor='30',custo_unitario='2')
        r=self.resumo(medida='quantidade',unidade='KG')
        self.assertEqual((r['faturamento'],r['quantidade']),(30,1))
        ind=r['indicadores_produto']
        self.assertEqual(ind['unidade_recorte'],'KG')
        self.assertEqual(len(ind['linhas']),1)
        self.assertEqual(ind['linhas'][0]['preco_medio'],10)
        self.assertEqual(ind['cmv'],6)
        self.assertEqual(len(ind['quantidades_vendidas']),2)

    def test_consulta_sem_escrita_sem_custo_atual_sem_novos_registros(self):
        self.item('2',custo_unitario='5',valor='20')
        antes=list(ItemVenda.objects.values())
        with CaptureQueriesContext(connection) as sql:
            self.resumo()
        self.assertTrue(all(x['sql'].lstrip().upper().startswith(('SELECT','(SELECT')) for x in sql))
        self.assertFalse(any('preco_compra' in x['sql'].lower() for x in sql))
        self.assertEqual(list(ItemVenda.objects.values()),antes)

    def test_layout_quantidade_exata_decimal_e_custos_disponiveis_e_ausentes(self):
        i=self.item('65.5',valor='2966.50');self.evidencia(i)
        un=self.item('4','UN',valor='16')
        self.client.force_login(self.admin)
        url=reverse('estoque:relatorios_produto',args=[self.p.pk])
        params=dict(periodo='personalizado',data_inicial='2026-10-01',data_final='2026-10-31')
        completo=self.client.get(url,params).content.decode()
        ItemVenda.objects.filter(pk=un.pk).update(custo_total_snapshot=None)
        incompleto=self.client.get(url,params).content.decode()
        root=Path(__file__).resolve().parents[1]
        css=(root/'static/estoque/home.css').read_text(encoding='utf-8')
        bootstrap=root/'artifacts/relatorios-produtos-20261120/bootstrap.min.css'
        css=(bootstrap.read_text(encoding='utf-8') if bootstrap.exists() else '')+css
        css+=(root/'static/estoque/relatorios.css').read_text(encoding='utf-8')
        with TemporaryDirectory(prefix='indicadores-layout-',ignore_cleanup_errors=True) as perfil:
            browser=Chrome(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',perfil)
            original=subprocess.Popen
            with patch('offline.browser_support.subprocess.Popen',side_effect=lambda cmd,**kw:original([cmd[0],'--no-sandbox',*cmd[1:]],**kw)):
                browser.start()
            try:
                tab=browser.tab();tab.call('Network.enable')
                tab.call('Network.setBlockedURLs',{'urls':['http://*','https://*']})
                for largura in (1366,768,390,320):
                    tab.call('Emulation.setDeviceMetricsOverride',dict(width=largura,height=740,deviceScaleFactor=1,mobile=largura<768))
                    for nome,html in [('completo',completo),('incompleto',incompleto)]:
                        tab.evaluate('document.open()')
                        for pos in range(0,len(html),8000):
                            tab.evaluate('document.write('+json.dumps(html[pos:pos+8000])+')')
                        tab.evaluate("document.close();document.querySelectorAll('link,script[src]').forEach(e=>e.remove());document.head.insertAdjacentHTML('beforeend','<style id=css-teste></style>')")
                        for pos in range(0,len(css),8000):
                            tab.evaluate('document.getElementById("css-teste").textContent+='+json.dumps(css[pos:pos+8000]))
                        self.assertTrue(tab.evaluate('document.documentElement.scrollWidth<=innerWidth'),(largura,nome))
                        texto=tab.evaluate("document.querySelector('.produto-indicadores').textContent")
                        for valor in ('65 DZ + 10 UN','65,83 DZ','790 UN'):
                            self.assertIn(valor,texto)
                        self.assertEqual(tab.evaluate("document.querySelectorAll('.produto-rentabilidade-indicadores > div').length"),5)
                        if nome=='incompleto':
                            self.assertIn('Custo histórico não disponível',tab.evaluate("document.querySelector('.produto-rentabilidade').textContent"))
                        if os.environ.get('INDICADORES_VISUAL_ARTIFACTS') and largura in (1366,390):
                            pasta=Path(os.environ['INDICADORES_VISUAL_ARTIFACTS']);pasta.mkdir(parents=True,exist_ok=True)
                            (pasta/f'indicadores-{nome}-{largura}.png').write_bytes(base64.b64decode(tab.call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True})['data']))
                tab.call('Page.close');tab.socket.close()
            finally:
                browser.stop()
