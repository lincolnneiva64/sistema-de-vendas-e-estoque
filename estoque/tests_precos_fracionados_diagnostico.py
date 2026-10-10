"""Reproduce the reported fractional editor flow with isolated, fictional data."""
from pathlib import Path
from tempfile import TemporaryDirectory
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import close_old_connections, connection, models
from django.test import TestCase, TransactionTestCase, override_settings
from django.core.exceptions import ValidationError
from django.urls import reverse
from decimal import Decimal

from offline.browser_support import Chrome
from .models import Categoria, Unidade, Produto, AlteracaoPrecoVinculado
from .forms import ProdutoForm
from .grupos_produtos import criar_grupo
from .tests_precos_vinculados import criar_operador_precos
from .services.precos_vinculados import (
    alterar_precos_grupo, diagnosticar_grupo, versao_cadastro_produto,
    regularizar_grupo,
)


class FracionadosFixture:
    def setUp(self):
        self.operador=criar_operador_precos('fractional-diagnostic')
        self.client.force_login(self.operador)
        Categoria.objects.get_or_create(nome='Mercearia')
        for sigla in ('FD','UN'):Unidade.objects.get_or_create(sigla=sigla,defaults={'nome':sigla})
        self.produtos=[]
        for i in range(4):
            self.produtos.append(Produto.objects.create(nome='Produto diagnostico '+str(i),categoria='Mercearia',
                unidade_compra='FD',unidade_venda_2='UN' if i<2 else None,fator_conversao=24 if i<2 else 0,
                vende_fracionado=i<2,preco_compra=24,preco_vista=30,preco_prazo=31,
                preco_vista_fracionado='1.20' if i<2 else 0,preco_prazo_fracionado='1.30' if i==0 else '1.20' if i==1 else 0,
                quantidade=10,estoque_minimo=1))
        self.grupo=criar_grupo([p.pk for p in self.produtos],'Grupo diagnostico FD')

    def compatibilizar_fixture(self):
        # Only fictional fixture data, never a runtime normalization.
        models.QuerySet.update(Produto.objects.filter(pk__in=[p.pk for p in self.produtos[2:]]),
            vende_fracionado=True,unidade_venda_2='UN',fator_conversao=24,
            preco_vista_fracionado='1.20',preco_prazo_fracionado='1.30')

    def preparar_lopitos(self):
        nomes=('Lopitos Cebolinha 24/30G','Lopitos Gal Caipira 24/30G',
            'Lopitos Queijo 24/30G','Lopitos Requeijão 24/30G')
        precos_fracionados=(('1.20','1.30'),('1.20','1.20'),('0','0'),('0','0'))
        for produto,nome,fracionados in zip(self.produtos,nomes,precos_fracionados):
            models.QuerySet.update(Produto.objects.filter(pk=produto.pk),
                nome=nome,preco_compra=20,preco_compra_fracionado='0.83',
                unidade_compra='FD',unidade_venda_1='FD',unidade_venda_2='UN',
                fator_conversao=24,vende_fracionado=True,preco_venda=26,
                preco_vista=26,preco_prazo=27,
                preco_vista_fracionado=fracionados[0],
                preco_prazo_fracionado=fracionados[1],
                percentual_vista_fracionado='44.58' if fracionados[0]!='0' else 0,
                percentual_prazo_fracionado='56.63' if fracionados[1]=='1.30' else
                    '44.58' if fracionados[1]!='0' else 0)
        models.QuerySet.update(type(self.grupo).objects.filter(pk=self.grupo.pk),
            nome='Lopitos 24/30G')
        observacao=diagnosticar_grupo(list(self.grupo.produtos.all()),validar_precos=False)
        regularizar_grupo(self.grupo.pk,observacao['versao_observada'],
            confirmar=True,operador=self.operador)

    def comando_lopitos(self,selecionados=None,fonte=1):
        produtos=list(self.grupo.produtos.order_by('pk'))
        self.grupo.refresh_from_db()
        return {
            'versao_esperada':self.grupo.versao_precos,
            'operador':self.operador,
            'selecionados':selecionados or [p.pk for p in produtos],
            'confirmar':True,
            'assinatura_esperada':diagnosticar_grupo(
                produtos,validar_precos=False)['assinatura_precos'],
        }

    def atualizar_lopitos(self,selecionados=None,fonte=1):
        valores={'preco_vista':Decimal('26.00'),'preco_prazo':Decimal('27.00'),
            'preco_vista_fracionado':Decimal('1.20'),
            'preco_prazo_fracionado':Decimal('1.30')}
        return alterar_precos_grupo(self.produtos[fonte].pk,valores,
            **self.comando_lopitos(selecionados,fonte))


