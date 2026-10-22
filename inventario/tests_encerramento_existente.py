from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings, Client
from django.urls import reverse
from estoque.models import Produto, Funcionario, MovimentacaoEstoqueManual
from inventario import services, ajustes, views, contagem_admin
from inventario.models import (Inventario,RodadaContagem,ConfirmacaoContagem,DecisaoAjuste,
 AplicacaoAjuste,EventoAjuste,MovimentoObservado,AtribuicaoContagem)
from inventario.tests import produto


class EncerramentoFixture:
 def setUp(self):
  self.usuario=get_user_model().objects.create_superuser('gestor-sintetico','','teste')
  Inventario.objects.create(titulo='Reserva sintetica',criterio='manual',criado_por=self.usuario)
  ps=[produto(nome=f'Produto sintetico {i:02}') for i in range(30)]
  self.inv=services.criar_inventario(self.usuario,'Encerramento sintetico','manual',[p.pk for p in ps])
  self.rodadas=list(RodadaContagem.objects.filter(item__inventario=self.inv).select_related('item__produto').order_by('pk'))
  f=Funcionario.objects.create(nome='Contador sintetico')
  self.acesso,self.token=services.gerar_acesso(self.usuario,self.inv.pk,f.pk,[r.pk for r in self.rodadas])
  for n,r in enumerate(self.rodadas):
   services.salvar_contagem(self.acesso.pk,r.pk,0,'8' if n<12 else '0' if n==12 else '10','0',True)
  propostas=[]
  for r in self.rodadas[:12]:
   r.refresh_from_db();r.item.produto.refresh_from_db()
   a=ajustes.avaliar(r)
   propostas.append(ajustes.decidir(self.usuario,self.inv.pk,r.pk,a['token'],'Evidencia sintetica',confirmacao=True))
  ajustes.aplicar(self.usuario,self.inv.pk,ajustes.previa_aplicacao(self.inv.pk,propostas),True)
  self.pendente=self.rodadas[12]
  Produto.objects.filter(pk=self.pendente.item.produto_id).update(quantidade=11)
  self.inv.refresh_from_db()
  self.mobile=Client();url=reverse('inventario:convite',args=[self.token]);self.mobile.get(url);self.mobile.post(url)

 def estado_preservado(self):
  return [list(m.objects.order_by('pk').values()) for m in (Produto,RodadaContagem,ConfirmacaoContagem,
   DecisaoAjuste,AplicacaoAjuste,EventoAjuste,MovimentoObservado,MovimentacaoEstoqueManual,AtribuicaoContagem)]

 def bloqueio(self):
  r=RodadaContagem.objects.select_related('item__produto').get(pk=self.pendente.pk)
  return ajustes.avaliar(r)['bloqueio']



