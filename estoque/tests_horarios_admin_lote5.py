"""Admin opt-in: apresentação, formulários e permissões preservados."""
import ast
import base64
import json
import os
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from django.apps import apps
from django.contrib import admin
from django.contrib.admin.models import LogEntry, CHANGE
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from estoque import admin as estoque_admin
from locacoes import admin as locacoes_admin
from estoque import tests_horarios_grupo_a_lote3 as base
from estoque.models import OperacaoRecebimentoCliente, ProdutoFornecedor, ResolucaoVisitaFornecedor, EnvioListaCompraFornecedor
from locacoes.models import MovimentoEstoqueLocacao, PagamentoLocacao, RegistroCobrancaLocacao, EventoLocacao
from estoque.templatetags.horarios_admin import conteudo_horario_admin
from offline.browser_support import Chrome

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT/'artifacts/horarios-grupo-a-lote5-20261028'


class Fixture(base.Fixture):
    def setUp(self):
        super().setUp()
        self.operacao = OperacaoRecebimentoCliente.objects.create(cliente=self.cliente,
            cliente_nome_snapshot=self.cliente.nome,valor_recebido=10,valor_aplicado=10,
            saldo_anterior=20,saldo_atual=10,data_recebimento=date(2018,11,1),forma_pagamento='Dinheiro',criado_por=self.user,
            recibo_confirmado_em=base.INSTANTE)
        self.vinculo = ProdutoFornecedor.objects.create(produto=self.produto,fornecedor=self.fornecedor)
        self.resolucao = ResolucaoVisitaFornecedor.objects.create(fornecedor=self.fornecedor,
            data_visita_original=date(2018,11,1),tipo_resolucao=ResolucaoVisitaFornecedor.TIPO_ADIADA,
            nova_data_visita=date(2018,11,2),responsavel=self.user)
        self.envio = EnvioListaCompraFornecedor.objects.create(lista=self.lista,fornecedor=self.fornecedor,
            nome_destinatario='Fornecedor teste',telefone_destinatario='5591999999999',confirmado_em=base.INSTANTE,
            confirmado_por=self.user,origem_destinatario='personalizado',chave_idempotencia='horarios-admin')
        self.movimento = MovimentoEstoqueLocacao.objects.create(item='mesa',tipo='entrada',quantidade=1,
            saldo_anterior=0,saldo_posterior=1,responsavel='Teste',locacao=self.locacao,data_hora=base.CADASTRO)
        self.pagamento = PagamentoLocacao.objects.create(locacao=self.locacao,valor=1,
            forma_pagamento=PagamentoLocacao.FORMA_DINHEIRO,data_hora=base.CADASTRO,recibo_enviado_em=base.INSTANTE)
        self.cobranca = RegistroCobrancaLocacao.objects.create(locacao=self.locacao,tipo='telefone',status='contatado')
        self.evento = EventoLocacao.objects.create(locacao=self.locacao,tipo='teste',descricao='Histórico antigo')
        self.objetos = [self.operacao,self.vinculo,self.resolucao,self.envio,self.movimento,
            self.locacao,self.cobranca,self.tarefa,self.evento,self.pagamento]
        for obj in self.objetos:
            updates = {f.name:base.INSTANTE for f in obj._meta.fields if f.name in
                ['criado_em','atualizado_em','resolvido_em','confirmado_em']}
            type(obj).objects.filter(pk=obj.pk).update(**updates)
            obj.refresh_from_db()
            log = LogEntry.objects.create(user=self.user,content_type=ContentType.objects.get_for_model(obj),
                object_id=str(obj.pk),object_repr=str(obj)[:200],action_flag=CHANGE,change_message='Alteração antiga preservada')
            LogEntry.objects.filter(pk=log.pk).update(action_time=base.INSTANTE)

    def urls_admin(self):
        for obj in self.objetos:
            prefix='admin:'+obj._meta.app_label+'_'+obj._meta.model_name+'_'
            yield obj,reverse(prefix+'changelist'),reverse(prefix+'change',args=[obj.pk]),reverse(prefix+'history',args=[obj.pk])

    def retrato(self):
        modelos = [m for app in ['estoque','locacoes','inventario'] for m in apps.get_app_config(app).get_models()]
        modelos += [LogEntry]
        return {m._meta.label:list(m.objects.order_by('pk').values()) for m in modelos}


