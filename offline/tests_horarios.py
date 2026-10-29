"""Apresentação somente de leitura; nenhuma operação é enviada/processada."""
import base64
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.http import HttpResponse
from django.test import override_settings
from django.urls import include, path
from django.contrib.auth import get_user_model

from .browser_support import Chrome
from .models import OperacaoSincronizacao

OUT = Path(os.environ['HORARIOS_OFFLINE_ARTIFACTS'])
urlpatterns = [path('offline/', lambda request: HttpResponse('''<!doctype html><html><head>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>
body{font:16px system-ui;color:#172033;margin:0}main{max-width:900px;padding:16px;margin:auto}
article{padding:12px;border:1px solid #ccc;margin:12px 0;overflow-wrap:anywhere}dd{margin-left:0}
</style></head><body><main><h1>Horários de sincronização — dados fictícios</h1><div id="samples"></div></main></body></html>''')),
    path('', include('offline.urls'))]


@override_settings(ROOT_URLCONF=__name__)
class OfflineTimesBrowserTests(StaticLiveServerTestCase):
    def test_readonly_times_desktop_mobile_legacy_and_clock_skew(self):
        before = list(OperacaoSincronizacao.objects.values())
        with TemporaryDirectory(prefix='offline-horarios-chrome-') as profile:
            chrome = Chrome(r'C:\Program Files\Google\Chrome\Application\chrome.exe', profile).start()
            try:
                tab = chrome.tab()
                tab.call('Emulation.setTimezoneOverride', {'timezoneId':'Asia/Tokyo'})
                tab.call('Page.navigate', {'url':self.live_server_url + '/offline/'})
                tab.wait('document.readyState === "complete"')
                result = tab.evaluate('''(async()=>{
                    const p=await import('/offline/assets/2-8f/presentation.js');
                    window.p=p;
                    const checks=[
                        [null,'Horário não registrado'],[undefined,'Horário não registrado'],['','Horário não registrado'],
                        ['2026-10-25','Horário não registrado'],['2026-10-25T01:02:03','Horário não registrado'],
                        ['2026-02-30T01:02:03Z','Horário não registrado'],['<img src=x onerror=alert(1)>','Horário não registrado'],
                        ['2026-10-25T01:02:03Z','24/10/2026 às 22:02:03'],
                        ['2026-10-25T04:02:03+03:00','24/10/2026 às 22:02:03'],
                        [Date.parse('2026-10-25T01:02:03Z'),'24/10/2026 às 22:02:03']];
                    for(const [value,expected] of checks)if(p.formatOfflineTime(value)!==expected)throw Error(String(value));
                    const {openDB,Repository,indicatorState,Stability}=await import('/offline/assets/2-8f/core.js');
                    const stability=new Stability();stability.success(100,100);stability.connection={kind:'online',checked_at:100};
                    for(const status of ['pendente','erro','resultado_desconhecido','conflito']){
                        const state=indicatorState({operations:[{status}],stability,now:100,wall:100,authenticated:true});
                        if(state.connectionKind!=='online'||!state.requiresAction)throw Error('Indicador alterado: '+status);
                    }
                    window.repo=new Repository(await openDB());
                    const {operationDetails}=await import('/offline/assets/2-8g-close/operation-details.js');
                    const scope={actor_id:'1',environment_id:'fixture'};
                    window.samples=[];window.posts=[];
                    const fetchOriginal=window.fetch;
                    window.fetch=(url,options={})=>{if(options.method&&options.method!=='GET'){posts.push(String(url));throw Error('Envio proibido')}return fetchOriginal(url,options)};
                    for(const [index,status] of ['pendente','enviando','conflito','erro','resultado_desconhecido','confirmada'].entries()){
                        const operation={operation_id:crypto.randomUUID(),device_id:crypto.randomUUID(),...scope,
                            status,type:'observacao_operacional',aggregate_id:'1',sequence:index+1,attempts:3,
                            created_at:'2026-10-24T18:04:05Z',payload:{observacao:'Observação preservada'},
                            payload_hash:'hash-original',last_error:status==='erro'?'Erro preservado':'',
                            server_result:index>1?{status,completed_at:'2026-10-25T01:02:03Z'}:null};
                        await repo.put('operations',operation);samples.push(operation);
                        const row=document.createElement('article');row.append(status,p.operationTimes(operation));
                        const detail=operationDetails(operation,scope);if(detail){detail.open=true;row.append(detail)}
                        document.querySelector('#samples').append(row);
                    }
                    const legacy={status:'pendente',payload:{}};
                    document.querySelector('#samples').append(p.operationTimes(legacy));
                    const historical={created_at:'2026-10-24T18:04:05Z',server_result:{completed_at:'2026-10-25T01:02:03Z'},synchronized_at:'2026-10-25T01:03:04Z'};
                    document.querySelector('#samples').append(p.operationTimes(historical));
                    const catalog=document.createElement('p');p.timeText(catalog,p.catalogTimes({prepared_at:'2026-10-24T18:04:05Z',gerado_em:'2026-10-25T01:02:03Z'}));document.querySelector('#samples').append(catalog);
                    const conflict={...samples[2],type:'criar_venda',payload_hash:'a'.repeat(64)};
                    conflict.server_result={status:'conflito',operation_id:conflict.operation_id,hash:conflict.payload_hash,completed_at:'2026-10-25T01:02:03Z',record_id:null,erro:'Conflito sintético'};
                    const closure={status:'encerrada_sem_venda',closure_id:crypto.randomUUID(),operation_id:conflict.operation_id,
                        device_id:conflict.device_id,original_device_id:conflict.device_id,actor_id:scope.actor_id,environment_id:scope.environment_id,hash:conflict.payload_hash,
                        motivo:'Encerramento sintético',record_id:null,closed_at:'2026-10-25T01:03:04Z',original_receipt:conflict.server_result};
                    const closed=operationDetails(conflict,scope,null,closure);closed.open=true;document.querySelector('#samples').append(closed);
                    if(!closed.textContent.includes('Encerramento registrado no servidor: 24/10/2026 às 22:03:04'))throw Error('Horário administrativo ausente');
                    window.originalOperations=JSON.stringify(await repo.all('operations'));
                    return true;
                })()''')
                self.assertTrue(result)
                for width in (1366, 390, 320):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width':width,'height':900,'deviceScaleFactor':1,'mobile':width<768})
                    text = tab.evaluate('document.body.innerText')
                    for part in ['Criação informada pelo dispositivo: 24/10/2026 às 15:04:05',
                                 'Conclusão do processamento no servidor (resultado recebido): 24/10/2026 às 22:02:03',
                                 'Confirmação salva neste dispositivo: 24/10/2026 às 22:03:04',
                                 'Horário não registrado', 'horário individual não registrado', 'Erro preservado']:
                        self.assertIn(part, text)
                    self.assertLessEqual(tab.evaluate('document.documentElement.scrollWidth'), width)
                    self.assertTrue(tab.evaluate("[...document.querySelectorAll('.offline-timestamp')].every(e=>getComputedStyle(e).fontWeight==='700'&&getComputedStyle(e).color==='rgb(30, 41, 59)')"))
                    image = tab.call('Page.captureScreenshot', {'format':'png','captureBeyondViewport':True})
                    (OUT / f'horarios-offline-{width}.png').write_bytes(base64.b64decode(image['data']))
                self.assertTrue(tab.evaluate('originalOperations===JSON.stringify(await repo.all("operations"))'))
                syntax = tab.evaluate('''(async()=>{
                    const errors=[];
                    for(const name of ['commercial-ui.js','sales.js','sales-draft-ui.js','sales-revision-ui.js','checklist.js'])
                        import('/offline/assets/2-8g-close/'+name).catch(error=>{if(error instanceof SyntaxError)errors.push(String(error))});
                    await new Promise(resolve=>setTimeout(resolve,150));return errors;
                })()''')
                self.assertEqual(syntax, [])
                self.assertTrue(tab.evaluate('originalOperations===JSON.stringify(await repo.all("operations"))'))
                self.assertEqual(tab.evaluate('posts'), [])
                self.assertEqual(before, list(OperacaoSincronizacao.objects.values()))
                (OUT / 'readonly.json').write_text(json.dumps({'operations_unchanged':True,'posts':[],
                    'server_operation_count':len(before),'viewports':[1366,390,320],'browser_timezone':'Asia/Tokyo'}), encoding='utf8')
            finally:
                chrome.stop()