@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True,INVENTARIO_PRODUTOS_EXCLUIDOS_PILOTO=())
class EncerramentoExistenteTests(EncerramentoFixture,TestCase):
 def test_mobile_finaliza_30_ok_com_12_aplicados_e_1_bloqueado_sem_reaplicar(self):
  self.assertEqual(DecisaoAjuste.objects.count(),12)
  self.assertTrue(self.bloqueio())
  resumo=ajustes.preparar_resultado(self.inv)['resumo']
  self.assertEqual((resumo['aplicados'],resumo['bloqueados'],resumo['aptos']),(12,1,0))
  self.assertIsNone(self.inv.finalizada_em)
  antes=self.estado_preservado();bloqueio=self.bloqueio();versao=self.inv.versao
  pagina=self.mobile.get(reverse('inventario:mobile'))
  self.assertContains(pagina,'Finalizar e enviar')
  self.assertEqual(pagina.context['progresso'],dict(total=30,conferidos=30,faltam=0))
  resposta=self.mobile.post(reverse('inventario:finalizar'),{'versao':versao},HTTP_ACCEPT='application/json')
  self.assertEqual(resposta.status_code,200)
  self.assertTrue(resposta.json()['conferencia']['finalizada'])
  self.inv.refresh_from_db();self.assertIsNotNone(self.inv.finalizada_em)
  self.assertEqual(antes,self.estado_preservado());self.assertEqual(bloqueio,self.bloqueio())
  self.assertEqual(self.inv.eventos.filter(acao='conferencia_finalizada').count(),1)
  repetida=self.mobile.post(reverse('inventario:finalizar'),{'versao':versao},HTTP_ACCEPT='application/json')
  self.assertEqual(repetida.status_code,200)
  self.assertEqual(antes,self.estado_preservado())
  self.assertEqual(self.inv.eventos.filter(acao='conferencia_finalizada').count(),1)

 def test_encerramento_recusa_ok_faltante_sem_reabrir_ou_alterar_ajustes(self):
  RodadaContagem.objects.filter(pk=self.pendente.pk).update(ok_em=None)
  antes=self.estado_preservado()
  with self.assertRaises(ValueError):services.finalizar_conferencia(self.acesso.pk,self.inv.versao)
  self.assertEqual(antes,self.estado_preservado())
  self.inv.refresh_from_db();self.assertIsNone(self.inv.finalizada_em)

 def test_abertura_de_outra_conferencia_nao_depende_de_finalizar_a_anterior(self):
  antes=self.estado_preservado()
  nova=services.criar_inventario(self.usuario,'Outra sintetica','manual',[self.rodadas[0].item.produto_id])
  self.assertNotEqual(nova.pk,self.inv.pk)
  self.assertEqual(list(Produto.objects.order_by('pk').values()),antes[0])

 def test_ok_admin_sem_envio_nao_dispara_finalizacao_automaticamente(self):
  self.inv.refresh_from_db()
  nova=services.recontar(self.usuario,self.pendente.pk,'Recontagem sintetica')
  self.inv.refresh_from_db()
  contagem_admin.confirmar(self.usuario,self.inv.pk,nova.pk,contagem_admin.autorizacao(self.usuario,self.inv,nova),'0','0')
  self.inv.refresh_from_db()
  self.assertIsNone(self.inv.finalizada_em)
  # The valid outstanding employee submission can still conclude it.
  services.finalizar_conferencia(self.acesso.pk,self.inv.versao)
  self.inv.refresh_from_db();self.assertIsNotNone(self.inv.finalizada_em)

 def test_recontagem_admin_apos_ciclo_finalizado_nao_tem_envio_valido_dos_acessos_antigos(self):
  services.finalizar_conferencia(self.acesso.pk,self.inv.versao)
  self.inv.refresh_from_db();self.assertIsNotNone(self.inv.finalizada_em)
  nova=services.recontar(self.usuario,self.pendente.pk,'Recontagem administrativa sintetica')
  self.inv.refresh_from_db()
  contagem_admin.confirmar(self.usuario,self.inv.pk,nova.pk,contagem_admin.autorizacao(self.usuario,self.inv,nova),'0','0')
  self.inv.refresh_from_db()
  self.assertIsNone(self.inv.finalizada_em)
  self.assertEqual(sum(r.ok_em is not None for r in services.estado_conclusao(self.inv)[0]),30)
  self.assertTrue(services.estado_conclusao(self.inv)[1])
  antes=self.estado_preservado()
  from django.core.exceptions import PermissionDenied
  with self.assertRaises(PermissionDenied):services.finalizar_conferencia(self.acesso.pk,self.inv.versao)
  self.assertEqual(antes,self.estado_preservado())


