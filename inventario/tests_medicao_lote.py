from estoque.services import estoque_manual_lote
"""Three local measurements reusing the audited synthetic fixture; no artificial latency."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import ctypes
from ctypes import wintypes
from decimal import Decimal as D
import json
import os
import re
from pathlib import Path
from threading import Event, Thread, Barrier
import time
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.conf import settings
from django.db import connection, close_old_connections, transaction, DatabaseError
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from estoque.models import Funcionario, Produto, Venda, ItemVenda, EventoVenda, MovimentacaoEstoqueManual
from estoque.views import _baixar_estoque_produto
from . import services, ajustes, conciliacao_documental, acesso_ajustes
from .models import Inventario, AplicacaoAjuste, ConfirmacaoContagem, PropostaAjuste, EventoAjuste, RodadaContagem, MovimentoObservado
from .tests import produto
from .tests_confirmacao_final import HEADERS


class Memory(ctypes.Structure):
    _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[
        (n,ctypes.c_size_t) for n in ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage',
        'QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage',
        'PeakPagefileUsage','PrivateUsage')]


def rss():
    if os.name != 'nt':
        return None
    memoria=Memory();memoria.cb=ctypes.sizeof(memoria)
    ctypes.windll.kernel32.GetCurrentProcess.restype=wintypes.HANDLE
    ctypes.windll.psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Memory),wintypes.DWORD]
    handle=ctypes.windll.kernel32.GetCurrentProcess()
    if not ctypes.windll.psapi.GetProcessMemoryInfo(handle,ctypes.byref(memoria),memoria.cb):
        return None
    return memoria.WorkingSetSize/1024/1024


def registrar(dado):
    destino_nome=os.environ.get('LOTE_MEDICOES')
    if not destino_nome:
        return
    destino=Path(destino_nome)
    with destino.open('a',encoding='utf-8') as handle:
        handle.write(json.dumps(dado,ensure_ascii=False)+'\n')


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class MedicaoLoteTests(TransactionTestCase):
    def medir(self,nome,acao,latencia=0):
        if latencia:
            raise AssertionError('Esta medição é somente local; latência deve ser estimada.')
        contagem=Counter();tabelas=Counter();total_sql=0.;maior_sql=0.;espera=0.;lock_inicio=None;lock_fim=None
        amostras=[];parar=Event()
        def amostrar():
            while not parar.is_set():
                valor=rss()
                if valor is not None:
                    amostras.append(valor)
                parar.wait(.05)
        def execute(executar,sql,params,many,context):
            nonlocal total_sql,maior_sql,espera,lock_inicio
            contagem[sql.lstrip().split()[0]]+=1
            tabela=re.search(r'(?:FROM|INTO|UPDATE)\s+"?(\w+)',sql,re.I)
            if tabela:
                tabelas[tabela[1]]+=1
            if latencia:
                antes=time.perf_counter();time.sleep(latencia);espera+=time.perf_counter()-antes
            inicio=time.perf_counter()
            try:
                return executar(sql,params,many,context)
            finally:
                t=time.perf_counter()-inicio;total_sql+=t;maior_sql=max(maior_sql,t)
                if 'FOR UPDATE' in sql.upper() and lock_inicio is None:
                    lock_inicio=time.perf_counter()
        commit_original=connection.commit
        def commit():
            nonlocal espera,lock_fim
            contagem['COMMIT']+=1
            if latencia:
                antes=time.perf_counter();time.sleep(latencia);espera+=time.perf_counter()-antes
            try:
                return commit_original()
            finally:
                lock_fim=time.perf_counter()
        sampler=Thread(target=amostrar,daemon=True);sampler.start()
        inicio=time.perf_counter()
        try:
            with connection.execute_wrapper(execute), patch.object(connection,'commit',side_effect=commit), \
                    patch.object(ajustes,'avaliar',wraps=ajustes.avaliar) as avaliar, \
                    patch.object(conciliacao_documental,'conciliar',wraps=conciliacao_documental.conciliar) as conciliar:
                resultado=acao()
        finally:
            duracao=time.perf_counter()-inicio;parar.set();sampler.join(1)
        dado=dict(cenario=self.cenario,etapa=nome,latencia_ms=latencia*1000,
            segundos=round(duracao,4),consultas=sum(contagem.values()),comandos=dict(contagem),
            sql_local_s=round(total_sql,4),sql_local_max_ms=round(maior_sql*1000,3),
            espera_injetada_s=round(espera,4),avaliacoes=avaliar.call_count,documentais=conciliar.call_count,
            tabelas=dict(tabelas),locks_ate_commit_s=round(lock_fim-lock_inicio,4) if lock_inicio and lock_fim else None,
            rss_max_mib=round(max(amostras),2) if amostras else None,
            rss_inicio_mib=round(amostras[0],2) if amostras else None)
        self.medidas.append(dado);registrar(dado)
        return resultado

    def preparar(self,n,a):
        if not os.environ.get('INVENTARIO_CONFIABILIDADE_PG_PORT'):
            self.skipTest('Execute pelo PostgreSQL descartável com identidade verificada.')
        self.assertEqual(connection.settings_dict['HOST'],'127.0.0.1')
        self.assertEqual(str(connection.settings_dict['PORT']),os.environ['INVENTARIO_CONFIABILIDADE_PG_PORT'])
        self.cenario=f'{n}/{a}';self.medidas=[]
        self.usuario=get_user_model().objects.create_superuser('gestor-capacidade','','ficticio')
        self.admin=Client();self.admin.force_login(self.usuario)
        # Synthetic reference #1, preserving the existing protection.
        Inventario.objects.create(titulo='Reserva fictícia',criterio='manual',criado_por=self.usuario)
        Inventario.objects.get_or_create(pk=1,defaults=dict(titulo='Reserva fictícia',criterio='manual',criado_por=self.usuario))
        unidades=[('PCT',6),('CX',12),('DZ',12),('FD',24),('UN',1),('KG',1)]
        self.produtos=[]
        for i in range(n):
            base,fator=unidades[i%len(unidades)]
            dados=dict(nome=f'Capacidade fictícia {i:03}',categoria='CAPACIDADE FICTICIA',quantidade=50,
                unidade_compra=base,unidade_venda_1=base,vende_fracionado=base not in ('UN','KG'),
                unidade_venda_2='UN' if base not in ('UN','KG') else '',fator_conversao=fator)
            p=produto(**dados)
            while p.pk in settings.INVENTARIO_PRODUTOS_EXCLUIDOS_PILOTO:
                # Respect production exclusions: move only this dummy to a
                # different fixture category and allocate another synthetic SKU.
                Produto.objects.filter(pk=p.pk).update(categoria='RESERVA FICTICIA')
                p=produto(**dados)
            self.produtos.append(p)
        resposta=self.medir('criar_categoria',lambda:self.admin.post(reverse('inventario:lista'),
            dict(criterio='categoria',categoria='CAPACIDADE FICTICIA')))
        self.assertEqual(resposta.status_code,302)
        self.inv=Inventario.objects.exclude(pk=1).latest('pk')
        self.assertEqual(self.inv.itens.count(),n)
        self.detalhe=reverse('inventario:detalhe',args=[self.inv.pk])
        self.painel=reverse('inventario:ajustes',args=[self.inv.pk])
        self.medir('abrir_distribuicao',lambda:self.admin.get(self.detalhe))
        rodadas=list(RodadaContagem.objects.filter(item__inventario=self.inv).order_by('item__produto_id'))
        self.assertEqual(len(rodadas),n)
        self.ajustar_ids=[r.item.produto_id for r in rodadas[:a]]
        self.funcionarios=[]
        for grupo in (rodadas[:n//2],rodadas[n//2:]):
            f=Funcionario.objects.create(nome=f'Funcionário fictício {len(self.funcionarios)}')
            self.inv.refresh_from_db()
            r=self.medir('distribuir',lambda:self.admin.post(self.detalhe,dict(acao='acesso',funcionario=f.pk,
                rodadas=[r.pk for r in grupo],horas=24,versao=self.inv.versao,manter_nao_selecionados='sim')))
            self.assertEqual(r.status_code,200)
            token=r.context['link'].rstrip('/').split('/')[-1]
            cliente=Client()
            self.assertEqual(cliente.post(reverse('inventario:convite',args=[token])).status_code,302)
            mobile=self.medir('abrir_mobile',lambda:cliente.get(reverse('inventario:mobile'),HTTP_ACCEPT='application/json'))
            self.assertEqual(len(mobile.json()['produtos']),len(grupo))
            for rodada in grupo:
                quantidade=48 if rodada.item.produto_id in self.ajustar_ids else 50
                avulsos='1' if quantidade==48 and rodada.item.cadastro['fracionado'] else '0'
                url=reverse('inventario:contar',args=[rodada.pk])
                dados=dict(acao='salvar',versao=0,embalagens=str(quantidade),avulsos=avulsos)
                r=self.medir('salvar_rascunho',lambda:cliente.post(url,dados))
                self.assertEqual(r.status_code,200)
                dados.update(acao='ok',versao=r.json()['produto']['versao'])
                r=self.medir('confirmar_ok',lambda:cliente.post(url,dados))
                self.assertEqual(r.status_code,200)
            self.funcionarios.append(cliente)
        # Simple histories (1 sale) and complex histories (3 sales, 12 events each).
        self.vendas=[]
        for i,p in enumerate(self.produtos):
            for _ in range(1 if i%2==0 else 3):
                venda=Venda.objects.create(data_venda=timezone.localdate())
                with transaction.atomic():
                    snap=_baixar_estoque_produto(p.pk,D(1),unidade=p.unidade_compra)
                    item=ItemVenda.objects.create(venda=venda,produto=p,quantidade=1,unidade=p.unidade_compra,
                        preco_unitario=0,valor_total=0,**snap)
                    m=MovimentoObservado.objects.filter(produto=p).latest('sequencia')
                    MovimentoObservado.objects.filter(pk=m.pk).update(referencia=f'Venda #{venda.pk}')
                for _ in range(1 if i%2==0 else 12):
                    EventoVenda.objects.create(venda=venda,tipo_evento='venda_gravada')
                self.vendas.append(item.pk)
        self.medir('receber_admin',lambda:self.admin.get(self.detalhe))
        for cliente in self.funcionarios:
            self.inv.refresh_from_db()
            r=self.medir('envio_funcionario',lambda:cliente.post(reverse('inventario:finalizar'),dict(versao=self.inv.versao)))
            self.assertEqual(r.status_code,200)
        self.inv.refresh_from_db();self.assertIsNotNone(self.inv.finalizada_em)
        self.assertEqual(ConfirmacaoContagem.objects.filter(rodada__item__inventario=self.inv).count(),n)
        self.medir('resultado_finalizado',lambda:self.admin.get(self.detalhe))
        r=self.admin.post(self.detalhe,dict(acao='revisar_ajustes_historicos'))
        self.assertEqual(r.status_code,302)
        self.revisao=r['Location']
        # Deliberate existing administrative access, no parallel confirmation flow.
        historica=parse_qs(urlsplit(self.revisao).query)['revisao_historica'][0]
        pagina=self.medir('previa_ajustes',lambda:self.admin.get(self.painel,
            {'confirmar':'todos','revisao_historica':historica}))
        self.assertEqual(pagina.status_code,200)
        self.assertEqual(pagina.context['resumo']['aptos'],a)
        self.assertTrue(pagina.context['token_aplicacao'])
        self.dados=dict(acao='atualizar',previa=pagina.context['token_aplicacao'],confirmacao='sim',
            revisao_historica=pagina.context['revisao_historica'])
        self.oks=list(ConfirmacaoContagem.objects.order_by('pk').values())
        self.antes=dict(Produto.objects.filter(pk__in=[p.pk for p in self.produtos]).values_list('pk','quantidade'))
        self.esperado={r.item.produto_id:self.antes[r.item.produto_id]+r.contado_base-r.esperado
            for r in RodadaContagem.objects.filter(item__inventario=self.inv).select_related('item')}
        self.vendas_antes=list(ItemVenda.objects.filter(pk__in=self.vendas).order_by('pk').values())

    def conferir(self,a):
        self.assertEqual(AplicacaoAjuste.objects.count(),a)
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(),a)
        self.assertEqual(self.oks,list(ConfirmacaoContagem.objects.order_by('pk').values()))
        self.assertEqual(self.vendas_antes,list(ItemVenda.objects.filter(pk__in=self.vendas).order_by('pk').values()))
        atuais=dict(Produto.objects.filter(pk__in=self.esperado).values_list('pk','quantidade'))
        self.assertEqual(atuais,self.esperado)

    def executar(self,n,a,ms):
        self.preparar(n,a)
        r=self.medir('confirmacao_final',lambda:self.admin.post(self.painel,self.dados,**HEADERS),ms/1000)
        self.assertEqual(r.status_code,200,r.content)
        self.assertTrue(r.json()['ok']);self.assertEqual(len(r.json()['aplicacoes']),a)
        self.conferir(a)
        eventos=EventoAjuste.objects.count()
        repetida=self.medir('confirmacao_repetida',lambda:self.admin.post(self.painel,self.dados,**HEADERS))
        self.assertEqual(sorted(repetida.json()['aplicacoes']),sorted(r.json()['aplicacoes']))
        self.assertEqual(EventoAjuste.objects.count(),eventos);self.conferir(a)
        self.assertEqual(self.medir('resultado_aplicado',lambda:self.admin.get(self.detalhe)).status_code,200)
        self.assertEqual(self.medir('recibo',lambda:self.admin.get(self.painel,
            dict(resultado_atualizacao=self.dados['previa']),**HEADERS)).json()['estado'],'aplicado')
        registrar(dict(cenario=self.cenario,latencia_ms=ms,integridade='APROVADA'))


    def test_40_produtos_10_ajustes(self):
        self.executar(40,10,0)

    def test_50_produtos_25_ajustes(self):
        self.executar(50,25,0)

    def test_70_produtos_70_ajustes(self):
        self.executar(70,70,0)


@override_settings(INVENTARIO_AJUSTES_HABILITADOS=True)
class IntegridadeLote70Tests(TransactionTestCase):
    medir=MedicaoLoteTests.medir
    preparar=MedicaoLoteTests.preparar
    conferir=MedicaoLoteTests.conferir

    def setUp(self):
        self.preparar(70,70)

    def sem_escritas(self):
        self.assertEqual(AplicacaoAjuste.objects.count(),0)
        self.assertEqual(PropostaAjuste.objects.count(),0)
        self.assertEqual(EventoAjuste.objects.count(),0)
        self.assertEqual(MovimentacaoEstoqueManual.objects.count(),0)
        self.assertEqual(dict(Produto.objects.filter(pk__in=self.antes).values_list('pk','quantidade')),self.antes)
        self.assertEqual(self.oks,list(ConfirmacaoContagem.objects.order_by('pk').values()))
        self.assertEqual(self.vendas_antes,list(ItemVenda.objects.filter(pk__in=self.vendas).order_by('pk').values()))

    def test_rollback_no_setuagesimo_sem_sucesso(self):
        original=estoque_manual_lote.conferir_movimento
        chamadas=[]
        def gravar(*args,**kwargs):
            chamadas.append(args[0])
            if len(chamadas)==70:
                raise ValueError('Falha fictícia no produto 70')
            return original(*args,**kwargs)
        with patch.object(estoque_manual_lote,'conferir_movimento',side_effect=gravar):
            r=self.admin.post(self.painel,self.dados,**HEADERS)
        self.assertEqual(r.status_code,400);self.assertFalse(r.json()['ok'])
        self.assertEqual(len(chamadas),70);self.sem_escritas()
        registrar(dict(teste='rollback_70',resultado='APROVADO'))

    def executar_thread(self,acao):
        close_old_connections()
        try:
            return acao()
        finally:
            close_old_connections()

    def test_confirmacoes_concorrentes_aplicam_um_unico_lote(self):
        barreira=Barrier(2)
        def aplicar():
            barreira.wait(15)
            return sorted(str(a.pk) for a in ajustes.atualizar(self.usuario,self.inv.pk,
                self.dados['previa'],confirmacao=True))
        inicio=time.perf_counter()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(self.executar_thread,aplicar);b=pool.submit(self.executar_thread,aplicar)
            self.assertEqual(a.result(180),b.result(180))
        self.conferir(70)
        self.assertEqual(EventoAjuste.objects.count(),210)
        registrar(dict(teste='duas_confirmacoes_70',resultado='APROVADO',segundos=time.perf_counter()-inicio))