class FracionadosDiagnosticoTests(FracionadosFixture,TestCase):
    def dados_editor(self,indice):
        produto=Produto.objects.get(pk=self.produtos[indice].pk)
        form=ProdutoForm(instance=produto,integrar_precos_vinculados=True)
        dados={k:v for k,v in form.initial.items() if v is not None}
        dados.pop('fornecedores',None)
        dados.update(categoria='Mercearia')
        return dados

    def test_mixed_configuration_is_blocked_not_price_divergence(self):
        d=diagnosticar_grupo(list(self.grupo.produtos.all()),validar_precos=False)
        self.assertTrue(d['bloqueios'])
        por_campo={item['campo']:item for item in d['incompatibilidades']}
        self.assertEqual(set(por_campo),{'unidade_venda_2','fator_conversao','vende_fracionado'})
        self.assertEqual({p['produto_id'] for p in por_campo['vende_fracionado']['integrantes']},{p.pk for p in self.produtos})
        self.compatibilizar_fixture()
        d=diagnosticar_grupo(list(self.grupo.produtos.all()),validar_precos=False)
        self.assertFalse(d['bloqueios'])
        self.assertIn('preco_prazo_fracionado',d['divergentes'])

    def test_invalid_configuration_identifies_product_and_fields(self):
        models.QuerySet.update(Produto.objects.filter(pk=self.produtos[0].pk),fator_conversao=0,unidade_venda_2=None)
        d=diagnosticar_grupo(list(self.grupo.produtos.all()),validar_precos=False)
        mensagem=' '.join(b['motivo'] for b in d['bloqueios'] if b['produto_id']==self.produtos[0].pk)
        self.assertIn(self.produtos[0].nome,mensagem)
        self.assertIn('fator_conversao=0',mensagem)
        self.assertIn('unidade_venda_2=ausente',mensagem)

    def test_unit_and_factor_differences_remain_blocked(self):
        self.compatibilizar_fixture()
        models.QuerySet.update(Produto.objects.filter(pk=self.produtos[3].pk),fator_conversao=12,unidade_venda_2='DZ')
        d=diagnosticar_grupo(list(self.grupo.produtos.all()),validar_precos=False)
        self.assertTrue(d['bloqueios'])
        self.assertEqual({i['campo'] for i in d['incompatibilidades']},{'unidade_venda_2','fator_conversao'})

    def test_direct_model_write_cannot_enable_fractionation_and_price_without_command(self):
        p=Produto.objects.get(pk=self.produtos[2].pk)
        antes=Produto.objects.filter(pk=p.pk).values().get()
        p.vende_fracionado=True;p.unidade_venda_2='UN';p.fator_conversao=24
        p.preco_vista_fracionado=Decimal('1.20');p.preco_prazo_fracionado=Decimal('1.30')
        with self.assertRaises(ValidationError):p.save()
        self.assertEqual(Produto.objects.filter(pk=p.pk).values().get(),antes)

    def test_real_stock_or_cost_change_refuses_stale_form(self):
        self.compatibilizar_fixture()
        for campo,valor in [('quantidade',9),('preco_compra',25)]:
            with self.subTest(campo=campo):
                dados=self.dados_editor(1)
                models.QuerySet.update(Produto.objects.filter(pk=self.produtos[1].pk),**{campo:valor})
                antes=list(Produto.objects.order_by('pk').values())
                dados['nome']='Nome atualizado'
                resposta=self.client.post(reverse('estoque:produto_editar',args=[self.produtos[1].pk]),dados)
                self.assertEqual(resposta.status_code,200)
                self.assertIn('Cadastro, custo ou estoque alterado',str(resposta.context['form'].errors))
                self.assertEqual(list(Produto.objects.order_by('pk').values()),antes)

    def test_own_previous_save_requires_fresh_form(self):
        self.compatibilizar_fixture()
        antigo=self.dados_editor(1)
        dados=dict(antigo,nome='Primeira alteracao')
        url=reverse('estoque:produto_editar',args=[self.produtos[1].pk])
        self.assertEqual(self.client.post(url,dados).status_code,302)
        self.assertEqual(self.client.post(url,dict(antigo,nome='Formulario antigo')).status_code,200)
        novo=self.dados_editor(1)
        self.assertEqual(self.client.post(url,dict(novo,nome='Formulario novo')).status_code,302)

    def test_new_fractional_prices_cannot_bypass_confirmation_while_enabling_fractionation(self):
        antes=list(Produto.objects.order_by('pk').values())
        dados=self.dados_editor(2)
        dados.update(vende_fracionado='on',unidade_venda_2='UN',fator_conversao=24,
            preco_vista_fracionado='1.20',preco_prazo_fracionado='1.30')
        resposta=self.client.post(reverse('estoque:produto_editar',args=[self.produtos[2].pk]),dados)
        self.assertEqual(resposta.status_code,200,'Fractional prices must not bypass explicit consent')
        self.assertTrue(resposta.context['form'].errors)
        self.assertEqual(list(Produto.objects.order_by('pk').values()),antes)

    def test_configuration_can_be_repaired_without_prices_or_group_activation(self):
        dados=self.dados_editor(2)
        dados.update(vende_fracionado='on',unidade_venda_2='UN',fator_conversao=24)
        resposta=self.client.post(reverse('estoque:produto_editar',args=[self.produtos[2].pk]),dados)
        self.assertEqual(resposta.status_code,302)
        p=Produto.objects.get(pk=self.produtos[2].pk)
        self.assertTrue(p.vende_fracionado);self.assertEqual(p.fator_conversao,24)
        self.assertEqual(p.preco_vista_fracionado,0);self.assertEqual(p.preco_prazo_fracionado,0)
        self.grupo.refresh_from_db();self.assertFalse(self.grupo.precos_regularizados)
        self.assertFalse(AlteracaoPrecoVinculado.objects.exists())

    def test_zero_prices_do_not_block_activation_or_update_unselected_products(self):
        self.compatibilizar_fixture()
        models.QuerySet.update(Produto.objects.filter(pk__in=[p.pk for p in self.produtos[2:]]),preco_vista_fracionado=0,preco_prazo_fracionado=0)
        produtos=list(self.grupo.produtos.all());antes=list(Produto.objects.order_by('pk').values())
        diagnostico=diagnosticar_grupo(produtos,validar_precos=False)
        self.assertFalse(diagnostico['bloqueios'])
        regularizar_grupo(self.grupo.pk,diagnostico['versao_observada'],confirmar=True,operador=self.operador)
        self.assertEqual(list(Produto.objects.order_by('pk').values()),antes)
        dados=self.dados_editor(2)
        dados.update(vende_fracionado='on',preco_vista_fracionado='1.20',preco_prazo_fracionado='1.30',
            confirmar_precos_seletivos=True,assinatura_precos_seletivos=diagnosticar_grupo(produtos,validar_precos=False)['assinatura_precos'])
        resposta=self.client.post(reverse('estoque:produto_editar',args=[self.produtos[2].pk]),dados)
        self.assertEqual(resposta.status_code,302)
        for p,anterior in zip(Produto.objects.order_by('pk').values(),antes):
            if p['id']!=self.produtos[2].pk:self.assertEqual(p,anterior)

    def test_selected_zero_fractional_prices_are_validated_after_proposal(self):
        self.compatibilizar_fixture()
        selecionados=self.produtos[2:]
        models.QuerySet.update(Produto.objects.filter(pk__in=[p.pk for p in selecionados]),
            preco_compra=20,preco_compra_fracionado=0,
            preco_vista_fracionado=0,preco_prazo_fracionado=0)
        diagnostico=diagnosticar_grupo(list(self.grupo.produtos.all()),validar_precos=False)
        regularizar_grupo(self.grupo.pk,diagnostico['versao_observada'],confirmar=True,operador=self.operador)
        antes={p['id']:p for p in Produto.objects.values()}

        from estoque.views import _custo_produto_para_unidade_venda
        for produto in selecionados:
            self.assertEqual(_custo_produto_para_unidade_venda(
                Produto.objects.get(pk=produto.pk),'UN'),Decimal('0.83'))

        dados=self.dados_editor(2)
        dados.update(preco_vista_fracionado='1.20',preco_prazo_fracionado='1.30',
            destinatarios_precos=[str(selecionados[1].pk)],
            confirmar_precos_seletivos=True,
            assinatura_precos_seletivos=diagnosticar_grupo(
                list(self.grupo.produtos.all()),validar_precos=False)['assinatura_precos'])
        resposta=self.client.post(reverse('estoque:produto_editar',args=[selecionados[0].pk]),dados)

        self.assertEqual(resposta.status_code,302,str(resposta.context['form'].errors) if resposta.context else '')
        depois={p['id']:p for p in Produto.objects.values()}
        selecionados_ids={p.pk for p in selecionados}
        for produto_id,anterior in antes.items():
            novo=depois[produto_id]
            if produto_id not in selecionados_ids:
                self.assertEqual(novo,anterior)
                continue
            self.assertEqual(novo['preco_vista_fracionado'],Decimal('1.20'))
            self.assertEqual(novo['preco_prazo_fracionado'],Decimal('1.30'))
            for campo in ('preco_vista','preco_prazo','preco_compra',
                          'preco_compra_fracionado','quantidade'):
                self.assertEqual(novo[campo],anterior[campo])

        evento=AlteracaoPrecoVinculado.objects.order_by('-versao_depois').first()
        self.assertEqual(evento.evidencia['selecionados'],sorted(selecionados_ids))
        for produto in selecionados:
            self.assertEqual(set(evento.evidencia['alteracoes'][str(produto.pk)]),
                {'preco_vista_fracionado','preco_prazo_fracionado'})
        self.grupo.refresh_from_db()
        self.assertEqual(self.grupo.versao_precos,2)

    def test_below_cost_rejection_reports_each_final_fractional_value_and_cost(self):
        self.compatibilizar_fixture()
        selecionados=self.produtos[2:]
        models.QuerySet.update(Produto.objects.filter(pk__in=[p.pk for p in selecionados]),
            preco_compra=20,preco_compra_fracionado=0,
            preco_vista_fracionado=0,preco_prazo_fracionado=0)
        models.QuerySet.update(Produto.objects.filter(pk=selecionados[1].pk),preco_compra=36)
        diagnostico=diagnosticar_grupo(list(self.grupo.produtos.all()),validar_precos=False)
        regularizar_grupo(self.grupo.pk,diagnostico['versao_observada'],confirmar=True,operador=self.operador)
        antes=list(Produto.objects.order_by('pk').values())
        eventos_antes=AlteracaoPrecoVinculado.objects.count()
        self.grupo.refresh_from_db()
        versao_antes=self.grupo.versao_precos

        dados=self.dados_editor(2)
        dados.update(preco_vista_fracionado='1.20',preco_prazo_fracionado='1.30',
            destinatarios_precos=[str(selecionados[1].pk)],
            confirmar_precos_seletivos=True,
            assinatura_precos_seletivos=diagnosticar_grupo(
                list(self.grupo.produtos.all()),validar_precos=False)['assinatura_precos'])
        resposta=self.client.post(reverse('estoque:produto_editar',args=[selecionados[0].pk]),dados)
        erros=str(resposta.context['form'].errors)

        self.assertEqual(resposta.status_code,200)
        self.assertIn('Produto '+str(selecionados[1].pk),erros)
        self.assertIn('preco_vista_fracionado=R$ 1,20 (custo individual R$ 1,50)',erros)
        self.assertIn('preco_prazo_fracionado=R$ 1,30 (custo individual R$ 1,50)',erros)
        self.assertEqual(list(Produto.objects.order_by('pk').values()),antes)
        self.assertEqual(AlteracaoPrecoVinculado.objects.count(),eventos_antes)
        self.grupo.refresh_from_db()
        self.assertEqual(self.grupo.versao_precos,versao_antes)

    def test_lopitos_explicit_targets_regularize_all_four_including_zero_fields(self):
        self.preparar_lopitos()
        antes={p['id']:p for p in Produto.objects.order_by('pk').values()}
        evento=self.atualizar_lopitos(fonte=1)
        depois={p['id']:p for p in Produto.objects.order_by('pk').values()}

        self.assertEqual([p['nome'] for p in depois.values()],[
            'Lopitos Cebolinha 24/30G','Lopitos Gal Caipira 24/30G',
            'Lopitos Queijo 24/30G','Lopitos Requeijão 24/30G'])
        for produto in self.produtos:
            anterior=antes[produto.pk]
            novo=depois[produto.pk]
            for campo,esperado in {
                'preco_vista':Decimal('26.00'),'preco_prazo':Decimal('27.00'),
                'preco_vista_fracionado':Decimal('1.20'),
                'preco_prazo_fracionado':Decimal('1.30'),
            }.items():
                self.assertEqual(novo[campo],esperado)
            for campo in ('preco_compra','preco_compra_fracionado','quantidade',
                          'unidade_compra','unidade_venda_1','unidade_venda_2',
                          'fator_conversao'):
                self.assertEqual(novo[campo],anterior[campo])
            self.assertTrue(novo['precos_canonicos_adotados'])
            self.assertEqual(novo['percentual_vista_fracionado'],Decimal('44.58'))
            self.assertEqual(novo['percentual_prazo_fracionado'],Decimal('56.63'))

        self.assertEqual(evento.evidencia['selecionados'],
            sorted(p.pk for p in self.produtos))
        self.assertEqual(evento.evidencia['campos'],[
            'preco_prazo','preco_prazo_fracionado','preco_vista',
            'preco_vista_fracionado'])
        self.assertEqual(set(evento.evidencia['alteracoes'][str(self.produtos[0].pk)]),set())
        for produto in self.produtos[1:]:
            self.assertEqual(set(evento.evidencia['alteracoes'][str(produto.pk)]),{
                'preco_prazo_fracionado',
            } if produto.pk==self.produtos[1].pk else {
                'preco_vista_fracionado','preco_prazo_fracionado',
            })
        self.grupo.refresh_from_db()
        self.assertEqual(self.grupo.versao_precos,2)
        self.assertEqual(AlteracaoPrecoVinculado.objects.count(),2)

    def test_lopitos_partial_selection_keeps_other_group_members_unchanged(self):
        self.preparar_lopitos()
        antes={p['id']:p for p in Produto.objects.order_by('pk').values()}
        selecionados=[self.produtos[1].pk,self.produtos[2].pk]
        evento=self.atualizar_lopitos(selecionados=selecionados,fonte=1)
        depois={p['id']:p for p in Produto.objects.order_by('pk').values()}

        for produto in self.produtos:
            if produto.pk not in selecionados:
                self.assertEqual(depois[produto.pk],antes[produto.pk])
            else:
                self.assertEqual(depois[produto.pk]['preco_vista_fracionado'],Decimal('1.20'))
                self.assertEqual(depois[produto.pk]['preco_prazo_fracionado'],Decimal('1.30'))
                self.assertTrue(depois[produto.pk]['precos_canonicos_adotados'])
        self.assertEqual(evento.evidencia['selecionados'],sorted(selecionados))
        self.assertEqual(set(evento.evidencia['alteracoes']),{str(p.pk) for p in self.produtos})
        self.assertEqual(evento.evidencia['alteracoes'][str(self.produtos[0].pk)],{})
        self.assertEqual(evento.evidencia['alteracoes'][str(self.produtos[3].pk)],{})

    def test_lopitos_repeated_explicit_targets_are_idempotent(self):
        self.preparar_lopitos()
        self.atualizar_lopitos(fonte=1)
        antes=list(Produto.objects.order_by('pk').values())
        eventos=AlteracaoPrecoVinculado.objects.count()
        self.grupo.refresh_from_db()
        versao=self.grupo.versao_precos

        resultado=self.atualizar_lopitos(fonte=1)

        self.assertIsNone(resultado)
        self.assertEqual(list(Produto.objects.order_by('pk').values()),antes)
        self.assertEqual(AlteracaoPrecoVinculado.objects.count(),eventos)
        self.grupo.refresh_from_db()
        self.assertEqual(self.grupo.versao_precos,versao)

    def test_lopitos_failure_during_writes_rolls_back_products_group_and_audit(self):
        self.preparar_lopitos()
        antes=list(Produto.objects.order_by('pk').values())
        eventos=AlteracaoPrecoVinculado.objects.count()
        self.grupo.refresh_from_db()
        versao=self.grupo.versao_precos
        original=models.QuerySet.update
        writes=0

        def fail_on_third_product_write(queryset,**values):
            nonlocal writes
            if queryset.model is Produto and 'precos_canonicos_adotados' in values:
                writes+=1
                if writes==3:
                    raise RuntimeError('isolated injected Lopitos write failure')
            return original(queryset,**values)

        with patch.object(models.QuerySet,'update',fail_on_third_product_write):
            with self.assertRaises(RuntimeError):
                self.atualizar_lopitos(fonte=1)

        self.assertEqual(list(Produto.objects.order_by('pk').values()),antes)
        self.assertEqual(AlteracaoPrecoVinculado.objects.count(),eventos)
        self.grupo.refresh_from_db()
        self.assertEqual(self.grupo.versao_precos,versao)


