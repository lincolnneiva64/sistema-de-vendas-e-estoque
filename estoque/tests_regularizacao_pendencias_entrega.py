"""Regularização administrativa em fixtures, sem alterar fatos físicos/comerciais."""
import copy
import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import connection, connections, transaction, DatabaseError
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from estoque import views
from estoque.models import (Produto, Venda, ItemVenda, ItemVendaRemovido, EntregaRota,
    EntregaRotaItem, EntregaChecklistItem, ContaReceber, ContaPagar, MovimentoFinanceiro, EventoVenda)
from estoque.services import regularizacao_pendencias_entrega as admin
from estoque.tests_pendencias_entrega_500 import PendenciasEntregaFixtures

MOTIVO = 'Ocorrência resolvida operacionalmente; regularização administrativa autorizada.'


class RegularizacaoFixtures(PendenciasEntregaFixtures):
    def dados_preservados(self):
        return [list(m.objects.order_by('pk').values()) for m in (Produto, Venda, ItemVenda,
            ItemVendaRemovido, EntregaRota, EntregaRotaItem, EntregaChecklistItem,
            ContaReceber, ContaPagar, MovimentoFinanceiro)]

    def aplicar(self, manifesto=None):
        m = manifesto or admin.consultar()
        return admin.regularizar(m, usuario=self.user, justificativa=MOTIVO,
            executar=True, confirmacao=m['hash'])

    def desfazer(self, ids):
        previa = admin.reverter(ids, usuario=self.user, justificativa='Reversão autorizada para teste.')
        return admin.reverter(ids, usuario=self.user, justificativa='Reversão autorizada para teste.',
            executar=True, confirmacao=previa['confirmacao'])