from unittest.mock import patch
from django.contrib.auth.models import Permission
from inventario import encerramento
from inventario.models import EventoInventario


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True,INVENTARIO_PRODUTOS_EXCLUIDOS_PILOTO=())
class EncerramentoAdministrativoTests(EncerramentoFixture,TestCase):
 def preparar_ciclo_sem_envios_validos(self):
  services.finalizar_conferencia(self.acesso.pk,self.inv.versao)
  nova=services.recontar(self.usuario,self.pendente.pk,'Recontagem administrativa sintetica')
  self.inv.refresh_from_db()
  contagem_admin.confirmar(self.usuario,self.inv.pk,nova.pk,contagem_admin.autorizacao(self.usuario,self.inv,nova),'0','0')
  self.inv.refresh_from_db()
  # A new unidentified movement keeps the current round blocked for adjustment.
  Produto.objects.filter(pk=nova.item.produto_id).update(quantidade=12)
  return nova

 def previa(self):
  self.inv.refresh_from_db()
  return encerramento.previa(self.usuario,self.inv)

 def test_encerra_admin_ciclo_2_com_12_aplicados_sem_envios_novos_e_repete_sem_efeito(self):
  nova=self.preparar_ciclo_sem_envios_validos()
  r=RodadaContagem.objects.select_related('item__produto').get(pk=nova.pk)
  bloqueio=ajustes.avaliar(r)['bloqueio'];self.assertTrue(bloqueio)
  previa=self.previa()
  self.assertEqual((previa['total'],previa['aplicados'],previa['sem_ajuste'],previa['diferencas_pendentes']),(30,12,18,1))
  antes=self.estado_preservado()
  dados=encerramento.finalizar(self.usuario,self.inv.pk,previa['token'],'sim')
  self.assertEqual(dados['sem_ajuste'],18)
  self.inv.refresh_from_db();self.assertIsNotNone(self.inv.finalizada_em)
  self.assertEqual(self.inv.finalizada_por,self.usuario)
  self.assertEqual(antes,self.estado_preservado())
  r=RodadaContagem.objects.select_related('item__produto').get(pk=nova.pk)
  self.assertEqual(bloqueio,ajustes.avaliar(r)['bloqueio'])
  eventos=EventoInventario.objects.filter(inventario=self.inv,acao='conferencia_finalizada',dados__origem='encerramento_administrativo')
  self.assertEqual(eventos.count(),1)
  self.assertEqual(eventos.get().dados['aplicados'],12)
  self.assertFalse(eventos.get().dados['estoque_alterado'])
  self.assertTrue(encerramento.finalizar(self.usuario,self.inv.pk,previa['token'],'sim')['ja_finalizada'])
  self.assertEqual(eventos.count(),1);self.assertEqual(antes,self.estado_preservado())

 def test_http_confirmacao_totais_redireciona_lista_e_historico(self):
  self.preparar_ciclo_sem_envios_validos();self.client.force_login(self.usuario)
  url=reverse('inventario:finalizar_administrativamente',args=[self.inv.pk])
  detalhe=self.client.get(reverse('inventario:detalhe',args=[self.inv.pk]))
  self.assertContains(detalhe,'Finalizar confer')
  self.assertContains(detalhe,'18 produtos permanecer')
  self.assertEqual(self.client.get(url).status_code,405)
  token=detalhe.context['encerramento_previa']['token']
  csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.usuario)
  self.assertEqual(csrf.post(url,{'previa':token,'confirmacao':'sim'}).status_code,403)
  resposta=self.client.post(url,{'previa':token,'confirmacao':'sim'})
  self.assertRedirects(resposta,reverse('inventario:lista'))
  lista=self.client.get(reverse('inventario:lista'))
  self.assertIn(self.inv.pk,[i.pk for i in lista.context['finalizados']])
  self.assertNotIn(self.inv.pk,[i.pk for i in lista.context['em_andamento']])

 def test_permissao_confirmacao_e_token_adulterado_bloqueiam_sem_escrever(self):
  previa=self.previa();antes=self.estado_preservado()
  for tk,conf in [(previa['token'],'nao'),(previa['token']+'x','sim')]:
   with self.assertRaises(ValueError):encerramento.finalizar(self.usuario,self.inv.pk,tk,conf)
  outro=get_user_model().objects.create_user('sem-revisao')
  outro.user_permissions.add(Permission.objects.get(content_type__app_label='inventario',codename='gerenciar_inventario'))
  from django.core.exceptions import PermissionDenied
  with self.assertRaises(PermissionDenied):encerramento.finalizar(outro,self.inv.pk,previa['token'],'sim')
  self.assertEqual(antes,self.estado_preservado())
  self.inv.refresh_from_db();self.assertIsNone(self.inv.finalizada_em)

 def test_versao_antiga_novo_ok_e_confirmacao_inconsistente_recusados(self):
  previa=self.previa()
  Inventario.objects.filter(pk=self.inv.pk).update(versao=self.inv.versao+1)
  with self.assertRaises(ValueError):encerramento.finalizar(self.usuario,self.inv.pk,previa['token'],'sim')
  r=self.rodadas[-1];RodadaContagem.objects.filter(pk=r.pk).update(contado_base=9)
  with self.assertRaises(ValueError):self.previa()
  self.inv.refresh_from_db();self.assertIsNone(self.inv.finalizada_em)

 def test_falha_da_auditoria_reverte_encerramento(self):
  previa=self.previa();antes=self.estado_preservado();estado=Inventario.objects.values().get(pk=self.inv.pk)
  with patch('inventario.encerramento.services.evento',side_effect=RuntimeError('Falha sintetica')):
   with self.assertRaises(RuntimeError):encerramento.finalizar(self.usuario,self.inv.pk,previa['token'],'sim')
  self.assertEqual(estado,Inventario.objects.values().get(pk=self.inv.pk))
  self.assertEqual(antes,self.estado_preservado())

 def test_bloqueia_sem_ok_e_nao_reabre_conferencia_cancelada(self):
  previa=self.previa()
  RodadaContagem.objects.filter(pk=self.pendente.pk).update(ok_em=None)
  with self.assertRaises(ValueError):encerramento.finalizar(self.usuario,self.inv.pk,previa['token'],'sim')
  from django.utils import timezone
  Inventario.objects.filter(pk=self.inv.pk).update(cancelada_em=timezone.now(),cancelada_por=self.usuario)
  from django.core.exceptions import PermissionDenied
  with self.assertRaises(PermissionDenied):encerramento.finalizar(self.usuario,self.inv.pk,previa['token'],'sim')