class HorariosAdminTests(Fixture,TestCase):
    def test_listas_detalhes_historico_e_leitura_sem_mutacao(self):
        antes=self.retrato()
        with timezone.override('Asia/Tokyo'):
            for obj,lista,detalhe,historico in self.urls_admin():
                for tipo,url in [('lista',lista),('detalhe',detalhe),('historico',historico)]:
                    response=self.client.get(url)
                    self.assertEqual(response.status_code,200,(obj._meta.label,tipo))
                    if tipo=='historico':
                        self.assertContains(response,base.TEXTO)
                        self.assertContains(response,'Alteração antiga preservada')
                    if tipo=='lista' and not obj is self.locacao:
                        esperado='27/07/2026 às 11:02:03' if obj is self.pagamento else base.TEXTO
                        self.assertContains(response,esperado)
                if obj is self.movimento:
                    response=self.client.get(detalhe)
                    self.assertContains(response,'Horário de referência do movimento')
                    self.assertContains(response,'Movimento registrado no servidor')
                    self.assertContains(response,'27/07/2026 às 11:02:03')
        self.assertEqual(antes,self.retrato())

    def test_formularios_filtros_ordenacao_e_acoes_iguais_ao_anterior(self):
        request=RequestFactory().get('/admin/');request.user=self.user
        for obj in self.objetos:
            atual=admin.site._registry[type(obj)]
            modulo=estoque_admin if obj._meta.app_label=='estoque' else locacoes_admin
            tree=ast.parse((ARTIFACTS/'antes'/obj._meta.app_label/'admin.py').read_text(encoding='utf8'))
            node=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==type(atual).__name__)
            node.decorator_list=[]
            ns=dict(vars(modulo));exec(compile(ast.Module(body=[node],type_ignores=[]),'admin-anterior','exec'),ns)
            anterior=ns[node.name](type(obj),admin.AdminSite())
            for atributo in ['readonly_fields','list_filter','ordering','search_fields','list_editable','actions','form','inlines','fields','exclude']:
                self.assertEqual(getattr(atual,atributo),getattr(anterior,atributo),(obj._meta.label,atributo))
            velho,novo=anterior.get_form(request,obj)(),atual.get_form(request,obj)()
            self.assertEqual(list(velho.fields),list(novo.fields))
            for nome in velho.fields:
                a,b=velho.fields[nome],novo.fields[nome]
                self.assertEqual(type(a.widget),type(b.widget))
                self.assertEqual(a.widget.attrs,b.widget.attrs)
                self.assertEqual(getattr(a,'input_formats',None),getattr(b,'input_formats',None))
                self.assertEqual((a.required,a.disabled),(b.required,b.disabled))
            self.assertEqual(set(atual.get_actions(request)),set(anterior.get_actions(request)))
            cols=tuple(c.removeprefix('horario_') if c.removeprefix('horario_') in atual.horarios_admin else c for c in atual.list_display)
            esperado=anterior.list_display+('criado_em',) if obj is self.movimento else anterior.list_display
            self.assertEqual(cols,esperado)
            for nome in atual.list_display:
                if nome.startswith('horario_') and nome.removeprefix('horario_') in atual.horarios_admin:
                    self.assertEqual(getattr(atual,nome).admin_order_field,nome.removeprefix('horario_'))

    def test_ausencias_preservam_traco_e_templates_isolados(self):
        for obj in self.objetos:
            model_admin=admin.site._registry[type(obj)]
            for nome in model_admin.horarios_admin:
                field=SimpleNamespace(is_readonly=True,field={'name':nome},model_admin=model_admin,
                    form=SimpleNamespace(instance=SimpleNamespace(**{nome:None})),empty_value_display='-')
                self.assertEqual(conteudo_horario_admin(field),'-')
        installed=Path(admin.__file__).resolve().parent/'templates/admin'
        original=(installed/'includes/fieldset.html').read_text(encoding='utf8')
        local=(ROOT/'estoque/templates/admin/horarios/fieldset.html').read_text(encoding='utf8')
        self.assertEqual(local,'{% load horarios_admin %}\n'+original.replace('{{ field.label_tag }}','{% rotulo_horario_admin field %}').replace('{{ field.contents }}','{% conteudo_horario_admin field %}'))
        original=(installed/'object_history.html').read_text(encoding='utf8')
        content=original.split('{% block content %}',1)[1].rsplit('{% endblock %}',1)[0]
        content=content.replace("{% translate 'Date/time' %}",'Ação administrativa registrada em').replace('{{ action.action_time|date:"DATETIME_FORMAT" }}','{% instante_horario_admin action.action_time %}')
        local=(ROOT/'estoque/templates/admin/horarios/object_history.html').read_text(encoding='utf8')
        self.assertEqual(local,'{% extends "admin/object_history.html" %}\n{% load i18n horarios_admin %}\n{% block content %}'+content+'{% endblock %}\n')

    def test_permissoes_e_confirmacao_de_acao_sem_excluir(self):
        antes=self.retrato()
        _,lista,detalhe,historico=next(self.urls_admin())
        response=self.client.post(lista,{'action':'delete_selected','_selected_action':[self.operacao.pk]})
        self.assertEqual(response.status_code,200)
        self.assertTemplateUsed(response,'admin/delete_selected_confirmation.html')
        self.assertEqual(antes,self.retrato())
        limitado=get_user_model().objects.create_user('admin-limitado',is_staff=True)
        self.client.force_login(limitado)
        for url in [lista,detalhe,historico]:self.assertEqual(self.client.get(url).status_code,403)
        limitado.user_permissions.add(Permission.objects.get(content_type=ContentType.objects.get_for_model(self.operacao),codename='view_operacaorecebimentocliente'))
        response=self.client.get(detalhe)
        self.assertEqual(response.status_code,200)
        self.assertContains(response,base.TEXTO)
        self.assertNotContains(response,'name="_save"')
        self.assertEqual(antes,self.retrato())
        self.client.logout()
        self.assertEqual(self.client.get(lista).status_code,302)


