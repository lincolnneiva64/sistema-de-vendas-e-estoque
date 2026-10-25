import json
import subprocess
import base64
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from django.core.management import call_command
from io import StringIO

from . import tests_correcao_simples as simple
from .models import Compra, ItemCompra, Produto, ProdutoFornecedor, EdicaoCompra, MovimentoFinanceiro
from .services.catalogo_correcao_compra import regularizar
from . import views
from offline.browser_support import Chrome


def publicado():
    source = subprocess.check_output(['git','show','5c3404d:estoque/services/correcao_compra.py']).decode('utf-8')
    namespace = {'__name__':'estoque.services.correcao_publicada'}
    exec(compile(source,'5c3404d:correcao_compra.py','exec'),namespace)
    return namespace['aplicar']


class Fixture(simple.SimpleFixture):
    def nota(self):
        return self.simple(**{'preco_unitario[]':['35,65','46,81']})

    def snapshot(self):
        data=super().snapshot()
        data['ProdutoFornecedor']=list(ProdutoFornecedor.objects.order_by('pk').values())
        return data

    def legado(self):
        with patch('estoque.services.correcao_compra.aplicar',publicado()):
            response=self.save(self.nota())
        self.assertEqual(response.status_code,200,response.content)
        self.compra.refresh_from_db()

    def nova(self, produto=None, custo='40', data=None):
        produto=produto or self.correct
        compra=Compra.objects.create(fornecedor=self.fornecedor,data_compra=data or self.compra.data_compra,
            data_vencimento=date(2026,11,24),tipo_pagamento='aprazo',status='rascunho',total=custo,total_produtos=custo)
        ItemCompra.objects.create(compra=compra,produto=produto,quantidade=1,unidade=produto.unidade_compra,
            preco_unitario=custo,valor_total=custo)
        return compra


