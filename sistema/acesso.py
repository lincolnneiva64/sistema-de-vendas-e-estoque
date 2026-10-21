"""Deny-by-default server policy; no business or offline writes here."""
from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse, HttpResponseForbidden
from django.urls import reverse
from django.utils.cache import patch_cache_control, patch_vary_headers

# These endpoints enforce their own actor, permission or capability checks.
PROTECAO_PROPRIA = {
    'inventario.views': {'lista', 'detalhe', 'relatorio', 'historico_produto', 'movimento_manual',
        'convite', 'mobile', 'contar', 'sinalizar', 'finalizar', 'previa_conversao'},
    'inventario.ajustes_views': {'painel'},
    'locacoes.pickup_links': {'individual', 'send', 'manage'},
    'estoque.autorizacao_estoque': {'autorizar', 'autorizada'},
    'estoque.views': {'compras_lista_fornecedor_conferencia_externa',
        'conferencia_estoque_confirmar', 'conferencia_estoque_corrigir', 'conferencia_estoque_produto'},
    'offline.views': {'snapshot', 'commercial_snapshot', 'synchronize', 'operation_result',
        'close_sale_conflict', 'sale_closure_result'},
}
PUBLICAS = {'shell', 'sales_shell', 'revision_shell', 'versioned_asset', 'health', 'session', 'service_worker'}

# Unlisted commercial actions are restricted to active superusers. Never infer
# authorization from is_staff, the URL prefix, selected employee or hidden UI.
LEITURA = {
    'home': ('estoque.view_produto',), 'produto_detalhe': ('estoque.view_produto',),
    'verificar_produto': ('estoque.view_produto',),
    'clientes': ('estoque.view_cliente',), 'clientes_consulta': ('estoque.view_cliente',),
    'clientes_autocomplete': ('estoque.view_cliente',),
    'vendas': ('estoque.view_venda', 'estoque.view_cliente', 'estoque.view_produto'),
    'consultar_vendas': ('estoque.view_venda',), 'venda_detalhe': ('estoque.view_venda',),
    'venda_editar_revisao': ('estoque.change_venda', 'estoque.view_cliente', 'estoque.view_produto'),
    'contas_receber': ('estoque.view_contareceber',),
    'receber_cliente_escolher': ('estoque.view_contareceber', 'estoque.view_cliente'),
    'receber_cliente': ('estoque.view_contareceber', 'estoque.view_cliente'),
    'conta_receber_receber': ('estoque.view_contareceber',),
    'caixa_banco': ('estoque.view_movimentofinanceiro',),
    'painel_financeiro': ('estoque.view_movimentofinanceiro',),
    'despesas_diarias': ('estoque.view_despesadiaria',),
    'compras_lista': ('estoque.view_compra',), 'compras_detalhe': ('estoque.view_compra',),
    'compras_nova': ('estoque.add_compra',), 'fornecedores': ('estoque.view_fornecedor',),
    'contas_pagar': ('estoque.view_contapagar',),
    'entregas_dia': ('estoque.view_entregarota',),
    'separacao_vendas_fila': ('estoque.view_separacaovenda',),
    'separacao_venda_detalhe': ('estoque.view_separacaovenda',),
    'entrega_rota_checklist': ('estoque.view_entregarota',),
    'entrega_rota_checklist_cliente': ('estoque.view_entregarota',),
}
ESCRITA = {
    'home': ('estoque.change_produto',),
    'gravar_venda': ('estoque.add_venda', 'estoque.view_cliente', 'estoque.view_produto'),
    'venda_cancelar': ('estoque.change_venda',),
    'conta_receber_receber': ('estoque.add_recebimentocontareceber', 'estoque.change_contareceber'),
    'receber_cliente': ('estoque.add_recebimentocontareceber', 'estoque.change_contareceber'),
    'conta_pagar_baixar': ('estoque.add_pagamentocontapagar', 'estoque.change_contapagar'),
    'despesas_diarias': ('estoque.add_despesadiaria', 'estoque.add_movimentofinanceiro'),
    'produto_alternar_ativo': ('estoque.change_produto',),
    'entrega_rota_checklist': ('estoque.change_entregachecklistitem',),
    'entrega_rota_checklist_cliente': ('estoque.change_entregachecklistitem',),
    'separacao_venda_detalhe': ('estoque.change_separacaovenda',),
    'separacao_venda_item_salvar': ('estoque.change_separacaovendaitem',),
}