@skipUnless(connection.vendor=='postgresql','Requires isolated PostgreSQL')
class FracionadosConcorrenciaTests(FracionadosFixture,TransactionTestCase):
    def test_lopitos_same_version_explicit_target_race_has_one_winner(self):
        self.preparar_lopitos()
        versao=self.grupo.versao_precos
        assinatura=diagnosticar_grupo(
            list(self.grupo.produtos.order_by('pk')),validar_precos=False)['assinatura_precos']
        valores={'preco_vista':Decimal('26.00'),'preco_prazo':Decimal('27.00'),
            'preco_vista_fracionado':Decimal('1.20'),
            'preco_prazo_fracionado':Decimal('1.30')}
        ids=[p.pk for p in self.produtos]
        barreira=Barrier(2)

        def executar(index):
            close_old_connections()
            try:
                barreira.wait(timeout=15)
                try:
                    alterar_precos_grupo(self.produtos[index].pk,valores,
                        versao_esperada=versao,operador=self.operador,
                        selecionados=ids,confirmar=True,
                        assinatura_esperada=assinatura)
                    return True
                except ValidationError:
                    return False
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            resultados=list(pool.map(executar,(0,1)))

        self.assertEqual(sum(resultados),1)
        self.assertEqual(AlteracaoPrecoVinculado.objects.count(),2)
        self.grupo.refresh_from_db()
        self.assertEqual(self.grupo.versao_precos,2)
        self.assertEqual(set(Produto.objects.values_list(
            'preco_vista_fracionado',flat=True)),{Decimal('1.20')})
        self.assertEqual(set(Produto.objects.values_list(
            'preco_prazo_fracionado',flat=True)),{Decimal('1.30')})