class Backend(Fixture, TestCase):
    def test_causa_comprovada_executando_servico_publicado_5c3404d(self):
        self.legado()
        self.correct.refresh_from_db();self.other.refresh_from_db()
        self.assertEqual(self.compra.total,Decimal('434.62'))
        self.assertEqual(self.correct.preco_compra,Decimal('35.40'))
        self.assertEqual(self.other.preco_compra,Decimal('47.40'))
        self.assertFalse(self.compra.revisao_precos_pendente)
        self.assertFalse(ProdutoFornecedor.objects.filter(produto__in=[self.correct,self.other]).exists())

    def test_correcao_atualiza_custos_fornecedor_e_revisao_sem_mudar_venda(self):
        vendas={p.pk:(p.preco_vista,p.preco_prazo) for p in (self.correct,self.other)}
        data=self.nota();response=self.save(data);self.assertEqual(response.status_code,200,response.content)
        for p,esperado,antigo in [(self.correct,'35.65','35.40'),(self.other,'46.81','47.40')]:
            p.refresh_from_db();self.assertEqual(p.preco_compra,Decimal(esperado))
            self.assertEqual((p.preco_vista,p.preco_prazo),vendas[p.pk])
            item=self.compra.itens.get(produto=p)
            self.assertEqual(item.preco_compra_anterior,Decimal(antigo));self.assertFalse(item.revisao_preco_concluida)
            self.assertEqual(ProdutoFornecedor.objects.get(produto=p,fornecedor=self.fornecedor).ultimo_preco_compra,Decimal(esperado))
        self.compra.refresh_from_db();self.assertTrue(self.compra.revisao_precos_pendente)
        self.assertEqual(self.save(data).status_code,200)
        self.assertEqual(EdicaoCompra.objects.count(),1)
        self.assertEqual(ProdutoFornecedor.objects.count(),2)
        pending=self.client.get(reverse('estoque:revisao_precos_posterior_pendentes')).json()
        self.assertEqual(pending['produtos'],2)

    def test_finalizacao_normal_preserva_regras_existentes(self):
        compra=self.nova(custo='36')
        views._finalizar_compra_com_financeiro(compra,atualizar_custo_produto_ids={self.correct.pk},revisao_precos_pendente=True)
        self.correct.refresh_from_db();compra.refresh_from_db()
        self.assertEqual(self.correct.preco_compra,36);self.assertTrue(compra.revisao_precos_pendente)
        self.assertEqual(compra.itens.get().preco_compra_anterior,Decimal('35.40'))
        self.assertEqual(compra.contas_pagar.get().valor_em_aberto,36)

    def test_precos_de_venda_so_mudam_depois_da_aprovacao(self):
        self.assertEqual(self.save(self.nota()).status_code,200)
        item=self.compra.itens.get(produto=self.correct)
        data={'compra_id':self.compra.pk,'item_id':item.pk,'produto_id':self.correct.pk,
            'atualizar_preco_venda_produto_ids[]':[str(self.correct.pk)],
            'atualizar_preco_venda_nomes[]':['preco_vista'],'atualizar_preco_venda_valores[]':['52']}
        response=self.client.post(reverse('estoque:revisao_precos_posterior_salvar'),data)
        self.assertEqual(response.status_code,200,response.content)
        self.correct.refresh_from_db();self.assertEqual(self.correct.preco_vista,52)
        self.assertEqual(self.correct.preco_compra,Decimal('35.65'))
        self.other.refresh_from_db();self.assertEqual(self.other.preco_vista,60)

    def test_revisao_obsoleta_nao_aplica_venda_sobre_custo_mais_recente(self):
        self.assertEqual(self.save(self.nota()).status_code,200)
        posterior=self.nova(custo='40')
        views._finalizar_compra_com_financeiro(posterior,atualizar_custo_produto_ids={self.correct.pk})
        item=self.compra.itens.get(produto=self.correct)
        data={'compra_id':self.compra.pk,'item_id':item.pk,'produto_id':self.correct.pk,
            'atualizar_preco_venda_produto_ids[]':[str(self.correct.pk)],
            'atualizar_preco_venda_nomes[]':['preco_vista'],'atualizar_preco_venda_valores[]':['52']}
        before=self.snapshot()
        response=self.client.post(reverse('estoque:revisao_precos_posterior_salvar'),data)
        self.assertEqual(response.status_code,400,response.content);self.assertEqual(self.snapshot(),before)

    def test_grupo_vinculado_mantem_precos_e_custo_dos_demais_integrantes(self):
        from .grupos_produtos import criar_grupo
        peer=Produto.objects.create(nome='Outro sabor',preco_compra=30,preco_vista=50,preco_prazo=50,unidade_compra='DZ')
        grupo=criar_grupo([self.correct.pk,peer.pk],'Grupo de teste')
        versao=grupo.versao_precos
        self.assertEqual(self.save(self.nota()).status_code,200)
        peer.refresh_from_db();grupo.refresh_from_db()
        self.assertEqual(peer.preco_compra,30);self.assertEqual(peer.preco_vista,50)
        self.assertEqual(grupo.versao_precos,versao)

    def test_vinculo_fornecedor_do_produto_errado_nao_guarda_ultima_compra_ficticia(self):
        vinculo=ProdutoFornecedor.objects.create(produto=self.produto,fornecedor=self.fornecedor,
            ultimo_preco_compra=Decimal('36.80'),ultima_compra_em=self.compra.data_compra,
            codigo_produto_fornecedor='PRESERVAR',ativo=False)
        self.assertEqual(self.save(self.nota()).status_code,200)
        vinculo.refresh_from_db()
        self.assertIsNone(vinculo.ultimo_preco_compra);self.assertIsNone(vinculo.ultima_compra_em)
        self.assertEqual(vinculo.codigo_produto_fornecedor,'PRESERVAR');self.assertFalse(vinculo.ativo)

    def test_fator_invalido_recusa_toda_correcao(self):
        self.correct.vende_fracionado=True;self.correct.fator_conversao=0;self.correct.save()
        before=self.snapshot();response=self.save(self.nota())
        self.assertEqual(response.status_code,422,response.content);self.assertEqual(self.snapshot(),before)

    def test_quantidade_sem_mudanca_de_custo_nao_recalcula_catalogo(self):
        data=self.simple(**{'produto_id[]':[str(self.produto.pk),str(self.other.pk)],
            'quantidade[]':['4','7'],'unidade[]':['PCT','PCT'],'preco_unitario[]':['36,80','47,40']})
        self.assertEqual(self.save(data).status_code,200)
        self.compra.refresh_from_db();self.assertFalse(self.compra.revisao_precos_pendente)
        self.assertEqual(ProdutoFornecedor.objects.count(),0)

    def test_fracionamento_converte_custo_sem_usar_estoque_arredondado(self):
        self.correct.vende_fracionado=True;self.correct.unidade_venda_1='DZ';self.correct.unidade_venda_2='UN'
        self.correct.fator_conversao=12;self.correct.preco_compra_fracionado=Decimal('2.95')
        self.correct.preco_vista_fracionado=5;self.correct.preco_prazo_fracionado=6;self.correct.save()
        data=self.nota();data['quantidade[]']=['36','7'];data['unidade[]']=['UN','PCT'];data['preco_unitario[]']=['3','46,81']
        self.assertEqual(self.save(data).status_code,200)
        self.correct.refresh_from_db();self.assertEqual(self.correct.preco_compra,36)
        self.assertEqual(self.correct.preco_compra_fracionado,3);self.assertEqual(self.correct.preco_vista_fracionado,5)
        pending=self.client.get(reverse('estoque:revisao_precos_posterior_pendentes')).json()['itens']
        self.assertEqual(next(i for i in pending if i['produto_id']==self.correct.pk)['custo_novo'],'36.00')

    def test_compra_posterior_impede_sobrescrita_e_informa_no_resultado(self):
        posterior=self.nova(custo='50')
        views._finalizar_compra_com_financeiro(posterior,atualizar_custo_produto_ids={self.correct.pk})
        response=self.save(self.nota());self.assertEqual(response.status_code,200)
        self.correct.refresh_from_db();self.assertEqual(self.correct.preco_compra,50)
        audit=self.compra.edicoes.get();efeito=next(e for e in audit.depois['ajustes']['catalogo'] if e['produto_id']==self.correct.pk)
        self.assertEqual(efeito['status'],'compra_posterior')
        detail=self.client.get(response.json()['redirect_url'])
        self.assertContains(detail,'Custos do cadastro preservados por compras posteriores')

    def test_rollback_de_catalogo_reverte_tambem_estoque_e_financeiro(self):
        for target in ['estoque.services.catalogo_correcao_compra.aplicar_precos_compra_em_lote',
                       'estoque.services.catalogo_correcao_compra.ProdutoFornecedor.save']:
            before=self.snapshot()
            with patch(target,side_effect=RuntimeError('catalog failure')):
                response=self.save(self.nota())
            self.assertEqual(response.status_code,500);self.assertEqual(self.snapshot(),before)

    def test_regularizacao_previa_e_aplicacao_idempotente_sem_reeditar_nota(self):
        self.legado();before=self.snapshot()
        preview=regularizar(self.compra.pk,self.user)
        self.assertFalse(preview['aplicado']);self.assertEqual(self.snapshot(),before)
        movimentos=list(MovimentoFinanceiro.objects.values());estoques=list(Produto.objects.order_by('pk').values_list('pk','quantidade'))
        regularizar(self.compra.pk,self.user,executar=True)
        self.correct.refresh_from_db();self.assertEqual(self.correct.preco_compra,Decimal('35.65'))
        self.assertEqual(list(MovimentoFinanceiro.objects.values()),movimentos)
        self.assertEqual(list(Produto.objects.order_by('pk').values_list('pk','quantidade')),estoques)
        self.assertTrue(regularizar(self.compra.pk,self.user,executar=True)['ja_processado'])
        self.assertEqual(EdicaoCompra.objects.count(),2)

    def test_regularizacao_recusa_catalogo_modificado(self):
        self.legado();Produto.objects.filter(pk=self.correct.pk).update(preco_compra=60)
        before=self.snapshot()
        with self.assertRaisesMessage(ValueError,'mudou após'):
            regularizar(self.compra.pk,self.user,executar=True)
        self.assertEqual(self.snapshot(),before)

    def test_regularizacao_command_padrao_somente_previa(self):
        self.legado();before=self.snapshot();out=StringIO()
        call_command('regularizar_catalogo_correcao',compra=self.compra.pk,usuario=self.user.pk,stdout=out)
        self.assertFalse(json.loads(out.getvalue())['aplicado']);self.assertEqual(self.snapshot(),before)

    def test_regularizacao_encontra_omissao_antes_de_outra_correcao_sem_mudar_preco(self):
        self.legado();self.legado()
        result=regularizar(self.compra.pk,self.user,executar=True)
        self.assertTrue(result['aplicado'])
        self.correct.refresh_from_db();self.assertEqual(self.correct.preco_compra,Decimal('35.65'))

    def test_regularizacao_falha_na_auditoria_reverte_custos_e_pendencias(self):
        self.legado();before=self.snapshot()
        with patch('estoque.services.catalogo_correcao_compra.EdicaoCompra.objects.create',side_effect=RuntimeError('repair audit')):
            with self.assertRaises(RuntimeError):
                regularizar(self.compra.pk,self.user,executar=True)
        self.assertEqual(self.snapshot(),before)



