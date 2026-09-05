from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from .models import Produto


class EstoqueManualVendasFrontendTests(TestCase):
    def setUp(self):
        self.produto_pendente = Produto.objects.create(
            nome="Produto Pendente Frontend",
            preco_compra=Decimal("10.00"),
            preco_vista=Decimal("15.00"),
            preco_prazo=Decimal("16.00"),
            quantidade=Decimal("4.000"),
            estoque_conferido=False,
        )
        self.produto_conferido = Produto.objects.create(
            nome="Produto Conferido Frontend",
            preco_compra=Decimal("10.00"),
            preco_vista=Decimal("15.00"),
            preco_prazo=Decimal("16.00"),
            quantidade=Decimal("7.125"),
            estoque_conferido=True,
        )

    def _vendas(self):
        resposta = self.client.get(reverse("estoque:vendas"), secure=True)
        self.assertEqual(resposta.status_code, 200)
        return resposta

    def _html(self):
        return self._vendas().content.decode()

    def test_tela_vendas_continua_renderizando(self):
        resposta = self._vendas()

        self.assertContains(resposta, 'id="produtoBusca"')
        self.assertContains(resposta, 'id="produtoSugestoes"')

    def test_template_contem_data_estoque_conferido(self):
        html = self._html()

        self.assertIn('data-estoque-conferido="false"', html)
        self.assertIn('data-estoque-conferido="true"', html)
        self.assertIn(f'data-produto-id="{self.produto_pendente.id}"', html)
        self.assertIn('data-estoque="4.000"', html)

    def test_template_contem_badge_amarela_e_verde_no_autocomplete(self):
        html = self._html()

        self.assertIn(".estoque-item-sugestao.estoque-conferido", html)
        self.assertIn(".estoque-item-sugestao.estoque-nao-conferido", html)
        self.assertIn("function classeBadgeConferenciaEstoque(opt)", html)

    def test_modal_de_conferencia_existe_com_campos_e_acoes(self):
        html = self._html()

        self.assertIn('id="modalConferenciaEstoque"', html)
        self.assertIn('id="modalConferenciaEstoqueProduto"', html)
        self.assertIn('id="modalConferenciaEstoqueAtual"', html)
        self.assertIn('id="modalConferenciaEstoqueStatus"', html)
        self.assertIn('id="modalConferenciaEstoqueHistorico"', html)
        self.assertIn('id="modalConferenciaNovoEstoque"', html)
        self.assertIn('id="modalConferenciaMotivo"', html)
        self.assertIn('id="btnConfirmarConferenciaEstoque"', html)
        self.assertIn('id="btnCorrigirConferenciaEstoque"', html)

    def test_urls_dos_tres_endpoints_estao_presentes(self):
        html = self._html()

        self.assertIn(reverse("estoque:conferencia_estoque_produto", args=[0]), html)
        self.assertIn(reverse("estoque:conferencia_estoque_confirmar", args=[0]), html)
        self.assertIn(reverse("estoque:conferencia_estoque_corrigir", args=[0]), html)
        self.assertIn("function urlConferenciaEstoque(base, produtoId)", html)

    def test_contador_existe_com_contexto_inicial(self):
        html = self._html()

        self.assertIn('id="conferenciaEstoqueContador"', html)
        self.assertIn('data-total-produtos="2"', html)
        self.assertIn('data-total-conferidos="1"', html)
        self.assertIn('data-total-faltantes="1"', html)
        self.assertIn("Conferidos: 1 / 2", html)

    def test_estrutura_impede_propagacao_do_clique_da_badge(self):
        html = self._html()

        self.assertIn('badgeEstoqueSugestao.addEventListener("mousedown"', html)
        self.assertIn('badgeEstoqueSugestao.addEventListener("click"', html)
        self.assertIn('badgeEstoqueSugestao.addEventListener("touchend"', html)
        self.assertIn('if (evento.target.closest(".estoque-item-sugestao")) return;', html)
        self.assertIn("evento.preventDefault();", html)
        self.assertIn("evento.stopPropagation();", html)
        self.assertIn("abrirModalConferenciaEstoque(opt);", html)

    def test_clique_normal_da_sugestao_continua_selecionando_produto(self):
        html = self._html()

        self.assertIn('div.addEventListener("mousedown"', html)
        self.assertIn('div.addEventListener("click"', html)
        self.assertIn('div.addEventListener("touchend"', html)
        self.assertGreaterEqual(html.count("selecionarProduto(opt);"), 3)

    def test_confirmar_e_corrigir_atualizam_dom_sem_reload(self):
        html = self._html()

        self.assertIn("async function enviarConferenciaEstoque(url, payload, mensagemSucesso)", html)
        self.assertIn("renderizarModalConferenciaEstoque(dados);", html)
        self.assertIn("atualizarBadgesConferenciaEstoque(produtoDados);", html)
        self.assertIn("atualizarContadorConferenciaEstoque(dados.conferencia);", html)
        self.assertNotIn("window.location.reload", html)

    def test_frontend_bloqueia_submissao_dupla_e_usa_csrf(self):
        html = self._html()

        self.assertIn("let conferenciaEstoqueRequisicaoEmAndamento = false;", html)
        self.assertIn("if (!conferenciaEstoqueProdutoAtualId || conferenciaEstoqueRequisicaoEmAndamento) return;", html)
        self.assertIn("btnCancelarConferenciaEstoque", html)
        self.assertIn("btnFecharConferenciaEstoque", html)
        self.assertIn("botao.disabled = bloquear;", html)
        self.assertIn('"X-CSRFToken": obterCookie("csrftoken")', html)
        self.assertIn('"X-Operador-Venda": operadorConferenciaEstoqueAtual()', html)
        self.assertIn("operador: operadorConferenciaEstoqueAtual()", html)

    def test_frontend_envia_operador_selecionado_no_get_do_modal(self):
        html = self._html()

        self.assertIn("function operadorConferenciaEstoqueAtual()", html)
        self.assertIn("return operadorVenda ? operadorVenda.value.trim() : \"\";", html)
        self.assertIn('"X-Operador-Venda": operadorConferenciaEstoqueAtual()', html)

    def test_frontend_valida_status_e_content_type_antes_de_ler_json(self):
        html = self._html()

        self.assertIn("async function lerJsonConferenciaEstoque(resposta, mensagemPadrao)", html)
        self.assertIn('const contentType = resposta.headers.get("Content-Type") || "";', html)
        self.assertIn("if (!resposta.ok)", html)
        self.assertIn('if (!contentType.includes("application/json"))', html)
        self.assertIn("Sessao expirada ou autenticacao necessaria", html)
        self.assertIn("lerJsonConferenciaEstoque(resposta, \"Nao foi possivel consultar o estoque.\")", html)

    def test_modal_nao_exibe_zero_como_dado_valido_antes_do_get(self):
        html = self._html()

        self.assertIn("modalConferenciaEstoqueProduto.textContent = opt.value || \"Produto\";", html)
        self.assertIn('modalConferenciaEstoqueAtual.textContent = "--";', html)
        self.assertIn('modalConferenciaEstoqueStatus.textContent = "Carregando...";', html)

    def test_modal_seleciona_todo_novo_estoque_apos_carregar(self):
        html = self._html()

        self.assertIn("function focarNovoEstoqueConferencia()", html)
        self.assertIn("modalConferenciaNovoEstoque.focus();", html)
        self.assertIn("modalConferenciaNovoEstoque.select();", html)
        self.assertIn("focarNovoEstoqueConferencia();", html)

    def test_modal_permite_motivo_vazio_no_frontend(self):
        html = self._html()

        self.assertIn('motivo: modalConferenciaMotivo ? modalConferenciaMotivo.value : ""', html)
        self.assertNotIn("Informe o motivo", html)

    def test_historico_de_correcao_omite_linha_de_motivo_quando_vazio(self):
        html = self._html()

        self.assertIn("const linhas = [", html)
        self.assertIn('if (movimentacao.motivo) linhas.push(`Motivo: ${movimentacao.motivo}`);', html)
        self.assertIn('modalConferenciaEstoqueHistorico.textContent = linhas.join("\\n");', html)
        self.assertIn("white-space: pre-line;", html)

    def test_enter_no_novo_estoque_move_para_motivo(self):
        html = self._html()

        self.assertIn("function configurarTecladoConferenciaEstoque()", html)
        self.assertIn('modalConferenciaNovoEstoque.addEventListener("keydown"', html)
        self.assertIn('if (evento.key !== "Enter") return;', html)
        self.assertIn("modalConferenciaMotivo.focus();", html)
        self.assertIn("modalConferenciaMotivo.select();", html)

    def test_enter_no_motivo_move_para_corrigir(self):
        html = self._html()

        self.assertIn('modalConferenciaMotivo.addEventListener("keydown"', html)
        self.assertIn("btnCorrigirConferenciaEstoque.focus();", html)

    def test_setas_alternam_corrigir_e_fechar(self):
        html = self._html()

        self.assertIn("function alternarFocoAcoesConferenciaEstoque(evento)", html)
        self.assertIn('if (!["ArrowLeft", "ArrowRight"].includes(evento.key)) return;', html)
        self.assertIn("btnCancelarConferenciaEstoque", html)
        self.assertIn('botao.addEventListener("keydown", alternarFocoAcoesConferenciaEstoque);', html)

    def test_enter_no_botao_focado_preserva_click_nativo(self):
        html = self._html()

        self.assertIn('type="button" id="btnCorrigirConferenciaEstoque"', html)
        self.assertIn('btnCorrigirConferenciaEstoque.addEventListener("click"', html)
        self.assertIn('type="button" id="btnCancelarConferenciaEstoque"', html)
        self.assertIn('botao.addEventListener("click", fecharModalConferenciaEstoque);', html)

    def test_confirmar_estoque_atual_continua_com_click_nativo(self):
        html = self._html()

        self.assertIn('type="button" id="btnConfirmarConferenciaEstoque"', html)
        self.assertIn('btnConfirmarConferenciaEstoque.addEventListener("click"', html)
        self.assertIn("urlConferenciaEstoque(conferenciaEstoqueConfirmarUrlBase, conferenciaEstoqueProdutoAtualId)", html)

    def test_enter_nos_inputs_nao_dispara_submit_do_modal(self):
        html = self._html()

        self.assertGreaterEqual(html.count("evento.preventDefault();"), 2)
        self.assertGreaterEqual(html.count("evento.stopPropagation();"), 2)
        self.assertIn('type="button" id="btnCorrigirConferenciaEstoque"', html)
        self.assertIn('type="button" id="btnCancelarConferenciaEstoque"', html)