class OfflinePanelReadOnlyBrowserTests(StaticLiveServerTestCase):
    def test_real_panel_keeps_operations_history_and_indicator(self):
        user = get_user_model().objects.create_superuser('offline-horarios', 'teste@example.test', 'teste')
        self.client.force_login(user)
        with TemporaryDirectory(prefix='offline-horarios-painel-') as profile:
            chrome = Chrome(r'C:\Program Files\Google\Chrome\Application\chrome.exe', profile).start()
            try:
                tab = chrome.tab()
                tab.call('Network.enable')
                tab.call('Network.setCookie', {'name':'sessionid','value':self.client.cookies['sessionid'].value,'url':self.live_server_url})
                tab.call('Emulation.setTimezoneOverride', {'timezoneId':'Asia/Tokyo'})
                tab.call('Page.navigate', {'url':self.live_server_url + '/offline/'})
                tab.wait('document.readyState === "complete"')
                tab.evaluate('''(async()=>{
                    window.app=await import('/offline/assets/2-8g-close/app.js');await app.initialized;
                    const {Repository,openDB}=await import('/offline/assets/2-8f/core.js');window.repo=new Repository(await openDB());
                    window.posts=[];const realFetch=fetch;window.fetch=(url,options={})=>{
                        if(options.method&&options.method!=='GET'){posts.push(String(url));throw Error('Envio proibido')}
                        return realFetch(url,options)};
                    const response=await fetch('/api/offline/session/');const session=await response.json();
                    const environment=(await (await fetch('/api/offline/health/')).json()).environment;
                    const snapshot={key:'pilot',actor:{id:session.actor_id,name:'Usuário fictício'},environment_id:environment,
                        prepared_at:'2026-10-24T18:04:05Z',tasks:[]};await repo.put('snapshots',snapshot);
                    for(const [index,status] of ['pendente','erro','resultado_desconhecido','conflito','confirmada'].entries()){
                        const op={operation_id:crypto.randomUUID(),device_id:crypto.randomUUID(),actor_id:session.actor_id,
                            environment_id:environment,type:'observacao_operacional',aggregate_id:'1',sequence:index+1,
                            status,attempts:2,created_at:'2026-10-24T18:04:05Z',payload:{observacao:'Mensagem preservada'},
                            server_result:index>2?{status,completed_at:'2026-10-25T01:02:03Z'}:null};
                        await repo.put('operations',op);
                        if(status==='confirmada')await repo.put('history',{...op,synchronized_at:'2026-10-25T01:03:04Z'});
                    }
                    window.original=JSON.stringify([await repo.all('operations'),await repo.all('history')]);
                    const channel=new BroadcastChannel('offline-pilot');channel.postMessage({type:'changed'});channel.close();return true;
                })()''')
                tab.wait('document.querySelectorAll("#offline-operations li").length === 5')
                for width in (1366, 390, 320):
                    tab.call('Emulation.setDeviceMetricsOverride', {'width':width,'height':900,'deviceScaleFactor':1,'mobile':width<768})
                    text = tab.evaluate('document.getElementById("offline-operations").innerText')
                    self.assertIn('24/10/2026 às 22:03:04', text)
                    self.assertIn('Mensagem preservada', text)
                    self.assertTrue(tab.evaluate('document.getElementById("offline-sync").disabled'))
                    self.assertLessEqual(tab.evaluate('document.documentElement.scrollWidth'), width)
                    image = tab.call('Page.captureScreenshot', {'format':'png','captureBeyondViewport':True})
                    (OUT / f'painel-offline-{width}.png').write_bytes(base64.b64decode(image['data']))
                tab.evaluate('await app.probe();true')
                self.assertTrue(tab.evaluate('original===JSON.stringify([await repo.all("operations"),await repo.all("history")])'))
                self.assertEqual(tab.evaluate('posts'), [])
                self.assertEqual(OperacaoSincronizacao.objects.count(), 0)
            finally:
                chrome.stop()