class Browser(Fixture, StaticLiveServerTestCase):
    open = simple.Browser.open
    select = simple.Browser.select
    fresh_purchase = simple.Browser.fresh_purchase

    def test_fluxo_simples_custos_e_revisao_em_cinco_larguras(self):
        from .tests_precos_vinculados import atualizar_fixture
        out=Path(__file__).resolve().parent.parent/'evidencias-custos-correcao'
        out.mkdir(exist_ok=True)
        evidence=[]
        with TemporaryDirectory(prefix='custos-correcao-chrome-') as profile:
            browser=Chrome(r'C:\Program Files\Google\Chrome\Application\chrome.exe',profile).start()
            try:
                for n,width in enumerate([1280,1440,320,360,390]):
                    if n:self.fresh_purchase()
                    atualizar_fixture(Produto.objects.filter(pk=self.correct.pk),preco_compra=Decimal('35.40'),autoria_precos={})
                    atualizar_fixture(Produto.objects.filter(pk=self.other.pk),preco_compra=Decimal('47.40'),autoria_precos={})
                    tab=self.open(browser,width)
                    self.assertFalse(tab.evaluate('!!document.querySelector("[name=valor_pago_real], [name=tipo_correcao]")'))
                    tab.evaluate('document.getElementById("motivoEdicaoCompra").value="Custos conferidos na nota";true')
                    if width<820:tab.evaluate('document.querySelector("#tabelaItensCompra .btn-editar-item-mobile").click();true')
                    self.select(tab,'document.querySelector("#tabelaItensCompra tbody .linha-item")')
                    tab.evaluate('row.querySelector(\'.campo-preco-unitario-compra\').value="35,65";row.querySelector(\'.campo-preco-unitario-compra\').dispatchEvent(new Event("input",{bubbles:true}));true')
                    if width<820:tab.evaluate('row.querySelector(".btn-salvar-edicao-item-mobile").click();document.querySelectorAll("#tabelaItensCompra .btn-editar-item-mobile")[1].click();true')
                    tab.evaluate('window.segunda=document.querySelectorAll("#tabelaItensCompra tbody .linha-item")[1];segunda.querySelector(\'.campo-preco-unitario-compra\').value="46,81";segunda.querySelector(\'.campo-preco-unitario-compra\').dispatchEvent(new Event("input",{bubbles:true}));true')
                    if width<820:tab.evaluate('segunda.querySelector(".btn-salvar-edicao-item-mobile").click();true')
                    self.assertFalse(tab.evaluate('document.documentElement.scrollWidth>innerWidth'))
                    shot=tab.call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True})['data']
                    (out/f'corrigir-{width}.png').write_bytes(base64.b64decode(shot))
                    tab.evaluate('document.getElementById("btnSalvarCompra").click();true')
                    tab.wait(f'location.pathname==={reverse("estoque:compras_detalhe",args=[self.compra.pk])!r} && document.readyState==="complete"')
                    self.correct.refresh_from_db();self.other.refresh_from_db();self.compra.refresh_from_db()
                    self.assertEqual(self.correct.preco_compra,Decimal('35.65'))
                    self.assertEqual(self.other.preco_compra,Decimal('46.81'))
                    self.assertEqual(self.correct.preco_vista,50);self.assertEqual(self.other.preco_vista,60)
                    self.assertEqual(self.compra.total,Decimal('434.62'));self.assertTrue(self.compra.revisao_precos_pendente)
                    self.assertIn('revisão de preços de venda pendente',tab.evaluate('document.body.innerText'))
                    self.assertEqual(tab.evaluate('purchaseConsoleErrors'),[])
                    shot=tab.call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True})['data']
                    (out/f'salvo-{width}.png').write_bytes(base64.b64decode(shot))
                    evidence.append({'width':width,'total':'434.62','custos':['35.65','46.81'],'venda_preservada':True,'revisao_pendente':True})
            finally:browser.stop()
        (out/'browser.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