class RegularizacaoPendenciasTests(RegularizacaoFixtures, TestCase):
    def test_consulta_e_simulacao_so_leem(self):
        self.criar()
        antes = self.dados_preservados()
        eventos = list(EventoVenda.objects.values())
        with CaptureQueriesContext(connection) as queries:
            m = admin.consultar()
            resultado = admin.regularizar(m, usuario=self.user, justificativa=MOTIVO)
        self.assertFalse(resultado['executado'])
        self.assertEqual(m['total'], 1)
        self.assertEqual(m['ocorrencias'][0]['chave']['tipo'], 'item')
        self.assertEqual(antes, self.dados_preservados())
        self.assertEqual(eventos, list(EventoVenda.objects.values()))
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for q in queries))

    def test_item_regularizado_preserva_tudo_exceto_novo_evento(self):
        bloco, checklist = self.criar()
        ContaReceber.objects.create(venda=bloco.venda, cliente=self.cliente,
            data_emissao=bloco.venda.data_venda, valor_original=10, valor_em_aberto=10, status='aberta')
        antes = self.dados_preservados()
        resultado = self.aplicar()
        self.assertEqual(antes, self.dados_preservados())
        self.assertEqual(views.listar_pendencias_entrega(), [])
        self.assertEqual(views.pendencias_sugeriveis_entrega(), [])
        self.assertEqual(views.pendencias_checklist_validas([checklist.pk]), [])
        evento = EventoVenda.objects.get(tipo_evento=admin.REGULARIZADA)
        self.assertEqual(resultado['ocorrencias'][0]['evento_id'], evento.pk)
        dados = json.loads(evento.descricao)
        self.assertEqual(dados['usuario_id'], self.user.pk)
        self.assertEqual(dados['ocorrencia']['chave']['checklist_id'], checklist.pk)
        self.assertIn(MOTIVO, evento.descricao_exibicao)
        self.assertNotIn('"schema"', evento.descricao_exibicao)

    def test_blocos_sem_item_parcial_e_cancelada(self):
        for status in ('parcial','cancelada'):
            self.criar(status=status, sem_checklist=True)
        antes = self.dados_preservados()
        m = admin.consultar()
        self.assertEqual(m['total'],2)
        self.assertTrue(all(r['chave']['tipo']=='bloco' for r in m['ocorrencias']))
        self.aplicar(m)
        self.assertEqual(views.listar_pendencias_entrega(), [])
        self.assertEqual(antes,self.dados_preservados())

    def test_lote_35_seleciona_so_ocorrencias_explicitas(self):
        for _ in range(35):
            self.criar()
        m = admin.consultar()
        nova, _ = self.criar()
        antes = self.dados_preservados()
        self.aplicar(m)
        restantes = views.listar_pendencias_entrega()
        self.assertEqual([p['item_rota_id'] for p in restantes],[nova.pk])
        self.assertEqual(antes,self.dados_preservados())
        self.assertEqual(EventoVenda.objects.filter(tipo_evento=admin.REGULARIZADA).count(),35)

    def test_repeticao_mesmo_ou_novo_lote_nao_duplica(self):
        self.criar()
        m = admin.consultar()
        outro = admin.consultar()
        self.aplicar(m)
        self.aplicar(m)
        self.aplicar(outro)
        self.assertEqual(EventoVenda.objects.filter(tipo_evento=admin.REGULARIZADA).count(),1)

    def test_reversao_repetida_e_manifesto_antigo_nao_desfaz_reversao(self):
        self.criar()
        m = admin.consultar()
        ids = [r['evento_id'] for r in self.aplicar(m)['ocorrencias']]
        antes = self.dados_preservados()
        self.desfazer(ids)
        self.desfazer(ids)
        self.assertEqual(len(views.listar_pendencias_entrega()),1)
        self.assertEqual(EventoVenda.objects.filter(tipo_evento=admin.REVERTIDA).count(),1)
        with self.assertRaises(ValidationError):
            self.aplicar(m)
        self.aplicar(admin.consultar())
        self.desfazer(ids)  # Repetir a reversão antiga não desfaz uma nova aprovação.
        self.assertEqual(views.listar_pendencias_entrega(),[])
        self.assertEqual(antes,self.dados_preservados())

    def test_nova_pendencia_mesma_venda_nao_e_ocultada(self):
        bloco, checklist = self.criar()
        self.aplicar()
        rota = EntregaRota.objects.create(data=bloco.rota.data,tipo='unitaria')
        nova = EntregaRotaItem.objects.create(rota=rota,venda=bloco.venda,status='parcial')
        c = EntregaChecklistItem.objects.create(rota_item=nova,item_venda=checklist.item_venda,entregue=False)
        self.assertEqual([p['id'] for p in views.listar_pendencias_entrega()],[c.pk])

    def test_novo_estado_do_mesmo_checklist_exige_nova_aprovacao(self):
        bloco, checklist = self.criar()
        m = admin.consultar()
        self.aplicar(m)
        checklist.carregado = False
        checklist.save(update_fields=['carregado','atualizado_em'])
        self.assertEqual(len(views.listar_pendencias_entrega()),1)
        with self.assertRaises(ValidationError):
            self.aplicar(m)

    def test_mudancas_secundarias_nao_reabrem_item_ou_bloco(self):
        for sem_checklist in (False, True):
            with self.subTest(bloco=sem_checklist):
                bloco, checklist = self.criar(sem_checklist=sem_checklist)
                m = admin.consultar()
                self.aplicar(m)
                bloco.observacao = 'Observacao administrativa complementar'
                bloco.status = 'cancelada' if sem_checklist else 'pendente'
                bloco.conferido_cliente = True
                bloco.save()
                if checklist:
                    checklist.save()  # Atualiza somente o timestamp.
                self.assertEqual(views.listar_pendencias_entrega(), [])
                self.assertEqual(views.pendencias_sugeriveis_entrega(), [])
                self.assertEqual(admin.consultar()['total'], 0)
                with self.assertRaises(ValidationError):
                    self.aplicar(m)  # Versao continua protegendo consulta -> execucao.
                self.assertEqual(EventoVenda.objects.filter(tipo_evento=admin.REGULARIZADA).count(),
                                 2 if sem_checklist else 1)

    def test_mudanca_secundaria_antes_da_execucao_rejeita_manifesto(self):
        bloco, _ = self.criar()
        m = admin.consultar()
        bloco.observacao = 'Alterada depois da consulta'
        bloco.save(update_fields=['observacao'])
        with self.assertRaises(ValidationError):
            self.aplicar(m)
        self.assertFalse(EventoVenda.objects.exists())

    def test_cancelamento_do_bloco_nao_reabre_itens_regularizados(self):
        bloco, checklist = self.criar()
        ids = [r['evento_id'] for r in self.aplicar()['ocorrencias']]
        bloco.status = 'cancelada'
        bloco.save(update_fields=['status'])
        self.assertEqual(views.listar_pendencias_entrega(), [])
        self.assertEqual(admin.consultar()['total'], 0)
        self.desfazer(ids)
        self.assertEqual(len(views.listar_pendencias_entrega()), 1)

    def test_bloco_cancelado_nao_oculta_novo_item_nao_regularizado(self):
        bloco, checklist = self.criar()
        self.aplicar()
        linha = ItemVenda.objects.create(venda=bloco.venda, produto=checklist.item_venda.produto,
            quantidade=2, unidade='UN', preco_unitario=10, valor_total=20)
        EntregaChecklistItem.objects.create(rota_item=bloco, item_venda=linha)
        bloco.status = 'cancelada'
        bloco.save(update_fields=['status'])
        self.assertEqual(len(views.listar_pendencias_entrega()), 1)
        self.assertEqual(admin.consultar()['total'], 1)

    def test_ids_reutilizados_com_nova_criacao_nao_herdam_regularizacao(self):
        from datetime import timedelta
        bloco, checklist = self.criar()
        self.aplicar()
        EntregaChecklistItem.objects.filter(pk=checklist.pk).update(
            criado_em=checklist.criado_em + timedelta(seconds=1))
        self.assertEqual([p['id'] for p in views.listar_pendencias_entrega()], [checklist.pk])

    def test_novo_item_em_bloco_regularizado_nao_e_ocultado(self):
        bloco, _ = self.criar(sem_checklist=True)
        self.aplicar()
        linha = bloco.venda.itens.get()
        c = EntregaChecklistItem.objects.create(rota_item=bloco,item_venda=linha)
        self.assertEqual([p['id'] for p in views.listar_pendencias_entrega()],[c.pk])

    def test_contador_e_resolvidas_get_sem_escrita(self):
        self.criar()
        self.aplicar()
        antes = self.dados_preservados()
        eventos = list(EventoVenda.objects.values())
        with CaptureQueriesContext(connection) as queries:
            r = self.client.get(reverse('estoque:pendencias_entrega'))
            self.assertContains(r,'0 registro(s)')
            r = self.client.get(reverse('estoque:pendencias_entrega'),{'status':'resolvidas'})
            self.assertContains(r,'Regularizada administrativamente')
            self.assertContains(r,'origem=regularizacao_administrativa')
            self.assertNotContains(r,'origem=pendencias_resolvidas')
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for q in queries))
        self.assertEqual(antes,self.dados_preservados())
        self.assertEqual(eventos,list(EventoVenda.objects.values()))
        self.assertEqual(admin.consultar()['total'],0)

    def test_limite_aplicado_depois_de_filtrar(self):
        self.criar()
        self.criar()
        m = admin.consultar()
        selecao = admin.consultar([m['ocorrencias'][0]['identificador']])
        self.aplicar(selecao)
        self.assertEqual(len(views.listar_pendencias_entrega(limite=1)),1)

    def test_manifesto_alterado_hash_errado_ou_outro_banco_rejeitados(self):
        self.criar()
        m = admin.consultar()
        for campo,valor in (('total',999),('banco','outro'),('hash','0'*64)):
            alterado = copy.deepcopy(m)
            alterado[campo]=valor
            with self.assertRaises(ValidationError):
                self.aplicar(alterado)
        with self.assertRaises(ValidationError):
            admin.regularizar(m,usuario=self.user,justificativa=MOTIVO,executar=True,confirmacao='0'*64)
        self.assertFalse(EventoVenda.objects.exists())

    def test_lote_divergente_nao_registra_nenhuma_ocorrencia(self):
        self.criar()
        _, c = self.criar()
        m = admin.consultar()
        c.entregue=True
        c.save()
        with self.assertRaises(ValidationError):
            self.aplicar(m)
        self.assertFalse(EventoVenda.objects.exists())

    def test_erro_na_gravacao_do_lote_faz_rollback_integral(self):
        self.criar()
        self.criar()
        m = admin.consultar()
        criar = EventoVenda.objects.create
        chamadas = []
        def falhar(**kwargs):
            chamadas.append(kwargs)
            if len(chamadas)==2:
                raise RuntimeError('Falha fictícia')
            return criar(**kwargs)
        with patch.object(EventoVenda.objects,'create',side_effect=falhar), self.assertRaises(RuntimeError):
            self.aplicar(m)
        self.assertFalse(EventoVenda.objects.exists())

    def test_permissao_e_justificativa_obrigatorias(self):
        self.criar()
        comum = get_user_model().objects.create_user('funcionario-ficticio',password='teste')
        m = admin.consultar()
        with self.assertRaises(PermissionDenied):
            admin.regularizar(m,usuario=comum,justificativa=MOTIVO)
        with self.assertRaises(ValidationError):
            admin.regularizar(m,usuario=self.user,justificativa='')
        with self.assertRaises(ValidationError):
            admin.regularizar(m,usuario=self.user,justificativa=MOTIVO,executar=True)

    def test_id_antigo_nao_cria_reentrega(self):
        bloco, c = self.criar()
        self.aplicar()
        antes = self.dados_preservados()
        r = self.client.post(reverse('estoque:entregas_dia'),{'acao':'rota', 'data':bloco.rota.data.isoformat(), 'pendencias_rota':[str(c.pk)]})
        self.assertEqual(r.status_code,302)
        self.assertEqual(antes,self.dados_preservados())
        self.assertFalse(views.pendencias_checklist_validas([c.pk]))

    def test_consulta_ignora_ids_desconhecidos_sem_supor_contagem(self):
        self.criar()
        with self.assertRaises(ValidationError):
            admin.consultar(['item:999:999:999:999:999'])

    def test_evento_incompleto_nao_oculta_nem_quebra_historico(self):
        bloco,_ = self.criar()
        for descricao in ('inválido','{}','{"schema":1,"ocorrencia":{}}'):
            EventoVenda.objects.create(venda=bloco.venda,tipo_evento=admin.REGULARIZADA,descricao=descricao)
        ocorrencia = admin.consultar()['ocorrencias'][0]
        ocorrencia['produto'] = None
        EventoVenda.objects.create(venda=bloco.venda, tipo_evento=admin.REGULARIZADA,
            descricao=admin._json({'schema': 1, 'ocorrencia': ocorrencia, 'justificativa': 'Teste'}))
        self.assertEqual(len(views.listar_pendencias_entrega()),1)
        self.assertEqual(admin.listar_regularizadas(),[])