from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.db import connections
from django.test import TransactionTestCase
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from offline.browser_support import Chrome
from inventario.tests_browser import CHROME


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True,INVENTARIO_PRODUTOS_EXCLUIDOS_PILOTO=())
class EncerramentoConcorrenciaTests(EncerramentoFixture,TransactionTestCase):
 def test_duas_finalizacoes_simultaneas_um_evento_e_nenhum_ajuste_novo(self):
  token=encerramento.previa(self.usuario,self.inv)['token']
  antes=self.estado_preservado();barreira=Barrier(2)
  def executar(_):
   try:
    user=get_user_model().objects.get(pk=self.usuario.pk)
    barreira.wait(timeout=15)
    return encerramento.finalizar(user,self.inv.pk,token,'sim').get('ja_finalizada',False)
   finally:connections.close_all()
  with ThreadPoolExecutor(max_workers=2) as pool:
   self.assertCountEqual(list(pool.map(executar,[0,1])),[False,True])
  self.assertEqual(self.estado_preservado(),antes)
  self.assertEqual(self.inv.eventos.filter(acao='conferencia_finalizada').count(),1)


@skipUnless(Path(CHROME).is_file(),'Chrome indisponivel')
@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True,INVENTARIO_PRODUTOS_EXCLUIDOS_PILOTO=())
class EncerramentoBrowserTests(EncerramentoFixture,StaticLiveServerTestCase):
 def test_confirma_totais_e_volta_automaticamente_a_lista(self):
  self.client.force_login(self.usuario);antes=self.estado_preservado()
  with TemporaryDirectory(prefix='encerramento-browser-') as profile:
   chrome=Chrome(CHROME,profile).start()
   try:
    tab=chrome.tab();tab.call('Network.enable')
    tab.call('Network.setCookie',{'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
    tab.call('Page.navigate',{'url':self.live_server_url+reverse('inventario:detalhe',args=[self.inv.pk])})
    tab.wait('window.inventarioContagemAdminPronta===true')
    self.assertIn('12 produtos com ajuste',tab.evaluate("document.querySelector('#encerramento-administrativo').textContent"))
    self.assertIn('18 produtos',tab.evaluate("document.querySelector('#encerramento-administrativo').textContent"))
    tab.evaluate("window.formFinal=document.querySelector('#encerramento-administrativo form');formFinal.elements.confirmacao.checked=true;formFinal.querySelector('button').click();true")
    tab.wait("location.pathname==="+repr(reverse('inventario:lista')))
    tab.wait("document.body.textContent.includes('Bloqueios e historico preservados')")
    self.assertEqual(antes,self.estado_preservado())
    self.inv.refresh_from_db();self.assertIsNotNone(self.inv.finalizada_em)
   finally:chrome.stop()