class HorariosAdminBrowserTests(Fixture,StaticLiveServerTestCase):
    def test_desktop_mobile_admin(self):
        antes=self.retrato();out=Path(os.environ['HORARIOS_A5_ARTIFACTS'])
        with TemporaryDirectory(prefix='horarios-a5-chrome-') as profile:
            chrome=Chrome(base.base.CHROME,profile).start()
            try:
                tab=chrome.tab();tab.call('Network.enable')
                tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Emulation.setTimezoneOverride',{'timezoneId':'Asia/Tokyo'})
                for width in (1366,390):
                    tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':900,'deviceScaleFactor':1,'mobile':width<768})
                    for obj,lista,detalhe,historico in self.urls_admin():
                        for tipo,url in [('lista',lista),('detalhe',detalhe),('historico',historico)]:
                            tab.call('Page.navigate',{'url':self.live_server_url+url});tab.wait('document.readyState === "complete"')
                            self.assertEqual(tab.evaluate('location.pathname'),url)
                            if tipo=='historico' or (obj in [self.movimento,self.pagamento] and tipo=='detalhe'):
                                found=tab.evaluate("[...document.querySelectorAll('.horario-registro')].filter(e=>e.getClientRects().length).map(e=>({weight:getComputedStyle(e).fontWeight,color:getComputedStyle(e).color}))")
                                self.assertTrue(found,(obj._meta.label,tipo,width))
                                self.assertTrue(all(e['weight']=='700' and e['color']=='rgb(30, 41, 59)' for e in found))
                                self.assertTrue(tab.evaluate("[...document.querySelectorAll('.admin-horario')].every(e=>getComputedStyle(e).backgroundColor==='rgb(248, 250, 252)')"))
                            if obj in [self.movimento,self.pagamento,self.resolucao] and tipo in ['lista','detalhe','historico']:
                                image=tab.call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True})
                                (out/f'{obj._meta.model_name}-{tipo}-{width}.png').write_bytes(base64.b64decode(image['data']))
                tab.evaluate("localStorage.setItem('theme','light');document.documentElement.dataset.theme='light';true")
                for width in (1366,390):
                    tab.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':900,'deviceScaleFactor':1,'mobile':width<768})
                    prefix='admin:locacoes_movimentoestoquelocacao_'
                    for tipo,url in [('detalhe',reverse(prefix+'change',args=[self.movimento.pk])),('historico',reverse(prefix+'history',args=[self.movimento.pk]))]:
                        tab.call('Page.navigate',{'url':self.live_server_url+url});tab.wait('document.readyState === "complete"')
                        self.assertEqual(tab.evaluate('document.documentElement.dataset.theme'),'light')
                        self.assertTrue(tab.evaluate("[...document.querySelectorAll('.admin-horario')].every(e=>getComputedStyle(e).backgroundColor==='rgb(248, 250, 252)')"))
                        image=tab.call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True})
                        (out/f'movimento-{tipo}-claro-{width}.png').write_bytes(base64.b64decode(image['data']))
                self.assertEqual(antes,self.retrato())
            finally:chrome.stop()