class RegularizacaoComandosTests(RegularizacaoFixtures, TransactionTestCase):
    def test_consulta_bloqueia_ate_escrita_acidental(self):
        bloco, _ = self.criar()
        def escrever(*args, **kwargs):
            EventoVenda.objects.create(venda=bloco.venda,tipo_evento='nao_pode_gravar',descricao='fixture')
        with patch('estoque.management.commands.consultar_pendencias_entrega_admin.consultar',side_effect=escrever):
            with self.assertRaises(DatabaseError):
                call_command('consultar_pendencias_entrega_admin',stdout=StringIO())
        self.assertFalse(EventoVenda.objects.exists())

    def test_consulta_protegida_e_comandos_exigem_confirmacao(self):
        self.criar(status='cancelada')
        antes = self.dados_preservados()
        saida = StringIO()
        with CaptureQueriesContext(connection) as queries:
            call_command('consultar_pendencias_entrega_admin',stdout=saida)
        m = json.loads(saida.getvalue())
        self.assertEqual(m['total'],1)
        self.assertEqual(m['ocorrencias'][0]['chave']['tipo'],'bloco')
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for q in queries))
        with TemporaryDirectory(prefix='manifesto-ficticio-') as pasta:
            caminho = Path(pasta)/'manifesto.json'
            caminho.write_text(json.dumps(m),encoding='utf-8')
            saida = StringIO()
            call_command('regularizar_pendencias_entrega_admin',str(caminho),usuario=self.user.pk,justificativa=MOTIVO,stdout=saida)
            self.assertFalse(json.loads(saida.getvalue())['executado'])
            self.assertFalse(EventoVenda.objects.exists())
            call_command('regularizar_pendencias_entrega_admin',str(caminho),usuario=self.user.pk,justificativa=MOTIVO,
                executar=True,confirmar_hash=m['hash'],stdout=StringIO())
        evento = EventoVenda.objects.get(tipo_evento=admin.REGULARIZADA)
        saida = StringIO()
        call_command('reverter_pendencias_entrega_admin',str(evento.pk),usuario=self.user.pk,justificativa=MOTIVO,stdout=saida)
        confirmacao = json.loads(saida.getvalue())['confirmacao']
        self.assertFalse(EventoVenda.objects.filter(tipo_evento=admin.REVERTIDA).exists())
        call_command('reverter_pendencias_entrega_admin',str(evento.pk),usuario=self.user.pk,justificativa=MOTIVO,
            executar=True,confirmar_hash=confirmacao,stdout=StringIO())
        self.assertEqual(len(views.listar_pendencias_entrega()),1)
        self.assertEqual(antes,self.dados_preservados())