def requisitos(request, module, name):
    if module == 'offline.views' and name in {'online_sale', 'online_sale_result'}:
        return ('estoque.add_venda', 'estoque.view_cliente', 'estoque.view_produto')
    if module == 'estoque.views':
        requirements = (LEITURA if request.method in {'GET', 'HEAD', 'OPTIONS'} else ESCRITA).get(name)
        # Creation permission never grants editing, deleting or financial corrections.
        if name == 'gravar_venda':
            import json
            try:
                data = json.loads(request.body)
            except (ValueError, UnicodeError):
                data = {}
            if not isinstance(data, dict):
                return None
            if data.get('venda_id'):
                requirements = ('estoque.change_venda', 'estoque.view_cliente', 'estoque.view_produto')
        if name == 'home' and request.method == 'POST':
            return None  # Mixed create/delete/import actions: owner-only for now.
        if name == 'despesas_diarias' and request.method == 'POST' and request.POST.get('acao') != 'salvar_despesa':
            return None
        return requirements
    if module == 'locacoes.views':
        if name in {'conferencia_entrega', 'checklist_entrega_cliente', 'checklist_operacional'}:
            return ('locacoes.view_tarefaoperacionallocacao',) if request.method == 'GET' else ('locacoes.add_conferenciaentregalocacao', 'locacoes.change_tarefaoperacionallocacao')
        if name in {'conferencia_recolhimento', 'checklist_recolhimento_cliente'}:
            from locacoes.pickup_security import PICKUP_PERMISSIONS
            return tuple(PICKUP_PERMISSIONS)
    return None


class AcessoComercialMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if getattr(request, 'acesso_comercial_privado', False):
            patch_cache_control(response, private=True, no_store=True)
            patch_vary_headers(response, ('Cookie',))
            response['Referrer-Policy'] = 'same-origin'
        return response

    def process_view(self, request, view, args, kwargs):
        match = request.resolver_match
        module, name = view.__module__, view.__name__
        if match.url_name in {'sistema_login', 'sistema_logout', 'offline_login'} or match.view_name == 'inventario:login' or match.namespace == 'admin':
            return None  # Django's Login/Logout/Admin own their checks.
        if module == 'offline.views' and name in PUBLICAS:
            return None
        if module == 'offline.views' and name in {'online_sale', 'online_sale_result'} and request.headers.get('X-Local-Sales') == '1':
            return None  # Existing submit/lookup reject Render, proxy and invalid owner cookies.
        if name in PROTECAO_PROPRIA.get(module, set()):
            return None
        if module == 'locacoes.views' and name in {'conferencia_recolhimento', 'checklist_recolhimento_cliente'} and request.method == 'POST':
            return None  # Existing permission gate also preserves recovery drafts.
        request.acesso_comercial_privado = True
        if module == 'estoque.views' and name == 'gravar_venda':
            from offline.local_sales import permitted
            if permitted(request):
                return None  # The view always rejects this legacy LOCAL transport with 409.
        from .acesso_funcionarios import autorizar
        if autorizar(request, module, name, kwargs):
            request.operational_authorized = True
            return None
        # Preserve the existing trusted loopback adapter, never a Render exception.
        if (module, name) in {('estoque.views', 'vendas'), ('offline.views', 'online_sale'), ('offline.views', 'online_sale_result')}:
            from offline.local_sales import permitted
            if permitted(request) and getattr(request, 'local_sales_owner', None):
                return None
        if module == 'estoque.views' and name in {'clientes_autocomplete', 'vendas_cliente_produto_historico'} and request.method == 'GET':
            # These two reads belong to the existing trusted LOCAL sales page.
            # Reuse its signed owner cookie; never mint a principal here.
            from offline.local_sales import permitted, COOKIE, SALT, AGE
            from django.core import signing
            if permitted(request):
                try:
                    principal = signing.loads(request.COOKIES.get(COOKIE, ''), salt=SALT, max_age=AGE)
                    if (isinstance(principal, dict) and len(principal.get('nonce', '')) == 64
                            and (not request.user.is_authenticated or principal.get('user') == str(request.user.pk))):
                        return None
                except (signing.BadSignature, TypeError, ValueError):
                    pass
        user = request.user
        is_json = (request.method not in {'GET', 'HEAD'} or request.path.startswith('/api/')
            or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            or 'application/json' in request.headers.get('Accept', '')
            or 'autocomplete' in name or name in {'verificar_produto', 'grupos_lista'})
        if not user.is_authenticated or not user.is_active:
            if is_json:
                return JsonResponse({'sucesso': False, 'erro': 'Autenticação necessária.',
                    'mensagem': 'Entre com sua conta para continuar. Nenhuma operação foi executada.',
                    'login_url': reverse('sistema_login')}, status=401)
            return redirect_to_login(request.get_full_path(), reverse('sistema_login'))
        if user.is_superuser:
            return None
        requirements = requisitos(request, module, name)
        if requirements and user.has_perms(requirements):
            return None
        if is_json:
            return JsonResponse({'sucesso': False, 'erro': 'Permissão insuficiente.',
                'mensagem': 'Sua conta não está autorizada para esta operação.'}, status=403)
        return HttpResponseForbidden('Sua conta não tem permissão para esta área. Solicite autorização ao responsável.')