@skipUnless(Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe').is_file(),'Chrome unavailable')
class FracionadosDiagnosticoBrowserTests(FracionadosFixture,StaticLiveServerTestCase):
    def fluxo(self,width,compativel,zerado=False,multiplo=False,ativo=False):
        if compativel:self.compatibilizar_fixture()
        fonte=self.produtos[2] if zerado else self.produtos[1]
        if zerado:
            models.QuerySet.update(Produto.objects.all(),preco_compra_fracionado=1)
            models.QuerySet.update(Produto.objects.filter(pk=fonte.pk),preco_vista_fracionado=0,preco_prazo_fracionado=0)
        if ativo:
            d=diagnosticar_grupo(list(self.grupo.produtos.all()),validar_precos=False)
            regularizar_grupo(self.grupo.pk,d['versao_observada'],confirmar=True,operador=self.operador)
        antes={p['id']:p for p in Produto.objects.values()}
        with TemporaryDirectory(prefix='fractional-diagnostic-',ignore_cleanup_errors=True) as profile:
            chrome=Chrome(r'C:\Program Files\Google\Chrome\Application\chrome.exe',profile).start()
            try:
                tab=chrome.tab()
                tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':844,'deviceScaleFactor':1,'mobile':width<860})
                tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Page.navigate',{'url':self.live_server_url+reverse('estoque:produto_editar',args=[fonte.pk])})
                tab.wait('document.readyState === "complete" && !!document.getElementById("form-produto")')
                token=tab.evaluate('document.getElementById("id_versao_cadastro_precos").value')
                self.assertEqual(token,versao_cadastro_produto(Produto.objects.get(pk=fonte.pk)))
                if zerado:
                    tab.evaluate('document.querySelector("[name=preco_vista_fracionado]").value="1,20";document.querySelector("[name=preco_vista_fracionado]").dispatchEvent(new Event("input",{bubbles:true}));true')
                tab.evaluate('document.querySelector("[name=preco_prazo_fracionado]").value="1,30";document.querySelector("[name=preco_prazo_fracionado]").dispatchEvent(new Event("input",{bubbles:true}));document.getElementById("form-produto").requestSubmit();true')
                tab.wait('!!document.querySelector("dialog[open] [data-destinatario]")')
                self.assertEqual(token,versao_cadastro_produto(Produto.objects.get(pk=fonte.pk)))
                if multiplo:tab.evaluate(f'Array.from(document.querySelectorAll("dialog[open] [data-destinatario]")).find(b=>b.value==={str(self.produtos[1].pk)!r}).checked=true;true')
                tab.evaluate('Array.from(document.querySelectorAll("dialog[open] button")).find(b=>b.textContent.includes("Confirmar selecionados")).click();true')
                tab.wait('location.pathname === "/" || !!document.getElementById("alerta-erros-produto")')
                if compativel:
                    self.assertEqual(tab.evaluate('location.pathname'), '/',tab.evaluate('document.body.textContent')[:1500])
                    fonte.refresh_from_db();self.assertEqual(str(fonte.preco_prazo_fracionado),'1.30')
                    if zerado:
                        selecionados={fonte.pk,self.produtos[1].pk} if multiplo else {fonte.pk}
                        for p in Produto.objects.values():
                            self.assertEqual(p['quantidade'],antes[p['id']]['quantidade'])
                            self.assertEqual(p['preco_compra'],antes[p['id']]['preco_compra'])
                            if p['id'] not in selecionados:self.assertEqual(p,antes[p['id']])
                            else:self.assertEqual(p['preco_prazo_fracionado'],Decimal('1.30'))
                        self.assertTrue(AlteracaoPrecoVinculado.objects.exists())
                else:
                    texto=tab.evaluate('document.getElementById("alerta-erros-produto").textContent')
                    self.assertNotIn('Cadastro, custo ou estoque alterado',texto)
                    self.assertIn('Integrantes com unidades',texto)
            finally:chrome.stop()

    def test_desktop_compatible_fractional_edit(self):self.fluxo(1280,True)
    def test_mobile_compatible_fractional_edit(self):self.fluxo(390,True)
    def test_desktop_mixed_configuration_refused(self):self.fluxo(1280,False)
    def test_mobile_mixed_configuration_refused(self):self.fluxo(390,False)
    def test_desktop_zero_price_pending_source_only(self):self.fluxo(1280,True,zerado=True)
    def test_mobile_zero_price_active_multiple(self):self.fluxo(390,True,zerado=True,multiplo=True,ativo=True)

    def configuracao_sem_precos(self,width):
        fonte=self.produtos[2]
        models.QuerySet.update(Produto.objects.filter(pk=fonte.pk),cadastro_incompleto=True,percentual_vista_fracionado=5,percentual_prazo_fracionado=7)
        models.QuerySet.update(Produto.objects.filter(pk=self.produtos[3].pk),vende_fracionado=True,unidade_venda_2='UN',fator_conversao=24)
        antes={p['id']:p for p in Produto.objects.values()}
        with TemporaryDirectory(prefix='fractional-config-only-',ignore_cleanup_errors=True) as profile:
            chrome=Chrome(r'C:\Program Files\Google\Chrome\Application\chrome.exe',profile).start()
            try:
                tab=chrome.tab()
                tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':844,'deviceScaleFactor':1,'mobile':width<860})
                tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                url=self.live_server_url+reverse('estoque:produto_editar',args=[fonte.pk])
                tab.call('Page.navigate',{'url':url})
                tab.wait('document.readyState === "complete" && !!document.getElementById("vende_fracionado_visual")')
                tab.evaluate('document.getElementById("vende_fracionado_visual").value="True";document.getElementById("vende_fracionado_visual").dispatchEvent(new Event("change",{bubbles:true}));document.getElementById("fator_visual").value="24";document.getElementById("fator_visual").dispatchEvent(new Event("input",{bubbles:true}));document.getElementById("unidade_fracionada_visual").value="UN";document.getElementById("unidade_fracionada_visual").dispatchEvent(new Event("change",{bubbles:true}));true')
                self.assertEqual(Decimal(tab.evaluate('document.querySelector("[name=preco_vista_fracionado]").value').replace(',','.')),0)
                tab.evaluate('window.avisoSeparacao="";window.alert=mensagem=>{window.avisoSeparacao=mensagem;};document.querySelector("[name=preco_prazo_fracionado]").value="1,30";document.getElementById("form-produto").requestSubmit();true')
                self.assertIn('Salve primeiro',tab.evaluate('window.avisoSeparacao'))
                self.assertFalse(tab.evaluate('!!document.querySelector("dialog[open] [data-destinatario]")'))
                self.assertEqual({p['id']:p for p in Produto.objects.values()},antes)
                tab.evaluate('document.querySelector("[name=preco_prazo_fracionado]").value="0.00";true')
                tab.evaluate('window.dialogoDePrecosAberto=false;const abrir=HTMLDialogElement.prototype.showModal;HTMLDialogElement.prototype.showModal=function(){window.dialogoDePrecosAberto=true;return abrir.call(this);};document.getElementById("form-produto").requestSubmit();true')
                tab.wait('location.pathname === "/" || !!document.querySelector("dialog[open] [data-destinatario]") || !!document.getElementById("alerta-erros-produto")')
                self.assertEqual(tab.evaluate('location.pathname'),'/',tab.evaluate('document.getElementById("alerta-erros-produto")?.textContent || document.querySelector("dialog[open]")?.textContent || "no error"'))
                fonte.refresh_from_db()
                self.assertTrue(fonte.vende_fracionado)
                self.assertEqual(fonte.fator_conversao,24)
                self.assertEqual(fonte.unidade_venda_2,'UN')
                self.assertEqual(fonte.preco_vista_fracionado,0)
                self.assertEqual(fonte.preco_prazo_fracionado,0)
                self.assertEqual(fonte.percentual_vista_fracionado,5)
                self.assertEqual(fonte.percentual_prazo_fracionado,7)
                for p in Produto.objects.exclude(pk=fonte.pk).values():self.assertEqual(p,antes[p['id']])
                self.assertFalse(AlteracaoPrecoVinculado.objects.exists())
                self.grupo.refresh_from_db();self.assertFalse(self.grupo.precos_regularizados)
                self.assertEqual(fonte.quantidade,antes[fonte.pk]['quantidade'])
                depois={p['id']:p for p in Produto.objects.values()}
                tab.call('Page.navigate',{'url':url})
                tab.wait('document.readyState === "complete" && !!document.getElementById("form-produto")')
                tab.evaluate('document.querySelector("[name=preco_vista_fracionado]").value="1,20";document.querySelector("[name=preco_vista_fracionado]").dispatchEvent(new Event("input",{bubbles:true}));document.querySelector("[name=preco_prazo_fracionado]").value="1,30";document.querySelector("[name=preco_prazo_fracionado]").dispatchEvent(new Event("input",{bubbles:true}));document.getElementById("form-produto").requestSubmit();true')
                tab.wait('!!document.querySelector("dialog[open] [data-destinatario]")')
                self.assertFalse(tab.evaluate('Array.from(document.querySelectorAll("dialog[open] [data-destinatario]:not(:disabled)")).some(b=>b.checked)'))
                tab.evaluate('Array.from(document.querySelectorAll("dialog[open] button")).find(b=>b.textContent.includes("Confirmar selecionados")).click();true')
                tab.wait('location.pathname === "/"')
                fonte.refresh_from_db();self.assertEqual(fonte.preco_vista_fracionado,Decimal('1.20'));self.assertEqual(fonte.preco_prazo_fracionado,Decimal('1.30'))
                for p in Produto.objects.exclude(pk=fonte.pk).values():self.assertEqual(p,depois[p['id']])
                self.assertTrue(AlteracaoPrecoVinculado.objects.exists())
            finally:chrome.stop()

    def test_desktop_enable_fractionation_without_price_confirmation(self):self.configuracao_sem_precos(1280)
    def test_mobile_enable_fractionation_without_price_confirmation(self):self.configuracao_sem_precos(390)

    @override_settings(ROOT_URLCONF='estoque.tests_precos_vinculados_interfaces')
    def diagnostico_visual(self,width):
        antes=list(Produto.objects.order_by('pk').values())
        with TemporaryDirectory(prefix='fractional-fields-',ignore_cleanup_errors=True) as profile:
            chrome=Chrome(r'C:\Program Files\Google\Chrome\Application\chrome.exe',profile).start()
            try:
                tab=chrome.tab()
                tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':844,'deviceScaleFactor':1,'mobile':width<860})
                tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Page.navigate',{'url':self.live_server_url+'/novo-grupo-teste/'})
                tab.wait('document.readyState === "complete" && !!document.getElementById("grupoProdutoGerenciar")')
                tab.evaluate('document.getElementById("grupoProdutoGerenciar").click();true')
                tab.wait('!!document.querySelector("#grupoProdutoGrupos button")')
                tab.evaluate('document.querySelector("#grupoProdutoGrupos button").click();true')
                tab.wait('!document.getElementById("grupoProdutoIncompatibilidades").hidden')
                texto=tab.evaluate('document.getElementById("grupoProdutoIncompatibilidades").textContent')
                for p in self.produtos:self.assertIn(p.nome,texto)
                for campo in ('unidade_venda_2','fator_conversao','vende_fracionado'):self.assertIn(campo,texto)
                self.assertTrue(tab.evaluate('document.getElementById("grupoProdutoRegularizar").disabled'))
                self.assertEqual(list(Produto.objects.order_by('pk').values()),antes)
            finally:chrome.stop()

    def test_desktop_product_field_diagnostic(self):self.diagnostico_visual(1280)
    def test_mobile_product_field_diagnostic(self):self.diagnostico_visual(390)