class RegularizacaoConcorrenciaTests(RegularizacaoFixtures, TransactionTestCase):
    def setUp(self):
        super().setUp()
        if connection.vendor != 'postgresql':
            self.skipTest('Bloqueios concorrentes verificados em PostgreSQL descartável.')

    def _regularizacao_contra_reversao(self, reversao_primeiro):
        self.criar()
        m = admin.consultar()
        ids = [r['evento_id'] for r in self.aplicar(m)['ocorrencias']]
        antes = self.dados_preservados()
        bloqueado, liberar, segunda_iniciada = Event(), Event(), Event()
        bloquear = admin._bloquear

        def segurar(registros):
            bloquear(registros)
            if not bloqueado.is_set():
                bloqueado.set()
                if not liberar.wait(timeout=10):
                    raise RuntimeError('Teste concorrente nao liberado')

        def executar(reversao, segunda=False):
            try:
                if segunda:
                    segunda_iniciada.set()
                if reversao:
                    return self.desfazer(ids)
                try:
                    self.aplicar(m)
                    return 'ja regularizada'
                except ValidationError:
                    return 'manifesto revertido'
            finally:
                connections.close_all()

        with patch.object(admin, '_bloquear', side_effect=segurar), ThreadPoolExecutor(max_workers=2) as pool:
            primeira = pool.submit(executar, reversao_primeiro)
            self.assertTrue(bloqueado.wait(timeout=10))
            segunda = pool.submit(executar, not reversao_primeiro, True)
            try:
                self.assertTrue(segunda_iniciada.wait(timeout=10))
                from concurrent.futures import TimeoutError
                with self.assertRaises(TimeoutError):
                    segunda.result(timeout=0.2)
            finally:
                liberar.set()
            r1, r2 = primeira.result(timeout=20), segunda.result(timeout=20)
        self.assertEqual(r2 if reversao_primeiro else r1,
                         'manifesto revertido' if reversao_primeiro else 'ja regularizada')
        self.assertEqual(EventoVenda.objects.filter(tipo_evento=admin.REGULARIZADA).count(), 1)
        self.assertEqual(EventoVenda.objects.filter(tipo_evento=admin.REVERTIDA).count(), 1)
        self.assertEqual(len(views.listar_pendencias_entrega()), 1)
        self.assertEqual(antes, self.dados_preservados())

    def test_regularizacao_concorrente_com_reversao_regularizacao_primeiro(self):
        self._regularizacao_contra_reversao(False)

    def test_regularizacao_concorrente_com_reversao_reversao_primeiro(self):
        self._regularizacao_contra_reversao(True)

    def test_isolamento_incompativel_rejeitado_sem_eventos(self):
        self.criar()
        m = admin.consultar()
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            with self.assertRaises(ValidationError):
                self.aplicar(m)
        self.assertFalse(EventoVenda.objects.exists())

    def test_duas_regularizacoes_concorrentes_criam_um_evento(self):
        self.criar()
        m = admin.consultar()
        barreira = Barrier(2)
        def executar():
            try:
                barreira.wait(timeout=10)
                return self.aplicar(m)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            resultados = [pool.submit(executar) for _ in range(2)]
            for r in resultados:
                r.result(timeout=20)
        self.assertEqual(EventoVenda.objects.filter(tipo_evento=admin.REGULARIZADA).count(),1)

    def test_duas_reversoes_concorrentes_criam_um_evento(self):
        self.criar()
        ids = [r['evento_id'] for r in self.aplicar()['ocorrencias']]
        barreira = Barrier(2)
        def executar():
            try:
                barreira.wait(timeout=10)
                self.desfazer(ids)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            resultados = [pool.submit(executar) for _ in range(2)]
            for r in resultados:
                r.result(timeout=20)
        self.assertEqual(EventoVenda.objects.filter(tipo_evento=admin.REVERTIDA).count(),1)

    def test_reprogramacao_revalida_apos_regularizacao_concorrente(self):
        _, c = self.criar()
        m = admin.consultar()
        gravando, liberar = Event(), Event()
        criar = EventoVenda.objects.create
        def aguardar(**kwargs):
            gravando.set()
            if not liberar.wait(timeout=10):
                raise RuntimeError('Teste concorrente não liberado')
            return criar(**kwargs)
        def regularizar():
            try:
                return self.aplicar(m)
            finally:
                connections.close_all()
        def validar():
            try:
                with transaction.atomic():
                    return admin.validar_reprogramacao([c.pk])
            finally:
                connections.close_all()
        with patch.object(EventoVenda.objects,'create',side_effect=aguardar), ThreadPoolExecutor(max_workers=2) as pool:
            primeiro = pool.submit(regularizar)
            self.assertTrue(gravando.wait(timeout=10))
            segundo = pool.submit(validar)
            liberar.set()
            primeiro.result(timeout=20)
            self.assertFalse(segundo.result(timeout=20))
