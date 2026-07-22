import io
import importlib
import json
import os
import re
import tempfile
import types
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import patch

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Sum
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import FornecedorForm, FuncionarioForm, PixRecebidoForm
from .models import AjusteItemVendaQuitada, Categoria, Cliente, Compra, ContaPagar, ContaReceber, CreditoCliente, EntregaChecklistItem, EntregaRota, EntregaRotaItem, EventoVenda, EnvioListaCompraFornecedor, EnvioInternoListaCompraFornecedor, Fornecedor, FornecedorContato, FornecedorContatoTelefone, FornecedorDestinatarioLista, FornecedorDestinatarioRecente, Funcionario, ItemCompra, ItemListaCompraFornecedor, ItemVenda, ItemVendaRemovido, ListaCompraFornecedor, MovimentoFinanceiro, OperacaoRecebimentoCliente, PagamentoContaPagar, PixRecebido, Produto, ProdutoFornecedor, RecebimentoContaReceber, ResolucaoVisitaFornecedor, Unidade, Venda
from .services.avisos_fornecedores import DIAS_ANTECEDENCIA_AVISO_VISITA, ESTADO_LISTA_ALTERADA_FALTA_REENVIAR, ESTADO_LISTA_PREPARADA_FALTA_ENVIAR, ESTADO_PREPARAR_LISTA, data_ciclo_visita_valida, datas_validas_ciclo_visita_fornecedor, obter_avisos_visitas_fornecedores
from .services.fornecedor_contatos import telefone_principal_contato, telefones_ativos_contato, telefones_whatsapp_contato
from .services.fornecedor_visitas import calcular_proxima_visita
from .utils_pix import analisar_comprovante_pix, analisar_comprovante_pix_google_vision, _preparar_recortes_ocr
from . import views


class FornecedorDestinatarioListaTests(TestCase):
    def setUp(self):
        self.fornecedor = Fornecedor.objects.create(
            nome="Fornecedor Destinat?rio"
        )
        self.contato = FornecedorContato.objects.create(
            fornecedor=self.fornecedor,
            nome="Vendedor Padr?o",
            principal=True,
            ativo=True,
        )
        self.telefone = FornecedorContatoTelefone.objects.create(
            contato=self.contato,
            numero="91999998888",
            whatsapp=True,
            principal=True,
            ativo=True,
        )

    def test_cria_destinatario_padrao_com_telefone_whatsapp_ativo(self):
        destinatario = FornecedorDestinatarioLista.objects.create(
            fornecedor=self.fornecedor,
            contato=self.contato,
            telefone=self.telefone,
        )

        self.assertEqual(
            destinatario.tipo,
            FornecedorDestinatarioLista.TIPO_PADRAO,
        )
        self.assertTrue(destinatario.ativo)

    def test_rejeita_contato_de_outro_fornecedor(self):
        outro_fornecedor = Fornecedor.objects.create(nome="Outro Fornecedor")
        outro_contato = FornecedorContato.objects.create(
            fornecedor=outro_fornecedor,
            nome="Outro Vendedor",
            principal=True,
            ativo=True,
        )

        destinatario = FornecedorDestinatarioLista(
            fornecedor=self.fornecedor,
            contato=outro_contato,
            telefone=self.telefone,
        )

        with self.assertRaisesMessage(
            ValidationError,
            "O contato escolhido precisa pertencer a este fornecedor.",
        ):
            destinatario.full_clean()

    def test_rejeita_telefone_de_outro_contato(self):
        outro_contato = FornecedorContato.objects.create(
            fornecedor=self.fornecedor,
            nome="Segundo Contato",
            principal=False,
            ativo=True,
        )
        outro_telefone = FornecedorContatoTelefone.objects.create(
            contato=outro_contato,
            numero="91911112222",
            whatsapp=True,
            principal=True,
            ativo=True,
        )

        destinatario = FornecedorDestinatarioLista(
            fornecedor=self.fornecedor,
            contato=self.contato,
            telefone=outro_telefone,
        )

        with self.assertRaisesMessage(
            ValidationError,
            "O telefone escolhido precisa pertencer ao contato informado.",
        ):
            destinatario.full_clean()

    def test_rejeita_telefone_inativo(self):
        self.telefone.ativo = False
        self.telefone.principal = False
        self.telefone.save()

        destinatario = FornecedorDestinatarioLista(
            fornecedor=self.fornecedor,
            contato=self.contato,
            telefone=self.telefone,
        )

        with self.assertRaisesMessage(
            ValidationError,
            "O telefone escolhido precisa estar ativo.",
        ):
            destinatario.full_clean()

    def test_rejeita_telefone_sem_whatsapp(self):
        self.telefone.whatsapp = False
        self.telefone.save()

        destinatario = FornecedorDestinatarioLista(
            fornecedor=self.fornecedor,
            contato=self.contato,
            telefone=self.telefone,
        )

        with self.assertRaisesMessage(
            ValidationError,
            "O telefone escolhido precisa estar marcado como WhatsApp.",
        ):
            destinatario.full_clean()

    def test_nao_permite_dois_destinatarios_padrao_ativos(self):
        FornecedorDestinatarioLista.objects.create(
            fornecedor=self.fornecedor,
            contato=self.contato,
            telefone=self.telefone,
        )

        segundo = FornecedorDestinatarioLista(
            fornecedor=self.fornecedor,
            contato=self.contato,
            telefone=self.telefone,
        )

        with self.assertRaises(ValidationError):
            segundo.full_clean()

    def test_destinatario_temporario_preserva_padrao(self):
        padrao = FornecedorDestinatarioLista.objects.create(
            fornecedor=self.fornecedor,
            contato=self.contato,
            telefone=self.telefone,
        )

        temporario = FornecedorDestinatarioLista.objects.create(
            fornecedor=self.fornecedor,
            contato=self.contato,
            telefone=self.telefone,
            tipo=FornecedorDestinatarioLista.TIPO_TEMPORARIO,
            vigencia_inicio=date(2026, 7, 14),
            vigencia_fim=date(2026, 7, 20),
            motivo="Teste de substitui??o",
        )

        self.assertTrue(padrao.ativo)
        self.assertTrue(temporario.ativo)
        self.assertNotEqual(padrao.tipo, temporario.tipo)

    def test_rejeita_vigencia_temporaria_invertida(self):
        temporario = FornecedorDestinatarioLista(
            fornecedor=self.fornecedor,
            contato=self.contato,
            telefone=self.telefone,
            tipo=FornecedorDestinatarioLista.TIPO_TEMPORARIO,
            vigencia_inicio=date(2026, 7, 20),
            vigencia_fim=date(2026, 7, 14),
        )

        with self.assertRaisesMessage(
            ValidationError,
            "A data final n?o pode ser anterior ? data inicial.",
        ):
            temporario.full_clean()


class FornecedorFrequenciaVisitaTests(TestCase):
    def fornecedor_frequencia(self, **alteracoes):
        dados = {
            "nome": "Fornecedor Visita",
            "frequencia_visita_ativa": True,
            "frequencia_visita_intervalo_dias": 7,
            "frequencia_visita_dia_semana": Fornecedor.DIA_SEMANA_TERCA,
            "frequencia_visita_data_referencia": date(2026, 7, 7),
        }
        dados.update(alteracoes)
        return Fornecedor(**dados)

    def test_frequencia_desativada_retorna_none(self):
        fornecedor = Fornecedor(nome="Fornecedor Sem Frequencia")

        self.assertIsNone(calcular_proxima_visita(fornecedor, data_base=date(2026, 7, 7)))

    def test_referencia_igual_data_base_retorna_referencia(self):
        fornecedor = self.fornecedor_frequencia()

        self.assertEqual(calcular_proxima_visita(fornecedor, date(2026, 7, 7)), date(2026, 7, 7))

    def test_referencia_futura_retorna_referencia(self):
        fornecedor = self.fornecedor_frequencia(
            frequencia_visita_intervalo_dias=14,
            frequencia_visita_data_referencia=date(2026, 7, 21),
        )

        self.assertEqual(calcular_proxima_visita(fornecedor, date(2026, 7, 12)), date(2026, 7, 21))

    def test_frequencia_semanal_calcula_proxima_ocorrencia(self):
        fornecedor = self.fornecedor_frequencia()

        self.assertEqual(calcular_proxima_visita(fornecedor, date(2026, 7, 8)), date(2026, 7, 14))

    def test_frequencia_quinzenal_calcula_proxima_ocorrencia(self):
        fornecedor = self.fornecedor_frequencia(
            frequencia_visita_intervalo_dias=14,
            frequencia_visita_data_referencia=date(2026, 6, 30),
        )

        self.assertEqual(calcular_proxima_visita(fornecedor, date(2026, 7, 12)), date(2026, 7, 14))

    def test_calculo_funciona_apos_varios_ciclos(self):
        fornecedor = self.fornecedor_frequencia(
            frequencia_visita_intervalo_dias=14,
            frequencia_visita_data_referencia=date(2026, 1, 6),
        )

        self.assertEqual(calcular_proxima_visita(fornecedor, date(2026, 7, 12)), date(2026, 7, 21))

    def test_calculo_nao_altera_fornecedor(self):
        fornecedor = self.fornecedor_frequencia()
        estado_original = fornecedor.__dict__.copy()

        calcular_proxima_visita(fornecedor, date(2026, 7, 8))

        self.assertEqual(fornecedor.__dict__, estado_original)

    def test_calculo_retorna_none_com_intervalo_ausente(self):
        fornecedor = self.fornecedor_frequencia(frequencia_visita_intervalo_dias=None)

        self.assertIsNone(calcular_proxima_visita(fornecedor, date(2026, 7, 8)))

    def test_calculo_retorna_none_com_intervalo_menor_ou_igual_a_zero(self):
        fornecedor = self.fornecedor_frequencia(frequencia_visita_intervalo_dias=0)

        self.assertIsNone(calcular_proxima_visita(fornecedor, date(2026, 7, 8)))

    def test_calculo_retorna_none_com_intervalo_nao_multiplo_de_sete(self):
        fornecedor = self.fornecedor_frequencia(frequencia_visita_intervalo_dias=10)

        self.assertIsNone(calcular_proxima_visita(fornecedor, date(2026, 7, 8)))

    def test_calculo_retorna_none_com_dia_semana_ausente(self):
        fornecedor = self.fornecedor_frequencia(frequencia_visita_dia_semana=None)

        self.assertIsNone(calcular_proxima_visita(fornecedor, date(2026, 7, 8)))

    def test_calculo_retorna_none_com_dia_semana_fora_da_faixa(self):
        fornecedor = self.fornecedor_frequencia(frequencia_visita_dia_semana=7)

        self.assertIsNone(calcular_proxima_visita(fornecedor, date(2026, 7, 8)))

    def test_calculo_retorna_none_com_data_referencia_ausente(self):
        fornecedor = self.fornecedor_frequencia(frequencia_visita_data_referencia=None)

        self.assertIsNone(calcular_proxima_visita(fornecedor, date(2026, 7, 8)))

    def test_calculo_retorna_none_com_referencia_em_dia_diferente_do_configurado(self):
        fornecedor = self.fornecedor_frequencia(
            frequencia_visita_dia_semana=Fornecedor.DIA_SEMANA_SEGUNDA,
        )

        self.assertIsNone(calcular_proxima_visita(fornecedor, date(2026, 7, 8)))

    def test_full_clean_aceita_frequencia_desativada_com_campos_vazios(self):
        fornecedor = Fornecedor(nome="Fornecedor Sem Frequencia")

        fornecedor.full_clean()

    def test_full_clean_rejeita_frequencia_ativa_sem_intervalo(self):
        fornecedor = self.fornecedor_frequencia(frequencia_visita_intervalo_dias=None)

        with self.assertRaises(ValidationError) as erro:
            fornecedor.full_clean()

        self.assertIn("frequencia_visita_intervalo_dias", erro.exception.message_dict)

    def test_full_clean_rejeita_intervalo_nao_multiplo_de_sete(self):
        fornecedor = self.fornecedor_frequencia(frequencia_visita_intervalo_dias=10)

        with self.assertRaises(ValidationError) as erro:
            fornecedor.full_clean()

        self.assertIn("frequencia_visita_intervalo_dias", erro.exception.message_dict)

    def test_full_clean_rejeita_ausencia_de_dia_semana(self):
        fornecedor = self.fornecedor_frequencia(frequencia_visita_dia_semana=None)

        with self.assertRaises(ValidationError) as erro:
            fornecedor.full_clean()

        self.assertIn("frequencia_visita_dia_semana", erro.exception.message_dict)

    def test_full_clean_rejeita_ausencia_de_data_referencia(self):
        fornecedor = self.fornecedor_frequencia(frequencia_visita_data_referencia=None)

        with self.assertRaises(ValidationError) as erro:
            fornecedor.full_clean()

        self.assertIn("frequencia_visita_data_referencia", erro.exception.message_dict)

    def test_full_clean_rejeita_referencia_em_dia_diferente_do_selecionado(self):
        fornecedor = self.fornecedor_frequencia(
            frequencia_visita_dia_semana=Fornecedor.DIA_SEMANA_SEGUNDA,
        )

        with self.assertRaises(ValidationError) as erro:
            fornecedor.full_clean()

        self.assertIn("frequencia_visita_data_referencia", erro.exception.message_dict)

    def test_full_clean_aceita_configuracao_semanal_valida(self):
        fornecedor = self.fornecedor_frequencia()

        fornecedor.full_clean()

    def test_full_clean_aceita_configuracao_quinzenal_valida(self):
        fornecedor = self.fornecedor_frequencia(frequencia_visita_intervalo_dias=14)

        fornecedor.full_clean()

    def test_lista_compra_fornecedor_aceita_data_visita_vazia(self):
        fornecedor = Fornecedor.objects.create(nome="Fornecedor Lista Sem Visita")

        lista = ListaCompraFornecedor.objects.create(
            fornecedor=fornecedor,
            data_lista=date(2026, 7, 12),
            data_inicio_periodo=date(2026, 7, 1),
            data_fim_periodo=date(2026, 7, 12),
        )

        self.assertIsNone(lista.data_visita_fornecedor)

    def test_lista_compra_fornecedor_persiste_data_visita_informada(self):
        fornecedor = Fornecedor.objects.create(nome="Fornecedor Lista Com Visita")

        lista = ListaCompraFornecedor.objects.create(
            fornecedor=fornecedor,
            data_lista=date(2026, 7, 12),
            data_inicio_periodo=date(2026, 7, 1),
            data_fim_periodo=date(2026, 7, 12),
            data_visita_fornecedor=date(2026, 7, 14),
        )

        lista.refresh_from_db()
        self.assertEqual(lista.data_visita_fornecedor, date(2026, 7, 14))


class ResolucaoVisitaFornecedorModelTests(TestCase):
    def setUp(self):
        self.fornecedor = Fornecedor.objects.create(
            nome="Fornecedor Resolucao Visita"
        )
        self.usuario = get_user_model().objects.create_user(
            username="responsavel_resolucao",
            password="senha-segura",
        )
        self.data_original = date(2026, 7, 14)

    def test_grava_visita_nao_ocorreu_com_responsavel_e_historico(self):
        resolucao = ResolucaoVisitaFornecedor.objects.create(
            fornecedor=self.fornecedor,
            data_visita_original=self.data_original,
            tipo_resolucao=ResolucaoVisitaFornecedor.TIPO_NAO_OCORREU,
            observacao="Representante nao passou.",
            responsavel=self.usuario,
        )

        self.assertEqual(resolucao.fornecedor, self.fornecedor)
        self.assertEqual(resolucao.data_visita_original, self.data_original)
        self.assertEqual(
            resolucao.tipo_resolucao,
            ResolucaoVisitaFornecedor.TIPO_NAO_OCORREU,
        )
        self.assertIsNone(resolucao.nova_data_visita)
        self.assertEqual(resolucao.observacao, "Representante nao passou.")
        self.assertEqual(resolucao.responsavel, self.usuario)
        self.assertIsNotNone(resolucao.resolvido_em)

    def test_visita_adiada_exige_nova_data(self):
        resolucao = ResolucaoVisitaFornecedor(
            fornecedor=self.fornecedor,
            data_visita_original=self.data_original,
            tipo_resolucao=ResolucaoVisitaFornecedor.TIPO_ADIADA,
            responsavel=self.usuario,
        )

        with self.assertRaises(ValidationError) as erro:
            resolucao.save()

        self.assertIn("nova_data_visita", erro.exception.message_dict)

    def test_nova_data_deve_ser_posterior_a_data_original(self):
        resolucao = ResolucaoVisitaFornecedor(
            fornecedor=self.fornecedor,
            data_visita_original=self.data_original,
            tipo_resolucao=ResolucaoVisitaFornecedor.TIPO_ADIADA,
            nova_data_visita=self.data_original,
            responsavel=self.usuario,
        )

        with self.assertRaises(ValidationError) as erro:
            resolucao.save()

        self.assertIn("nova_data_visita", erro.exception.message_dict)

    def test_visita_adiada_aceita_nova_data_futura(self):
        nova_data = self.data_original + timedelta(days=3)

        resolucao = ResolucaoVisitaFornecedor.objects.create(
            fornecedor=self.fornecedor,
            data_visita_original=self.data_original,
            tipo_resolucao=ResolucaoVisitaFornecedor.TIPO_ADIADA,
            nova_data_visita=nova_data,
            responsavel=self.usuario,
        )

        self.assertEqual(resolucao.nova_data_visita, nova_data)

    def test_nova_data_nao_pode_ser_usada_em_outro_tipo(self):
        resolucao = ResolucaoVisitaFornecedor(
            fornecedor=self.fornecedor,
            data_visita_original=self.data_original,
            tipo_resolucao=ResolucaoVisitaFornecedor.TIPO_IGNORAR_CICLO,
            nova_data_visita=self.data_original + timedelta(days=2),
            responsavel=self.usuario,
        )

        with self.assertRaises(ValidationError) as erro:
            resolucao.save()

        self.assertIn("nova_data_visita", erro.exception.message_dict)

    def test_nao_permite_duas_resolucoes_para_mesmo_ciclo(self):
        ResolucaoVisitaFornecedor.objects.create(
            fornecedor=self.fornecedor,
            data_visita_original=self.data_original,
            tipo_resolucao=ResolucaoVisitaFornecedor.TIPO_NAO_OCORREU,
            responsavel=self.usuario,
        )

        duplicada = ResolucaoVisitaFornecedor(
            fornecedor=self.fornecedor,
            data_visita_original=self.data_original,
            tipo_resolucao=ResolucaoVisitaFornecedor.TIPO_IGNORAR_CICLO,
            responsavel=self.usuario,
        )

        with self.assertRaises(ValidationError):
            duplicada.save()

    def test_mesma_data_pode_ser_resolvida_para_fornecedores_diferentes(self):
        outro_fornecedor = Fornecedor.objects.create(
            nome="Outro Fornecedor Resolucao"
        )

        primeira = ResolucaoVisitaFornecedor.objects.create(
            fornecedor=self.fornecedor,
            data_visita_original=self.data_original,
            tipo_resolucao=ResolucaoVisitaFornecedor.TIPO_NAO_OCORREU,
            responsavel=self.usuario,
        )
        segunda = ResolucaoVisitaFornecedor.objects.create(
            fornecedor=outro_fornecedor,
            data_visita_original=self.data_original,
            tipo_resolucao=ResolucaoVisitaFornecedor.TIPO_IGNORAR_CICLO,
            responsavel=self.usuario,
        )

        self.assertNotEqual(primeira.fornecedor_id, segunda.fornecedor_id)


class AvisosVisitasFornecedoresServiceTests(TestCase):
    def setUp(self):
        self.data_base = date(2026, 7, 15)

    def criar_fornecedor(self, nome="Fornecedor Aviso", referencia=None, intervalo=7, **alteracoes):
        referencia = referencia or self.data_base
        dados = {
            "nome": nome,
            "ativo": True,
            "frequencia_visita_ativa": True,
            "frequencia_visita_intervalo_dias": intervalo,
            "frequencia_visita_dia_semana": referencia.weekday(),
            "frequencia_visita_data_referencia": referencia,
        }
        dados.update(alteracoes)
        return Fornecedor.objects.create(**dados)

    def criar_lista(self, fornecedor, data_visita=None, status=ListaCompraFornecedor.STATUS_ABERTA):
        data_visita = data_visita if data_visita is not None else self.data_base
        return ListaCompraFornecedor.objects.create(
            fornecedor=fornecedor,
            data_lista=self.data_base,
            data_inicio_periodo=self.data_base - timedelta(days=14),
            data_fim_periodo=self.data_base,
            data_visita_fornecedor=data_visita,
            status=status,
            total_sugerido_original=Decimal("10.00"),
            total_lista=Decimal("10.00"),
        )

    def confirmar_envio(self, lista, chave=None, confirmado_em=None):
        return EnvioListaCompraFornecedor.objects.create(
            lista=lista,
            fornecedor=lista.fornecedor,
            nome_destinatario="Vendedor",
            telefone_destinatario="5591999999999",
            confirmado_em=confirmado_em or timezone.now() + timedelta(minutes=1),
            origem_destinatario=EnvioListaCompraFornecedor.ORIGEM_PERSONALIZADO,
            chave_idempotencia=chave or f"teste-aviso-{lista.id}-{EnvioListaCompraFornecedor.objects.count()}",
        )

    def unico_aviso(self, data_referencia=None):
        avisos = obter_avisos_visitas_fornecedores(data_referencia or self.data_base)
        self.assertEqual(len(avisos), 1)
        return avisos[0]

    def test_fornecedor_inativo_nao_gera_aviso(self):
        self.criar_fornecedor(ativo=False)

        self.assertEqual(obter_avisos_visitas_fornecedores(self.data_base), [])

    def test_frequencia_desativada_nao_gera_aviso(self):
        self.criar_fornecedor(frequencia_visita_ativa=False)

        self.assertEqual(obter_avisos_visitas_fornecedores(self.data_base), [])

    def test_configuracao_incompleta_nao_quebra_servico(self):
        self.criar_fornecedor(
            frequencia_visita_intervalo_dias=None,
            frequencia_visita_dia_semana=None,
            frequencia_visita_data_referencia=None,
        )

        self.assertEqual(obter_avisos_visitas_fornecedores(self.data_base), [])

    def test_visita_alem_de_sete_dias_nao_gera_aviso(self):
        self.criar_fornecedor(referencia=self.data_base + timedelta(days=8))

        self.assertEqual(obter_avisos_visitas_fornecedores(self.data_base), [])

    def test_visita_em_sete_dias_gera_aviso(self):
        self.criar_fornecedor(referencia=self.data_base + timedelta(days=DIAS_ANTECEDENCIA_AVISO_VISITA))

        aviso = self.unico_aviso()
        self.assertEqual(aviso["dias_para_visita"], DIAS_ANTECEDENCIA_AVISO_VISITA)
        self.assertEqual(aviso["estado"], ESTADO_PREPARAR_LISTA)

    def test_visita_amanha_gera_preparar_lista_sem_lista(self):
        self.criar_fornecedor(referencia=self.data_base + timedelta(days=1))

        aviso = self.unico_aviso()
        self.assertEqual(aviso["estado"], ESTADO_PREPARAR_LISTA)
        self.assertEqual(aviso["dias_para_visita"], 1)

    def test_visita_hoje_gera_preparar_lista_sem_lista(self):
        self.criar_fornecedor()

        aviso = self.unico_aviso()
        self.assertEqual(aviso["estado"], ESTADO_PREPARAR_LISTA)
        self.assertEqual(aviso["dias_para_visita"], 0)

    def test_visita_atrasada_pendente_continua_aparecendo(self):
        self.criar_fornecedor(referencia=self.data_base - timedelta(days=7), intervalo=14)

        aviso = self.unico_aviso()
        self.assertEqual(aviso["data_visita"], (self.data_base - timedelta(days=7)).isoformat())
        self.assertEqual(aviso["dias_para_visita"], -7)
        self.assertEqual(aviso["estado"], ESTADO_PREPARAR_LISTA)

    def test_visita_nao_ocorreu_encerra_apenas_o_ciclo_original(self):
        data_original = self.data_base - timedelta(days=1)
        fornecedor = self.criar_fornecedor(
            referencia=data_original,
            intervalo=14,
        )
        ResolucaoVisitaFornecedor.objects.create(
            fornecedor=fornecedor,
            data_visita_original=data_original,
            tipo_resolucao=ResolucaoVisitaFornecedor.TIPO_NAO_OCORREU,
            observacao="O representante nao compareceu.",
        )

        avisos = obter_avisos_visitas_fornecedores(self.data_base)

        self.assertEqual(avisos, [])

    def test_ignorar_ciclo_impede_reapresentacao_da_ocorrencia(self):
        data_original = self.data_base - timedelta(days=1)
        fornecedor = self.criar_fornecedor(
            referencia=data_original,
            intervalo=14,
        )
        ResolucaoVisitaFornecedor.objects.create(
            fornecedor=fornecedor,
            data_visita_original=data_original,
            tipo_resolucao=ResolucaoVisitaFornecedor.TIPO_IGNORAR_CICLO,
            observacao="Ciclo ignorado por decisao operacional.",
        )

        avisos = obter_avisos_visitas_fornecedores(self.data_base)

        self.assertEqual(avisos, [])

    def test_visita_adiada_substitui_data_original_no_aviso(self):
        data_original = self.data_base - timedelta(days=1)
        nova_data = self.data_base + timedelta(days=3)
        fornecedor = self.criar_fornecedor(
            referencia=data_original,
            intervalo=14,
        )
        ResolucaoVisitaFornecedor.objects.create(
            fornecedor=fornecedor,
            data_visita_original=data_original,
            tipo_resolucao=ResolucaoVisitaFornecedor.TIPO_ADIADA,
            nova_data_visita=nova_data,
            observacao="Representante solicitou nova data.",
        )

        aviso = self.unico_aviso()

        self.assertEqual(aviso["data_visita"], nova_data.isoformat())
        self.assertEqual(aviso["dias_para_visita"], 3)
        self.assertEqual(aviso["estado"], ESTADO_PREPARAR_LISTA)
        self.assertIn(
            f"data_visita={nova_data.isoformat()}",
            aviso["acao"]["url"],
        )

    def test_visita_adiada_para_fora_da_janela_nao_aparece_antes_da_hora(self):
        data_original = self.data_base - timedelta(days=1)
        nova_data = self.data_base + timedelta(days=10)
        fornecedor = self.criar_fornecedor(
            referencia=data_original,
            intervalo=21,
        )
        ResolucaoVisitaFornecedor.objects.create(
            fornecedor=fornecedor,
            data_visita_original=data_original,
            tipo_resolucao=ResolucaoVisitaFornecedor.TIPO_ADIADA,
            nova_data_visita=nova_data,
        )

        avisos = obter_avisos_visitas_fornecedores(self.data_base)

        self.assertEqual(avisos, [])

    def test_adiamentos_sucessivos_usam_a_ultima_data(self):
        data_original = self.data_base - timedelta(days=1)
        primeira_nova_data = self.data_base + timedelta(days=2)
        segunda_nova_data = self.data_base + timedelta(days=4)
        fornecedor = self.criar_fornecedor(
            referencia=data_original,
            intervalo=21,
        )
        ResolucaoVisitaFornecedor.objects.create(
            fornecedor=fornecedor,
            data_visita_original=data_original,
            tipo_resolucao=ResolucaoVisitaFornecedor.TIPO_ADIADA,
            nova_data_visita=primeira_nova_data,
        )
        ResolucaoVisitaFornecedor.objects.create(
            fornecedor=fornecedor,
            data_visita_original=primeira_nova_data,
            tipo_resolucao=ResolucaoVisitaFornecedor.TIPO_ADIADA,
            nova_data_visita=segunda_nova_data,
        )

        aviso = self.unico_aviso()

        self.assertEqual(
            aviso["data_visita"],
            segunda_nova_data.isoformat(),
        )
        self.assertEqual(aviso["dias_para_visita"], 4)

    def test_lista_do_ciclo_gera_falta_enviar(self):
        fornecedor = self.criar_fornecedor()
        lista = self.criar_lista(fornecedor)

        aviso = self.unico_aviso()
        self.assertEqual(aviso["estado"], ESTADO_LISTA_PREPARADA_FALTA_ENVIAR)
        self.assertEqual(aviso["lista_id"], lista.id)

    def test_lista_de_outra_data_nao_atende_ciclo_atual(self):
        fornecedor = self.criar_fornecedor()
        self.criar_lista(fornecedor, data_visita=self.data_base + timedelta(days=7))

        aviso = self.unico_aviso()
        self.assertEqual(aviso["estado"], ESTADO_PREPARAR_LISTA)
        self.assertIsNone(aviso["lista_id"])

    def test_lista_de_outro_fornecedor_nao_atende_ciclo(self):
        fornecedor = self.criar_fornecedor(nome="Fornecedor Principal")
        outro = self.criar_fornecedor(nome="Outro Fornecedor")
        self.criar_lista(outro)

        avisos = obter_avisos_visitas_fornecedores(self.data_base)
        aviso_principal = [aviso for aviso in avisos if aviso["fornecedor_id"] == fornecedor.id][0]
        self.assertEqual(aviso_principal["estado"], ESTADO_PREPARAR_LISTA)
        self.assertIsNone(aviso_principal["lista_id"])

    def test_lista_cancelada_nao_atende_ciclo(self):
        fornecedor = self.criar_fornecedor()
        self.criar_lista(fornecedor, status=ListaCompraFornecedor.STATUS_CANCELADA)

        aviso = self.unico_aviso()
        self.assertEqual(aviso["estado"], ESTADO_PREPARAR_LISTA)
        self.assertIsNone(aviso["lista_id"])

    def test_lista_aberta_sem_confirmacao_gera_falta_enviar(self):
        fornecedor = self.criar_fornecedor()
        self.criar_lista(fornecedor, status=ListaCompraFornecedor.STATUS_ABERTA)

        aviso = self.unico_aviso()
        self.assertEqual(aviso["lista_status"], ListaCompraFornecedor.STATUS_ABERTA)
        self.assertEqual(aviso["estado"], ESTADO_LISTA_PREPARADA_FALTA_ENVIAR)

    def test_lista_enviada_sem_confirmacao_auditavel_gera_falta_enviar(self):
        fornecedor = self.criar_fornecedor()
        self.criar_lista(fornecedor, status=ListaCompraFornecedor.STATUS_ENVIADA)

        aviso = self.unico_aviso()
        self.assertEqual(aviso["lista_status"], ListaCompraFornecedor.STATUS_ENVIADA)
        self.assertEqual(aviso["estado"], ESTADO_LISTA_PREPARADA_FALTA_ENVIAR)

    def test_lista_finalizada_sem_confirmacao_auditavel_gera_falta_enviar(self):
        fornecedor = self.criar_fornecedor()
        self.criar_lista(fornecedor, status=ListaCompraFornecedor.STATUS_FINALIZADA)

        aviso = self.unico_aviso()
        self.assertEqual(aviso["lista_status"], ListaCompraFornecedor.STATUS_FINALIZADA)
        self.assertEqual(aviso["estado"], ESTADO_LISTA_PREPARADA_FALTA_ENVIAR)

    def test_confirmacao_auditavel_encerra_aviso_do_ciclo(self):
        fornecedor = self.criar_fornecedor(intervalo=14)
        lista = self.criar_lista(fornecedor)
        self.confirmar_envio(lista)

        self.assertEqual(obter_avisos_visitas_fornecedores(self.data_base), [])

    def test_confirmacao_auditavel_permite_avancar_para_proximo_ciclo(self):
        fornecedor = self.criar_fornecedor()
        lista = self.criar_lista(fornecedor)
        self.confirmar_envio(lista)

        aviso = self.unico_aviso(self.data_base + timedelta(days=1))
        self.assertEqual(aviso["data_visita"], (self.data_base + timedelta(days=7)).isoformat())
        self.assertEqual(aviso["estado"], ESTADO_PREPARAR_LISTA)

    def test_lista_alterada_depois_do_envio_gera_falta_reenviar(self):
        fornecedor = self.criar_fornecedor()
        lista = self.criar_lista(fornecedor)
        self.confirmar_envio(lista)

        novo_horario_alteracao = timezone.now() + timedelta(minutes=5)
        ListaCompraFornecedor.objects.filter(pk=lista.pk).update(
            total_lista=Decimal("12.00"),
            atualizado_em=novo_horario_alteracao,
        )
        lista.refresh_from_db()

        aviso = self.unico_aviso()

        self.assertEqual(aviso["lista_id"], lista.id)
        self.assertEqual(aviso["estado"], ESTADO_LISTA_ALTERADA_FALTA_REENVIAR)
        self.assertEqual(
            aviso["mensagem"],
            "Lista alterada depois do envio, falta reenviar ao vendedor.",
        )
        self.assertFalse(aviso["tem_envio_confirmado"])
        self.assertEqual(aviso["acao"]["tipo"], "enviar_ao_vendedor")

    def test_reenvio_depois_da_alteracao_encerra_aviso(self):
        fornecedor = self.criar_fornecedor(intervalo=14)
        lista = self.criar_lista(fornecedor)
        self.confirmar_envio(lista)

        novo_horario_alteracao = timezone.now() + timedelta(minutes=5)
        ListaCompraFornecedor.objects.filter(pk=lista.pk).update(
            total_lista=Decimal("12.00"),
            atualizado_em=novo_horario_alteracao,
        )
        lista.refresh_from_db()

        self.confirmar_envio(
            lista,
            chave="teste-reenvio-apos-alteracao",
            confirmado_em=lista.atualizado_em + timedelta(minutes=1),
        )

        self.assertEqual(obter_avisos_visitas_fornecedores(self.data_base), [])

    def test_item_alterado_depois_do_envio_gera_falta_reenviar(self):
        fornecedor = self.criar_fornecedor()
        lista = self.criar_lista(fornecedor)
        item = ItemListaCompraFornecedor.objects.create(
            lista=lista,
            estoque_atual=Decimal("0.000"),
            estoque_minimo=Decimal("0.000"),
            quantidade_final=Decimal("1.000"),
            unidade="UN",
            preco_compra=Decimal("10.00"),
            total=Decimal("10.00"),
        )
        horario_original = timezone.make_aware(datetime(2026, 7, 15, 9, 0))
        ListaCompraFornecedor.objects.filter(pk=lista.pk).update(
            atualizado_em=horario_original,
        )
        self.confirmar_envio(
            lista,
            chave="teste-envio-antes-alterar-item",
            confirmado_em=horario_original + timedelta(hours=1),
        )

        item.quantidade_final = Decimal("2.000")
        item.total = Decimal("20.00")
        item.save()
        lista.refresh_from_db()

        aviso = self.unico_aviso()

        self.assertEqual(aviso["lista_id"], lista.id)
        self.assertEqual(aviso["estado"], ESTADO_LISTA_ALTERADA_FALTA_REENVIAR)
        self.assertGreater(
            lista.atualizado_em,
            horario_original + timedelta(hours=1),
        )

    def test_item_removido_depois_do_envio_gera_falta_reenviar(self):
        fornecedor = self.criar_fornecedor()
        lista = self.criar_lista(fornecedor)
        item = ItemListaCompraFornecedor.objects.create(
            lista=lista,
            estoque_atual=Decimal("0.000"),
            estoque_minimo=Decimal("0.000"),
            quantidade_final=Decimal("1.000"),
            unidade="UN",
            preco_compra=Decimal("10.00"),
            total=Decimal("10.00"),
        )
        horario_original = timezone.make_aware(datetime(2026, 7, 15, 9, 0))
        ListaCompraFornecedor.objects.filter(pk=lista.pk).update(
            atualizado_em=horario_original,
        )
        self.confirmar_envio(
            lista,
            chave="teste-envio-antes-remover-item",
            confirmado_em=horario_original + timedelta(hours=1),
        )

        item.delete()
        lista.refresh_from_db()

        aviso = self.unico_aviso()

        self.assertEqual(aviso["lista_id"], lista.id)
        self.assertEqual(aviso["estado"], ESTADO_LISTA_ALTERADA_FALTA_REENVIAR)
        self.assertGreater(
            lista.atualizado_em,
            horario_original + timedelta(hours=1),
        )

    def test_lista_alterada_depois_do_envio_gera_falta_reenviar(self):
        fornecedor = self.criar_fornecedor()
        lista = self.criar_lista(fornecedor)
        self.confirmar_envio(lista)

        novo_horario_alteracao = timezone.now() + timedelta(minutes=5)
        ListaCompraFornecedor.objects.filter(pk=lista.pk).update(
            total_lista=Decimal("12.00"),
            atualizado_em=novo_horario_alteracao,
        )
        lista.refresh_from_db()

        aviso = self.unico_aviso()

        self.assertEqual(aviso["lista_id"], lista.id)
        self.assertEqual(aviso["estado"], ESTADO_LISTA_ALTERADA_FALTA_REENVIAR)
        self.assertEqual(
            aviso["mensagem"],
            "Lista alterada depois do envio, falta reenviar ao vendedor.",
        )
        self.assertFalse(aviso["tem_envio_confirmado"])
        self.assertEqual(aviso["acao"]["tipo"], "enviar_ao_vendedor")

    def test_reenvio_depois_da_alteracao_encerra_aviso(self):
        fornecedor = self.criar_fornecedor(intervalo=14)
        lista = self.criar_lista(fornecedor)
        self.confirmar_envio(lista)

        novo_horario_alteracao = timezone.now() + timedelta(minutes=5)
        ListaCompraFornecedor.objects.filter(pk=lista.pk).update(
            total_lista=Decimal("12.00"),
            atualizado_em=novo_horario_alteracao,
        )
        lista.refresh_from_db()

        self.confirmar_envio(
            lista,
            chave="teste-reenvio-apos-alteracao",
            confirmado_em=lista.atualizado_em + timedelta(minutes=1),
        )

        self.assertEqual(obter_avisos_visitas_fornecedores(self.data_base), [])

    def test_destinatario_recente_nao_encerra_aviso(self):
        fornecedor = self.criar_fornecedor()
        lista = self.criar_lista(fornecedor)
        FornecedorDestinatarioRecente.objects.create(
            fornecedor=fornecedor,
            nome="Vendedor",
            telefone="(91) 99999-9999",
            ultima_utilizacao=timezone.now(),
        )

        aviso = self.unico_aviso()
        self.assertEqual(aviso["lista_id"], lista.id)
        self.assertEqual(aviso["estado"], ESTADO_LISTA_PREPARADA_FALTA_ENVIAR)

    def test_compartilhamento_interno_nao_encerra_aviso(self):
        fornecedor = self.criar_fornecedor()
        lista = self.criar_lista(fornecedor)
        lista.checklist_externa_token_hash = "a" * 64
        lista.checklist_externa_token_usado_em = timezone.now()
        lista.save(update_fields=["checklist_externa_token_hash", "checklist_externa_token_usado_em"])

        aviso = self.unico_aviso()
        self.assertEqual(aviso["lista_id"], lista.id)
        self.assertEqual(aviso["estado"], ESTADO_LISTA_PREPARADA_FALTA_ENVIAR)

    def test_duas_listas_no_mesmo_ciclo_usam_mais_recente(self):
        fornecedor = self.criar_fornecedor()
        lista_antiga = self.criar_lista(fornecedor)
        lista_recente = self.criar_lista(fornecedor)

        aviso = self.unico_aviso()
        self.assertNotEqual(aviso["lista_id"], lista_antiga.id)
        self.assertEqual(aviso["lista_id"], lista_recente.id)

    def test_lista_sem_data_visita_nao_atende_ciclo_com_data(self):
        fornecedor = self.criar_fornecedor()
        ListaCompraFornecedor.objects.create(
            fornecedor=fornecedor,
            data_lista=self.data_base,
            data_inicio_periodo=self.data_base - timedelta(days=14),
            data_fim_periodo=self.data_base,
            status=ListaCompraFornecedor.STATUS_ABERTA,
        )

        aviso = self.unico_aviso()
        self.assertEqual(aviso["estado"], ESTADO_PREPARAR_LISTA)
        self.assertIsNone(aviso["lista_id"])

    def test_payload_contem_campos_obrigatorios(self):
        fornecedor = self.criar_fornecedor()

        aviso = self.unico_aviso()
        self.assertEqual(set(aviso.keys()), {
            "fornecedor_id",
            "fornecedor_nome",
            "data_visita",
            "dias_para_visita",
            "estado",
            "prioridade",
            "titulo",
            "mensagem",
            "lista_id",
            "lista_status",
            "tem_envio_confirmado",
            "acao",
        })
        self.assertEqual(aviso["fornecedor_id"], fornecedor.id)
        self.assertIn("tipo", aviso["acao"])
        self.assertIn("url", aviso["acao"])

    def test_rota_preparar_lista_contem_fornecedor_e_data(self):
        fornecedor = self.criar_fornecedor()

        aviso = self.unico_aviso()
        partes = urlsplit(aviso["acao"]["url"])
        parametros = parse_qs(partes.query)
        self.assertEqual(partes.path, reverse("estoque:sugestao_compra_fornecedor"))
        self.assertEqual(parametros["fornecedor"], [str(fornecedor.id)])
        self.assertEqual(parametros["fornecedor_ciclo"], [str(fornecedor.id)])
        self.assertEqual(parametros["data_visita"], [self.data_base.isoformat()])

    def test_visita_de_ontem_sem_lista_gera_preparar_lista_atrasado(self):
        ontem = self.data_base - timedelta(days=1)
        self.criar_fornecedor(referencia=ontem)

        aviso = self.unico_aviso()
        self.assertEqual(aviso["data_visita"], ontem.isoformat())
        self.assertEqual(aviso["dias_para_visita"], -1)
        self.assertEqual(aviso["estado"], ESTADO_PREPARAR_LISTA)

    def test_visita_de_ontem_com_lista_sem_confirmacao_gera_falta_enviar(self):
        ontem = self.data_base - timedelta(days=1)
        fornecedor = self.criar_fornecedor(referencia=ontem)
        lista = self.criar_lista(fornecedor, data_visita=ontem)

        aviso = self.unico_aviso()
        self.assertEqual(aviso["data_visita"], ontem.isoformat())
        self.assertEqual(aviso["lista_id"], lista.id)
        self.assertEqual(aviso["estado"], ESTADO_LISTA_PREPARADA_FALTA_ENVIAR)

    def test_visita_de_ontem_confirmada_nao_reaparece(self):
        ontem = self.data_base - timedelta(days=1)
        fornecedor = self.criar_fornecedor(referencia=ontem, intervalo=14)
        lista = self.criar_lista(fornecedor, data_visita=ontem)
        self.confirmar_envio(lista)

        self.assertEqual(obter_avisos_visitas_fornecedores(self.data_base), [])

    def test_nao_gera_varios_ciclos_historicos_atrasados_para_mesmo_fornecedor(self):
        referencia_antiga = self.data_base - timedelta(days=28)
        self.criar_fornecedor(referencia=referencia_antiga)

        avisos = obter_avisos_visitas_fornecedores(self.data_base)

        self.assertEqual(len(avisos), 1)
        self.assertEqual(avisos[0]["data_visita"], self.data_base.isoformat())
        self.assertNotEqual(avisos[0]["data_visita"], referencia_antiga.isoformat())

    def test_ciclo_atrasado_respeita_configuracao_atual_da_frequencia(self):
        fornecedor = self.criar_fornecedor(referencia=self.data_base - timedelta(days=1))
        fornecedor.frequencia_visita_data_referencia = self.data_base
        fornecedor.frequencia_visita_dia_semana = self.data_base.weekday()
        fornecedor.save()

        aviso = self.unico_aviso()
        self.assertEqual(aviso["data_visita"], self.data_base.isoformat())
        self.assertEqual(aviso["dias_para_visita"], 0)

    def test_rota_enviar_aponta_para_lista_correta(self):
        fornecedor = self.criar_fornecedor()
        lista = self.criar_lista(fornecedor)

        aviso = self.unico_aviso()
        self.assertEqual(aviso["acao"]["tipo"], "enviar_ao_vendedor")
        self.assertEqual(
            aviso["acao"]["url"],
            reverse("estoque:compras_lista_fornecedor_whatsapp", kwargs={"pk": lista.id}),
        )

    def test_ordenacao_por_urgencia_e_deterministica(self):
        atrasado = self.criar_fornecedor(nome="Fornecedor Atrasado", referencia=self.data_base - timedelta(days=7), intervalo=14)
        hoje = self.criar_fornecedor(nome="Fornecedor Hoje")
        amanha = self.criar_fornecedor(nome="Fornecedor Amanha", referencia=self.data_base + timedelta(days=1))

        avisos = obter_avisos_visitas_fornecedores(self.data_base)

        self.assertEqual(
            [aviso["fornecedor_id"] for aviso in avisos],
            [atrasado.id, hoje.id, amanha.id],
        )

    def test_data_referencia_explicita_independe_do_dia_atual(self):
        referencia = date(2026, 8, 3)
        self.criar_fornecedor(referencia=referencia)

        aviso = self.unico_aviso(referencia)
        self.assertEqual(aviso["data_visita"], referencia.isoformat())

    def test_ausencia_total_de_fornecedores_retorna_lista_vazia(self):
        self.assertEqual(obter_avisos_visitas_fornecedores(self.data_base), [])

    def test_servico_nao_altera_dados_no_banco(self):
        fornecedor = self.criar_fornecedor()
        lista = self.criar_lista(fornecedor)
        estado_fornecedor = Fornecedor.objects.values("atualizado_em").get(pk=fornecedor.pk)
        estado_lista = ListaCompraFornecedor.objects.values("atualizado_em").get(pk=lista.pk)

        obter_avisos_visitas_fornecedores(self.data_base)

        self.assertEqual(Fornecedor.objects.values("atualizado_em").get(pk=fornecedor.pk), estado_fornecedor)
        self.assertEqual(ListaCompraFornecedor.objects.values("atualizado_em").get(pk=lista.pk), estado_lista)
        self.assertEqual(EnvioListaCompraFornecedor.objects.count(), 0)

    def test_data_local_padrao_nao_desloca_data_da_visita(self):
        referencia = date(2026, 7, 16)
        self.criar_fornecedor(referencia=referencia)

        with patch("estoque.services.avisos_fornecedores.timezone.localdate", return_value=referencia):
            avisos = obter_avisos_visitas_fornecedores()

        self.assertEqual(len(avisos), 1)
        aviso = avisos[0]
        self.assertEqual(aviso["data_visita"], referencia.isoformat())

    def test_painel_vendas_nao_exibe_visita_em_dois_dias(self):
        aviso = {
            "fornecedor_id": 1,
            "fornecedor_nome": "Fornecedor Futuro",
            "data_visita": (self.data_base + timedelta(days=2)).isoformat(),
            "dias_para_visita": 2,
            "estado": ESTADO_PREPARAR_LISTA,
            "prioridade": 3,
            "titulo": "Visita em 2 dias",
            "mensagem": "Prepare a lista para a visita em 2 dias.",
            "lista_id": None,
            "lista_status": None,
            "tem_envio_confirmado": False,
            "acao": {
                "tipo": "preparar_lista",
                "url": "/compras/sugestao-fornecedor/",
            },
        }

        avisos_painel = views._avisos_visitas_painel_vendas([aviso])

        self.assertEqual(avisos_painel, [])
        self.assertEqual(views._prioridade_pendencias_vendas(avisos_painel), "baixa")

    def test_painel_vendas_prioriza_reenvio_como_alta(self):
        aviso = {
            "fornecedor_id": 1,
            "fornecedor_nome": "Fornecedor Reenvio",
            "data_visita": self.data_base.isoformat(),
            "dias_para_visita": 2,
            "estado": ESTADO_LISTA_ALTERADA_FALTA_REENVIAR,
            "prioridade": 3,
            "titulo": "Lista alterada",
            "mensagem": "Lista alterada depois do envio, falta reenviar ao vendedor.",
            "lista_id": 1,
            "lista_status": ListaCompraFornecedor.STATUS_ABERTA,
            "tem_envio_confirmado": False,
            "acao": {
                "tipo": "enviar_ao_vendedor",
                "url": "/compras/listas-fornecedor/1/whatsapp/",
            },
        }

        avisos_painel = views._avisos_visitas_painel_vendas([aviso])

        self.assertEqual(avisos_painel, [aviso])
        self.assertEqual(views._prioridade_pendencias_vendas(avisos_painel), "alta")


class ResolucaoVisitaFornecedorViewTests(TestCase):
    def setUp(self):
        self.hoje = date(2026, 7, 15)
        self.data_original = self.hoje - timedelta(days=1)
        self.usuario = get_user_model().objects.create_user(
            username="usuario_resolve_visita",
            password="senha-teste",
        )
        self.fornecedor = Fornecedor.objects.create(
            nome="Fornecedor Visita Atrasada",
            ativo=True,
            frequencia_visita_ativa=True,
            frequencia_visita_intervalo_dias=14,
            frequencia_visita_dia_semana=self.data_original.weekday(),
            frequencia_visita_data_referencia=self.data_original,
        )
        self.url = reverse(
            "estoque:resolver_visita_fornecedor_atrasada"
        )

    def dados(self, **alteracoes):
        dados = {
            "fornecedor_id": str(self.fornecedor.id),
            "data_visita_original": self.data_original.isoformat(),
            "tipo_resolucao": ResolucaoVisitaFornecedor.TIPO_NAO_OCORREU,
            "nova_data_visita": "",
            "observacao": "Representante nao compareceu.",
        }
        dados.update(alteracoes)
        return dados

    def postar(self, **alteracoes):
        self.client.force_login(self.usuario)
        with patch(
            "estoque.views.timezone.localdate",
            return_value=self.hoje,
        ):
            return self.client.post(
                self.url,
                self.dados(**alteracoes),
                secure=True,
            )

    def test_exige_usuario_autenticado(self):
        with patch(
            "estoque.views.timezone.localdate",
            return_value=self.hoje,
        ):
            resposta = self.client.post(
                self.url,
                self.dados(),
                secure=True,
            )

        self.assertEqual(resposta.status_code, 403)
        self.assertEqual(
            ResolucaoVisitaFornecedor.objects.count(),
            0,
        )

    def test_registra_visita_nao_ocorrida_com_usuario(self):
        resposta = self.postar()

        self.assertRedirects(
            resposta,
            reverse("estoque:vendas"),
            fetch_redirect_response=False,
        )
        resolucao = ResolucaoVisitaFornecedor.objects.get()
        self.assertEqual(
            resolucao.tipo_resolucao,
            ResolucaoVisitaFornecedor.TIPO_NAO_OCORREU,
        )
        self.assertEqual(resolucao.responsavel, self.usuario)
        self.assertEqual(
            resolucao.observacao,
            "Representante nao compareceu.",
        )

    def test_visita_adiada_exige_nova_data(self):
        resposta = self.postar(
            tipo_resolucao=ResolucaoVisitaFornecedor.TIPO_ADIADA,
            nova_data_visita="",
        )

        self.assertRedirects(
            resposta,
            reverse("estoque:vendas"),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            ResolucaoVisitaFornecedor.objects.count(),
            0,
        )

    def test_visita_adiada_registra_nova_data(self):
        nova_data = self.hoje + timedelta(days=3)

        resposta = self.postar(
            tipo_resolucao=ResolucaoVisitaFornecedor.TIPO_ADIADA,
            nova_data_visita=nova_data.isoformat(),
        )

        self.assertRedirects(
            resposta,
            reverse("estoque:vendas"),
            fetch_redirect_response=False,
        )
        resolucao = ResolucaoVisitaFornecedor.objects.get()
        self.assertEqual(
            resolucao.tipo_resolucao,
            ResolucaoVisitaFornecedor.TIPO_ADIADA,
        )
        self.assertEqual(
            resolucao.nova_data_visita,
            nova_data,
        )

    def test_nao_resolve_visita_de_hoje(self):
        resposta = self.postar(
            data_visita_original=self.hoje.isoformat(),
        )

        self.assertRedirects(
            resposta,
            reverse("estoque:vendas"),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            ResolucaoVisitaFornecedor.objects.count(),
            0,
        )

    def test_repeticao_identica_nao_cria_duplicidade(self):
        self.postar()
        segunda_resposta = self.postar()

        self.assertRedirects(
            segunda_resposta,
            reverse("estoque:vendas"),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            ResolucaoVisitaFornecedor.objects.count(),
            1,
        )

    def test_resolucao_diferente_nao_substitui_historico(self):
        self.postar()
        nova_data = self.hoje + timedelta(days=2)

        segunda_resposta = self.postar(
            tipo_resolucao=ResolucaoVisitaFornecedor.TIPO_ADIADA,
            nova_data_visita=nova_data.isoformat(),
        )

        self.assertRedirects(
            segunda_resposta,
            reverse("estoque:vendas"),
            fetch_redirect_response=False,
        )
        resolucao = ResolucaoVisitaFornecedor.objects.get()
        self.assertEqual(
            resolucao.tipo_resolucao,
            ResolucaoVisitaFornecedor.TIPO_NAO_OCORREU,
        )
        self.assertIsNone(resolucao.nova_data_visita)

    def _aviso_tela(self, dias_para_visita=-1):
        return {
            "fornecedor_id": self.fornecedor.id,
            "fornecedor_nome": self.fornecedor.nome,
            "data_visita": self.data_original.isoformat(),
            "dias_para_visita": dias_para_visita,
            "estado": ESTADO_PREPARAR_LISTA,
            "prioridade": 0,
            "titulo": "Visita atrasada",
            "mensagem": "Visita atrasada.",
            "lista_id": None,
            "lista_status": None,
            "tem_envio_confirmado": False,
            "acao": {
                "tipo": "preparar_lista",
                "url": "/compras/sugestao-fornecedor/",
            },
        }

    def test_painel_mostra_formulario_nas_visitas_atrasadas(self):
        self.client.force_login(self.usuario)

        with patch(
            "estoque.views.obter_avisos_visitas_fornecedores",
            return_value=[self._aviso_tela()],
        ):
            resposta = self.client.get(
                reverse("estoque:vendas"),
                secure=True,
            )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Resolver visita atrasada")
        self.assertContains(
            resposta,
            reverse("estoque:resolver_visita_fornecedor_atrasada"),
        )
        self.assertContains(
            resposta,
            f'value="{self.fornecedor.id}"',
        )
        self.assertContains(
            resposta,
            f'value="{self.data_original.isoformat()}"',
        )
        self.assertContains(resposta, 'value="nao_ocorreu"')
        self.assertContains(resposta, 'value="adiada"')
        self.assertContains(resposta, 'value="ignorar_ciclo"')
        self.assertContains(resposta, 'name="nova_data_visita"')
        self.assertContains(resposta, 'name="observacao"')
        self.assertContains(
            resposta,
            'painel.addEventListener("click"',
        )
        self.assertContains(
            resposta,
            'painel.addEventListener("submit"',
        )
        self.assertContains(
            resposta,
            "__ignorarValidacaoOperadorVenda",
        )
        self.assertContains(
            resposta,
            "data-registrar-resolucao-visita",
        )
        self.assertContains(
            resposta,
            "HTMLFormElement.prototype.submit.call(formulario)",
        )
        self.assertContains(
            resposta,
            "stopImmediatePropagation",
        )
        self.assertContains(resposta, "Visita não ocorreu")
        self.assertContains(resposta, "Motivo ou observação")
        self.assertContains(resposta, "Registrar resolução")

    def test_painel_nao_mostra_resolucao_para_visita_de_hoje(self):
        self.client.force_login(self.usuario)

        with patch(
            "estoque.views.obter_avisos_visitas_fornecedores",
            return_value=[self._aviso_tela(dias_para_visita=0)],
        ):
            resposta = self.client.get(
                reverse("estoque:vendas"),
                secure=True,
            )

        self.assertEqual(resposta.status_code, 200)
        self.assertNotContains(
            resposta,
            "Resolver visita atrasada",
        )
        self.assertNotContains(
            resposta,
            'name="tipo_resolucao"',
        )

    def test_tipo_invalido_nao_cria_resolucao(self):
        resposta = self.postar(
            tipo_resolucao="tipo_inexistente",
        )

        self.assertRedirects(
            resposta,
            reverse("estoque:vendas"),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            ResolucaoVisitaFornecedor.objects.count(),
            0,
        )


class FechamentoCompraFinanceiroTests(TestCase):
    def setUp(self):
        self.fornecedor = Fornecedor.objects.create(nome="Fornecedor Teste")
        self.produto = Produto.objects.create(
            nome="Produto Compra",
            preco_compra=Decimal("100.00"),
            preco_vista=Decimal("150.00"),
            preco_prazo=Decimal("160.00"),
            quantidade=Decimal("10.000"),
        )
        self.url = "/estoque/compras/nova/"

    def dados(self, **alteracoes):
        dados = {
            "fechamento_token": "a" * 32,
            "fornecedor_id": str(self.fornecedor.id),
            "data_compra": "2026-06-19",
            "tipo_pagamento": "pix",
            "produto_id[]": [str(self.produto.id)],
            "quantidade[]": ["1"],
            "unidade[]": ["UN"],
            "preco_unitario[]": ["1.000,00"],
            "observacao_item[]": [""],
            "origem_caixa": "600,00",
            "origem_reserva": "200,00",
            "origem_banco": "200,00",
        }
        dados.update(alteracoes)
        return dados

    def _criar_lista_fornecedor_conferida(self, quantidade=Decimal("1.000"), total=Decimal("100.00")):
        lista = ListaCompraFornecedor.objects.create(
            fornecedor=self.fornecedor,
            data_lista=timezone.localdate(),
            data_inicio_periodo=timezone.localdate(),
            data_fim_periodo=timezone.localdate(),
        )
        ItemListaCompraFornecedor.objects.create(
            lista=lista,
            produto=self.produto,
            estoque_atual=self.produto.quantidade,
            estoque_minimo=Decimal("0.000"),
            quantidade_final=quantidade,
            quantidade_recebida=quantidade,
            unidade="UN",
            preco_compra=(total / quantidade).quantize(Decimal("0.01")),
            total=total,
            status_conferencia=ItemListaCompraFornecedor.STATUS_CONFERENCIA_OK,
            conferido=True,
        )
        return lista

    def _gerar_compra_da_lista(self, lista):
        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_gerar_compra", kwargs={"pk": lista.pk}),
            secure=True,
        )
        compra = Compra.objects.get(observacao__icontains=f"Lista de Compras #{lista.id}")
        return resposta, compra

    def _dados_finalizacao_compra_lista(self, compra, tipo_pagamento="avista", **alteracoes):
        data_vencimento = "2026-07-16" if views._compra_pagamento_a_prazo(tipo_pagamento) else ""
        dados = {
            "acao_compra": "confirmar_financeiro",
            "fornecedor_id": str(self.fornecedor.id),
            "data_compra": timezone.localdate().isoformat(),
            "tipo_pagamento": tipo_pagamento,
            "data_vencimento": data_vencimento,
            "observacao": compra.observacao or "",
            "produto_id[]": [str(self.produto.id)],
            "quantidade[]": ["1"],
            "unidade[]": ["UN"],
            "preco_unitario[]": ["100,00"],
            "observacao_item[]": [""],
            "origem_caixa": "100,00",
            "origem_reserva": "0,00",
            "origem_banco": "0,00",
        }
        dados.update(alteracoes)
        return dados

    def _criar_compra_para_alerta_rascunho(self, **campos):
        dados = {
            "fornecedor": self.fornecedor,
            "data_compra": timezone.localdate(),
            "tipo_pagamento": "avista",
            "total": Decimal("100.00"),
            "status": Compra.STATUS_RASCUNHO,
            "cancelada": False,
        }
        dados.update(campos)
        return Compra.objects.create(**dados)

    def _criar_compra_rascunho_com_item(self, tipo_pagamento="avista", total=Decimal("100.00"), **campos):
        compra = Compra.objects.create(
            fornecedor=self.fornecedor,
            data_compra=timezone.localdate(),
            tipo_pagamento=tipo_pagamento,
            total=total,
            status=Compra.STATUS_RASCUNHO,
            **campos,
        )
        ItemCompra.objects.create(
            compra=compra,
            produto=self.produto,
            quantidade=Decimal("1.000"),
            unidade="UN",
            preco_unitario=total,
            valor_total=total,
        )
        return compra

    def _avisos_visitas_vendas(self, quantidade=4):
        base = [
            ("Fornecedor Atrasado", "2026-07-14", -1, "preparar_lista", "Visita atrasada: lista ainda nao preparada.", "/compras/sugestao-fornecedor/?fornecedor=1&data_visita=2026-07-14"),
            ("Fornecedor Hoje", "2026-07-15", 0, "preparar_lista", "Visita prevista para hoje: prepare a lista.", "/compras/sugestao-fornecedor/?fornecedor=2&data_visita=2026-07-15"),
            ("Fornecedor Amanha", "2026-07-16", 1, "preparar_lista", "Prepare a lista para a visita de amanha.", "/compras/sugestao-fornecedor/?fornecedor=3&data_visita=2026-07-16"),
            ("Fornecedor Futuro", "2026-07-18", 3, "lista_preparada_falta_enviar", "Lista preparada, falta enviar ao vendedor.", "/compras/listas-fornecedor/10/whatsapp/"),
            ("Fornecedor Dois Dias", "2026-07-17", 2, "preparar_lista", "Prepare a lista para a visita em 2 dias.", "/compras/sugestao-fornecedor/?fornecedor=5&data_visita=2026-07-17"),
            ("Fornecedor Seis Dias", "2026-07-21", 6, "preparar_lista", "Prepare a lista para a visita em 6 dias.", "/compras/sugestao-fornecedor/?fornecedor=6&data_visita=2026-07-21"),
            ("Fornecedor Sete Dias", "2026-07-22", 7, "preparar_lista", "Prepare a lista para a visita em 7 dias.", "/compras/sugestao-fornecedor/?fornecedor=7&data_visita=2026-07-22"),
        ]
        avisos = []
        for indice, (nome, data_visita, dias, estado, mensagem, url) in enumerate(base[:quantidade], start=1):
            avisos.append({
                "fornecedor_id": indice,
                "fornecedor_nome": nome,
                "data_visita": data_visita,
                "dias_para_visita": dias,
                "estado": estado,
                "prioridade": 0 if dias < 0 else indice,
                "titulo": "Lista preparada" if estado == "lista_preparada_falta_enviar" else "Visita",
                "mensagem": mensagem,
                "lista_id": 10 if estado == "lista_preparada_falta_enviar" else None,
                "lista_status": ListaCompraFornecedor.STATUS_ABERTA if estado == "lista_preparada_falta_enviar" else None,
                "tem_envio_confirmado": False,
                "acao": {"tipo": "enviar_ao_vendedor" if estado == "lista_preparada_falta_enviar" else "preparar_lista", "url": url},
            })
        return avisos

    def test_vendas_nao_exibe_aviso_compras_rascunho_sem_pendencia(self):
        resposta = self.client.get(reverse("estoque:vendas"), secure=True)

        self.assertNotContains(resposta, "compra em rascunho aguardando")
        self.assertNotContains(resposta, "Continuar compra")

    def test_vendas_exibe_aviso_para_uma_compra_rascunho(self):
        compra = self._criar_compra_para_alerta_rascunho()

        resposta = self.client.get(reverse("estoque:vendas"), secure=True)

        self.assertContains(resposta, 'id="vendasPendenciasLateral"')
        self.assertContains(resposta, "1 compra em rascunho aguardando")
        self.assertContains(resposta, "Continuar compra")
        self.assertContains(resposta, reverse("estoque:compra_editar", kwargs={"pk": compra.pk}))

    def test_vendas_exibe_contador_para_multiplas_compras_rascunho(self):
        self._criar_compra_para_alerta_rascunho()
        self._criar_compra_para_alerta_rascunho()

        resposta = self.client.get(reverse("estoque:vendas"), secure=True)

        self.assertContains(resposta, "2 compras em rascunho aguardando")
        self.assertContains(resposta, "Ver compras")
        self.assertContains(resposta, reverse("estoque:compras_lista"))
        self.assertNotContains(resposta, "Continuar compra")

    def test_vendas_ignora_compras_finalizadas_e_canceladas_no_aviso_rascunho(self):
        self._criar_compra_para_alerta_rascunho(status=Compra.STATUS_FINALIZADA)
        self._criar_compra_para_alerta_rascunho(status=Compra.STATUS_RASCUNHO, cancelada=True)
        self._criar_compra_para_alerta_rascunho(status=Compra.STATUS_CANCELADA, cancelada=True)

        resposta = self.client.get(reverse("estoque:vendas"), secure=True)

        self.assertNotContains(resposta, "compra em rascunho aguardando")
        self.assertNotContains(resposta, "Continuar compra")

    def test_vendas_exibe_avisos_visitas_fornecedores_do_servico(self):
        avisos = self._avisos_visitas_vendas()
        self.produto.cadastro_incompleto = True
        self.produto.save(update_fields=["cadastro_incompleto"])
        compra = self._criar_compra_para_alerta_rascunho()

        with patch("estoque.views.obter_avisos_visitas_fornecedores", return_value=avisos) as obter_avisos:
            resposta = self.client.get(reverse("estoque:vendas"), secure=True)
        conteudo = resposta.content.decode()

        obter_avisos.assert_called_once_with()
        self.assertEqual(resposta.context["avisos_visitas_fornecedores"], avisos)
        self.assertEqual(resposta.context["pendencias_vendas_qtd"], 6)
        self.assertEqual(resposta.context["pendencias_vendas_prioridade"], "critica")
        self.assertContains(resposta, 'id="vendasPendenciasLateral"')
        self.assertContains(resposta, 'class="vendas-pendencias-lateral prioridade-critica pulsar"')
        self.assertContains(resposta, 'id="btnVendasPendencias"')
        self.assertContains(resposta, 'aria-controls="vendasPendenciasPainel"')
        self.assertContains(resposta, '<span class="vendas-pendencias-toggle-texto">Pendencias</span>')
        self.assertContains(resposta, 'class="vendas-pendencias-badge" aria-label="6 pendencias">6</span>')
        self.assertContains(resposta, 'id="vendasPendenciasPainel" hidden')
        self.assertContains(resposta, 'id="btnFecharVendasPendencias"')
        self.assertContains(resposta, 'class="vendas-pendencias-contador">6</span>')
        self.assertContains(resposta, "vendas-pendencias-item-corpo")
        self.assertContains(resposta, "vendas-pendencias-item-rodape")
        indice_hoje = conteudo.index("Fornecedor Hoje")
        indice_falta_enviar = conteudo.index("Fornecedor Futuro")
        indice_amanha = conteudo.index("Fornecedor Amanha")
        indice_atrasado = conteudo.index("Fornecedor Atrasado")
        self.assertLess(indice_hoje, indice_falta_enviar)
        self.assertLess(indice_falta_enviar, indice_amanha)
        self.assertLess(indice_amanha, indice_atrasado)
        self.assertContains(resposta, "Visita atrasada.")
        self.assertContains(resposta, "Ultimo dia: prepare e envie a lista hoje.")
        self.assertContains(resposta, "Visita amanha.")
        self.assertContains(resposta, "Hoje</span>")
        self.assertContains(resposta, "Falta enviar</span>")
        self.assertContains(resposta, "Amanha</span>")
        self.assertContains(resposta, "Atrasada</span>")
        self.assertContains(resposta, "Preparar lista")
        self.assertContains(resposta, "Lista preparada, falta enviar ao vendedor.")
        self.assertContains(resposta, "Enviar ao vendedor")
        self.assertContains(resposta, "Produtos incompletos")
        self.assertContains(resposta, "prioridade-cadastro")
        self.assertContains(resposta, "1 produto com cadastro incompleto.")
        self.assertContains(resposta, "Compras em rascunho")
        self.assertContains(resposta, "prioridade-rascunho")
        self.assertContains(resposta, "1 compra")
        self.assertContains(resposta, 'href="?incompletos=1#vendas-produtos-incompletos"')
        self.assertContains(resposta, reverse("estoque:compra_editar", kwargs={"pk": compra.pk}))
        self.assertContains(resposta, 'href="/compras/listas-fornecedor/10/whatsapp/"')
        self.assertEqual(conteudo.count("data-pendencia-fornecedor"), 4)
        self.assertNotIn("vendas-aviso-visita-card", conteudo)
        self.assertNotIn("vendas-aviso-visita-rotativo", conteudo)
        self.assertNotIn("btnAvisoVisitaAnterior", conteudo)
        self.assertNotIn("btnAvisoVisitaProximo", conteudo)
        self.assertNotIn("setInterval(function()", conteudo)
        self.assertContains(resposta, ":focus-visible")
        self.assertContains(resposta, "prefers-reduced-motion: reduce")
        self.assertContains(resposta, "animation: none")
        self.assertContains(resposta, 'data-pulso-ms="7000"')
        self.assertContains(resposta, "window.setTimeout(pararPulso, pulsoMs)")
        self.assertContains(resposta, 'event.key === "Escape"')
        self.assertContains(resposta, "backdrop?.addEventListener")
        self.assertContains(resposta, "max-width: calc(100vw - 12px)")
        self.assertContains(resposta, "width: 36px;")
        self.assertContains(resposta, "min-height: 102px;")
        self.assertContains(resposta, "background: linear-gradient(180deg, #2563eb 0%, #1e40af 100%);")
        self.assertContains(resposta, "width: min(330px, calc(100vw - 48px));")
        self.assertContains(resposta, "background: linear-gradient(180deg, #ffffff 0%, #f8fafc 100%);")
        self.assertContains(resposta, "justify-self: end;")
        self.assertContains(resposta, "grid-template-columns: minmax(0, 1fr) auto;")
        self.assertContains(resposta, "white-space: nowrap;")
        self.assertContains(resposta, "white-space: normal;")
        self.assertContains(resposta, 'content: ">";')
        self.assertContains(resposta, "background: #f8fbff;")
        self.assertContains(resposta, "background: #fff3e8;")
        self.assertContains(resposta, "background: #eff6ff;")
        self.assertContains(resposta, "border-bottom: 1px solid #e2e8f0;")
        self.assertContains(resposta, "width: 5px;")
        self.assertContains(resposta, "box-shadow: 0 20px 46px rgba(15, 23, 42, .24);")
        self.assertContains(resposta, "0 0 0 4px rgba(59, 130, 246, .10)")
        self.assertContains(resposta, "prioridade-cadastro .vendas-pendencias-rotulo")
        self.assertContains(resposta, "prioridade-rascunho .vendas-pendencias-item-acao")
        self.assertContains(resposta, 'id="atalhosVenda"')
        self.assertNotContains(resposta, "Pendência 1 de")

    def test_vendas_nao_exibe_bloco_visitas_sem_avisos(self):
        with patch("estoque.views.obter_avisos_visitas_fornecedores", return_value=[]):
            resposta = self.client.get(reverse("estoque:vendas"), secure=True)

        self.assertEqual(resposta.context["avisos_visitas_fornecedores"], [])
        self.assertEqual(resposta.context["pendencias_vendas_qtd"], 0)
        self.assertNotContains(resposta, 'id="vendasPendenciasLateral"')

    def test_vendas_filtra_avisos_futuros_distantes_do_painel(self):
        avisos = self._avisos_visitas_vendas(quantidade=7)

        with patch("estoque.views.obter_avisos_visitas_fornecedores", return_value=avisos):
            resposta = self.client.get(reverse("estoque:vendas"), secure=True)
        conteudo = resposta.content.decode()

        self.assertEqual(resposta.context["avisos_visitas_fornecedores"], avisos)
        self.assertEqual(len(resposta.context["avisos_visitas_fornecedores_painel"]), 4)
        self.assertEqual(resposta.context["pendencias_vendas_qtd"], 4)
        self.assertContains(resposta, 'class="vendas-pendencias-badge" aria-label="4 pendencias">4</span>')
        self.assertContains(resposta, "Fornecedor Amanha")
        self.assertNotIn("Fornecedor Dois Dias", conteudo)
        self.assertNotIn("Fornecedor Seis Dias", conteudo)
        self.assertNotIn("Fornecedor Sete Dias", conteudo)
        self.assertNotIn("Visita em 2 dias", conteudo)
        self.assertNotIn("Visita em 6 dias", conteudo)
        self.assertNotIn("Visita em 7 dias", conteudo)

    def test_vendas_avisos_distantes_sozinhos_nao_ativam_aba_nem_pulso(self):
        avisos = self._avisos_visitas_vendas(quantidade=7)[4:]

        with patch("estoque.views.obter_avisos_visitas_fornecedores", return_value=avisos):
            resposta = self.client.get(reverse("estoque:vendas"), secure=True)

        self.assertEqual(resposta.context["avisos_visitas_fornecedores"], avisos)
        self.assertEqual(resposta.context["avisos_visitas_fornecedores_painel"], [])
        self.assertEqual(resposta.context["pendencias_vendas_qtd"], 0)
        self.assertNotContains(resposta, 'id="vendasPendenciasLateral"')
        self.assertNotContains(resposta, "pulsar")

    def test_vendas_um_aviso_exibe_aba_sem_controles_de_rotacao(self):
        avisos = self._avisos_visitas_vendas(quantidade=1)

        with patch("estoque.views.obter_avisos_visitas_fornecedores", return_value=avisos):
            resposta = self.client.get(reverse("estoque:vendas"), secure=True)

        self.assertContains(resposta, 'class="vendas-pendencias-badge" aria-label="1 pendencia">1</span>')
        self.assertContains(resposta, "Fornecedor Atrasado")
        self.assertNotContains(resposta, 'id="btnAvisoVisitaAnterior"')
        self.assertNotContains(resposta, 'id="btnAvisoVisitaProximo"')

    def test_vendas_prioridade_alta_para_lista_preparada(self):
        avisos = [self._avisos_visitas_vendas()[3]]

        with patch("estoque.views.obter_avisos_visitas_fornecedores", return_value=avisos):
            resposta = self.client.get(reverse("estoque:vendas"), secure=True)

        self.assertEqual(resposta.context["pendencias_vendas_prioridade"], "alta")
        self.assertContains(resposta, 'class="vendas-pendencias-lateral prioridade-alta pulsar"')
        self.assertContains(resposta, "Lista preparada, falta enviar ao vendedor.")

    def test_vendas_prioridade_media_para_visita_amanha(self):
        avisos = [self._avisos_visitas_vendas()[2]]

        with patch("estoque.views.obter_avisos_visitas_fornecedores", return_value=avisos):
            resposta = self.client.get(reverse("estoque:vendas"), secure=True)

        self.assertEqual(resposta.context["pendencias_vendas_prioridade"], "media")
        self.assertContains(resposta, 'class="vendas-pendencias-lateral prioridade-media"')
        self.assertContains(resposta, "Visita amanha.")

    def test_vendas_atraso_usa_prioridade_baixa(self):
        avisos = [self._avisos_visitas_vendas()[0]]

        with patch("estoque.views.obter_avisos_visitas_fornecedores", return_value=avisos):
            resposta = self.client.get(reverse("estoque:vendas"), secure=True)

        self.assertEqual(resposta.context["pendencias_vendas_prioridade"], "baixa")
        self.assertContains(resposta, 'class="vendas-pendencias-lateral prioridade-baixa"')
        self.assertNotContains(resposta, "prioridade-critica pulsar")

    def test_vendas_escapa_nome_fornecedor_no_aviso_visita(self):
        avisos = self._avisos_visitas_vendas(quantidade=1)
        avisos[0]["fornecedor_nome"] = 'Fornecedor <script>alert("x")</script>'

        with patch("estoque.views.obter_avisos_visitas_fornecedores", return_value=avisos):
            resposta = self.client.get(reverse("estoque:vendas"), secure=True)

        self.assertContains(resposta, "Fornecedor &lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;")
        self.assertNotContains(resposta, '<script>alert("x")</script>')

    def test_sugestao_fornecedor_ciclo_preenche_chegada_e_prepara_foco_periodo(self):
        data_visita = timezone.localdate()
        with patch("estoque.views.data_ciclo_visita_valida", return_value=True):
            resposta = self.client.get(
                reverse("estoque:sugestao_compra_fornecedor"),
                {
                    "fornecedor": str(self.fornecedor.id),
                    "fornecedor_ciclo": str(self.fornecedor.id),
                    "data_visita": data_visita.isoformat(),
                },
                secure=True,
            )

        self.assertContains(resposta, f'id="data_chegada" value="{data_visita.isoformat()}"')
        self.assertContains(resposta, 'const abrirPorCicloFornecedor = !modoEdicaoLista && params.has("fornecedor_ciclo") && params.has("data_visita");')
        self.assertContains(resposta, "periodo.focus();")
        self.assertContains(resposta, "periodo.select();")

    def test_sugestao_fornecedor_criacao_manual_continua_sem_ciclo(self):
        resposta = self.client.get(reverse("estoque:sugestao_compra_fornecedor"), secure=True)

        self.assertContains(resposta, 'id="fornecedorCicloLista" value=""')
        self.assertContains(resposta, 'id="dataVisitaFornecedor" value=""')

    def test_home_e_compras_lista_exibem_aviso_compras_rascunho(self):
        compra = self._criar_compra_para_alerta_rascunho()

        resposta_home = self.client.get(reverse("estoque:home"), secure=True)
        resposta_compras = self.client.get(reverse("estoque:compras_lista"), secure=True)

        self.assertContains(resposta_home, "Compras em rascunho")
        self.assertContains(resposta_home, "1 compra em rascunho aguardando finalização")
        self.assertContains(resposta_home, reverse("estoque:compra_editar", kwargs={"pk": compra.pk}))
        self.assertContains(resposta_compras, "1 compra em rascunho aguardando finalização")
        self.assertContains(resposta_compras, "Continuar compra")

    def test_nova_compra_exibe_apenas_tipos_pagamento_simplificados(self):
        resposta = self.client.get(self.url, secure=True)

        self.assertContains(resposta, '<option value="">Selecione...</option>')
        self.assertContains(resposta, '<option value="avista"')
        self.assertContains(resposta, "A vista (Dinheiro / Pix)")
        self.assertContains(resposta, '<option value="aprazo" selected>A prazo</option>')
        self.assertContains(resposta, '<option value="cartao_credito"')
        self.assertContains(resposta, "Cartao credito")
        self.assertContains(resposta, '<option value="cartao_debito"')
        self.assertContains(resposta, "Cartao debito")
        for valor_antigo in ["pix", "dinheiro", "banco", "boleto", "cartao"]:
            self.assertNotContains(resposta, f'<option value="{valor_antigo}">')

    def test_nova_compra_exibe_produto_rapido_botoes_topo_e_hooks(self):
        resposta = self.client.get(reverse("estoque:compras_nova"), secure=True)
        html = resposta.content.decode()
        topo = re.search(r'<div class="compras-actions">(.*?)</div>', html, re.S).group(1)
        bloco = re.search(r'<section class="compras-card produto-rapido-destaque.*?</section>', html, re.S).group(0)
        modal_remover = html.split('id="modalRemoverItemCompra"', 1)[1].split('id="secaoProdutoRapidoCompra"', 1)[0]
        restauracao = html.split("function restaurarRascunhoCompraNova()", 1)[1].split("function limparRascunhoCompraNova()", 1)[0]
        restauracao_itens = restauracao.split('const linhas = Array.from(tbody.querySelectorAll(".linha-item"));', 1)[1]

        self.assertContains(resposta, 'id="secaoProdutoRapidoCompra"')
        self.assertContains(resposta, reverse("estoque:compra_produto_rapido_nova"))
        self.assertIn("Consultar Compras", topo)
        self.assertIn("Nova Compra", topo)
        self.assertIn('id="btnNovaCompraTopo"', topo)
        self.assertNotIn("Contas a pagar", topo)
        self.assertNotIn("Fornecedores", topo)
        self.assertContains(resposta, '<h2 class="compra-atalhos-titulo">Atalhos</h2>')
        self.assertContains(resposta, 'class="compra-atalhos-btn">Fornecedores</a>')
        self.assertLess(bloco.index('id="produtoRapidoNome"'), bloco.index('id="abrirCategoriaProdutoRapido"'))
        self.assertLess(bloco.index('id="abrirCategoriaProdutoRapido"'), bloco.index('id="abrirUnidadeProdutoRapido"'))
        self.assertLess(bloco.index('id="abrirUnidadeProdutoRapido"'), bloco.index('id="produtoRapidoPrecoCompra"'))
        self.assertLess(bloco.index('id="produtoRapidoPrecoCompra"'), bloco.index('id="btnCadastrarProdutoRapidoCompra"'))
        self.assertContains(resposta, 'id="produtoRapidoPrecoCompra" class="campo-moeda-br-produto-rapido" inputmode="decimal"')
        self.assertContains(resposta, 'value="0,00" placeholder="0,00"')
        self.assertContains(resposta, "function normalizarPrecoProdutoRapido()")
        self.assertContains(resposta, "precoProdutoRapido.addEventListener(\"input\", limparEntradaPrecoProdutoRapido)")
        self.assertContains(resposta, "precoProdutoRapido.addEventListener(\"blur\", normalizarPrecoProdutoRapido)")
        self.assertContains(resposta, "window.compraPrecoCampo = precoCampo;")
        self.assertContains(resposta, "function bloquearEnvioProdutoRapido()")
        self.assertContains(resposta, 'formProdutoRapido.dataset.enviando = "1";')
        self.assertContains(resposta, 'formProdutoRapido.dataset.enviando === "1"')
        self.assertContains(resposta, 'btnCadastrarProdutoRapido.disabled = true;')
        self.assertContains(resposta, 'btnCadastrarProdutoRapido.textContent = "Cadastrando...";')
        self.assertContains(resposta, "function liberarEnvioProdutoRapido()")
        self.assertContains(resposta, 'btnCadastrarProdutoRapido.textContent = "Cadastrar Produto Rapido";')
        self.assertContains(resposta, 'window.addEventListener("pageshow", liberarEnvioProdutoRapido);')
        self.assertContains(resposta, 'const chaveRascunhoNovaCompra = "compraNovaRascunhoProdutoRapido";')
        self.assertContains(resposta, 'const chaveRascunhoNovaCompraPendente = "compraNovaRascunhoProdutoRapidoPendente";')
        self.assertContains(resposta, 'sessionStorage.setItem(chaveRascunhoNovaCompraPendente, "1");')
        self.assertContains(resposta, 'sessionStorage.getItem(chaveRascunhoNovaCompraPendente) !== "1"')
        self.assertContains(resposta, 'sessionStorage.removeItem(chaveRascunhoNovaCompra);')
        self.assertContains(resposta, 'sessionStorage.removeItem(chaveRascunhoNovaCompraPendente);')
        self.assertContains(resposta, 'const linhasCompra = Array.from(document.querySelectorAll("#tabelaItensCompra tbody .linha-item"))')
        self.assertContains(resposta, 'concat(Array.from(document.querySelectorAll("#tbodyAdicionarProdutoMobile .linha-item")))')
        self.assertContains(resposta, 'preco_unitario_oculto: precoOculto ? precoOculto.value || "" : ""')
        self.assertContains(resposta, 'input[type="hidden"][name="preco_unitario[]"]')
        self.assertContains(resposta, '#tbodyAdicionarProdutoMobile .linha-item')
        self.assertContains(resposta, 'window.compraFecharSugestoesLinha = fecharSugestoes;')
        self.assertContains(resposta, 'window.compraFecharSugestoesLinha(linha);')
        self.assertContains(resposta, 'linha.classList.remove("linha-item-ativa");')
        self.assertNotIn('campo.dispatchEvent(new Event("input"', restauracao_itens)
        self.assertNotIn('campo.dispatchEvent(new Event("change"', restauracao_itens)
        self.assertContains(resposta, 'window.compraRecalcularAposRestaurar')
        self.assertContains(resposta, 'window.compraLimparRascunhoProdutoRapido')
        self.assertContains(resposta, "compraTemAlteracoesNaoSalvas")
        self.assertContains(resposta, 'return [campo.name, valorCampo(campo)];')
        self.assertContains(resposta, 'name="produto_id[]"')
        self.assertContains(resposta, 'name="quantidade[]"')
        self.assertContains(resposta, 'campo-preco-unitario-compra')
        self.assertContains(resposta, "Iniciar uma nova compra?")
        self.assertContains(resposta, "Descartar e iniciar nova compra")
        self.assertContains(resposta, f'{reverse("estoque:compras_nova")}?descartar=1')
        self.assertContains(resposta, 'const descartarCompraNovaAoAbrir = parametrosProdutoRapido.get("descartar") === "1";')
        self.assertContains(resposta, 'if (descartarCompraNovaAoAbrir) {')
        self.assertContains(resposta, 'function limparCompraDescartadaAoAbrir()')
        self.assertContains(resposta, '!descartarCompraNovaAoAbrir && !continuarItensAoAbrir')
        self.assertContains(resposta, 'fornecedor.value = "";')
        self.assertContains(resposta, 'fornecedorBusca.value = "";')
        self.assertContains(resposta, 'precoOculto.value = "";')
        self.assertContains(resposta, "nova-compra-confirmacao-modal")
        self.assertContains(resposta, "nova-compra-confirmacao-card")
        self.assertNotIn("nova-compra-confirmacao-", modal_remover)
        self.assertContains(resposta, 'btnSalvar.closest(".compras-actions-final")')
        self.assertContains(resposta, "window.compraAtualizarEstadoLimpo = atualizarEstadoLimpoCompra;")

    def test_edicao_compra_rascunho_continua_exibindo_produto_rapido(self):
        compra = self._criar_compra_rascunho_com_item()

        resposta = self.client.get(reverse("estoque:compra_editar", kwargs={"pk": compra.pk}), secure=True)

        self.assertContains(resposta, 'id="secaoProdutoRapidoCompra"')
        self.assertContains(resposta, reverse("estoque:compra_produto_rapido", kwargs={"pk": compra.pk}))
        self.assertContains(resposta, 'id="produtoRapidoNome"')
        self.assertContains(resposta, 'id="btnCadastrarProdutoRapidoCompra"')

    def test_produto_rapido_na_compra_nova_cria_produto(self):
        Categoria.objects.get_or_create(nome="Bebidas", defaults={"ativa": True})
        Unidade.objects.get_or_create(sigla="UN", defaults={"nome": "Unidade", "ativa": True})
        Produto.objects.filter(nome="Produto Rapido Nova").delete()

        resposta = self.client.post(
            reverse("estoque:compra_produto_rapido_nova"),
            {
                "nome": "Produto Rapido Nova",
                "categoria": "Bebidas",
                "unidade": "UN",
                "preco_compra": "12,50",
            },
            secure=True,
        )

        produto = Produto.objects.get(nome="Produto Rapido Nova")
        self.assertRedirects(resposta, reverse("estoque:compras_nova"), fetch_redirect_response=False)
        self.assertEqual(produto.categoria, "Bebidas")
        self.assertEqual(produto.unidade_compra, "UN")
        self.assertEqual(produto.preco_compra, Decimal("12.50"))
        self.assertTrue(produto.cadastro_incompleto)
        self.assertTrue(produto.permitir_prejuizo)

    def test_produto_rapido_na_compra_nova_aceita_moeda_brasileira(self):
        casos = [
            ("Produto Rapido 10", "10,00", Decimal("10.00")),
            ("Produto Rapido 1050", "10,50", Decimal("10.50")),
            ("Produto Rapido 123456", "1.234,56", Decimal("1234.56")),
        ]
        Produto.objects.filter(nome__in=[nome for nome, _, _ in casos]).delete()

        for nome, preco, esperado in casos:
            resposta = self.client.post(
                reverse("estoque:compra_produto_rapido_nova"),
                {
                    "nome": nome,
                    "categoria": "Bebidas",
                    "unidade": "UN",
                    "preco_compra": preco,
                },
                secure=True,
            )

            self.assertRedirects(resposta, reverse("estoque:compras_nova"), fetch_redirect_response=False)
            self.assertEqual(Produto.objects.get(nome=nome).preco_compra, esperado)

    def test_rotulo_pagamento_compra_preserva_valores_antigos(self):
        self.assertEqual(Compra(tipo_pagamento="pix").tipo_pagamento_texto, "Pix")
        self.assertEqual(Compra(tipo_pagamento="cartao").tipo_pagamento_texto, "Cartão")
        self.assertEqual(
            Compra(tipo_pagamento="A vista").tipo_pagamento_texto,
            "À vista (Dinheiro / Pix)",
        )
        self.assertEqual(
            Compra(tipo_pagamento="cartao_debito").tipo_pagamento_texto,
            "Cartão débito",
        )
        self.assertFalse(views._compra_pagamento_imediato("cartao_credito"))
        self.assertTrue(views._compra_pagamento_imediato("cartao_debito"))
        self.assertTrue(views._compra_pagamento_a_prazo("aprazo"))

    def test_cartao_credito_sem_data_fatura_nao_finaliza(self):
        compra = Compra.objects.create(
            fornecedor=self.fornecedor,
            data_compra=timezone.localdate(),
            tipo_pagamento="cartao_credito",
            total=Decimal("100.00"),
            status=Compra.STATUS_RASCUNHO,
        )
        ItemCompra.objects.create(
            compra=compra,
            produto=self.produto,
            quantidade=Decimal("1.000"),
            unidade="UN",
            preco_unitario=Decimal("100.00"),
            valor_total=Decimal("100.00"),
        )
        estoque_antes = self.produto.quantidade

        resposta = self.client.post(
            reverse("estoque:compra_finalizar", kwargs={"pk": compra.pk}),
            self._dados_finalizacao_compra_lista(
                compra,
                tipo_pagamento="cartao_credito",
                data_vencimento="",
                origem_caixa="0,00",
                origem_reserva="0,00",
                origem_banco="0,00",
            ),
            follow=True,
            secure=True,
        )

        compra.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertEqual(compra.status, Compra.STATUS_RASCUNHO)
        self.assertEqual(self.produto.quantidade, estoque_antes)
        self.assertEqual(MovimentoFinanceiro.objects.count(), 0)
        self.assertEqual(ContaPagar.objects.count(), 0)
        self.assertContains(resposta, "Informe a data de vencimento da fatura do cartao antes de finalizar.")

    def test_cartao_credito_com_data_fatura_finaliza_e_cria_conta_pagar(self):
        compra = Compra.objects.create(
            fornecedor=self.fornecedor,
            data_compra=timezone.localdate(),
            tipo_pagamento="cartao_credito",
            total=Decimal("100.00"),
            status=Compra.STATUS_RASCUNHO,
        )
        ItemCompra.objects.create(
            compra=compra,
            produto=self.produto,
            quantidade=Decimal("1.000"),
            unidade="UN",
            preco_unitario=Decimal("100.00"),
            valor_total=Decimal("100.00"),
        )
        estoque_antes = self.produto.quantidade

        resposta = self.client.post(
            reverse("estoque:compra_finalizar", kwargs={"pk": compra.pk}),
            self._dados_finalizacao_compra_lista(
                compra,
                tipo_pagamento="cartao_credito",
                data_vencimento="2026-07-21",
                origem_caixa="0,00",
                origem_reserva="0,00",
                origem_banco="0,00",
            ),
            secure=True,
        )

        compra.refresh_from_db()
        self.produto.refresh_from_db()
        conta = compra.conta_pagar
        self.assertRedirects(resposta, reverse("estoque:compras_lista"), fetch_redirect_response=False)
        self.assertEqual(compra.status, Compra.STATUS_FINALIZADA)
        self.assertEqual(compra.data_vencimento.isoformat(), "2026-07-21")
        self.assertEqual(self.produto.quantidade, estoque_antes + Decimal("1.000"))
        self.assertEqual(conta.data_vencimento.isoformat(), "2026-07-21")
        self.assertEqual(conta.valor_original, Decimal("100.00"))
        self.assertEqual(MovimentoFinanceiro.objects.count(), 0)

    def test_cartao_credito_post_duplo_nao_duplica_estoque_ou_conta_pagar(self):
        compra = Compra.objects.create(
            fornecedor=self.fornecedor,
            data_compra=timezone.localdate(),
            tipo_pagamento="cartao_credito",
            total=Decimal("100.00"),
            status=Compra.STATUS_RASCUNHO,
        )
        ItemCompra.objects.create(
            compra=compra,
            produto=self.produto,
            quantidade=Decimal("1.000"),
            unidade="UN",
            preco_unitario=Decimal("100.00"),
            valor_total=Decimal("100.00"),
        )
        dados = self._dados_finalizacao_compra_lista(
            compra,
            tipo_pagamento="cartao_credito",
            data_vencimento="2026-07-21",
            origem_caixa="0,00",
            origem_reserva="0,00",
            origem_banco="0,00",
        )

        self.client.post(reverse("estoque:compra_finalizar", kwargs={"pk": compra.pk}), dados, secure=True)
        compra.refresh_from_db()
        self.produto.refresh_from_db()
        estoque_apos_primeira = self.produto.quantidade
        segunda_resposta = self.client.post(reverse("estoque:compra_finalizar", kwargs={"pk": compra.pk}), dados, secure=True)

        compra.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertRedirects(segunda_resposta, reverse("estoque:compras_detalhe", kwargs={"pk": compra.pk}), fetch_redirect_response=False)
        self.assertEqual(compra.status, Compra.STATUS_FINALIZADA)
        self.assertEqual(self.produto.quantidade, estoque_apos_primeira)
        self.assertEqual(ContaPagar.objects.filter(compra=compra).count(), 1)
        self.assertEqual(MovimentoFinanceiro.objects.count(), 0)

    def test_cartao_debito_continua_pagamento_imediato(self):
        dados = self.dados(
            fechamento_token="d" * 32,
            acao_compra="confirmar_financeiro",
            tipo_pagamento="cartao_debito",
            data_vencimento="",
            **{"preco_unitario[]": ["100,00"], "origem_caixa": "100,00", "origem_reserva": "0,00", "origem_banco": "0,00"},
        )
        estoque_antes = self.produto.quantidade

        resposta = self.client.post(self.url, dados, secure=True)

        compra = Compra.objects.get(fechamento_token="d" * 32)
        self.produto.refresh_from_db()
        self.assertRedirects(resposta, reverse("estoque:compras_lista"), fetch_redirect_response=False)
        self.assertEqual(compra.status, Compra.STATUS_FINALIZADA)
        self.assertEqual(self.produto.quantidade, estoque_antes + Decimal("1.000"))
        self.assertEqual(MovimentoFinanceiro.objects.filter(compra=compra, origem="compra_a_vista").count(), 1)
        self.assertEqual(ContaPagar.objects.filter(compra=compra).count(), 0)

    def test_modal_de_fechamento_tem_resumo_compacto_e_sem_usar_restante(self):
        resposta = self.client.get(self.url, secure=True)

        self.assertContains(resposta, "Total da compra")
        self.assertContains(resposta, "Falta distribuir")
        self.assertContains(resposta, "Tudo distribuído")
        self.assertContains(resposta, 'campo.addEventListener("input"')
        self.assertNotContains(resposta, "distribuirRestanteApos")
        self.assertNotContains(resposta, 'id="valorOrigemDistribuidoCompra"')
        self.assertContains(resposta, "Distribua o total da compra entre Caixa, Sangria e Banco/Pix.")
        self.assertContains(resposta, 'id="origemCaixaCompra"')
        self.assertContains(resposta, 'id="origemReservaCompra"')
        self.assertContains(resposta, 'id="origemBancoCompra"')
        self.assertNotContains(resposta, "Total informado")
        self.assertNotContains(resposta, "Usar restante")
        self.assertContains(resposta, "Fechando compra...")
        self.assertContains(resposta, "definirEnvioModalEmAndamento(true)")
        self.assertContains(resposta, "Saldo atual: R$ 0,00", count=3)

    def test_mobile_oculta_apenas_caixa_e_mantem_origens_e_cartoes(self):
        resposta = self.client.get(self.url, secure=True)

        self.assertContains(resposta, 'id="origemCaixaCompra"')
        self.assertContains(resposta, 'id="origemReservaCompra"')
        self.assertContains(resposta, 'id="origemBancoCompra"')
        self.assertContains(resposta, 'name="fluxo_mobile_compra"')
        self.assertContains(resposta, "Salve como rascunho para continuar depois ou finalize quando a compra estiver conferida.")
        self.assertContains(resposta, "Salvar Rascunho")
        self.assertContains(resposta, "Finalizar Compra")
        self.assertContains(resposta, ".compras-origem-campo-caixa { display:none !important; }")
        self.assertContains(resposta, ".compras-origem-nota-mobile { display:block; }")
        self.assertContains(resposta, "No celular, distribua o total entre Sangria/Reserva e Banco/Pix.")
        self.assertContains(resposta, "return [origemReservaCompra, origemBancoCompra].filter(Boolean);")
        self.assertContains(resposta, "definirCampoPorCentavos(origemCaixaCompra, 0);")
        self.assertContains(resposta, "definirCampoPorCentavos(origemReservaCompra, totalCompraCentavosOrigem());")
        self.assertContains(resposta, "definirCampoPorCentavos(origemBancoCompra, 0);")
        self.assertContains(resposta, "focarSelecionando(primeiroCampoOrigemCompra());")
        self.assertContains(resposta, "function preencherBancoPixComRestanteMobile()")
        self.assertContains(resposta, "if (campo === origemReservaCompra)")
        self.assertContains(resposta, "definirCampoPorCentavos(origemBancoCompra, totalCompraCentavosOrigem() - reserva.valor);")
        self.assertContains(resposta, '<option value="cartao_credito"')
        self.assertContains(resposta, '<option value="cartao_debito"')

    def test_mobile_rascunho_exibe_saldos_reais_no_modal(self):
        compra = self._criar_compra_rascunho_com_item()
        for chave, valor in {
            "reserva": Decimal("261.80"),
            "banco": Decimal("985.35"),
        }.items():
            MovimentoFinanceiro.objects.create(
                conta=views._conta_financeira_padrao(chave),
                tipo=MovimentoFinanceiro.TIPO_ENTRADA,
                valor=valor,
                data=timezone.localdate(),
                origem="teste_saldo_mobile_compra",
            )

        resposta = self.client.get(reverse("estoque:compra_editar", kwargs={"pk": compra.pk}), secure=True)

        self.assertContains(resposta, "Saldo atual: R$ 261,80")
        self.assertContains(resposta, "Saldo atual: R$ 985,35")
        self.assertContains(resposta, "Sangria / Reserva")
        self.assertContains(resposta, "Banco/Pix")

    def test_mobile_edicao_inline_item_renderiza_controles_e_script(self):
        Unidade.objects.create(nome="Caixa", sigla="CX")
        compra = self._criar_compra_rascunho_com_item()

        resposta = self.client.get(reverse("estoque:compra_editar", kwargs={"pk": compra.pk}), secure=True)

        self.assertContains(resposta, "Itens lan&ccedil;ados")
        self.assertContains(resposta, "Pr&oacute;ximos passos")
        self.assertContains(resposta, 'id="totalItensCompraMobile"')
        self.assertContains(resposta, "Itens:")
        self.assertContains(resposta, "Remover item")
        self.assertContains(resposta, "Tem certeza que deseja remover este produto da compra?")
        self.assertContains(resposta, 'id="produtoModalRemoverItemCompra"')
        self.assertContains(resposta, "function abrirModalRemoverItemCompra")
        self.assertContains(resposta, "function executarRemocaoLinhaItem")
        self.assertContains(resposta, "btn-editar-item-mobile")
        self.assertContains(resposta, "Salvar altera&ccedil;&atilde;o")
        self.assertContains(resposta, "Cancelar edi&ccedil;&atilde;o")
        self.assertContains(resposta, 'list="unidadesCompraMobile"')
        self.assertContains(resposta, '<option value="CX">Caixa</option>')
        self.assertContains(resposta, "function iniciarEdicaoItemMobile")
        self.assertContains(resposta, "function salvarEdicaoItemMobile")
        self.assertContains(resposta, "function cancelarEdicaoItemMobile")
        self.assertContains(resposta, "function fecharEdicoesItensMobile")
        self.assertContains(resposta, "let rascunhoBloqueado = false;")
        self.assertContains(resposta, "function compraRascunhoBloqueado")
        self.assertContains(resposta, "function atualizarControlesRascunhoBloqueado")
        self.assertContains(resposta, "setBloqueadoRascunhoControle(botao, rascunhoBloqueado);")
        self.assertContains(resposta, "setBloqueadoRascunhoControle(btnAdicionar, rascunhoBloqueado);")
        self.assertContains(resposta, "setBloqueadoRascunhoControle(btnVoltarLancamentoProdutoMobile, rascunhoBloqueado);")
        self.assertContains(resposta, "rascunhoBloqueado = false;")
        self.assertContains(resposta, "if (compraRascunhoBloqueado()) return;")
        self.assertContains(resposta, 'btnSalvarRascunhoCompra.dataset.lancarItens === "1"')
        self.assertContains(resposta, 'enviarCompraComAcao("salvar_rascunho", btnSalvarRascunhoCompra);')
        self.assertNotContains(resposta, "#btnContinuarLancandoItens, #btnSalvarRascunhoCompra[data-lancar-itens='1']")
        self.assertContains(resposta, 'acao === "salvar_rascunho"')
        self.assertContains(resposta, "linha.dataset.edicaoProdutoOriginal")
        self.assertContains(resposta, "O produto deste item nao pode ser alterado por aqui.")
        self.assertContains(resposta, "const subtotal = quantidade * preco;")
        self.assertContains(resposta, "totalCompra.textContent = moeda(total);")
        self.assertNotContains(resposta, '<tr class="linha-item item-editando-mobile"')

    def test_mobile_salvar_rascunho_nao_altera_estoque_financeiro_ou_conta(self):
        compra = self._criar_compra_rascunho_com_item()
        estoque_antes = self.produto.quantidade

        resposta = self.client.post(
            reverse("estoque:compra_editar", kwargs={"pk": compra.pk}),
            self._dados_finalizacao_compra_lista(
                compra,
                tipo_pagamento="avista",
                acao_compra="salvar_rascunho",
                fluxo_mobile_compra="1",
                origem_caixa="0,00",
                origem_reserva="0,00",
                origem_banco="0,00",
            ),
            secure=True,
        )

        compra.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertRedirects(
            resposta,
            f"{reverse('estoque:compra_editar', kwargs={'pk': compra.pk})}?rascunho_salvo=1",
            fetch_redirect_response=False,
        )
        self.assertEqual(compra.status, Compra.STATUS_RASCUNHO)
        self.assertEqual(self.produto.quantidade, estoque_antes)
        self.assertEqual(MovimentoFinanceiro.objects.filter(compra=compra).count(), 0)
        self.assertEqual(ContaPagar.objects.filter(compra=compra).count(), 0)

        resposta_reabrir = self.client.get(
            f"{reverse('estoque:compra_editar', kwargs={'pk': compra.pk})}?rascunho_salvo=1",
            secure=True,
        )
        self.assertContains(resposta_reabrir, "Rascunho salvo")
        self.assertContains(resposta_reabrir, "Para continuar adicionando produtos")
        self.assertContains(resposta_reabrir, "Lan&ccedil;ar itens")
        self.assertContains(resposta_reabrir, 'data-lancar-itens="1"')
        self.assertContains(resposta_reabrir, "let rascunhoBloqueado = true;")
        self.assertContains(resposta_reabrir, "function compraRascunhoBloqueado")
        self.assertContains(resposta_reabrir, "if (compraRascunhoBloqueado()) return;")
        self.assertContains(resposta_reabrir, "setBloqueadoRascunhoControle(botao, rascunhoBloqueado);")
        self.assertContains(resposta_reabrir, "setBloqueadoRascunhoControle(btnAdicionar, rascunhoBloqueado);")
        self.assertContains(resposta_reabrir, "setBloqueadoRascunhoControle(btnVoltarLancamentoProdutoMobile, rascunhoBloqueado);")
        self.assertContains(resposta_reabrir, "atualizarControlesRascunhoBloqueado();")
        self.assertNotContains(resposta_reabrir, '>Salvar Rascunho</button>')
        self.assertNotContains(resposta_reabrir, "Salve como rascunho para continuar depois")

    def test_mobile_salvar_rascunho_suporta_ciclos_consecutivos(self):
        compra = self._criar_compra_rascunho_com_item()
        estoque_antes = self.produto.quantidade
        url = reverse("estoque:compra_editar", kwargs={"pk": compra.pk})

        for quantidade in ["1,50", "2,00", "3,25"]:
            dados = self._dados_finalizacao_compra_lista(
                compra,
                tipo_pagamento="avista",
                acao_compra="salvar_rascunho",
                fluxo_mobile_compra="1",
                origem_caixa="0,00",
                origem_reserva="0,00",
                origem_banco="0,00",
            )
            dados["quantidade[]"] = [quantidade]

            resposta = self.client.post(
                url,
                dados,
                secure=True,
            )
            self.assertRedirects(
                resposta,
                f"{url}?rascunho_salvo=1",
                fetch_redirect_response=False,
            )

            compra.refresh_from_db()
            self.produto.refresh_from_db()
            self.assertEqual(compra.status, Compra.STATUS_RASCUNHO)
            self.assertEqual(self.produto.quantidade, estoque_antes)
            self.assertEqual(MovimentoFinanceiro.objects.filter(compra=compra).count(), 0)
            self.assertEqual(ContaPagar.objects.filter(compra=compra).count(), 0)

            resposta_reabrir = self.client.get(f"{url}?rascunho_salvo=1", secure=True)
            self.assertContains(resposta_reabrir, "Rascunho salvo")
            self.assertContains(resposta_reabrir, 'data-lancar-itens="1"')
            self.assertContains(resposta_reabrir, "let rascunhoBloqueado = true;")

    def test_mobile_item_editado_persiste_ao_salvar_rascunho(self):
        compra = self._criar_compra_rascunho_com_item()
        dados = self._dados_finalizacao_compra_lista(
            compra,
            tipo_pagamento="avista",
            acao_compra="salvar_rascunho",
            fluxo_mobile_compra="1",
            origem_caixa="0,00",
            origem_reserva="0,00",
            origem_banco="0,00",
        )
        dados["quantidade[]"] = ["2,5"]
        dados["unidade[]"] = ["CX"]
        dados["preco_unitario[]"] = ["12,34"]

        resposta = self.client.post(
            reverse("estoque:compra_editar", kwargs={"pk": compra.pk}),
            dados,
            secure=True,
        )

        self.assertRedirects(
            resposta,
            f"{reverse('estoque:compra_editar', kwargs={'pk': compra.pk})}?rascunho_salvo=1",
            fetch_redirect_response=False,
        )
        item = compra.itens.get()
        compra.refresh_from_db()
        self.assertEqual(compra.status, Compra.STATUS_RASCUNHO)
        self.assertEqual(item.produto_id, self.produto.id)
        self.assertEqual(item.quantidade, Decimal("2.500"))
        self.assertEqual(item.unidade, "CX")
        self.assertEqual(item.preco_unitario, Decimal("12.34"))
        self.assertEqual(item.valor_total, Decimal("30.85"))
        self.assertEqual(compra.total, Decimal("30.85"))

    def test_mobile_salvar_rascunho_com_varios_itens_reabre_fechado(self):
        compra = self._criar_compra_rascunho_com_item()
        produto_extra = Produto.objects.create(
            nome="Produto Extra Compra",
            preco_compra=Decimal("20.00"),
            preco_vista=Decimal("35.00"),
            preco_prazo=Decimal("40.00"),
            quantidade=Decimal("5.000"),
        )
        dados = self._dados_finalizacao_compra_lista(
            compra,
            tipo_pagamento="avista",
            acao_compra="salvar_rascunho",
            fluxo_mobile_compra="1",
            origem_caixa="0,00",
            origem_reserva="0,00",
            origem_banco="0,00",
        )
        dados["produto_id[]"] = [str(self.produto.id), str(produto_extra.id)]
        dados["quantidade[]"] = ["2", "3"]
        dados["unidade[]"] = ["UN", "CX"]
        dados["preco_unitario[]"] = ["10,00", "15,00"]
        dados["observacao_item[]"] = ["", ""]

        resposta = self.client.post(
            reverse("estoque:compra_editar", kwargs={"pk": compra.pk}),
            dados,
            secure=True,
        )

        self.assertRedirects(
            resposta,
            f"{reverse('estoque:compra_editar', kwargs={'pk': compra.pk})}?rascunho_salvo=1",
            fetch_redirect_response=False,
        )
        compra.refresh_from_db()
        itens = list(compra.itens.order_by("produto__nome"))
        self.assertEqual(compra.status, Compra.STATUS_RASCUNHO)
        self.assertEqual(compra.total, Decimal("65.00"))
        self.assertEqual(len(itens), 2)
        self.assertEqual(itens[0].produto, self.produto)
        self.assertEqual(itens[0].quantidade, Decimal("2.000"))
        self.assertEqual(itens[0].unidade, "UN")
        self.assertEqual(itens[0].valor_total, Decimal("20.00"))
        self.assertEqual(itens[1].produto, produto_extra)
        self.assertEqual(itens[1].quantidade, Decimal("3.000"))
        self.assertEqual(itens[1].unidade, "CX")
        self.assertEqual(itens[1].valor_total, Decimal("45.00"))

        resposta_reabrir = self.client.get(
            f"{reverse('estoque:compra_editar', kwargs={'pk': compra.pk})}?rascunho_salvo=1",
            secure=True,
        )
        self.assertContains(resposta_reabrir, "Rascunho salvo com sucesso.")
        self.assertContains(resposta_reabrir, "Produto Compra")
        self.assertContains(resposta_reabrir, "Produto Extra Compra")
        self.assertNotContains(resposta_reabrir, '<tr class="linha-item item-editando-mobile"')

    def test_mobile_item_editado_e_usado_ao_finalizar(self):
        compra = self._criar_compra_rascunho_com_item()
        estoque_antes = self.produto.quantidade
        dados = self._dados_finalizacao_compra_lista(
            compra,
            tipo_pagamento="avista",
            fluxo_mobile_compra="1",
            origem_caixa="0,00",
            origem_reserva="0,00",
            origem_banco="60,00",
        )
        dados["quantidade[]"] = ["2"]
        dados["unidade[]"] = ["CX"]
        dados["preco_unitario[]"] = ["30,00"]

        resposta = self.client.post(
            reverse("estoque:compra_finalizar", kwargs={"pk": compra.pk}),
            dados,
            secure=True,
        )

        compra.refresh_from_db()
        self.produto.refresh_from_db()
        item = compra.itens.get()
        movimento = compra.movimentos_financeiros.get()
        self.assertRedirects(resposta, reverse("estoque:compras_lista"), fetch_redirect_response=False)
        self.assertEqual(compra.status, Compra.STATUS_FINALIZADA)
        self.assertEqual(item.produto_id, self.produto.id)
        self.assertEqual(item.quantidade, Decimal("2.000"))
        self.assertEqual(item.unidade, "CX")
        self.assertEqual(item.preco_unitario, Decimal("30.00"))
        self.assertEqual(item.valor_total, Decimal("60.00"))
        self.assertEqual(compra.total, Decimal("60.00"))
        self.assertEqual(self.produto.quantidade, estoque_antes + Decimal("2.000"))
        self.assertEqual(movimento.valor, Decimal("60.00"))

    def test_mobile_finaliza_com_banco_pix_sem_usar_caixa(self):
        compra = self._criar_compra_rascunho_com_item()
        estoque_antes = self.produto.quantidade

        resposta = self.client.post(
            reverse("estoque:compra_finalizar", kwargs={"pk": compra.pk}),
            self._dados_finalizacao_compra_lista(
                compra,
                tipo_pagamento="avista",
                fluxo_mobile_compra="1",
                origem_caixa="0,00",
                origem_reserva="0,00",
                origem_banco="100,00",
            ),
            secure=True,
        )

        compra.refresh_from_db()
        self.produto.refresh_from_db()
        movimento = compra.movimentos_financeiros.get()
        self.assertRedirects(resposta, reverse("estoque:compras_lista"), fetch_redirect_response=False)
        self.assertEqual(compra.status, Compra.STATUS_FINALIZADA)
        self.assertEqual(self.produto.quantidade, estoque_antes + Decimal("1.000"))
        self.assertEqual(movimento.conta, views._conta_financeira_padrao("banco"))
        self.assertNotEqual(movimento.conta, views._conta_financeira_padrao("caixa"))
        self.assertEqual(ContaPagar.objects.filter(compra=compra).count(), 0)

    def test_mobile_finaliza_com_sangria_reserva_sem_usar_caixa(self):
        compra = self._criar_compra_rascunho_com_item()
        estoque_antes = self.produto.quantidade

        resposta = self.client.post(
            reverse("estoque:compra_finalizar", kwargs={"pk": compra.pk}),
            self._dados_finalizacao_compra_lista(
                compra,
                tipo_pagamento="avista",
                fluxo_mobile_compra="1",
                origem_caixa="0,00",
                origem_reserva="100,00",
                origem_banco="0,00",
            ),
            secure=True,
        )

        compra.refresh_from_db()
        self.produto.refresh_from_db()
        movimento = compra.movimentos_financeiros.get()
        self.assertRedirects(resposta, reverse("estoque:compras_lista"), fetch_redirect_response=False)
        self.assertEqual(compra.status, Compra.STATUS_FINALIZADA)
        self.assertEqual(self.produto.quantidade, estoque_antes + Decimal("1.000"))
        self.assertEqual(movimento.conta, views._conta_financeira_padrao("reserva"))
        self.assertNotEqual(movimento.conta, views._conta_financeira_padrao("caixa"))
        self.assertEqual(ContaPagar.objects.filter(compra=compra).count(), 0)

    def test_mobile_finaliza_cartao_debito_com_movimento_imediato(self):
        compra = self._criar_compra_rascunho_com_item(tipo_pagamento="cartao_debito")
        estoque_antes = self.produto.quantidade

        resposta = self.client.post(
            reverse("estoque:compra_finalizar", kwargs={"pk": compra.pk}),
            self._dados_finalizacao_compra_lista(
                compra,
                tipo_pagamento="cartao_debito",
                fluxo_mobile_compra="1",
                origem_caixa="0,00",
                origem_reserva="0,00",
                origem_banco="100,00",
            ),
            secure=True,
        )

        compra.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertRedirects(resposta, reverse("estoque:compras_lista"), fetch_redirect_response=False)
        self.assertEqual(compra.status, Compra.STATUS_FINALIZADA)
        self.assertEqual(compra.tipo_pagamento, "cartao_debito")
        self.assertEqual(self.produto.quantidade, estoque_antes + Decimal("1.000"))
        self.assertEqual(MovimentoFinanceiro.objects.filter(compra=compra, origem="compra_a_vista").count(), 1)
        self.assertEqual(ContaPagar.objects.filter(compra=compra).count(), 0)

    def test_mobile_bloqueia_caixa_na_finalizacao_imediata(self):
        compra = self._criar_compra_rascunho_com_item()
        estoque_antes = self.produto.quantidade

        resposta = self.client.post(
            reverse("estoque:compra_finalizar", kwargs={"pk": compra.pk}),
            self._dados_finalizacao_compra_lista(
                compra,
                tipo_pagamento="avista",
                fluxo_mobile_compra="1",
                origem_caixa="100,00",
                origem_reserva="0,00",
                origem_banco="0,00",
            ),
            follow=True,
            secure=True,
        )

        compra.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertEqual(compra.status, Compra.STATUS_RASCUNHO)
        self.assertEqual(self.produto.quantidade, estoque_antes)
        self.assertEqual(MovimentoFinanceiro.objects.filter(compra=compra).count(), 0)
        self.assertContains(resposta, "No celular, use Sangria/Reserva ou Banco/Pix.")

    def test_mobile_cartao_credito_sem_vencimento_nao_finaliza(self):
        compra = self._criar_compra_rascunho_com_item(tipo_pagamento="cartao_credito")
        estoque_antes = self.produto.quantidade

        resposta = self.client.post(
            reverse("estoque:compra_finalizar", kwargs={"pk": compra.pk}),
            self._dados_finalizacao_compra_lista(
                compra,
                tipo_pagamento="cartao_credito",
                data_vencimento="",
                fluxo_mobile_compra="1",
                origem_caixa="0,00",
                origem_reserva="0,00",
                origem_banco="0,00",
            ),
            follow=True,
            secure=True,
        )

        compra.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertEqual(compra.status, Compra.STATUS_RASCUNHO)
        self.assertEqual(self.produto.quantidade, estoque_antes)
        self.assertEqual(ContaPagar.objects.filter(compra=compra).count(), 0)
        self.assertEqual(MovimentoFinanceiro.objects.filter(compra=compra).count(), 0)
        self.assertContains(resposta, "Informe a data de vencimento da fatura do cartao antes de finalizar.")

    def test_mobile_cartao_credito_com_vencimento_cria_conta_futura(self):
        compra = self._criar_compra_rascunho_com_item(tipo_pagamento="cartao_credito")
        estoque_antes = self.produto.quantidade

        resposta = self.client.post(
            reverse("estoque:compra_finalizar", kwargs={"pk": compra.pk}),
            self._dados_finalizacao_compra_lista(
                compra,
                tipo_pagamento="cartao_credito",
                data_vencimento="2026-07-21",
                fluxo_mobile_compra="1",
                origem_caixa="0,00",
                origem_reserva="0,00",
                origem_banco="0,00",
            ),
            secure=True,
        )

        compra.refresh_from_db()
        self.produto.refresh_from_db()
        conta = compra.conta_pagar
        self.assertRedirects(resposta, reverse("estoque:compras_lista"), fetch_redirect_response=False)
        self.assertEqual(compra.status, Compra.STATUS_FINALIZADA)
        self.assertEqual(compra.tipo_pagamento, "cartao_credito")
        self.assertEqual(self.produto.quantidade, estoque_antes + Decimal("1.000"))
        self.assertEqual(conta.data_vencimento.isoformat(), "2026-07-21")
        self.assertEqual(conta.valor_original, Decimal("100.00"))
        self.assertEqual(MovimentoFinanceiro.objects.filter(compra=compra).count(), 0)

    def test_modal_exibe_saldos_financeiros_calculados_na_renderizacao(self):
        saldos = {
            "caixa": Decimal("476.85"),
            "reserva": Decimal("1800.00"),
            "banco": Decimal("985.35"),
        }
        for chave, valor in saldos.items():
            MovimentoFinanceiro.objects.create(
                conta=views._conta_financeira_padrao(chave),
                tipo=MovimentoFinanceiro.TIPO_ENTRADA,
                valor=valor,
                data=timezone.localdate(),
                origem="teste_saldo_modal_compra",
            )

        resposta = self.client.get(self.url, secure=True)

        self.assertContains(resposta, "Saldo atual: R$ 476,85")
        self.assertContains(resposta, "Saldo atual: R$ 1800,00")
        self.assertContains(resposta, "Saldo atual: R$ 985,35")

    def test_compra_gerada_pela_lista_exibe_saldos_reais_no_modal(self):
        saldos = {
            "caixa": Decimal("476.85"),
            "reserva": Decimal("1800.00"),
            "banco": Decimal("985.35"),
        }
        for chave, valor in saldos.items():
            MovimentoFinanceiro.objects.create(
                conta=views._conta_financeira_padrao(chave),
                tipo=MovimentoFinanceiro.TIPO_ENTRADA,
                valor=valor,
                data=timezone.localdate(),
                origem="teste_saldo_lista_compra",
            )
        lista = ListaCompraFornecedor.objects.create(
            fornecedor=self.fornecedor,
            data_lista=timezone.localdate(),
            data_inicio_periodo=timezone.localdate(),
            data_fim_periodo=timezone.localdate(),
        )
        ItemListaCompraFornecedor.objects.create(
            lista=lista,
            produto=self.produto,
            estoque_atual=Decimal("10.000"),
            estoque_minimo=Decimal("0.000"),
            quantidade_final=Decimal("1.000"),
            unidade="UN",
            preco_compra=Decimal("100.00"),
            total=Decimal("100.00"),
        )

        resposta_geracao = self.client.post(
            reverse("estoque:compras_lista_fornecedor_gerar_compra", kwargs={"pk": lista.pk}),
            secure=True,
        )
        compra = Compra.objects.get(observacao__icontains=f"Lista de Compras #{lista.id}")
        resposta = self.client.get(reverse("estoque:compra_editar", kwargs={"pk": compra.pk}), secure=True)

        self.assertEqual(resposta_geracao.status_code, 302)
        self.assertContains(resposta, "Saldo atual: R$ 476,85")
        self.assertContains(resposta, "Saldo atual: R$ 1800,00")
        self.assertContains(resposta, "Saldo atual: R$ 985,35")
        self.assertNotContains(resposta, "Saldo atual: R$ 0,00")

    def test_compra_gerada_com_produto_sem_estoque_nao_exibe_none(self):
        produto_sem_estoque = Produto.objects.create(
            nome="Produto Sem Estoque",
            preco_compra=Decimal("10.00"),
            preco_vista=Decimal("15.00"),
            preco_prazo=Decimal("16.00"),
            quantidade=None,
        )
        compra = Compra.objects.create(
            fornecedor=self.fornecedor,
            data_compra=timezone.localdate(),
            tipo_pagamento="avista",
            total=Decimal("10.00"),
            status=Compra.STATUS_RASCUNHO,
            observacao="Gerada a partir da Lista de Compras #999",
        )
        ItemCompra.objects.create(
            compra=compra,
            produto=produto_sem_estoque,
            quantidade=Decimal("1.000"),
            unidade="UN",
            preco_unitario=Decimal("10.00"),
            valor_total=Decimal("10.00"),
        )

        resposta = self.client.get(reverse("estoque:compra_editar", kwargs={"pk": compra.pk}), secure=True)

        self.assertContains(resposta, '<span class="produto-estoque-valor">0</span>')
        self.assertNotContains(resposta, ">None<")
        self.assertNotContains(resposta, "data-estoque=\"None\"")

    def test_compra_gerada_pela_lista_exibe_erro_tipo_pagamento_no_campo(self):
        lista = ListaCompraFornecedor.objects.create(
            fornecedor=self.fornecedor,
            data_lista=timezone.localdate(),
            data_inicio_periodo=timezone.localdate(),
            data_fim_periodo=timezone.localdate(),
        )
        ItemListaCompraFornecedor.objects.create(
            lista=lista,
            produto=self.produto,
            estoque_atual=Decimal("10.000"),
            estoque_minimo=Decimal("0.000"),
            quantidade_final=Decimal("1.000"),
            unidade="UN",
            preco_compra=Decimal("100.00"),
            total=Decimal("100.00"),
        )
        self.client.post(
            reverse("estoque:compras_lista_fornecedor_gerar_compra", kwargs={"pk": lista.pk}),
            secure=True,
        )
        compra = Compra.objects.get(observacao__icontains=f"Lista de Compras #{lista.id}")

        resposta = self.client.post(
            reverse("estoque:compra_finalizar", kwargs={"pk": compra.pk}),
            {
                "acao_compra": "confirmar_financeiro",
                "fornecedor_id": str(self.fornecedor.id),
                "data_compra": timezone.localdate().isoformat(),
                "tipo_pagamento": "",
                "data_vencimento": "",
                "observacao": compra.observacao,
                "produto_id[]": [str(self.produto.id)],
                "quantidade[]": ["1"],
                "unidade[]": ["UN"],
                "preco_unitario[]": ["100,00"],
                "observacao_item[]": [""],
            },
            secure=True,
        )

        destino = f"{reverse('estoque:compra_editar', kwargs={'pk': compra.pk})}?erro_tipo_pagamento=1"
        self.assertRedirects(resposta, destino, fetch_redirect_response=False)

        resposta = self.client.get(destino, secure=True)
        html = resposta.content.decode()
        label_posicao = html.index("<label>Tipo de pagamento</label>")
        select_posicao = html.index('id="tipoPagamentoCompra"', label_posicao)
        erro_posicao = html.index('id="erroTipoPagamentoCompra"', select_posicao)

        self.assertLess(label_posicao, select_posicao)
        self.assertLess(select_posicao, erro_posicao)
        self.assertContains(resposta, 'class="compras-field-error" id="erroTipoPagamentoCompra"')
        self.assertContains(resposta, "Selecione o tipo de pagamento antes de finalizar a compra.", count=1)
        self.assertNotContains(
            resposta,
            '<div class="compras-alert error">Selecione o tipo de pagamento antes de finalizar a compra.</div>',
        )

    def test_compra_gerada_pela_lista_finaliza_avista_com_dados_do_modal(self):
        lista = ListaCompraFornecedor.objects.create(
            fornecedor=self.fornecedor,
            data_lista=timezone.localdate(),
            data_inicio_periodo=timezone.localdate(),
            data_fim_periodo=timezone.localdate(),
        )
        ItemListaCompraFornecedor.objects.create(
            lista=lista,
            produto=self.produto,
            estoque_atual=Decimal("10.000"),
            estoque_minimo=Decimal("0.000"),
            quantidade_final=Decimal("1.000"),
            unidade="UN",
            preco_compra=Decimal("100.00"),
            total=Decimal("100.00"),
        )
        self.client.post(
            reverse("estoque:compras_lista_fornecedor_gerar_compra", kwargs={"pk": lista.pk}),
            secure=True,
        )
        compra = Compra.objects.get(observacao__icontains=f"Lista de Compras #{lista.id}")
        self.assertEqual(compra.tipo_pagamento, "")
        estoque_antes = self.produto.quantidade

        resposta = self.client.post(
            reverse("estoque:compra_finalizar", kwargs={"pk": compra.pk}),
            {
                "acao_compra": "confirmar_financeiro",
                "fornecedor_id": str(self.fornecedor.id),
                "data_compra": timezone.localdate().isoformat(),
                "tipo_pagamento": "avista",
                "data_vencimento": "",
                "observacao": compra.observacao,
                "produto_id[]": [str(self.produto.id)],
                "quantidade[]": ["1"],
                "unidade[]": ["UN"],
                "preco_unitario[]": ["100,00"],
                "observacao_item[]": [""],
                "origem_caixa": "100,00",
                "origem_reserva": "0,00",
                "origem_banco": "0,00",
            },
            secure=True,
        )

        compra.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertRedirects(resposta, reverse("estoque:compras_lista"), fetch_redirect_response=False)
        self.assertEqual(compra.status, Compra.STATUS_FINALIZADA)
        self.assertEqual(compra.tipo_pagamento, "avista")
        self.assertTrue(compra.estoque_entrada_realizada)
        self.assertEqual(self.produto.quantidade, estoque_antes + Decimal("1.000"))
        movimento = compra.movimentos_financeiros.get()
        self.assertEqual(movimento.tipo, MovimentoFinanceiro.TIPO_SAIDA)
        self.assertEqual(movimento.valor, Decimal("100.00"))
        self.assertFalse(hasattr(compra, "conta_pagar"))

    def test_compra_gerada_pela_lista_finalizacao_avista_idempotente_nao_duplica_estoque_ou_movimento(self):
        lista = self._criar_lista_fornecedor_conferida()
        _, compra = self._gerar_compra_da_lista(lista)
        dados = self._dados_finalizacao_compra_lista(compra, tipo_pagamento="avista")

        primeira_resposta = self.client.post(
            reverse("estoque:compra_finalizar", kwargs={"pk": compra.pk}),
            dados,
            secure=True,
        )
        compra.refresh_from_db()
        self.produto.refresh_from_db()
        estoque_apos_primeira = self.produto.quantidade

        segunda_resposta = self.client.post(
            reverse("estoque:compra_finalizar", kwargs={"pk": compra.pk}),
            dados,
            secure=True,
        )

        compra.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertRedirects(primeira_resposta, reverse("estoque:compras_lista"), fetch_redirect_response=False)
        self.assertRedirects(segunda_resposta, reverse("estoque:compras_detalhe", kwargs={"pk": compra.pk}), fetch_redirect_response=False)
        self.assertEqual(compra.status, Compra.STATUS_FINALIZADA)
        self.assertEqual(self.produto.quantidade, estoque_apos_primeira)
        self.assertEqual(ContaPagar.objects.filter(compra=compra).count(), 0)
        self.assertEqual(MovimentoFinanceiro.objects.filter(compra=compra, origem="compra_a_vista").count(), 1)

    def test_compra_gerada_pela_lista_finaliza_aprazo_e_cria_conta_pagar(self):
        lista = ListaCompraFornecedor.objects.create(
            fornecedor=self.fornecedor,
            data_lista=timezone.localdate(),
            data_inicio_periodo=timezone.localdate(),
            data_fim_periodo=timezone.localdate(),
        )
        ItemListaCompraFornecedor.objects.create(
            lista=lista,
            produto=self.produto,
            estoque_atual=Decimal("10.000"),
            estoque_minimo=Decimal("0.000"),
            quantidade_final=Decimal("1.000"),
            unidade="UN",
            preco_compra=Decimal("100.00"),
            total=Decimal("100.00"),
        )
        self.client.post(
            reverse("estoque:compras_lista_fornecedor_gerar_compra", kwargs={"pk": lista.pk}),
            secure=True,
        )
        compra = Compra.objects.get(observacao__icontains=f"Lista de Compras #{lista.id}")
        estoque_antes = self.produto.quantidade
        movimentos_antes = MovimentoFinanceiro.objects.count()

        resposta = self.client.post(
            reverse("estoque:compra_editar", kwargs={"pk": compra.pk}),
            {
                "acao_compra": "finalizar",
                "fornecedor_id": str(self.fornecedor.id),
                "data_compra": timezone.localdate().isoformat(),
                "tipo_pagamento": "aprazo",
                "data_vencimento": "2026-07-16",
                "observacao": compra.observacao,
                "produto_id[]": [str(self.produto.id)],
                "quantidade[]": ["1"],
                "unidade[]": ["UN"],
                "preco_unitario[]": ["100,00"],
                "observacao_item[]": [""],
            },
            secure=True,
        )

        compra.refresh_from_db()
        self.produto.refresh_from_db()
        conta = compra.conta_pagar
        self.assertRedirects(resposta, reverse("estoque:compras_lista"), fetch_redirect_response=False)
        self.assertEqual(compra.status, Compra.STATUS_FINALIZADA)
        self.assertEqual(compra.tipo_pagamento, "aprazo")
        self.assertEqual(compra.data_vencimento.isoformat(), "2026-07-16")
        self.assertTrue(compra.estoque_entrada_realizada)
        self.assertEqual(self.produto.quantidade, estoque_antes + Decimal("1.000"))
        self.assertEqual(conta.fornecedor, self.fornecedor)
        self.assertEqual(conta.valor_original, Decimal("100.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("100.00"))
        self.assertEqual(conta.data_vencimento.isoformat(), "2026-07-16")
        self.assertEqual(conta.status, ContaPagar.STATUS_ABERTA)
        self.assertEqual(MovimentoFinanceiro.objects.count(), movimentos_antes)

    def test_compra_gerada_pela_lista_finalizacao_aprazo_idempotente_nao_duplica_estoque_ou_conta(self):
        lista = self._criar_lista_fornecedor_conferida()
        _, compra = self._gerar_compra_da_lista(lista)
        dados = self._dados_finalizacao_compra_lista(compra, tipo_pagamento="aprazo")

        primeira_resposta = self.client.post(
            reverse("estoque:compra_finalizar", kwargs={"pk": compra.pk}),
            dados,
            secure=True,
        )
        compra.refresh_from_db()
        self.produto.refresh_from_db()
        estoque_apos_primeira = self.produto.quantidade

        segunda_resposta = self.client.post(
            reverse("estoque:compra_finalizar", kwargs={"pk": compra.pk}),
            dados,
            secure=True,
        )

        compra.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertRedirects(primeira_resposta, reverse("estoque:compras_lista"), fetch_redirect_response=False)
        self.assertRedirects(segunda_resposta, reverse("estoque:compras_detalhe", kwargs={"pk": compra.pk}), fetch_redirect_response=False)
        self.assertEqual(compra.status, Compra.STATUS_FINALIZADA)
        self.assertEqual(self.produto.quantidade, estoque_apos_primeira)
        self.assertEqual(ContaPagar.objects.filter(compra=compra).count(), 1)
        self.assertEqual(MovimentoFinanceiro.objects.filter(compra=compra, origem="compra_a_vista").count(), 0)

    def test_compra_gerada_pela_lista_cartao_credito_exige_data_e_cria_conta_pagar(self):
        lista = self._criar_lista_fornecedor_conferida()
        _, compra = self._gerar_compra_da_lista(lista)
        estoque_antes = self.produto.quantidade
        dados_sem_data = self._dados_finalizacao_compra_lista(
            compra,
            tipo_pagamento="cartao_credito",
            data_vencimento="",
            origem_caixa="0,00",
            origem_reserva="0,00",
            origem_banco="0,00",
        )

        resposta_sem_data = self.client.post(
            reverse("estoque:compra_finalizar", kwargs={"pk": compra.pk}),
            dados_sem_data,
            follow=True,
            secure=True,
        )

        compra.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertEqual(compra.status, Compra.STATUS_RASCUNHO)
        self.assertEqual(self.produto.quantidade, estoque_antes)
        self.assertEqual(ContaPagar.objects.filter(compra=compra).count(), 0)
        self.assertEqual(MovimentoFinanceiro.objects.filter(compra=compra).count(), 0)
        self.assertContains(resposta_sem_data, "Informe a data de vencimento da fatura do cartao antes de finalizar.")

        dados_com_data = self._dados_finalizacao_compra_lista(
            compra,
            tipo_pagamento="cartao_credito",
            data_vencimento="2026-07-22",
            origem_caixa="0,00",
            origem_reserva="0,00",
            origem_banco="0,00",
        )
        resposta_com_data = self.client.post(
            reverse("estoque:compra_finalizar", kwargs={"pk": compra.pk}),
            dados_com_data,
            secure=True,
        )

        compra.refresh_from_db()
        self.produto.refresh_from_db()
        conta = compra.conta_pagar
        self.assertRedirects(resposta_com_data, reverse("estoque:compras_lista"), fetch_redirect_response=False)
        self.assertEqual(compra.status, Compra.STATUS_FINALIZADA)
        self.assertEqual(compra.tipo_pagamento, "cartao_credito")
        self.assertEqual(compra.data_vencimento.isoformat(), "2026-07-22")
        self.assertEqual(self.produto.quantidade, estoque_antes + Decimal("1.000"))
        self.assertEqual(conta.data_vencimento.isoformat(), "2026-07-22")
        self.assertEqual(conta.valor_original, Decimal("100.00"))
        self.assertEqual(MovimentoFinanceiro.objects.filter(compra=compra).count(), 0)

    def test_lista_fornecedor_nao_gera_segunda_compra_com_rascunho_existente(self):
        lista = self._criar_lista_fornecedor_conferida()
        _, compra = self._gerar_compra_da_lista(lista)

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_gerar_compra", kwargs={"pk": lista.pk}),
            follow=True,
            secure=True,
        )

        compras_lista = Compra.objects.filter(observacao__icontains=f"Lista de Compras #{lista.id}")
        self.assertEqual(compras_lista.count(), 1)
        self.assertEqual(compras_lista.get(), compra)
        self.assertContains(resposta, f"Esta lista ja gerou a Compra #{compra.id}")
        self.assertContains(resposta, f"Esta lista gerou a Compra #{compra.id}")

    def test_lista_fornecedor_nao_gera_segunda_compra_depois_de_finalizada(self):
        lista = self._criar_lista_fornecedor_conferida()
        _, compra = self._gerar_compra_da_lista(lista)
        self.client.post(
            reverse("estoque:compra_finalizar", kwargs={"pk": compra.pk}),
            self._dados_finalizacao_compra_lista(compra, tipo_pagamento="avista"),
            secure=True,
        )
        compra.refresh_from_db()
        self.assertEqual(compra.status, Compra.STATUS_FINALIZADA)

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_gerar_compra", kwargs={"pk": lista.pk}),
            follow=True,
            secure=True,
        )

        compras_lista = Compra.objects.filter(observacao__icontains=f"Lista de Compras #{lista.id}")
        self.assertEqual(compras_lista.count(), 1)
        self.assertEqual(compras_lista.get(), compra)
        self.assertContains(resposta, f"Esta lista ja gerou a Compra #{compra.id}")
        self.assertContains(resposta, f"Lista lancada como Compra #{compra.id}")

    def test_detalhe_lista_fornecedor_com_compra_finalizada_nao_oferece_gerar_outra_compra(self):
        lista = self._criar_lista_fornecedor_conferida()
        _, compra = self._gerar_compra_da_lista(lista)
        self.client.post(
            reverse("estoque:compra_finalizar", kwargs={"pk": compra.pk}),
            self._dados_finalizacao_compra_lista(compra, tipo_pagamento="avista"),
            secure=True,
        )
        compra.refresh_from_db()

        resposta = self.client.get(
            reverse("estoque:compras_lista_fornecedor_detalhe", kwargs={"pk": lista.pk}),
            secure=True,
        )

        self.assertEqual(compra.status, Compra.STATUS_FINALIZADA)
        self.assertContains(resposta, f"Lista lancada como Compra #{compra.id}")
        self.assertContains(resposta, "A compra ja foi finalizada, com estoque e financeiro lancados.")
        self.assertContains(resposta, "Ver Compra")
        self.assertNotContains(resposta, 'data-gerar-compra-lista-form="1"')
        self.assertNotContains(resposta, "Gerar Compra")

    def test_fecha_compra_e_cria_tres_saidas(self):
        resposta = self.client.post(self.url, self.dados(), follow=True, secure=True)

        self.assertEqual(Compra.objects.count(), 1)
        compra = Compra.objects.get()
        movimentos = list(compra.movimentos_financeiros.order_by("valor"))
        self.assertEqual(compra.status, Compra.STATUS_FINALIZADA)
        self.assertEqual([movimento.valor for movimento in movimentos], [Decimal("200.00"), Decimal("200.00"), Decimal("600.00")])
        self.assertTrue(all(movimento.tipo == MovimentoFinanceiro.TIPO_SAIDA for movimento in movimentos))
        self.assertTrue(all(f"Pagamento da compra #{compra.id}" in movimento.descricao for movimento in movimentos))
        self.assertContains(resposta, "Compra finalizada e valores lancados no financeiro com sucesso.")

    def test_detalhe_exibe_resumo_financeiro_compacto_e_lancamentos_fechados(self):
        self.client.post(self.url, self.dados(tipo_pagamento="avista"), secure=True)
        compra = Compra.objects.get()

        resposta = self.client.get(f"/estoque/compras/{compra.id}/", secure=True)

        self.assertContains(resposta, "Dados da compra")
        self.assertContains(resposta, ">À vista<")
        self.assertContains(resposta, "Nota paga")
        self.assertNotContains(resposta, "À vista (Dinheiro / Pix)")
        self.assertNotContains(resposta, "Compra à vista sai do Caixa/Banco")
        self.assertNotContains(resposta, "<strong>Status</strong>")
        self.assertNotContains(resposta, "<strong>Estoque</strong>")
        self.assertContains(resposta, "Resumo do pagamento")
        self.assertContains(resposta, "Compra à vista paga no fechamento. Nenhuma conta a pagar foi gerada.")
        self.assertContains(resposta, "Total pago")
        self.assertEqual(resposta.context["compra"].total, Decimal("1000.00"))
        self.assertContains(resposta, "Mostrar lançamentos financeiros (3)")
        self.assertContains(resposta, '<details class="nota-compra-lancamentos">')
        self.assertNotContains(resposta, "Detalhamento do pagamento")
        self.assertContains(resposta, "☰ Ações da compra")
        self.assertContains(resposta, 'id="painelAcoesCompraDetalhe"')
        self.assertContains(resposta, "Corrigir itens")
        self.assertContains(resposta, "Corrigir origem")
        self.assertContains(resposta, ">Excluir</button>")

    def test_fecha_com_os_rateios_visiveis_de_1173_83(self):
        casos = [
            {
                "token": "b" * 32,
                "caixa": "173,83",
                "reserva": "1.000,00",
                "banco": "0,00",
                "esperado": [Decimal("173.83"), Decimal("1000.00")],
            },
            {
                "token": "c" * 32,
                "caixa": "173,83",
                "reserva": "600,00",
                "banco": "400,00",
                "esperado": [Decimal("173.83"), Decimal("400.00"), Decimal("600.00")],
            },
        ]

        for caso in casos:
            with self.subTest(caso=caso):
                resposta = self.client.post(
                    self.url,
                    self.dados(
                        fechamento_token=caso["token"],
                        origem_caixa=caso["caixa"],
                        origem_reserva=caso["reserva"],
                        origem_banco=caso["banco"],
                        **{"preco_unitario[]": ["1.173,83"]},
                    ),
                    secure=True,
                )

                self.assertEqual(resposta.status_code, 302)
                compra = Compra.objects.get(fechamento_token=caso["token"])
                valores = list(compra.movimentos_financeiros.order_by("valor").values_list("valor", flat=True))
                self.assertEqual(valores, caso["esperado"])

    def test_bloqueia_soma_menor_maior_e_valor_invalido(self):
        casos = [
            {"origem_banco": "199,99"},
            {"origem_banco": "200,01"},
            {"origem_banco": "valor-invalido"},
            {"origem_caixa": "-600,00"},
        ]
        for indice, alteracoes in enumerate(casos):
            with self.subTest(alteracoes=alteracoes):
                alteracoes["fechamento_token"] = str(indice).zfill(32)
                self.client.post(self.url, self.dados(**alteracoes), secure=True)
                self.assertEqual(Compra.objects.count(), 0)
                self.assertEqual(MovimentoFinanceiro.objects.count(), 0)

    def test_reenvio_do_mesmo_token_nao_duplica_compra_nem_movimentos(self):
        dados = self.dados()
        self.client.post(self.url, dados, secure=True)
        resposta = self.client.post(self.url, dados, follow=True, secure=True)

        self.assertEqual(Compra.objects.count(), 1)
        self.assertEqual(MovimentoFinanceiro.objects.filter(origem="compra_a_vista").count(), 3)
        self.assertEqual(resposta.status_code, 200)

    def test_falha_financeira_desfaz_compra_itens_e_estoque(self):
        with patch("estoque.views.MovimentoFinanceiro.objects.create", side_effect=RuntimeError("falha simulada")):
            resposta = self.client.post(self.url, self.dados(), follow=True, secure=True)

        self.produto.refresh_from_db()
        self.assertEqual(Compra.objects.count(), 0)


class ComprasListaFornecedorConferenciaTests(TestCase):
    def setUp(self):
        self.fornecedor = Fornecedor.objects.create(nome="Fornecedor Teste")
        self.produto = Produto.objects.create(
            nome="Produto Conferencia",
            preco_compra=Decimal("10.00"),
            preco_vista=Decimal("15.00"),
            preco_prazo=Decimal("16.00"),
            quantidade=Decimal("10.000"),
        )
        self.lista = ListaCompraFornecedor.objects.create(
            fornecedor=self.fornecedor,
            data_lista=timezone.localdate(),
            data_inicio_periodo=timezone.localdate(),
            data_fim_periodo=timezone.localdate(),
        )
        self.item = ItemListaCompraFornecedor.objects.create(
            lista=self.lista,
            produto=self.produto,
            estoque_atual=Decimal("10.000"),
            estoque_minimo=Decimal("0.000"),
            quantidade_final=Decimal("2.000"),
            unidade="UN",
            preco_compra=Decimal("10.00"),
            preco_unitario=Decimal("10.00"),
            total=Decimal("20.00"),
        )

    def _post_conferencia(self, dados):
        url = reverse("estoque:compras_lista_fornecedor_conferencia_salvar", kwargs={"pk": self.lista.pk})
        return self.client.post(url, dados, secure=True)

    def _liberar_checklist_externa(self, conferente="Francisco"):
        token = views._liberar_conferencia_externa_lista_fornecedor(self.lista, conferente)
        self.lista.refresh_from_db()
        return token

    def _criar_lista_fornecedor_conferencia(self, nome_produto, status=ListaCompraFornecedor.STATUS_ABERTA):
        produto = Produto.objects.create(
            nome=nome_produto,
            preco_compra=Decimal("10.00"),
            preco_vista=Decimal("15.00"),
            preco_prazo=Decimal("16.00"),
            quantidade=Decimal("10.000"),
        )
        lista = ListaCompraFornecedor.objects.create(
            fornecedor=self.fornecedor,
            data_lista=timezone.localdate(),
            data_inicio_periodo=timezone.localdate(),
            data_fim_periodo=timezone.localdate(),
            total_lista=Decimal("20.00"),
            status=status,
        )
        ItemListaCompraFornecedor.objects.create(
            lista=lista,
            produto=produto,
            estoque_atual=Decimal("10.000"),
            estoque_minimo=Decimal("0.000"),
            quantidade_final=Decimal("2.000"),
            unidade="UN",
            preco_compra=Decimal("10.00"),
            preco_unitario=Decimal("10.00"),
            total=Decimal("20.00"),
        )
        return lista

    def _criar_compra_vinculada_lista_fornecedor(self, lista, status=Compra.STATUS_RASCUNHO, cancelada=False):
        return Compra.objects.create(
            fornecedor=self.fornecedor,
            data_compra=timezone.localdate(),
            tipo_pagamento="",
            total=lista.total_lista,
            status=status,
            cancelada=cancelada,
            observacao=f"Gerada a partir da Lista de Compras #{lista.id}",
        )

    def test_consulta_mobile_principal_mostra_lista_sem_compra_nao_cancelada_mesmo_com_status_filtrado(self):
        lista_aberta = self._criar_lista_fornecedor_conferencia("Produto Mobile Aberta Sem Compra")
        lista_enviada = self._criar_lista_fornecedor_conferencia(
            "Produto Mobile Enviada Sem Compra",
            status=ListaCompraFornecedor.STATUS_ENVIADA,
        )
        resposta = self.client.get(
            reverse("estoque:compras_listas_fornecedor"),
            {"status": ListaCompraFornecedor.STATUS_FINALIZADA},
            secure=True,
        )

        listas_mobile_ids = {lista.id for lista in resposta.context["listas_mobile"]}
        self.assertEqual(resposta.status_code, 200)
        self.assertIn(lista_aberta.id, listas_mobile_ids)
        self.assertIn(lista_enviada.id, listas_mobile_ids)

    def test_consulta_mobile_principal_nao_mostra_lista_com_compra_vinculada(self):
        lista = self._criar_lista_fornecedor_conferencia("Produto Mobile Com Compra Vinculada")
        self._criar_compra_vinculada_lista_fornecedor(lista)

        resposta = self.client.get(reverse("estoque:compras_listas_fornecedor"), secure=True)

        listas_mobile_ids = {lista.id for lista in resposta.context["listas_mobile"]}
        self.assertNotIn(lista.id, listas_mobile_ids)

    def test_consulta_mobile_principal_mostra_lista_com_apenas_compra_cancelada_vinculada(self):
        lista = self._criar_lista_fornecedor_conferencia("Produto Mobile Compra Cancelada Nao Bloqueia")
        self._criar_compra_vinculada_lista_fornecedor(lista, status=Compra.STATUS_CANCELADA, cancelada=True)

        resposta = self.client.get(reverse("estoque:compras_listas_fornecedor"), secure=True)

        listas_mobile_ids = {lista.id for lista in resposta.context["listas_mobile"]}
        lista_mobile = next(lista_mobile for lista_mobile in resposta.context["listas_mobile"] if lista_mobile.id == lista.id)
        self.assertIn(lista.id, listas_mobile_ids)
        self.assertIsNone(lista_mobile.compra_gerada)

    def test_consulta_mobile_principal_usa_compra_nao_cancelada_mesmo_com_cancelada_mais_recente(self):
        lista = self._criar_lista_fornecedor_conferencia("Produto Mobile Compra Ativa Mais Antiga")
        compra_ativa = self._criar_compra_vinculada_lista_fornecedor(lista, status=Compra.STATUS_RASCUNHO)
        self._criar_compra_vinculada_lista_fornecedor(lista, status=Compra.STATUS_CANCELADA, cancelada=True)

        listas_anotadas = views._anotar_compras_geradas_listas_fornecedor([lista])

        self.assertEqual(listas_anotadas[0].compra_gerada.id, compra_ativa.id)

    def test_consulta_mobile_principal_nao_mostra_lista_cancelada(self):
        lista = self._criar_lista_fornecedor_conferencia(
            "Produto Mobile Cancelada Fora Principal",
            status=ListaCompraFornecedor.STATUS_CANCELADA,
        )

        resposta = self.client.get(reverse("estoque:compras_listas_fornecedor"), secure=True)

        listas_mobile_ids = {lista.id for lista in resposta.context["listas_mobile"]}
        self.assertNotIn(lista.id, listas_mobile_ids)

    def test_consulta_mobile_principal_ignora_status_cancelada_do_filtro_desktop(self):
        lista_aberta = self._criar_lista_fornecedor_conferencia("Produto Mobile Principal Mesmo Status Cancelada")
        lista_cancelada = self._criar_lista_fornecedor_conferencia(
            "Produto Mobile Cancelada Pelo Filtro Desktop",
            status=ListaCompraFornecedor.STATUS_CANCELADA,
        )

        resposta = self.client.get(
            reverse("estoque:compras_listas_fornecedor"),
            {"status": ListaCompraFornecedor.STATUS_CANCELADA},
            secure=True,
        )

        listas_mobile_ids = {lista.id for lista in resposta.context["listas_mobile"]}
        self.assertContains(resposta, 'data-mobile-modo="pendentes"')
        self.assertIn(lista_aberta.id, listas_mobile_ids)
        self.assertNotIn(lista_cancelada.id, listas_mobile_ids)

    def test_consulta_mobile_historico_mostra_lista_com_compra_vinculada(self):
        lista = self._criar_lista_fornecedor_conferencia("Produto Mobile Historico Com Compra")
        self._criar_compra_vinculada_lista_fornecedor(lista, status=Compra.STATUS_FINALIZADA)

        resposta = self.client.get(
            reverse("estoque:compras_listas_fornecedor"),
            {"mobile": "historico"},
            secure=True,
        )

        listas_mobile_ids = {lista.id for lista in resposta.context["listas_mobile"]}
        self.assertIn(lista.id, listas_mobile_ids)

    def test_consulta_mobile_historico_mostra_lista_cancelada(self):
        lista = self._criar_lista_fornecedor_conferencia(
            "Produto Mobile Historico Cancelada",
            status=ListaCompraFornecedor.STATUS_CANCELADA,
        )

        resposta = self.client.get(
            reverse("estoque:compras_listas_fornecedor"),
            {"mobile": "historico"},
            secure=True,
        )

        listas_mobile_ids = {lista.id for lista in resposta.context["listas_mobile"]}
        self.assertIn(lista.id, listas_mobile_ids)

    def test_consulta_mobile_canceladas_mostra_somente_canceladas(self):
        lista_cancelada = self._criar_lista_fornecedor_conferencia(
            "Produto Mobile Botao Canceladas",
            status=ListaCompraFornecedor.STATUS_CANCELADA,
        )
        lista_aberta = self._criar_lista_fornecedor_conferencia("Produto Mobile Canceladas Aberta")
        lista_com_compra = self._criar_lista_fornecedor_conferencia("Produto Mobile Canceladas Com Compra")
        self._criar_compra_vinculada_lista_fornecedor(lista_com_compra)

        resposta = self.client.get(
            reverse("estoque:compras_listas_fornecedor"),
            {"mobile": "canceladas", "status": ListaCompraFornecedor.STATUS_CANCELADA},
            secure=True,
        )

        listas_mobile_ids = {lista.id for lista in resposta.context["listas_mobile"]}
        self.assertContains(resposta, 'data-mobile-modo="canceladas"')
        self.assertIn(lista_cancelada.id, listas_mobile_ids)
        self.assertNotIn(lista_aberta.id, listas_mobile_ids)
        self.assertNotIn(lista_com_compra.id, listas_mobile_ids)

    def test_consulta_mobile_exibe_links_de_historico_e_canceladas_com_parametros_funcionais(self):
        resposta = self.client.get(reverse("estoque:compras_listas_fornecedor"), secure=True)

        self.assertContains(resposta, "?mobile=historico")
        self.assertContains(resposta, "?mobile=canceladas")
        self.assertNotContains(resposta, "?mobile=canceladas&amp;status=cancelada")
        html = resposta.content.decode()
        self.assertEqual(html.count("?mobile=historico"), 1)
        self.assertEqual(html.count("?mobile=canceladas"), 1)
        self.assertEqual(html.count(">Historico</a>"), 1)
        self.assertEqual(html.count(">Listas canceladas</a>"), 1)
        self.assertIn("listas-fornecedor-top-canceladas", html)

    def test_consulta_mobile_editar_lista_e_link_real_com_mesma_rota_do_desktop(self):
        lista = self._criar_lista_fornecedor_conferencia("Produto Mobile Editar Link")
        url_edicao = reverse("estoque:compras_lista_fornecedor_editar", kwargs={"pk": lista.pk})

        resposta = self.client.get(reverse("estoque:compras_listas_fornecedor"), secure=True)
        html = resposta.content.decode()

        self.assertContains(resposta, f'href="{url_edicao}"', count=2)
        self.assertIn(
            f'<a class="listas-fornecedor-btn" href="{url_edicao}">Editar Lista</a>',
            html,
        )
        self.assertIn(
            f'<a class="listas-fornecedor-btn listas-fornecedor-mobile-action" data-mobile-action-feedback href="{url_edicao}">Editar Lista</a>',
            html,
        )
        self.assertNotIn(f'<button type="button" class="listas-fornecedor-btn" href="{url_edicao}">Editar Lista</button>', html)

    def test_consulta_mobile_nao_mostra_editar_para_lista_cancelada(self):
        lista = self._criar_lista_fornecedor_conferencia(
            "Produto Mobile Editar Cancelada",
            status=ListaCompraFornecedor.STATUS_CANCELADA,
        )

        resposta = self.client.get(
            reverse("estoque:compras_listas_fornecedor"),
            {"mobile": "canceladas"},
            secure=True,
        )

        url_edicao = reverse("estoque:compras_lista_fornecedor_editar", kwargs={"pk": lista.pk})
        self.assertContains(resposta, f'data-mobile-lista-id="{lista.id}"')
        self.assertNotContains(resposta, f'href="{url_edicao}"')

    def test_consulta_mobile_tem_feedback_visual_seguro_nas_acoes(self):
        resposta = self.client.get(reverse("estoque:compras_listas_fornecedor"), secure=True)

        self.assertContains(resposta, "data-mobile-action-feedback")
        self.assertContains(resposta, ".listas-fornecedor-btn:active")
        self.assertContains(resposta, ".listas-fornecedor-btn.is-pressed")
        self.assertContains(resposta, ".listas-fornecedor-btn.is-loading")
        self.assertContains(resposta, "pointer-events: auto")
        self.assertContains(resposta, 'textContent = "Abrindo..."')
        self.assertContains(resposta, 'textContent = "Cancelando..."')
        self.assertContains(resposta, 'closest("a[data-mobile-action-feedback]")')

    def test_edicao_lista_fornecedor_tem_layout_mobile_em_cards_sem_tabela_horizontal(self):
        resposta = self.client.get(
            reverse("estoque:compras_lista_fornecedor_editar", kwargs={"pk": self.lista.pk}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'id="mobileSugestaoProdutos"')
        self.assertContains(resposta, 'class="sugestao-mobile-card"')
        self.assertContains(resposta, "Sugestao compra")
        self.assertContains(resposta, "Quantidade/compra")
        self.assertContains(resposta, 'data-label="Produto"')
        self.assertContains(resposta, 'data-label="Quantidade/compra"')
        self.assertContains(resposta, ".sugestao-manual-table colgroup")
        self.assertContains(resposta, ".sugestao-manual-table td::before")
        self.assertContains(resposta, "overflow-x: hidden;")
        self.assertContains(resposta, 'id="btnAdicionarProdutoSugestao"')

    def test_consulta_desktop_mantem_regra_atual_do_filtro_de_status(self):
        lista_aberta = self._criar_lista_fornecedor_conferencia("Produto Desktop Aberta")
        lista_enviada = self._criar_lista_fornecedor_conferencia(
            "Produto Desktop Enviada",
            status=ListaCompraFornecedor.STATUS_ENVIADA,
        )

        resposta = self.client.get(
            reverse("estoque:compras_listas_fornecedor"),
            {"status": ListaCompraFornecedor.STATUS_ENVIADA},
            secure=True,
        )

        listas_ids = {lista.id for lista in resposta.context["listas"]}
        self.assertIn(lista_enviada.id, listas_ids)
        self.assertNotIn(lista_aberta.id, listas_ids)

    def test_detalhe_exibe_checklist_de_conferencia(self):
        resposta = self.client.get(
            reverse("estoque:compras_lista_fornecedor_detalhe", kwargs={"pk": self.lista.pk}),
            secure=True,
        )

        self.assertContains(resposta, "Produto confirmado?")
        self.assertContains(resposta, "Qtd. correta?")
        self.assertContains(resposta, "Qtd. entregue")
        self.assertContains(resposta, "+ Obs.")
        self.assertNotContains(resposta, "Previa da conferencia")
        self.assertNotContains(resposta, 'id="conferenciaHistoricoTitulo">Resumo da conferencia')
        self.assertContains(resposta, "data-unidade")
        self.assertContains(resposta, "Faltou ${diff}")
        self.assertContains(resposta, "Sobrou ${diff}")
        self.assertContains(resposta, "function atualizarResumoConferencia")
        self.assertContains(resposta, "grupos.pendente.length")
        self.assertContains(resposta, "resumoAtual.innerHTML = ''")
        self.assertContains(resposta, "Resumo da conferencia")
        self.assertContains(resposta, "Mostrar so pendencias")
        self.assertContains(resposta, "Limpar conferencia")
        self.assertContains(resposta, "Salvar conferencia")
        self.assertContains(resposta, "Conferencia incompleta")
        self.assertContains(resposta, "Confira todos os produtos antes de salvar")
        self.assertContains(resposta, "Entendi")
        self.assertContains(resposta, "Limpar conferencia?")
        self.assertContains(resposta, "Deseja mesmo limpar toda a conferencia?")
        self.assertContains(resposta, "Sim, limpar")
        self.assertContains(resposta, "Cancelar")
        self.assertContains(resposta, 'type="button" tabindex="0" class="conferencia-modal-btn cancelar" id="btn-cancelar-limpar-conferencia"')
        self.assertContains(resposta, 'type="button" tabindex="0" class="conferencia-modal-btn confirmar" id="btn-confirmar-limpar-conferencia"')
        self.assertContains(resposta, "requestAnimationFrame(function ()")
        self.assertContains(resposta, "}, 120);")
        self.assertContains(resposta, "btnCancelar.focus({ preventScroll: true })")
        self.assertContains(resposta, "atualizarResumoConferencia")
        self.assertContains(resposta, "formConferencia.addEventListener('submit'")
        self.assertContains(resposta, "encontrarPrimeiroItemInvalido")
        self.assertContains(resposta, "event.preventDefault()")
        self.assertNotContains(resposta, 'id="conferenciaResumo"')
        self.assertNotContains(resposta, 'id="resumo_total"')
        self.assertNotContains(resposta, 'id="resumo_conferidos"')
        self.assertNotContains(resposta, 'id="resumo_pendentes"')
        self.assertNotContains(resposta, 'id="resumo_ok"')
        self.assertNotContains(resposta, 'id="resumo_faltou"')
        self.assertNotContains(resposta, 'id="resumo_veio_a_mais"')
        self.assertNotContains(resposta, 'id="resumo_nao_veio"')
        self.assertNotContains(resposta, "Marcar tudo como confirmado")
        self.assertNotContains(resposta, "Chegou certo")
        self.assertNotContains(resposta, "Nao veio")
        self.assertNotContains(resposta, "Editar qtd.")
        self.assertNotContains(resposta, "Qtd. recebida")
        self.assertNotContains(resposta, "Veio")
        self.assertNotContains(resposta, "window.confirm")

    def test_detalhe_exibe_resumo_compacto_da_conferencia_salva(self):
        produto_nao_veio = Produto.objects.create(
            nome="Produto Nao Chegou",
            preco_compra=Decimal("10.00"),
            preco_vista=Decimal("15.00"),
            preco_prazo=Decimal("16.00"),
            quantidade=Decimal("10.000"),
        )
        produto_faltou = Produto.objects.create(
            nome="Produto Faltou",
            preco_compra=Decimal("10.00"),
            preco_vista=Decimal("15.00"),
            preco_prazo=Decimal("16.00"),
            quantidade=Decimal("10.000"),
        )
        produto_sobrou = Produto.objects.create(
            nome="Produto Sobrou",
            preco_compra=Decimal("10.00"),
            preco_vista=Decimal("15.00"),
            preco_prazo=Decimal("16.00"),
            quantidade=Decimal("10.000"),
        )
        item_nao_veio = ItemListaCompraFornecedor.objects.create(
            lista=self.lista,
            produto=produto_nao_veio,
            estoque_atual=Decimal("10.000"),
            estoque_minimo=Decimal("0.000"),
            quantidade_final=Decimal("2.000"),
            quantidade_recebida=Decimal("0.000"),
            unidade="UN",
            preco_compra=Decimal("10.00"),
            total=Decimal("20.00"),
            status_conferencia=ItemListaCompraFornecedor.STATUS_CONFERENCIA_NAO_VEIO,
            observacao_conferencia="Caixa amassada",
            conferido=True,
        )
        item_faltou = ItemListaCompraFornecedor.objects.create(
            lista=self.lista,
            produto=produto_faltou,
            estoque_atual=Decimal("10.000"),
            estoque_minimo=Decimal("0.000"),
            quantidade_final=Decimal("2.000"),
            quantidade_recebida=Decimal("1.000"),
            unidade="UN",
            preco_compra=Decimal("10.00"),
            total=Decimal("20.00"),
            status_conferencia=ItemListaCompraFornecedor.STATUS_CONFERENCIA_FALTOU,
            conferido=True,
        )
        ItemListaCompraFornecedor.objects.create(
            lista=self.lista,
            produto=produto_sobrou,
            estoque_atual=Decimal("10.000"),
            estoque_minimo=Decimal("0.000"),
            quantidade_final=Decimal("2.000"),
            quantidade_recebida=Decimal("3.000"),
            unidade="UN",
            preco_compra=Decimal("10.00"),
            total=Decimal("20.00"),
            status_conferencia=ItemListaCompraFornecedor.STATUS_CONFERENCIA_VEIO_A_MAIS,
            conferido=True,
        )
        self.item.quantidade_recebida = Decimal("2.000")
        self.item.status_conferencia = ItemListaCompraFornecedor.STATUS_CONFERENCIA_OK
        self.item.conferido = True
        self.item.save(update_fields=["quantidade_recebida", "status_conferencia", "conferido"])

        resposta = self.client.get(
            reverse("estoque:compras_lista_fornecedor_detalhe", kwargs={"pk": self.lista.pk}),
            secure=True,
        )

        self.assertNotContains(resposta, "Previa da conferencia")
        self.assertContains(resposta, "Resumo da conferencia salva")
        self.assertContains(resposta, "Editar conferencia")
        self.assertContains(resposta, "Salvar alteracoes")
        self.assertContains(resposta, "data-conferencia-salva=\"1\"")
        self.assertContains(resposta, "definirModoEdicaoConferencia")
        self.assertContains(resposta, "Corretos:")
        self.assertContains(resposta, "Nao chegaram:")
        self.assertContains(resposta, "Com falta:")
        self.assertContains(resposta, "Com sobra:")
        self.assertContains(resposta, "Pendentes:")
        self.assertContains(resposta, item_nao_veio.produto.nome)
        self.assertContains(resposta, item_faltou.produto.nome)
        self.assertContains(resposta, "pediu 2.000 UN, chegou 1.000 UN")
        self.assertContains(resposta, "Produto Sobrou")
        self.assertContains(resposta, "Observacoes:")
        self.assertContains(resposta, "Caixa amassada")
        self.assertContains(resposta, "Produto confirmado?")
        self.assertContains(resposta, "Qtd. correta?")
        self.assertContains(resposta, "Qtd. entregue")
        self.assertContains(resposta, "+ Obs.")

    def test_detalhe_exibe_comparacao_final_da_lista_conferida(self):
        produto_nao_veio = Produto.objects.create(
            nome="Produto Nao Chegou Comparacao",
            preco_compra=Decimal("30.00"),
            preco_vista=Decimal("35.00"),
            preco_prazo=Decimal("36.00"),
            quantidade=Decimal("8.000"),
        )
        produto_faltou = Produto.objects.create(
            nome="Produto Faltou Comparacao",
            preco_compra=Decimal("20.49"),
            preco_vista=Decimal("25.00"),
            preco_prazo=Decimal("26.00"),
            quantidade=Decimal("9.000"),
        )
        produto_sobrou = Produto.objects.create(
            nome="Produto Sobrou Comparacao",
            preco_compra=Decimal("5.00"),
            preco_vista=Decimal("8.00"),
            preco_prazo=Decimal("9.00"),
            quantidade=Decimal("7.000"),
        )
        item_nao_veio = ItemListaCompraFornecedor.objects.create(
            lista=self.lista,
            produto=produto_nao_veio,
            estoque_atual=Decimal("8.000"),
            estoque_minimo=Decimal("0.000"),
            quantidade_final=Decimal("3.000"),
            quantidade_recebida=Decimal("0.000"),
            unidade="CX",
            preco_compra=Decimal("30.00"),
            preco_unitario=Decimal("30.00"),
            total=Decimal("90.00"),
            status_conferencia=ItemListaCompraFornecedor.STATUS_CONFERENCIA_NAO_VEIO,
            conferido=True,
        )
        item_faltou = ItemListaCompraFornecedor.objects.create(
            lista=self.lista,
            produto=produto_faltou,
            estoque_atual=Decimal("9.000"),
            estoque_minimo=Decimal("0.000"),
            quantidade_final=Decimal("2.000"),
            quantidade_recebida=Decimal("1.000"),
            unidade="UN",
            preco_compra=Decimal("20.49"),
            preco_unitario=Decimal("20.49"),
            total=Decimal("40.98"),
            status_conferencia=ItemListaCompraFornecedor.STATUS_CONFERENCIA_FALTOU,
            conferido=True,
        )
        item_sobrou = ItemListaCompraFornecedor.objects.create(
            lista=self.lista,
            produto=produto_sobrou,
            estoque_atual=Decimal("7.000"),
            estoque_minimo=Decimal("0.000"),
            quantidade_final=Decimal("1.000"),
            quantidade_recebida=Decimal("3.000"),
            unidade="UN",
            preco_compra=Decimal("5.00"),
            preco_unitario=Decimal("5.00"),
            total=Decimal("5.00"),
            status_conferencia=ItemListaCompraFornecedor.STATUS_CONFERENCIA_VEIO_A_MAIS,
            conferido=True,
        )
        self.item.preco_unitario = Decimal("10.00")
        self.item.total = Decimal("20.00")
        self.item.quantidade_recebida = Decimal("2.000")
        self.item.status_conferencia = ItemListaCompraFornecedor.STATUS_CONFERENCIA_OK
        self.item.conferido = True
        self.item.save()
        compras_antes = Compra.objects.count()
        movimentos_antes = MovimentoFinanceiro.objects.count()
        contas_pagar_antes = ContaPagar.objects.count()
        estoque_produto_original = self.produto.quantidade

        resposta = self.client.get(
            reverse("estoque:compras_lista_fornecedor_detalhe", kwargs={"pk": self.lista.pk}),
            secure=True,
        )

        comparacao = resposta.context["comparacao_conferencia"]
        self.assertTrue(comparacao["exibir"])
        self.assertEqual(comparacao["totais"]["planejado"], Decimal("155.98"))
        self.assertEqual(comparacao["totais"]["real"], Decimal("55.49"))
        self.assertEqual(comparacao["totais"]["diferenca"], Decimal("-100.49"))
        self.assertEqual(comparacao["diferenca_abs"], Decimal("100.49"))
        self.assertEqual(comparacao["diferenca_direcao"], "abaixo")
        self.assertEqual(comparacao["totais"]["nao_chegaram"], Decimal("90.00"))
        self.assertEqual(comparacao["totais"]["faltas"], Decimal("20.49"))
        self.assertEqual(comparacao["totais"]["sobras"], Decimal("10.00"))
        itens_por_produto = {item["produto"]: item for item in comparacao["itens"]}
        self.assertEqual(itens_por_produto[self.produto.nome]["diferenca_valor"], Decimal("0.00"))
        self.assertFalse(itens_por_produto[self.produto.nome]["tem_diferenca"])
        self.assertEqual(itens_por_produto[item_nao_veio.produto.nome]["valor_real"], Decimal("0.00"))
        self.assertEqual(itens_por_produto[item_nao_veio.produto.nome]["situacao"], "Nao chegou")
        self.assertEqual(itens_por_produto[item_faltou.produto.nome]["diferenca_valor"], Decimal("-20.49"))
        self.assertEqual(itens_por_produto[item_sobrou.produto.nome]["diferenca_valor"], Decimal("10.00"))
        self.assertContains(resposta, "Comparação da lista conferida")
        self.assertContains(resposta, "Total planejado")
        self.assertContains(resposta, "Total real recebido")
        self.assertContains(resposta, "Diferença total")
        self.assertContains(resposta, "Mostrar todos")
        self.assertContains(resposta, "Mostrar só diferenças")
        self.assertContains(resposta, "Atenção: a lista real recebida ficou R$")
        self.assertContains(resposta, "abaixo da lista planejada.")
        self.assertContains(resposta, "Produtos que explicam a diferença")
        self.assertContains(resposta, "Total explicado")
        self.assertContains(resposta, "Comparar com nota/boleto")
        self.assertContains(resposta, "Total real recebido")
        self.assertContains(resposta, "Valor da nota/boleto")
        self.assertContains(resposta, "ainda não informado")
        self.assertContains(resposta, "Diferença nota x recebido")
        self.assertContains(resposta, "filtrar-diferencas")
        self.assertContains(resposta, "Produto Nao Chegou Comparacao")
        self.assertContains(resposta, "Nao chegou")
        self.assertContains(resposta, "Com falta")
        self.assertContains(resposta, "Com sobra")
        self.assertEqual(Compra.objects.count(), compras_antes)
        self.assertEqual(MovimentoFinanceiro.objects.count(), movimentos_antes)
        self.assertEqual(ContaPagar.objects.count(), contas_pagar_antes)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, estoque_produto_original)

    def test_detalhe_comparacao_sem_diferenca_mostra_mensagem(self):
        self.item.preco_unitario = Decimal("10.00")
        self.item.total = Decimal("20.00")
        self.item.quantidade_recebida = Decimal("2.000")
        self.item.status_conferencia = ItemListaCompraFornecedor.STATUS_CONFERENCIA_OK
        self.item.conferido = True
        self.item.save()

        resposta = self.client.get(
            reverse("estoque:compras_lista_fornecedor_detalhe", kwargs={"pk": self.lista.pk}),
            secure=True,
        )

        comparacao = resposta.context["comparacao_conferencia"]
        self.assertTrue(comparacao["exibir"])
        self.assertEqual(comparacao["totais"]["diferenca"], Decimal("0.00"))
        self.assertEqual(comparacao["itens_com_diferenca"], [])
        self.assertContains(resposta, "A lista real recebida bate com a lista planejada.")
        self.assertContains(resposta, "Nenhum produto com diferença. A lista recebida bate com a planejada.")
        self.assertContains(resposta, "Comparar com nota/boleto")

    def test_comparacao_e_revisao_final_usam_preco_da_lista_para_item_em_caixa(self):
        self.produto.nome = "Oleo Soja Soya 20/900Ml"
        self.produto.save(update_fields=["nome"])
        self.item.unidade = "CX"
        self.item.quantidade_final = Decimal("2.000")
        self.item.preco_compra = Decimal("150.00")
        self.item.preco_unitario = Decimal("7.50")
        self.item.total = Decimal("300.00")
        self.item.quantidade_recebida = Decimal("2.000")
        self.item.status_conferencia = ItemListaCompraFornecedor.STATUS_CONFERENCIA_OK
        self.item.conferido = True
        self.item.save()
        self.lista.total_lista = Decimal("300.00")
        self.lista.save(update_fields=["total_lista"])

        resposta = self.client.get(
            reverse("estoque:compras_lista_fornecedor_detalhe", kwargs={"pk": self.lista.pk}),
            secure=True,
        )

        comparacao = resposta.context["comparacao_conferencia"]
        item_comparacao = comparacao["itens"][0]
        self.assertTrue(comparacao["exibir"])
        self.assertEqual(item_comparacao["preco_unitario"], Decimal("150.00"))
        self.assertEqual(item_comparacao["valor_previsto"], Decimal("300.00"))
        self.assertEqual(item_comparacao["valor_real"], Decimal("300.00"))
        self.assertEqual(comparacao["totais"]["planejado"], Decimal("300.00"))
        self.assertEqual(comparacao["totais"]["real"], Decimal("300.00"))
        self.assertEqual(comparacao["totais"]["diferenca"], Decimal("0.00"))
        self.assertEqual(comparacao["totais"]["real"], self.lista.total_lista)
        self.assertEqual(self.lista.total_lista, Decimal("300.00"))
        self.assertContains(resposta, "Comparação da lista conferida")
        self.assertContains(resposta, "Revisao final antes de gerar compra")
        self.assertContains(resposta, "R$ 150,00")
        self.assertContains(resposta, "Total da lista:")
        self.assertContains(resposta, "R$ 300,00")
        self.assertContains(resposta, "Total final da compra: R$ 300,00")
        self.assertNotContains(resposta, "R$ 15,00")

    def test_salva_comparacao_nota_boleto_informativa(self):
        self.item.quantidade_recebida = Decimal("2.000")
        self.item.status_conferencia = ItemListaCompraFornecedor.STATUS_CONFERENCIA_OK
        self.item.conferido = True
        self.item.save()

        compras_antes = Compra.objects.count()
        movimentos_antes = MovimentoFinanceiro.objects.count()
        contas_pagar_antes = ContaPagar.objects.count()
        estoque_original = self.produto.quantidade

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_detalhe", kwargs={"pk": self.lista.pk}),
            {
                "acao": "salvar_comparacao_nota",
                "valor_nota_boleto": "25,50",
                "classificacao_diferenca_nota": "frete",
                "observacao_diferenca_nota": "Frete cobrado na nota.",
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        self.lista.refresh_from_db()
        self.assertEqual(self.lista.valor_nota_boleto, Decimal("25.50"))
        self.assertEqual(self.lista.classificacao_diferenca_nota, "frete")
        self.assertEqual(self.lista.observacao_diferenca_nota, "Frete cobrado na nota.")
        self.assertEqual(Compra.objects.count(), compras_antes)
        self.assertEqual(MovimentoFinanceiro.objects.count(), movimentos_antes)
        self.assertEqual(ContaPagar.objects.count(), contas_pagar_antes)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, estoque_original)

    def test_diferenca_nota_boleto_aparece_negativa_formatada(self):
        self.item.preco_unitario = Decimal("10.00")
        self.item.total = Decimal("20.00")
        self.item.quantidade_recebida = Decimal("2.000")
        self.item.status_conferencia = ItemListaCompraFornecedor.STATUS_CONFERENCIA_OK
        self.item.conferido = True
        self.item.save()
        self.lista.valor_nota_boleto = Decimal("-150.79")
        self.lista.classificacao_diferenca_nota = "desconto"
        self.lista.observacao_diferenca_nota = "Desconto lançado na nota."
        self.lista.save()

        resposta = self.client.get(
            reverse("estoque:compras_lista_fornecedor_detalhe", kwargs={"pk": self.lista.pk}),
            secure=True,
        )

        comparacao_nota = resposta.context["comparacao_nota_boleto"]
        self.assertEqual(comparacao_nota["valor_real"], Decimal("20.00"))
        self.assertEqual(comparacao_nota["diferenca"], Decimal("-170.79"))
        self.assertContains(resposta, "-R$ 170,79")
        self.assertContains(resposta, "Desconto")
        self.assertContains(resposta, "Desconto lançado na nota.")

    def test_post_comparacao_nota_boleto_valor_invalido_nao_quebra(self):
        self.item.quantidade_recebida = Decimal("2.000")
        self.item.status_conferencia = ItemListaCompraFornecedor.STATUS_CONFERENCIA_OK
        self.item.conferido = True
        self.item.save()

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_detalhe", kwargs={"pk": self.lista.pk}),
            {
                "acao": "salvar_comparacao_nota",
                "valor_nota_boleto": "abc",
                "classificacao_diferenca_nota": "frete",
                "observacao_diferenca_nota": "Nao deve salvar.",
            },
            follow=True,
            secure=True,
        )

        self.lista.refresh_from_db()
        self.assertIsNone(self.lista.valor_nota_boleto)
        self.assertEqual(self.lista.classificacao_diferenca_nota, "")
        self.assertContains(resposta, "Valor numerico invalido.")

    def test_salva_forma_pagamento_boleto_unico_informativa(self):
        self.lista.valor_nota_boleto = Decimal("300.00")
        self.lista.save()
        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_detalhe", kwargs={"pk": self.lista.pk}),
            {
                "acao": "salvar_pagamento_nota",
                "forma_cobranca_nota": "boleto_unico",
                "quantidade_boletos": "1",
                "parcela_vencimento_1": "2026-07-10",
                "parcela_valor_1": "300,00",
                "parcela_observacao_1": "Boleto unico",
                "observacao_pagamento_nota": "Pagamento em boleto unico.",
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        self.lista.refresh_from_db()
        self.assertEqual(self.lista.forma_cobranca_nota, ListaCompraFornecedor.FORMA_COBRANCA_BOLETO_UNICO)
        self.assertEqual(self.lista.observacao_pagamento_nota, "Pagamento em boleto unico.")
        parcela = self.lista.parcelas_nota.get()
        self.assertEqual(parcela.valor, Decimal("300.00"))
        self.assertEqual(str(parcela.data_vencimento), "2026-07-10")

    def test_salva_varios_boletos_quando_soma_bate_com_nota(self):
        self.lista.valor_nota_boleto = Decimal("1000.00")
        self.lista.save()

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_detalhe", kwargs={"pk": self.lista.pk}),
            {
                "acao": "salvar_pagamento_nota",
                "forma_cobranca_nota": "varios_boletos",
                "quantidade_boletos": "3",
                "parcela_vencimento_1": "2026-07-10",
                "parcela_valor_1": "333,34",
                "parcela_vencimento_2": "2026-07-17",
                "parcela_valor_2": "333,33",
                "parcela_vencimento_3": "2026-07-24",
                "parcela_valor_3": "333,33",
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        self.lista.refresh_from_db()
        self.assertEqual(self.lista.forma_cobranca_nota, ListaCompraFornecedor.FORMA_COBRANCA_VARIOS_BOLETOS)
        self.assertEqual(self.lista.parcelas_nota.count(), 3)
        self.assertEqual(
            sum(self.lista.parcelas_nota.values_list("valor", flat=True), Decimal("0.00")),
            Decimal("1000.00"),
        )

    def test_bloqueia_boletos_com_soma_diferente_da_nota(self):
        self.lista.valor_nota_boleto = Decimal("1000.00")
        self.lista.save()

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_detalhe", kwargs={"pk": self.lista.pk}),
            {
                "acao": "salvar_pagamento_nota",
                "forma_cobranca_nota": "varios_boletos",
                "quantidade_boletos": "2",
                "parcela_vencimento_1": "2026-07-10",
                "parcela_valor_1": "300,00",
                "parcela_vencimento_2": "2026-07-17",
                "parcela_valor_2": "300,00",
            },
            follow=True,
            secure=True,
        )

        self.lista.refresh_from_db()
        self.assertEqual(self.lista.forma_cobranca_nota, "")
        self.assertEqual(self.lista.parcelas_nota.count(), 0)
        self.assertContains(resposta, "A soma dos boletos precisa bater com o valor da nota.")

    def test_salvar_igual_lista(self):
        resposta = self._post_conferencia({f"quantidade_recebida_{self.item.id}": "2.000", f"observacao_conferencia_{self.item.id}": ""})
        self.assertEqual(resposta.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status_conferencia, ItemListaCompraFornecedor.STATUS_CONFERENCIA_OK)
        self.assertTrue(self.item.conferido)
        self.assertEqual(self.item.diferenca_conferencia, Decimal('0.000'))
        self.assertEqual(Compra.objects.filter(observacao__icontains=f"Lista de Compras #{self.lista.id}").count(), 0)

    def test_salvar_menor(self):
        resposta = self._post_conferencia({f"quantidade_recebida_{self.item.id}": "1.000"})
        self.assertEqual(resposta.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status_conferencia, ItemListaCompraFornecedor.STATUS_CONFERENCIA_FALTOU)
        self.assertTrue(self.item.conferido)

    def test_salvar_maior(self):
        resposta = self._post_conferencia({f"quantidade_recebida_{self.item.id}": "3.000"})
        self.assertEqual(resposta.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status_conferencia, ItemListaCompraFornecedor.STATUS_CONFERENCIA_VEIO_A_MAIS)
        self.assertTrue(self.item.conferido)

    def test_salvar_zero(self):
        resposta = self._post_conferencia({f"quantidade_recebida_{self.item.id}": "0"})
        self.assertEqual(resposta.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status_conferencia, ItemListaCompraFornecedor.STATUS_CONFERENCIA_NAO_VEIO)
        self.assertTrue(self.item.conferido)

    def test_quantidade_vazia_pendente(self):
        resposta = self._post_conferencia({f"quantidade_recebida_{self.item.id}": ""})
        # endpoint deve redirecionar de volta (salva e redireciona)
        self.assertEqual(resposta.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status_conferencia, ItemListaCompraFornecedor.STATUS_CONFERENCIA_PENDENTE)
        self.assertFalse(self.item.conferido)
        # quantidade_recebida deve ficar como None
        self.assertIsNone(self.item.quantidade_recebida)
        # nao deve criar movimentos financeiros nem alterar estoque
        self.assertEqual(MovimentoFinanceiro.objects.count(), 0)
        self.assertEqual(Produto.objects.get(pk=self.produto.pk).quantidade, Decimal("10.000"))
        # opcionalmente garantir redirecionamento para detalhe
        self.assertRedirects(
            resposta,
            reverse("estoque:compras_lista_fornecedor_detalhe", kwargs={"pk": self.lista.pk}),
            fetch_redirect_response=False,
        )

    def test_detalhe_gera_link_externo_com_conferente(self):
        francisco = Funcionario.objects.create(
            nome="Francisco Miranda",
            telefone_whatsapp="85999990001",
            ativo=True,
            pode_receber_checklist=True,
        )
        Funcionario.objects.create(
            nome="Roseli Da Costa Gama",
            telefone_whatsapp="85999990002",
            ativo=True,
            pode_receber_checklist=True,
        )
        Funcionario.objects.create(
            nome="Funcionario Sem Checklist",
            telefone_whatsapp="85999990003",
            ativo=True,
            pode_receber_checklist=False,
        )
        Funcionario.objects.create(
            nome="Funcionario Inativo",
            telefone_whatsapp="85999990004",
            ativo=False,
            pode_receber_checklist=True,
        )

        resposta = self.client.get(
            reverse("estoque:compras_lista_fornecedor_detalhe", kwargs={"pk": self.lista.pk}),
            {"funcionario_checklist": str(francisco.id)},
            secure=True,
        )

        self.assertContains(resposta, "Checklist externa")
        self.assertContains(resposta, "Francisco Miranda")
        self.assertContains(resposta, "Roseli Da Costa Gama")
        self.assertNotContains(resposta, "Funcionario Sem Checklist")
        self.assertNotContains(resposta, "Funcionario Inativo")
        self.assertContains(resposta, "Conferente: <strong>Francisco Miranda</strong>", html=True)
        self.assertContains(resposta, "/checklist/")
        self.assertContains(resposta, "Enviar pelo WhatsApp")
        self.assertContains(resposta, "https://web.whatsapp.com/send?phone=85999990001")
        self.assertContains(resposta, "Fornecedor%20Teste")
        self.assertContains(resposta, "https%3A//sistema-de-vendas-e-estoque.onrender.com/checklist/")
        self.assertNotContains(resposta, "127.0.0.1")
        self.assertContains(resposta, "Abra%20o%20link%20abaixo%20no%20Chrome")
        self.assertNotIn("%3A", resposta.context["link_conferencia_externa"])
        self.assertIn("/checklist/v2-", resposta.context["link_conferencia_externa"])
        html_resposta = resposta.content.decode()
        href_link_gerado = re.search(r'href="([^"]+/checklist/v2-[^"]+)"', html_resposta).group(1)
        texto_link_gerado = re.search(r'>([^<]+/checklist/v2-[^<]+)</a>', html_resposta).group(1)
        self.assertEqual(href_link_gerado, resposta.context["link_conferencia_externa"])
        self.assertEqual(texto_link_gerado, resposta.context["link_conferencia_externa"])
        path_link_gerado = urlsplit(href_link_gerado).path
        resposta_link_gerado = self.client.get(
            path_link_gerado,
            secure=True,
        )
        self.assertEqual(resposta_link_gerado.status_code, 200)
        self.assertContains(resposta_link_gerado, "Conferencia de chegada")
        self.assertContains(resposta_link_gerado, "Francisco Miranda")
        self.assertNotContains(resposta_link_gerado, "Checklist indisponivel")

        resposta_avulso = self.client.get(
            reverse("estoque:compras_lista_fornecedor_detalhe", kwargs={"pk": self.lista.pk}),
            {"conferente_link": "Conferente Avulso"},
            secure=True,
        )

        self.assertContains(resposta_avulso, "Conferente: <strong>Conferente Avulso</strong>", html=True)
        self.assertContains(resposta_avulso, "https://sistema-de-vendas-e-estoque.onrender.com/checklist/")
        self.assertNotContains(resposta_avulso, "127.0.0.1")
        self.assertNotIn("%3A", resposta_avulso.context["link_conferencia_externa"])
        self.assertIn("/checklist/v2-", resposta_avulso.context["link_conferencia_externa"])
        self.assertNotContains(resposta_avulso, "Enviar pelo WhatsApp")

    def test_conferencia_externa_exibe_tela_isolada(self):
        token = self._liberar_checklist_externa("Francisco")
        self.assertTrue(token.startswith("v2-"))
        self.lista.refresh_from_db()
        self.assertEqual(self.lista.checklist_externa_token_hash, views._hash_token_conferencia_externa(token))
        resposta = self.client.get(
            reverse("estoque:compras_lista_fornecedor_conferencia_externa", kwargs={"token": token}),
            secure=True,
        )

        self.assertContains(resposta, "Conferencia de chegada")
        self.assertContains(resposta, "Fornecedor Teste")
        self.assertContains(resposta, "Francisco")
        self.assertContains(resposta, '<form method="post"')
        self.assertContains(resposta, f'action="{views._path_conferencia_externa_lista_fornecedor(token)}"')
        self.assertContains(resposta, 'name="csrfmiddlewaretoken"')
        self.assertContains(resposta, f'name="quantidade_recebida_{self.item.id}"')
        self.assertContains(resposta, f'name="observacao_conferencia_{self.item.id}"')
        self.assertContains(resposta, 'id="btnSalvarConferenciaExterna"')
        self.assertContains(resposta, 'type="submit"')
        self.assertContains(resposta, "Salvar conferencia")
        self.assertContains(resposta, 'data-conferencia-salva="0"')
        self.assertNotContains(resposta, "botaoSalvar.disabled = true")
        self.assertContains(resposta, "Buscar produto nesta lista")
        self.assertContains(resposta, "OK, proximo")
        self.assertNotContains(resposta, "Gerar Compra")
        self.assertNotContains(resposta, "Consultar Listas")
        self.assertNotContains(resposta, "WhatsApp")
        self.assertNotContains(resposta, "Cancelar Lista")
        self.assertNotContains(resposta, "Sistema de Vendas")

    @override_settings(ALLOWED_HOSTS=["sistema-de-vendas-e-estoque.onrender.com", "testserver"])
    def test_conferencia_externa_abre_url_literal_com_token_v2(self):
        token = self._liberar_checklist_externa("Francisco")
        self.assertTrue(token.startswith("v2-"))
        path_conferencia = views._path_conferencia_externa_lista_fornecedor(token)
        self.assertTrue(path_conferencia.startswith("/checklist/"))
        self.assertIn("/checklist/v2-", path_conferencia)
        self.assertNotIn("%3A", path_conferencia)
        self.lista.refresh_from_db()
        token_publico = path_conferencia.rstrip("/").rsplit("/", 1)[-1]
        self.assertEqual(token_publico, token)
        self.assertEqual(self.lista.checklist_externa_token_hash, views._hash_token_conferencia_externa(token_publico))

        resposta = self.client.get(
            path_conferencia,
            secure=True,
            HTTP_HOST="sistema-de-vendas-e-estoque.onrender.com",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Conferencia de chegada")
        self.assertContains(resposta, "Fornecedor Teste")
        self.assertContains(resposta, "Francisco")
        self.assertNotContains(resposta, "Checklist indisponivel")

    def test_conferencia_externa_abre_token_duplamente_codificado(self):
        token = self._liberar_checklist_externa("Francisco")
        path_conferencia = views._path_conferencia_externa_lista_fornecedor(token).replace("%", "%25")

        resposta = self.client.get(path_conferencia, secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Conferencia de chegada")
        self.assertContains(resposta, "Fornecedor Teste")
        self.assertNotContains(resposta, "Checklist indisponivel")
        self.assertContains(resposta, f'action="{views._path_conferencia_externa_lista_fornecedor(token)}"')

    def test_conferencia_externa_lista_inexistente_mostra_mensagem(self):
        lista_inexistente = types.SimpleNamespace(pk=self.lista.pk + 999)
        token_render_lista_9 = views._token_conferencia_externa_lista_fornecedor(
            lista_inexistente,
            "Lincoln Albuquerque Neiva",
        )
        resposta = self.client.get(
            f"/compras/listas-fornecedor/conferencia-externa/{token_render_lista_9}/",
            secure=True,
        )

        self.assertEqual(resposta.status_code, 404)
        self.assertContains(resposta, "Checklist indisponivel", status_code=404)
        self.assertContains(resposta, "lista de compras nao foi encontrada", status_code=404)

    def test_conferencia_externa_token_invalido_mostra_mensagem(self):
        token_invalido = self._liberar_checklist_externa("Francisco") + "x"
        resposta = self.client.get(
            f"/compras/listas-fornecedor/conferencia-externa/{token_invalido}/",
            secure=True,
        )

        self.assertEqual(resposta.status_code, 404)
        self.assertContains(resposta, "Checklist indisponivel", status_code=404)
        self.assertContains(resposta, "link de checklist esta invalido", status_code=404)

    def test_conferencia_externa_salva_conferencia(self):
        token = self._liberar_checklist_externa("Francisco")
        with self.assertLogs("estoque.views", level="INFO") as logs:
            resposta = self.client.post(
                views._path_conferencia_externa_lista_fornecedor(token),
                {f"quantidade_recebida_{self.item.id}": "2.000", f"observacao_conferencia_{self.item.id}": "OK"},
                secure=True,
            )

        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(resposta["Location"], views._path_conferencia_externa_lista_fornecedor(token))
        logs_texto = "\n".join(logs.output)
        self.assertIn("POST recebido", logs_texto)
        self.assertIn("token validado", logs_texto)
        self.assertIn("lista encontrada", logs_texto)
        self.assertIn("salvamento concluido", logs_texto)
        self.assertIn("itens_processados=1", logs_texto)
        self.assertIn("redirecionando apos POST", logs_texto)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status_conferencia, ItemListaCompraFornecedor.STATUS_CONFERENCIA_OK)
        self.assertTrue(self.item.conferido)
        self.assertEqual(self.item.observacao_conferencia, "OK")
        self.lista.refresh_from_db()
        self.assertIsNotNone(self.lista.checklist_externa_token_usado_em)
        externa_salva = self.client.get(resposta["Location"], secure=True)
        self.assertEqual(externa_salva.status_code, 200)
        self.assertContains(externa_salva, "Checklist já enviada. Esta tela está disponível apenas para consulta. Para alterar, gere uma nova liberação pelo desktop.")
        self.assertContains(externa_salva, "Produto Conferencia")
        self.assertContains(externa_salva, "Resumo da conferencia")
        self.assertNotContains(externa_salva, '<form method="post"')
        self.assertContains(externa_salva, f'name="quantidade_recebida_{self.item.id}"')
        self.assertContains(externa_salva, f'name="observacao_conferencia_{self.item.id}"')
        self.assertContains(externa_salva, "readonly")
        self.assertContains(externa_salva, "disabled")
        self.assertNotContains(externa_salva, 'id="btnSalvarConferenciaExterna"')
        self.assertNotContains(externa_salva, "Salvar conferencia")
        self.assertNotContains(externa_salva, "Salvar alteracoes")
        self.assertNotContains(externa_salva, "Editar conferencia")
        self.assertNotContains(externa_salva, "Mostrar so pendencias")
        self.assertNotContains(externa_salva, "Mostrar todos")
        self.assertNotContains(externa_salva, '<button type="button" class="opcao')
        self.assertNotContains(externa_salva, "OK, proximo")
        self.assertNotContains(externa_salva, "addEventListener")
        self.assertFalse(externa_salva.context["checklist_externa_editavel"])
        self.assertTrue(externa_salva.context["consulta_somente_leitura"])
        detalhe = self.client.get(reverse("estoque:compras_lista_fornecedor_detalhe", kwargs={"pk": self.lista.pk}), secure=True)
        self.assertContains(detalhe, "Resumo da conferencia salva")
        self.assertContains(detalhe, "Corretos:")

    def test_conferencia_externa_salva_bloqueia_post_repetido(self):
        token = self._liberar_checklist_externa("Francisco")
        self.client.post(
            views._path_conferencia_externa_lista_fornecedor(token),
            {f"quantidade_recebida_{self.item.id}": "2.000", f"observacao_conferencia_{self.item.id}": "OK"},
            secure=True,
        )

        resposta = self.client.get(views._path_conferencia_externa_lista_fornecedor(token), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Checklist já enviada. Esta tela está disponível apenas para consulta. Para alterar, gere uma nova liberação pelo desktop.")
        self.assertNotContains(resposta, 'id="btnSalvarConferenciaExterna"')
        self.assertNotContains(resposta, "Editar conferencia")
        self.assertNotContains(resposta, "Mostrar so pendencias")
        self.assertNotContains(resposta, "Mostrar todos")

        resposta_edicao = self.client.post(
            views._path_conferencia_externa_lista_fornecedor(token),
            {f"quantidade_recebida_{self.item.id}": "1.000", f"observacao_conferencia_{self.item.id}": "Editado"},
            secure=True,
        )
        self.assertEqual(resposta_edicao.status_code, 404)
        self.assertContains(resposta_edicao, "checklist ja foi enviada", status_code=404)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status_conferencia, ItemListaCompraFornecedor.STATUS_CONFERENCIA_OK)
        self.assertEqual(self.item.observacao_conferencia, "OK")

    def test_conferencia_externa_usada_apos_janela_de_consulta_fica_indisponivel(self):
        token = self._liberar_checklist_externa("Francisco")
        self.client.post(
            views._path_conferencia_externa_lista_fornecedor(token),
            {f"quantidade_recebida_{self.item.id}": "2.000", f"observacao_conferencia_{self.item.id}": "OK"},
            secure=True,
        )
        self.lista.refresh_from_db()
        self.lista.checklist_externa_token_usado_em = timezone.now() - views.LISTA_FORNECEDOR_CONFERENCIA_EXTERNA_CONSULTA_USADA - timedelta(minutes=1)
        self.lista.save(update_fields=["checklist_externa_token_usado_em", "atualizado_em"])

        resposta = self.client.get(views._path_conferencia_externa_lista_fornecedor(token), secure=True)

        self.assertEqual(resposta.status_code, 404)
        self.assertContains(resposta, "Checklist indisponivel", status_code=404)
        self.assertContains(resposta, "checklist ja foi enviada", status_code=404)

    def test_conferencia_externa_salva_token_normalizado(self):
        token = self._liberar_checklist_externa("Francisco")
        resposta = self.client.post(
            f"/checklist/{token}/",
            {f"quantidade_recebida_{self.item.id}": "2.000", f"observacao_conferencia_{self.item.id}": "Cru"},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(resposta["Location"], views._path_conferencia_externa_lista_fornecedor(token))
        self.item.refresh_from_db()
        self.assertEqual(self.item.status_conferencia, ItemListaCompraFornecedor.STATUS_CONFERENCIA_OK)
        self.assertTrue(self.item.conferido)
        self.assertEqual(self.item.observacao_conferencia, "Cru")

    def test_conferencia_externa_salva_token_duplamente_codificado(self):
        token = self._liberar_checklist_externa("Francisco")
        path_conferencia = views._path_conferencia_externa_lista_fornecedor(token).replace("%", "%25")

        resposta = self.client.post(
            path_conferencia,
            {f"quantidade_recebida_{self.item.id}": "1.000", f"observacao_conferencia_{self.item.id}": "Faltou"},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(resposta["Location"], views._path_conferencia_externa_lista_fornecedor(token))
        self.item.refresh_from_db()
        self.assertEqual(self.item.status_conferencia, ItemListaCompraFornecedor.STATUS_CONFERENCIA_FALTOU)
        self.assertTrue(self.item.conferido)
        self.assertEqual(self.item.observacao_conferencia, "Faltou")

    def test_nova_liberacao_gera_novo_link_e_invalida_antigo(self):
        token_antigo = self._liberar_checklist_externa("Francisco")
        self.client.post(
            views._path_conferencia_externa_lista_fornecedor(token_antigo),
            {f"quantidade_recebida_{self.item.id}": "2.000", f"observacao_conferencia_{self.item.id}": "OK"},
            secure=True,
        )
        token_novo = self._liberar_checklist_externa("Francisco")

        self.assertNotEqual(token_antigo, token_novo)
        resposta_antiga = self.client.get(views._path_conferencia_externa_lista_fornecedor(token_antigo), secure=True)
        resposta_nova = self.client.get(views._path_conferencia_externa_lista_fornecedor(token_novo), secure=True)

        self.assertEqual(resposta_antiga.status_code, 404)
        self.assertContains(resposta_antiga, "link nao e mais valido", status_code=404)
        self.assertContains(resposta_nova, "Conferencia de chegada")
        self.assertContains(resposta_nova, "Francisco")

    def test_conferencia_externa_link_expirado_nao_abre_edicao(self):
        token = self._liberar_checklist_externa("Francisco")
        self.lista.checklist_externa_token_expira_em = timezone.now() - timedelta(minutes=1)
        self.lista.save(update_fields=["checklist_externa_token_expira_em", "atualizado_em"])

        resposta = self.client.get(views._path_conferencia_externa_lista_fornecedor(token), secure=True)

        self.assertContains(resposta, "link expirou", status_code=404)
        self.assertContains(resposta, "nova liberacao", status_code=404)

    def test_conferencia_interna_desktop_continua_salvando_com_link_externo_usado(self):
        token = self._liberar_checklist_externa("Francisco")
        self.client.post(
            views._path_conferencia_externa_lista_fornecedor(token),
            {f"quantidade_recebida_{self.item.id}": "2.000", f"observacao_conferencia_{self.item.id}": "OK externo"},
            secure=True,
        )

        resposta = self._post_conferencia({
            f"quantidade_recebida_{self.item.id}": "1.000",
            f"observacao_conferencia_{self.item.id}": "Desktop",
        })

        self.assertEqual(resposta.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status_conferencia, ItemListaCompraFornecedor.STATUS_CONFERENCIA_FALTOU)
        self.assertEqual(self.item.observacao_conferencia, "Desktop")


class FornecedorFrequenciaVisitaFormTests(TestCase):
    def _produto_teste(self, nome="Produto Fornecedor Frequencia"):
        return Produto.objects.create(
            nome=nome,
            preco_compra=Decimal("10.00"),
            preco_vista=Decimal("15.00"),
            preco_prazo=Decimal("16.00"),
            quantidade=Decimal("1.000"),
        )

    def _dados_fornecedor(self, **alteracoes):
        dados = {
            "nome": "Fornecedor Frequencia",
            "nome_fantasia": "",
            "telefone_whatsapp": "",
            "cidade": "",
            "bairro": "",
            "prazos_pagamento_padrao": "",
            "observacao": "",
            "ativo": "on",
            "frequencia_visita_intervalo_dias": "",
            "frequencia_visita_dia_semana": "",
            "frequencia_visita_data_referencia": "",
            "produtos": [],
            "contatos-TOTAL_FORMS": "3",
            "contatos-INITIAL_FORMS": "0",
            "contatos-MIN_NUM_FORMS": "0",
            "contatos-MAX_NUM_FORMS": "1000",
        }
        for indice in range(3):
            dados.update({
                f"contatos-{indice}-id": "",
                f"contatos-{indice}-nome": "",
                f"contatos-{indice}-cargo": "",
                f"contatos-{indice}-telefone_whatsapp": "",
                f"contatos-{indice}-principal": "",
                f"contatos-{indice}-ativo": "",
                f"contatos-{indice}-observacao": "",
            })
        dados.update(alteracoes)
        return dados

    def _dados_frequencia_valida(self, **alteracoes):
        dados = self._dados_fornecedor(
            frequencia_visita_ativa="on",
            frequencia_visita_intervalo_dias="7",
            frequencia_visita_dia_semana=str(Fornecedor.DIA_SEMANA_TERCA),
            frequencia_visita_data_referencia="2026-07-07",
        )
        dados.update(alteracoes)
        return dados

    def test_fornecedor_form_tem_campos_de_frequencia(self):
        form = FornecedorForm()

        self.assertIn("frequencia_visita_ativa", form.fields)
        self.assertIn("frequencia_visita_intervalo_dias", form.fields)
        self.assertIn("frequencia_visita_dia_semana", form.fields)
        self.assertIn("frequencia_visita_data_referencia", form.fields)

    def test_fornecedor_form_tem_labels_de_frequencia(self):
        form = FornecedorForm()

        self.assertEqual(form.fields["frequencia_visita_ativa"].label, "Controlar frequência de visita")
        self.assertEqual(form.fields["frequencia_visita_intervalo_dias"].label, "Intervalo entre visitas")
        self.assertEqual(form.fields["frequencia_visita_dia_semana"].label, "Dia habitual da visita")
        self.assertEqual(form.fields["frequencia_visita_data_referencia"].label, "Data de referência")

    def test_cadastro_com_frequencia_desativada_funciona(self):
        form = FornecedorForm(data=self._dados_fornecedor())

        self.assertTrue(form.is_valid(), form.errors)
        fornecedor = form.save()
        self.assertFalse(fornecedor.frequencia_visita_ativa)
        self.assertIsNone(fornecedor.frequencia_visita_intervalo_dias)
        self.assertIsNone(fornecedor.frequencia_visita_dia_semana)
        self.assertIsNone(fornecedor.frequencia_visita_data_referencia)

    def test_cadastro_com_frequencia_semanal_valida_funciona(self):
        form = FornecedorForm(data=self._dados_frequencia_valida())

        self.assertTrue(form.is_valid(), form.errors)
        fornecedor = form.save()
        self.assertTrue(fornecedor.frequencia_visita_ativa)
        self.assertEqual(fornecedor.frequencia_visita_intervalo_dias, 7)
        self.assertEqual(fornecedor.frequencia_visita_dia_semana, Fornecedor.DIA_SEMANA_TERCA)
        self.assertEqual(fornecedor.frequencia_visita_data_referencia, date(2026, 7, 7))

    def test_cadastro_com_frequencia_quinzenal_valida_funciona(self):
        form = FornecedorForm(data=self._dados_frequencia_valida(frequencia_visita_intervalo_dias="14"))

        self.assertTrue(form.is_valid(), form.errors)
        fornecedor = form.save()
        self.assertEqual(fornecedor.frequencia_visita_intervalo_dias, 14)

    def test_edicao_carrega_valores_salvos(self):
        fornecedor = Fornecedor.objects.create(
            nome="Fornecedor Editar Frequencia",
            frequencia_visita_ativa=True,
            frequencia_visita_intervalo_dias=14,
            frequencia_visita_dia_semana=Fornecedor.DIA_SEMANA_TERCA,
            frequencia_visita_data_referencia=date(2026, 7, 7),
        )

        form = FornecedorForm(instance=fornecedor)

        self.assertEqual(form["frequencia_visita_intervalo_dias"].value(), 14)
        self.assertEqual(form["frequencia_visita_dia_semana"].value(), Fornecedor.DIA_SEMANA_TERCA)
        self.assertEqual(form["frequencia_visita_data_referencia"].value(), date(2026, 7, 7))

    def test_edicao_altera_intervalo_dia_e_referencia(self):
        fornecedor = Fornecedor.objects.create(
            nome="Fornecedor Altera Frequencia",
            frequencia_visita_ativa=True,
            frequencia_visita_intervalo_dias=7,
            frequencia_visita_dia_semana=Fornecedor.DIA_SEMANA_TERCA,
            frequencia_visita_data_referencia=date(2026, 7, 7),
        )
        dados = self._dados_frequencia_valida(
            nome=fornecedor.nome,
            frequencia_visita_intervalo_dias="14",
            frequencia_visita_dia_semana=str(Fornecedor.DIA_SEMANA_QUARTA),
            frequencia_visita_data_referencia="2026-07-08",
        )

        form = FornecedorForm(data=dados, instance=fornecedor)

        self.assertTrue(form.is_valid(), form.errors)
        fornecedor = form.save()
        self.assertEqual(fornecedor.frequencia_visita_intervalo_dias, 14)
        self.assertEqual(fornecedor.frequencia_visita_dia_semana, Fornecedor.DIA_SEMANA_QUARTA)
        self.assertEqual(fornecedor.frequencia_visita_data_referencia, date(2026, 7, 8))

    def test_desativar_frequencia_preserva_valores_auxiliares(self):
        fornecedor = Fornecedor.objects.create(
            nome="Fornecedor Preserva Frequencia",
            frequencia_visita_ativa=True,
            frequencia_visita_intervalo_dias=14,
            frequencia_visita_dia_semana=Fornecedor.DIA_SEMANA_TERCA,
            frequencia_visita_data_referencia=date(2026, 7, 7),
        )

        form = FornecedorForm(data=self._dados_fornecedor(nome=fornecedor.nome), instance=fornecedor)

        self.assertTrue(form.is_valid(), form.errors)
        fornecedor = form.save()
        self.assertFalse(fornecedor.frequencia_visita_ativa)
        self.assertEqual(fornecedor.frequencia_visita_intervalo_dias, 14)
        self.assertEqual(fornecedor.frequencia_visita_dia_semana, Fornecedor.DIA_SEMANA_TERCA)
        self.assertEqual(fornecedor.frequencia_visita_data_referencia, date(2026, 7, 7))

    def test_reativar_frequencia_recupera_valores_preservados(self):
        fornecedor = Fornecedor.objects.create(
            nome="Fornecedor Reativa Frequencia",
            frequencia_visita_ativa=True,
            frequencia_visita_intervalo_dias=14,
            frequencia_visita_dia_semana=Fornecedor.DIA_SEMANA_TERCA,
            frequencia_visita_data_referencia=date(2026, 7, 7),
        )
        form = FornecedorForm(data=self._dados_fornecedor(nome=fornecedor.nome), instance=fornecedor)
        self.assertTrue(form.is_valid(), form.errors)
        fornecedor = form.save()

        dados = self._dados_frequencia_valida(
            nome=fornecedor.nome,
            frequencia_visita_intervalo_dias=str(fornecedor.frequencia_visita_intervalo_dias),
            frequencia_visita_dia_semana=str(fornecedor.frequencia_visita_dia_semana),
            frequencia_visita_data_referencia=fornecedor.frequencia_visita_data_referencia.isoformat(),
        )
        form = FornecedorForm(data=dados, instance=fornecedor)

        self.assertTrue(form.is_valid(), form.errors)
        fornecedor = form.save()
        self.assertTrue(fornecedor.frequencia_visita_ativa)
        self.assertEqual(fornecedor.frequencia_visita_intervalo_dias, 14)

    def test_frequencia_ativa_sem_intervalo_mostra_erro_no_campo(self):
        form = FornecedorForm(data=self._dados_frequencia_valida(frequencia_visita_intervalo_dias=""))

        self.assertFalse(form.is_valid())
        self.assertIn("frequencia_visita_intervalo_dias", form.errors)

    def test_frequencia_ativa_com_campo_vazio_nao_preserva_valor_antigo(self):
        fornecedor = Fornecedor.objects.create(
            nome="Fornecedor Campo Vazio Frequencia",
            frequencia_visita_ativa=True,
            frequencia_visita_intervalo_dias=14,
            frequencia_visita_dia_semana=Fornecedor.DIA_SEMANA_TERCA,
            frequencia_visita_data_referencia=date(2026, 7, 7),
        )
        dados = self._dados_frequencia_valida(
            nome=fornecedor.nome,
            frequencia_visita_intervalo_dias="",
        )

        form = FornecedorForm(data=dados, instance=fornecedor)

        self.assertFalse(form.is_valid())
        self.assertIn("frequencia_visita_intervalo_dias", form.errors)
        fornecedor.refresh_from_db()
        self.assertEqual(fornecedor.frequencia_visita_intervalo_dias, 14)

    def test_intervalo_nao_multiplo_de_sete_mostra_erro_no_campo(self):
        form = FornecedorForm(data=self._dados_frequencia_valida(frequencia_visita_intervalo_dias="10"))

        self.assertFalse(form.is_valid())
        self.assertIn("frequencia_visita_intervalo_dias", form.errors)

    def test_frequencia_ativa_sem_dia_mostra_erro_no_campo(self):
        form = FornecedorForm(data=self._dados_frequencia_valida(frequencia_visita_dia_semana=""))

        self.assertFalse(form.is_valid())
        self.assertIn("frequencia_visita_dia_semana", form.errors)

    def test_frequencia_ativa_sem_referencia_mostra_erro_no_campo(self):
        form = FornecedorForm(data=self._dados_frequencia_valida(frequencia_visita_data_referencia=""))

        self.assertFalse(form.is_valid())
        self.assertIn("frequencia_visita_data_referencia", form.errors)

    def test_referencia_em_dia_diferente_mostra_erro_no_campo(self):
        form = FornecedorForm(data=self._dados_frequencia_valida(frequencia_visita_dia_semana=str(Fornecedor.DIA_SEMANA_SEGUNDA)))

        self.assertFalse(form.is_valid())
        self.assertIn("frequencia_visita_data_referencia", form.errors)

    def test_pagina_novo_fornecedor_renderiza_secao(self):
        resposta = self.client.get(reverse("estoque:fornecedor_novo"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Frequência de visita")
        self.assertContains(resposta, "Controlar frequência de visita")
        self.assertContains(resposta, 'id="fornecedorFrequenciaVisitaCampos"')

    def test_pagina_edicao_renderiza_valores_existentes(self):
        fornecedor = Fornecedor.objects.create(
            nome="Fornecedor Pagina Frequencia",
            frequencia_visita_ativa=True,
            frequencia_visita_intervalo_dias=14,
            frequencia_visita_dia_semana=Fornecedor.DIA_SEMANA_TERCA,
            frequencia_visita_data_referencia=date(2026, 7, 7),
        )

        resposta = self.client.get(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), secure=True)

        self.assertEqual(resposta.status_code, 200)
        form = resposta.context["form"]
        self.assertEqual(form["frequencia_visita_intervalo_dias"].value(), 14)
        self.assertEqual(form["frequencia_visita_data_referencia"].value(), date(2026, 7, 7))
        self.assertContains(resposta, 'value="2026-07-07"')

    def test_post_invalido_nao_salva_dados_incorretos(self):
        fornecedor = Fornecedor.objects.create(nome="Fornecedor Nao Salva Invalido")
        dados = self._dados_frequencia_valida(nome=fornecedor.nome, frequencia_visita_intervalo_dias="10")

        resposta = self.client.post(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), dados, secure=True)

        self.assertEqual(resposta.status_code, 200)
        fornecedor.refresh_from_db()
        self.assertFalse(fornecedor.frequencia_visita_ativa)
        self.assertIsNone(fornecedor.frequencia_visita_intervalo_dias)

    def test_produtos_e_contatos_continuam_funcionando(self):
        produto = self._produto_teste()
        dados = self._dados_frequencia_valida(
            produtos=[str(produto.pk)],
            **{
                "contatos-0-nome": "Maria Compras",
                "contatos-0-cargo": "Vendedora",
                "contatos-0-telefone_whatsapp": "85999990000",
                "contatos-0-principal": "on",
                "contatos-0-ativo": "on",
            },
        )

        resposta = self.client.post(reverse("estoque:fornecedor_novo"), dados, secure=True, follow=True)

        self.assertEqual(resposta.status_code, 200)
        fornecedor = Fornecedor.objects.get(nome="Fornecedor Frequencia")
        self.assertTrue(ProdutoFornecedor.objects.filter(fornecedor=fornecedor, produto=produto, ativo=True).exists())
        self.assertEqual(fornecedor.contatos.count(), 1)
        self.assertEqual(fornecedor.contatos.first().nome, "Maria Compras")


class FornecedorMascaraNormalizacaoTests(TestCase):
    def _produto_teste(self, nome="Produto Mascara Fornecedor"):
        return Produto.objects.create(
            nome=nome,
            preco_compra=Decimal("10.00"),
            preco_vista=Decimal("15.00"),
            preco_prazo=Decimal("16.00"),
            quantidade=Decimal("1.000"),
        )

    def _dados_fornecedor(self, **alteracoes):
        dados = {
            "nome": "Fornecedor Mascara",
            "nome_fantasia": "",
            "telefone_whatsapp": "",
            "cidade": "",
            "bairro": "",
            "prazos_pagamento_padrao": "",
            "observacao": "",
            "ativo": "on",
            "frequencia_visita_intervalo_dias": "",
            "frequencia_visita_dia_semana": "",
            "frequencia_visita_data_referencia": "",
            "produtos": [],
            "contatos-TOTAL_FORMS": "3",
            "contatos-INITIAL_FORMS": "0",
            "contatos-MIN_NUM_FORMS": "0",
            "contatos-MAX_NUM_FORMS": "1000",
        }
        for indice in range(3):
            dados.update({
                f"contatos-{indice}-id": "",
                f"contatos-{indice}-nome": "",
                f"contatos-{indice}-cargo": "",
                f"contatos-{indice}-telefone_whatsapp": "",
                f"contatos-{indice}-principal": "",
                f"contatos-{indice}-ativo": "",
                f"contatos-{indice}-observacao": "",
            })
        dados.update(alteracoes)
        return dados

    def test_data_referencia_tem_estrutura_compacta_e_responsiva(self):
        resposta = self.client.get(reverse("estoque:fornecedor_novo"), secure=True)

        self.assertContains(resposta, "fornecedor-visita-grid")
        self.assertContains(resposta, "fornecedor-visita-data")
        self.assertContains(resposta, "minmax(220px, 280px)")
        self.assertContains(resposta, ".fornecedor-visita-grid { grid-template-columns: 1fr; }")

    def test_campos_de_telefone_tem_atributos_de_mascara_no_formset(self):
        resposta = self.client.get(reverse("estoque:fornecedor_novo"), secure=True)

        self.assertContains(resposta, 'data-fornecedor-telefone="1"', count=10)
        self.assertContains(resposta, 'inputmode="tel"', count=10)
        self.assertContains(resposta, 'maxlength="15"', count=10)

    def test_telefone_11_digitos_e_apresentado_formatado(self):
        fornecedor = Fornecedor.objects.create(nome="Fornecedor Telefone 11", telefone_whatsapp="91999999999")

        resposta = self.client.get(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), secure=True)

        self.assertContains(resposta, 'value="(91) 99999-9999"')

    def test_telefone_10_digitos_e_apresentado_formatado(self):
        fornecedor = Fornecedor.objects.create(nome="Fornecedor Telefone 10", telefone_whatsapp="9133334444")

        resposta = self.client.get(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), secure=True)

        self.assertContains(resposta, 'value="(91) 3333-4444"')

    def test_telefone_pode_ser_apagado(self):
        fornecedor = Fornecedor.objects.create(nome="Fornecedor Apaga Telefone", telefone_whatsapp="91999999999")

        form = FornecedorForm(data=self._dados_fornecedor(nome=fornecedor.nome, telefone_whatsapp=""), instance=fornecedor)

        self.assertTrue(form.is_valid(), form.errors)
        fornecedor = form.save()
        self.assertIsNone(fornecedor.telefone_whatsapp)

    def test_edicao_de_numero_antigo_continua_funcionando(self):
        fornecedor = Fornecedor.objects.create(nome="Fornecedor Numero Antigo", telefone_whatsapp="91 99999 9999")

        resposta = self.client.get(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'value="(91) 99999-9999"')

    def test_armazenamento_de_telefone_usa_digitos(self):
        form = FornecedorForm(data=self._dados_fornecedor(telefone_whatsapp="(91) 99999-9999"))

        self.assertTrue(form.is_valid(), form.errors)
        fornecedor = form.save()
        self.assertEqual(fornecedor.telefone_whatsapp, "91999999999")

    def test_nome_fornecedor_e_normalizado(self):
        form = FornecedorForm(data=self._dados_fornecedor(nome="  ana   paula  "))

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().nome, "Ana Paula")

    def test_normalizacao_preserva_acentos_e_remove_espacos(self):
        form = FornecedorForm(data=self._dados_fornecedor(nome="  joão   da   silva  "))

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().nome, "João Da Silva")

    def test_nome_fantasia_e_normalizado(self):
        form = FornecedorForm(data=self._dados_fornecedor(nome_fantasia="  mercadinho  bom   preco "))

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().nome_fantasia, "Mercadinho Bom Preco")

    def test_cidade_e_bairro_sao_normalizados(self):
        form = FornecedorForm(data=self._dados_fornecedor(cidade="  sao   paulo ", bairro=" vila   maria "))

        self.assertTrue(form.is_valid(), form.errors)
        fornecedor = form.save()
        self.assertEqual(fornecedor.cidade, "Sao Paulo")
        self.assertEqual(fornecedor.bairro, "Vila Maria")

    def test_nome_e_cargo_do_contato_sao_normalizados(self):
        dados = self._dados_fornecedor(**{
            "contatos-0-nome": "  joao   silva ",
            "contatos-0-cargo": " vendedor   externo ",
            "contatos-0-telefone_whatsapp": "(91) 99999-9999",
            "contatos-0-ativo": "on",
        })

        resposta = self.client.post(reverse("estoque:fornecedor_novo"), dados, secure=True, follow=True)

        self.assertEqual(resposta.status_code, 200)
        contato = Fornecedor.objects.get(nome="Fornecedor Mascara").contatos.get()
        self.assertEqual(contato.nome, "Joao Silva")
        self.assertEqual(contato.cargo, "Vendedor Externo")
        self.assertEqual(contato.telefone_whatsapp, "91999999999")
        self.assertEqual(contato.telefone_whatsapp_normalizado, "91999999999")

    def test_observacao_nao_e_capitalizada_automaticamente(self):
        form = FornecedorForm(data=self._dados_fornecedor(observacao="observacao livre COM caixa misturada"))

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().observacao, "observacao livre COM caixa misturada")

    def test_produtos_e_contatos_existentes_continuam_funcionando(self):
        produto = self._produto_teste()
        dados = self._dados_fornecedor(
            produtos=[str(produto.pk)],
            **{
                "contatos-0-nome": "maria compras",
                "contatos-0-cargo": "financeiro",
                "contatos-0-telefone_whatsapp": "91999999999",
                "contatos-0-principal": "on",
                "contatos-0-ativo": "on",
            },
        )

        resposta = self.client.post(reverse("estoque:fornecedor_novo"), dados, secure=True, follow=True)

        self.assertEqual(resposta.status_code, 200)
        fornecedor = Fornecedor.objects.get(nome="Fornecedor Mascara")
        self.assertTrue(ProdutoFornecedor.objects.filter(fornecedor=fornecedor, produto=produto, ativo=True).exists())
        self.assertEqual(fornecedor.contatos.count(), 1)

    def test_frequencia_de_visita_continua_salvando_normalmente(self):
        form = FornecedorForm(data=self._dados_fornecedor(
            frequencia_visita_ativa="on",
            frequencia_visita_intervalo_dias="14",
            frequencia_visita_dia_semana=str(Fornecedor.DIA_SEMANA_TERCA),
            frequencia_visita_data_referencia="2026-07-07",
        ))

        self.assertTrue(form.is_valid(), form.errors)
        fornecedor = form.save()
        self.assertTrue(fornecedor.frequencia_visita_ativa)
        self.assertEqual(fornecedor.frequencia_visita_intervalo_dias, 14)


class FornecedorContatoTelefoneTests(TestCase):
    def setUp(self):
        self.fornecedor = Fornecedor.objects.create(nome="Fornecedor Telefones")
        self.contato = self.fornecedor.contatos.create(nome="Ana Paula", cargo="Vendedora")

    def criar_telefone(self, contato=None, **alteracoes):
        dados = {
            "contato": contato or self.contato,
            "numero": "91993152627",
            "tipo": FornecedorContatoTelefone.TIPO_CELULAR,
            "whatsapp": True,
            "principal": False,
            "ativo": True,
            "ordem": 1,
        }
        dados.update(alteracoes)
        return FornecedorContatoTelefone.objects.create(**dados)

    def test_cria_telefone_celular_valido(self):
        telefone = self.criar_telefone(principal=True)

        self.assertEqual(telefone.tipo, FornecedorContatoTelefone.TIPO_CELULAR)
        self.assertTrue(telefone.ativo)

    def test_salva_somente_digitos(self):
        telefone = self.criar_telefone(numero="(91) 99315-2627")

        self.assertEqual(telefone.numero, "91993152627")

    def test_aceita_numero_de_10_digitos(self):
        telefone = self.criar_telefone(numero="9132324444")

        self.assertEqual(telefone.numero, "9132324444")

    def test_aceita_numero_de_11_digitos(self):
        telefone = self.criar_telefone(numero="91993152627")

        self.assertEqual(telefone.numero, "91993152627")

    def test_rejeita_telefone_ativo_vazio(self):
        with self.assertRaises(ValidationError) as erro:
            self.criar_telefone(numero="")

        self.assertIn("numero", erro.exception.message_dict)

    def test_rejeita_numero_com_quantidade_invalida_de_digitos(self):
        with self.assertRaises(ValidationError) as erro:
            self.criar_telefone(numero="123456789")

        self.assertIn("numero", erro.exception.message_dict)

    def test_telefone_inativo_pode_permanecer_sem_numero(self):
        telefone = self.criar_telefone(numero="", ativo=False)

        self.assertEqual(telefone.numero, "")
        self.assertFalse(telefone.ativo)

    def test_rejeita_dois_numeros_ativos_iguais_no_mesmo_contato(self):
        self.criar_telefone(numero="91993152627")

        with self.assertRaises(ValidationError) as erro:
            self.criar_telefone(numero="(91) 99315-2627")

        self.assertIn("numero", erro.exception.message_dict)

    def test_permite_mesmo_numero_em_contatos_diferentes(self):
        outro_contato = self.fornecedor.contatos.create(nome="Joao", cargo="Financeiro")
        self.criar_telefone(numero="91993152627")
        telefone = self.criar_telefone(contato=outro_contato, numero="91993152627")

        self.assertEqual(telefone.contato, outro_contato)

    def test_rejeita_dois_principais_ativos_no_mesmo_contato(self):
        self.criar_telefone(numero="91993152627", principal=True)

        with self.assertRaises(ValidationError) as erro:
            self.criar_telefone(numero="91991000720", principal=True, ordem=2)

        self.assertIn("principal", erro.exception.message_dict)

    def test_permite_um_principal_e_outros_nao_principais(self):
        principal = self.criar_telefone(numero="91993152627", principal=True)
        secundario = self.criar_telefone(numero="91991000720", principal=False, ordem=2)

        self.assertTrue(principal.principal)
        self.assertFalse(secundario.principal)

    def test_rejeita_principal_inativo(self):
        with self.assertRaises(ValidationError) as erro:
            self.criar_telefone(ativo=False, principal=True)

        self.assertIn("principal", erro.exception.message_dict)

    def test_permite_telefone_fixo_sem_whatsapp(self):
        telefone = self.criar_telefone(
            numero="9132324444",
            tipo=FornecedorContatoTelefone.TIPO_FIXO,
            whatsapp=False,
        )

        self.assertEqual(telefone.tipo, FornecedorContatoTelefone.TIPO_FIXO)
        self.assertFalse(telefone.whatsapp)

    def test_maximo_de_3_telefones_ativos(self):
        self.criar_telefone(numero="91993152627", ordem=1)
        self.criar_telefone(numero="91991000720", ordem=2)
        self.criar_telefone(numero="9132324444", ordem=3)

        with self.assertRaises(ValidationError) as erro:
            self.criar_telefone(numero="9133335555", ordem=4)

        self.assertIn("ativo", erro.exception.message_dict)

    def test_permite_substituir_telefone_apos_desativar_um_dos_tres(self):
        telefone = self.criar_telefone(numero="91993152627", ordem=1)
        self.criar_telefone(numero="91991000720", ordem=2)
        self.criar_telefone(numero="9132324444", ordem=3)
        telefone.ativo = False
        telefone.save()

        novo = self.criar_telefone(numero="9133335555", ordem=4)

        self.assertTrue(novo.ativo)

    def test_helper_retorna_ativos_na_ordem_correta(self):
        self.criar_telefone(numero="91991000720", ordem=2)
        principal = self.criar_telefone(numero="91993152627", principal=True, ordem=3)
        primeiro = self.criar_telefone(numero="9132324444", ordem=1)

        self.assertEqual(telefones_ativos_contato(self.contato), [principal, primeiro, FornecedorContatoTelefone.objects.get(numero="91991000720")])

    def test_helper_prefere_principal(self):
        self.criar_telefone(numero="91991000720", ordem=1)
        principal = self.criar_telefone(numero="91993152627", principal=True, ordem=2)

        self.assertEqual(telefone_principal_contato(self.contato), principal)

    def test_helper_retorna_primeiro_ativo_quando_nao_existe_principal(self):
        primeiro = self.criar_telefone(numero="91991000720", ordem=1)
        self.criar_telefone(numero="91993152627", ordem=2)

        self.assertEqual(telefone_principal_contato(self.contato), primeiro)

    def test_helper_filtra_apenas_whatsapp(self):
        whatsapp = self.criar_telefone(numero="91993152627", whatsapp=True, principal=True)
        self.criar_telefone(numero="9132324444", tipo=FornecedorContatoTelefone.TIPO_FIXO, whatsapp=False, ordem=2)

        self.assertEqual(telefones_whatsapp_contato(self.contato), [whatsapp])

    def test_helper_whatsapp_nao_usa_legado_quando_existe_telefone_novo_sem_whatsapp(self):
        self.contato.telefone_whatsapp = "91999990000"
        self.contato.save()
        self.criar_telefone(
            numero="9132324444",
            tipo=FornecedorContatoTelefone.TIPO_FIXO,
            whatsapp=False,
        )

        self.assertEqual(telefones_whatsapp_contato(self.contato), [])

    def test_helper_nao_altera_nem_salva_o_contato(self):
        atualizado_em = self.contato.atualizado_em

        telefone_principal_contato(self.contato)
        self.contato.refresh_from_db()

        self.assertEqual(self.contato.atualizado_em, atualizado_em)

    def test_contato_antigo_sem_telefone_novo_continua_preservado(self):
        self.contato.telefone_whatsapp = "91999990000"
        self.contato.save()

        telefone = telefone_principal_contato(self.contato)

        self.assertEqual(telefone.numero, "91999990000")
        self.assertEqual(self.contato.telefones.count(), 0)

    def test_campo_telefone_whatsapp_antigo_nao_e_removido_nem_alterado(self):
        self.contato.telefone_whatsapp = "91 99999 0000"
        self.contato.save()
        valor_antigo = self.contato.telefone_whatsapp
        self.criar_telefone(numero="91993152627")
        self.contato.refresh_from_db()

        self.assertEqual(self.contato.telefone_whatsapp, valor_antigo)
        self.assertEqual(self.contato.telefone_whatsapp_normalizado, "91999990000")

    def _executar_migracao_dados(self, setup_callback):
        FornecedorContatoTelefone.objects.all().delete()
        setup_callback(django_apps)
        migracao = importlib.import_module("estoque.migrations.0075_migrar_telefones_contatos_fornecedor")
        migracao.migrar_telefones_contatos(django_apps, None)
        return django_apps

    def test_migration_de_dados_copia_telefone_normalizado(self):
        def setup(apps):
            Fornecedor = apps.get_model("estoque", "Fornecedor")
            FornecedorContato = apps.get_model("estoque", "FornecedorContato")
            fornecedor = Fornecedor.objects.create(nome="Fornecedor Migration Normalizado")
            contato = FornecedorContato.objects.create(
                fornecedor=fornecedor,
                nome="Ana",
            )
            FornecedorContato.objects.filter(pk=contato.pk).update(
                telefone_whatsapp="texto antigo",
                telefone_whatsapp_normalizado="91993152627",
            )

        apps = self._executar_migracao_dados(setup)
        Telefone = apps.get_model("estoque", "FornecedorContatoTelefone")

        self.assertEqual(Telefone.objects.get().numero, "91993152627")

    def test_migration_de_dados_usa_telefone_original_quando_normalizado_vazio(self):
        def setup(apps):
            Fornecedor = apps.get_model("estoque", "Fornecedor")
            FornecedorContato = apps.get_model("estoque", "FornecedorContato")
            fornecedor = Fornecedor.objects.create(nome="Fornecedor Migration Original")
            contato = FornecedorContato.objects.create(
                fornecedor=fornecedor,
                nome="Ana",
            )
            FornecedorContato.objects.filter(pk=contato.pk).update(
                telefone_whatsapp="(91) 99100-0720",
                telefone_whatsapp_normalizado=None,
            )

        apps = self._executar_migracao_dados(setup)
        Telefone = apps.get_model("estoque", "FornecedorContatoTelefone")

        self.assertEqual(Telefone.objects.get().numero, "91991000720")

    def test_migration_nao_cria_telefone_para_contato_sem_numero(self):
        def setup(apps):
            Fornecedor = apps.get_model("estoque", "Fornecedor")
            FornecedorContato = apps.get_model("estoque", "FornecedorContato")
            fornecedor = Fornecedor.objects.create(nome="Fornecedor Migration Sem Numero")
            FornecedorContato.objects.create(fornecedor=fornecedor, nome="Ana")

        apps = self._executar_migracao_dados(setup)
        Telefone = apps.get_model("estoque", "FornecedorContatoTelefone")

        self.assertEqual(Telefone.objects.count(), 0)

    def test_migration_nao_duplica_telefone_equivalente(self):
        def setup(apps):
            Fornecedor = apps.get_model("estoque", "Fornecedor")
            FornecedorContato = apps.get_model("estoque", "FornecedorContato")
            Telefone = apps.get_model("estoque", "FornecedorContatoTelefone")
            fornecedor = Fornecedor.objects.create(nome="Fornecedor Migration Duplicado")
            contato = FornecedorContato.objects.create(
                fornecedor=fornecedor,
                nome="Ana",
            )
            FornecedorContato.objects.filter(pk=contato.pk).update(
                telefone_whatsapp="(91) 99315-2627",
                telefone_whatsapp_normalizado="91993152627",
            )
            Telefone.objects.create(
                contato=contato,
                numero="91993152627",
                tipo="celular",
                whatsapp=True,
                principal=True,
                ativo=True,
                ordem=1,
            )

        apps = self._executar_migracao_dados(setup)
        Telefone = apps.get_model("estoque", "FornecedorContatoTelefone")

        self.assertEqual(Telefone.objects.count(), 1)

    def test_telefone_migrado_fica_principal_ativo_e_whatsapp(self):
        def setup(apps):
            Fornecedor = apps.get_model("estoque", "Fornecedor")
            FornecedorContato = apps.get_model("estoque", "FornecedorContato")
            fornecedor = Fornecedor.objects.create(nome="Fornecedor Migration Flags")
            contato = FornecedorContato.objects.create(
                fornecedor=fornecedor,
                nome="Ana",
            )
            FornecedorContato.objects.filter(pk=contato.pk).update(
                telefone_whatsapp_normalizado="91993152627",
            )

        apps = self._executar_migracao_dados(setup)
        telefone = apps.get_model("estoque", "FornecedorContatoTelefone").objects.get()

        self.assertEqual(telefone.tipo, "celular")
        self.assertTrue(telefone.whatsapp)
        self.assertTrue(telefone.principal)
        self.assertTrue(telefone.ativo)
        self.assertEqual(telefone.ordem, 1)


class FornecedorContatoTelefonesFormTests(TestCase):
    def _produto_teste(self, nome="Produto Telefones Form"):
        return Produto.objects.create(
            nome=nome,
            preco_compra=Decimal("10.00"),
            preco_vista=Decimal("15.00"),
            preco_prazo=Decimal("16.00"),
            quantidade=Decimal("1.000"),
        )

    def _dados_fornecedor(self, nome="Fornecedor Telefones Form", **alteracoes):
        dados = {
            "nome": nome,
            "nome_fantasia": "",
            "telefone_whatsapp": "",
            "cidade": "",
            "bairro": "",
            "prazos_pagamento_padrao": "",
            "observacao": "",
            "ativo": "on",
            "frequencia_visita_intervalo_dias": "",
            "frequencia_visita_dia_semana": "",
            "frequencia_visita_data_referencia": "",
            "produtos": [],
            "contatos-TOTAL_FORMS": "3",
            "contatos-INITIAL_FORMS": "0",
            "contatos-MIN_NUM_FORMS": "0",
            "contatos-MAX_NUM_FORMS": "1000",
        }
        for contato_indice in range(3):
            dados.update({
                f"contatos-{contato_indice}-id": "",
                f"contatos-{contato_indice}-nome": "",
                f"contatos-{contato_indice}-cargo": "",
                f"contatos-{contato_indice}-telefone_whatsapp": "",
                f"contatos-{contato_indice}-principal": "",
                f"contatos-{contato_indice}-ativo": "",
                f"contatos-{contato_indice}-observacao": "",
            })
            for telefone_indice in range(3):
                prefixo = f"contatos-{contato_indice}-telefones-{telefone_indice}"
                dados.update({
                    f"{prefixo}-id": "",
                    f"{prefixo}-numero": "",
                    f"{prefixo}-tipo": FornecedorContatoTelefone.TIPO_CELULAR,
                    f"{prefixo}-whatsapp": "on",
                    f"{prefixo}-principal": "",
                    f"{prefixo}-ativo": "on",
                    f"{prefixo}-DELETE": "",
                })
        dados.update(alteracoes)
        return dados

    def _telefone(self, contato_indice, telefone_indice, **alteracoes):
        prefixo = f"contatos-{contato_indice}-telefones-{telefone_indice}"
        dados = {
            f"{prefixo}-numero": "91993152627",
            f"{prefixo}-tipo": FornecedorContatoTelefone.TIPO_CELULAR,
            f"{prefixo}-whatsapp": "on",
            f"{prefixo}-principal": "",
            f"{prefixo}-ativo": "on",
            f"{prefixo}-DELETE": "",
        }
        dados.update(alteracoes)
        return dados

    def _post_novo(self, dados):
        return self.client.post(reverse("estoque:fornecedor_novo"), dados, secure=True, follow=True)

    def _fornecedor(self, nome):
        return Fornecedor.objects.get(nome=nome)

    def _criar_fornecedor_com_contato(self, nome="Fornecedor Edita Telefones"):
        fornecedor = Fornecedor.objects.create(nome=nome)
        contato = fornecedor.contatos.create(nome="Ana Paula", cargo="Vendedora", ativo=True)
        return fornecedor, contato

    def test_pagina_mostra_subsecao_de_telefones(self):
        resposta = self.client.get(reverse("estoque:fornecedor_novo"), secure=True)

        self.assertContains(resposta, "Telefones deste contato")
        self.assertContains(resposta, "Adicionar outro telefone")

    def test_contato_existente_mostra_telefone_migrado(self):
        fornecedor, contato = self._criar_fornecedor_com_contato("Fornecedor Telefone Migrado")
        FornecedorContatoTelefone.objects.create(contato=contato, numero="91993152627", whatsapp=True, principal=True)

        resposta = self.client.get(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), secure=True)

        self.assertContains(resposta, 'value="(91) 99315-2627"')

    def test_editar_contato_com_telefone_migrado_sem_alteracoes_nao_duplica(self):
        fornecedor, contato = self._criar_fornecedor_com_contato("Fornecedor Sem Duplicar Migrado")
        telefone = FornecedorContatoTelefone.objects.create(contato=contato, numero="91993152627", whatsapp=True, principal=True)
        dados = self._dados_fornecedor(nome=fornecedor.nome, **{
            "contatos-INITIAL_FORMS": "1",
            "contatos-0-id": str(contato.pk),
            "contatos-0-nome": contato.nome,
            "contatos-0-ativo": "on",
            **self._telefone(0, 0, **{"contatos-0-telefones-0-id": str(telefone.pk), "contatos-0-telefones-0-principal": "on"}),
        })

        resposta = self.client.post(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), dados, secure=True, follow=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(contato.telefones.count(), 1)

    def test_telefone_existente_e_atualizado_pelo_id(self):
        fornecedor, contato = self._criar_fornecedor_com_contato("Fornecedor Atualiza Por Id")
        telefone = FornecedorContatoTelefone.objects.create(contato=contato, numero="91993152627", whatsapp=True, principal=True)
        dados = self._dados_fornecedor(nome=fornecedor.nome, **{
            "contatos-INITIAL_FORMS": "1",
            "contatos-0-id": str(contato.pk),
            "contatos-0-nome": contato.nome,
            "contatos-0-ativo": "on",
            **self._telefone(0, 0, **{
                "contatos-0-telefones-0-id": str(telefone.pk),
                "contatos-0-telefones-0-numero": "9132324444",
                "contatos-0-telefones-0-tipo": FornecedorContatoTelefone.TIPO_FIXO,
                "contatos-0-telefones-0-whatsapp": "",
                "contatos-0-telefones-0-principal": "on",
            }),
        })

        self.client.post(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), dados, secure=True)
        telefone.refresh_from_db()

        self.assertEqual(telefone.numero, "9132324444")
        self.assertEqual(telefone.tipo, FornecedorContatoTelefone.TIPO_FIXO)
        self.assertFalse(telefone.whatsapp)
        self.assertEqual(contato.telefones.count(), 1)

    def test_fallback_sem_id_encontra_telefone_existente_equivalente(self):
        fornecedor, contato = self._criar_fornecedor_com_contato("Fornecedor Fallback Sem Id")
        telefone = FornecedorContatoTelefone.objects.create(contato=contato, numero="91993152627", whatsapp=True, principal=True)
        dados = self._dados_fornecedor(nome=fornecedor.nome, **{
            "contatos-INITIAL_FORMS": "1",
            "contatos-0-id": str(contato.pk),
            "contatos-0-nome": contato.nome,
            "contatos-0-ativo": "on",
            **self._telefone(0, 0, **{
                "contatos-0-telefones-0-id": "",
                "contatos-0-telefones-0-numero": "(91) 99315-2627",
                "contatos-0-telefones-0-tipo": FornecedorContatoTelefone.TIPO_OUTRO,
                "contatos-0-telefones-0-principal": "on",
            }),
        })

        self.client.post(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), dados, secure=True)
        telefone.refresh_from_db()

        self.assertEqual(contato.telefones.count(), 1)
        self.assertEqual(telefone.tipo, FornecedorContatoTelefone.TIPO_OUTRO)

    def test_segundo_numero_diferente_e_criado(self):
        fornecedor, contato = self._criar_fornecedor_com_contato("Fornecedor Cria Segundo Numero")
        telefone = FornecedorContatoTelefone.objects.create(contato=contato, numero="91993152627", whatsapp=True, principal=True)
        dados = self._dados_fornecedor(nome=fornecedor.nome, **{
            "contatos-INITIAL_FORMS": "1",
            "contatos-0-id": str(contato.pk),
            "contatos-0-nome": contato.nome,
            "contatos-0-ativo": "on",
            **self._telefone(0, 0, **{"contatos-0-telefones-0-id": str(telefone.pk), "contatos-0-telefones-0-principal": "on"}),
            **self._telefone(0, 1, **{"contatos-0-telefones-1-numero": "91991000720"}),
        })

        self.client.post(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), dados, secure=True)

        self.assertEqual(contato.telefones.count(), 2)

    def test_mesmo_numero_sem_id_nao_e_duplicado(self):
        fornecedor, contato = self._criar_fornecedor_com_contato("Fornecedor Mesmo Numero Sem Id")
        FornecedorContatoTelefone.objects.create(contato=contato, numero="91993152627", whatsapp=True, principal=True)
        dados = self._dados_fornecedor(nome=fornecedor.nome, **{
            "contatos-INITIAL_FORMS": "1",
            "contatos-0-id": str(contato.pk),
            "contatos-0-nome": contato.nome,
            "contatos-0-ativo": "on",
            **self._telefone(0, 0, **{"contatos-0-telefones-0-id": "", "contatos-0-telefones-0-principal": "on"}),
        })

        resposta = self.client.post(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), dados, secure=True)

        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(contato.telefones.count(), 1)

    def test_cadastro_com_um_telefone(self):
        dados = self._dados_fornecedor(
            nome="Fornecedor Um Telefone",
            **{
                "contatos-0-nome": "Ana Paula",
                "contatos-0-ativo": "on",
                **self._telefone(0, 0, **{"contatos-0-telefones-0-principal": "on"}),
            },
        )

        self._post_novo(dados)

        contato = self._fornecedor("Fornecedor Um Telefone").contatos.get()
        self.assertEqual(contato.telefones.count(), 1)

    def test_cadastro_com_dois_telefones(self):
        dados = self._dados_fornecedor(
            nome="Fornecedor Dois Telefones",
            **{
                "contatos-0-nome": "Ana Paula",
                "contatos-0-ativo": "on",
                **self._telefone(0, 0, **{"contatos-0-telefones-0-principal": "on"}),
                **self._telefone(0, 1, **{"contatos-0-telefones-1-numero": "91991000720"}),
            },
        )

        self._post_novo(dados)

        self.assertEqual(self._fornecedor("Fornecedor Dois Telefones").contatos.get().telefones.count(), 2)

    def test_cadastro_com_tres_telefones(self):
        dados = self._dados_fornecedor(
            nome="Fornecedor Tres Telefones",
            **{
                "contatos-0-nome": "Ana Paula",
                "contatos-0-ativo": "on",
                **self._telefone(0, 0, **{"contatos-0-telefones-0-principal": "on"}),
                **self._telefone(0, 1, **{"contatos-0-telefones-1-numero": "91991000720"}),
                **self._telefone(0, 2, **{"contatos-0-telefones-2-numero": "9132324444"}),
            },
        )

        self._post_novo(dados)

        self.assertEqual(self._fornecedor("Fornecedor Tres Telefones").contatos.get().telefones.count(), 3)

    def test_bloqueio_visual_e_servidor_acima_de_tres(self):
        dados = self._dados_fornecedor(
            nome="Fornecedor Quatro Telefones",
            **{
                "contatos-0-nome": "Ana Paula",
                "contatos-0-ativo": "on",
                **self._telefone(0, 0, **{"contatos-0-telefones-0-principal": "on"}),
                **self._telefone(0, 1, **{"contatos-0-telefones-1-numero": "91991000720"}),
                **self._telefone(0, 2, **{"contatos-0-telefones-2-numero": "9132324444"}),
                **self._telefone(0, 3, **{"contatos-0-telefones-3-numero": "9133335555"}),
            },
        )

        resposta = self._post_novo(dados)

        self.assertContains(resposta, "Cada contato pode ter no maximo 3 telefones ativos")
        self.assertContains(resposta, "contatos-0-telefones-2-numero")
        self.assertNotContains(resposta, "contatos-0-telefones-3-numero")

    def test_telefone_de_10_digitos(self):
        dados = self._dados_fornecedor(nome="Fornecedor Telefone 10", **{"contatos-0-nome": "Ana", "contatos-0-ativo": "on", **self._telefone(0, 0, **{"contatos-0-telefones-0-numero": "9132324444"})})

        self._post_novo(dados)

        self.assertEqual(self._fornecedor("Fornecedor Telefone 10").contatos.get().telefones.get().numero, "9132324444")

    def test_telefone_de_11_digitos(self):
        dados = self._dados_fornecedor(nome="Fornecedor Telefone 11", **{"contatos-0-nome": "Ana", "contatos-0-ativo": "on", **self._telefone(0, 0)})

        self._post_novo(dados)

        self.assertEqual(self._fornecedor("Fornecedor Telefone 11").contatos.get().telefones.get().numero, "91993152627")

    def test_numero_formatado_e_salvo_somente_com_digitos(self):
        dados = self._dados_fornecedor(nome="Fornecedor Numero Formatado", **{"contatos-0-nome": "Ana", "contatos-0-ativo": "on", **self._telefone(0, 0, **{"contatos-0-telefones-0-numero": "(91) 99315-2627"})})

        self._post_novo(dados)

        self.assertEqual(self._fornecedor("Fornecedor Numero Formatado").contatos.get().telefones.get().numero, "91993152627")

    def test_tipo_celular(self):
        dados = self._dados_fornecedor(nome="Fornecedor Tipo Celular", **{"contatos-0-nome": "Ana", "contatos-0-ativo": "on", **self._telefone(0, 0)})

        self._post_novo(dados)

        self.assertEqual(self._fornecedor("Fornecedor Tipo Celular").contatos.get().telefones.get().tipo, FornecedorContatoTelefone.TIPO_CELULAR)

    def test_tipo_fixo(self):
        dados = self._dados_fornecedor(nome="Fornecedor Tipo Fixo", **{"contatos-0-nome": "Ana", "contatos-0-ativo": "on", **self._telefone(0, 0, **{"contatos-0-telefones-0-tipo": FornecedorContatoTelefone.TIPO_FIXO, "contatos-0-telefones-0-numero": "9132324444"})})

        self._post_novo(dados)

        self.assertEqual(self._fornecedor("Fornecedor Tipo Fixo").contatos.get().telefones.get().tipo, FornecedorContatoTelefone.TIPO_FIXO)

    def test_tipo_outro(self):
        dados = self._dados_fornecedor(nome="Fornecedor Tipo Outro", **{"contatos-0-nome": "Ana", "contatos-0-ativo": "on", **self._telefone(0, 0, **{"contatos-0-telefones-0-tipo": FornecedorContatoTelefone.TIPO_OUTRO})})

        self._post_novo(dados)

        self.assertEqual(self._fornecedor("Fornecedor Tipo Outro").contatos.get().telefones.get().tipo, FornecedorContatoTelefone.TIPO_OUTRO)

    def test_whatsapp_marcado(self):
        dados = self._dados_fornecedor(nome="Fornecedor Whatsapp Marcado", **{"contatos-0-nome": "Ana", "contatos-0-ativo": "on", **self._telefone(0, 0)})

        self._post_novo(dados)

        self.assertTrue(self._fornecedor("Fornecedor Whatsapp Marcado").contatos.get().telefones.get().whatsapp)

    def test_telefone_fixo_sem_whatsapp(self):
        dados = self._dados_fornecedor(nome="Fornecedor Fixo Sem Whatsapp", **{"contatos-0-nome": "Ana", "contatos-0-ativo": "on", **self._telefone(0, 0, **{"contatos-0-telefones-0-numero": "9132324444", "contatos-0-telefones-0-tipo": FornecedorContatoTelefone.TIPO_FIXO, "contatos-0-telefones-0-whatsapp": ""})})

        self._post_novo(dados)

        telefone = self._fornecedor("Fornecedor Fixo Sem Whatsapp").contatos.get().telefones.get()
        self.assertFalse(telefone.whatsapp)

    def test_apenas_um_principal(self):
        dados = self._dados_fornecedor(nome="Fornecedor Dois Principais", **{"contatos-0-nome": "Ana", "contatos-0-ativo": "on", **self._telefone(0, 0, **{"contatos-0-telefones-0-principal": "on"}), **self._telefone(0, 1, **{"contatos-0-telefones-1-numero": "91991000720", "contatos-0-telefones-1-principal": "on"})})

        resposta = self._post_novo(dados)

        self.assertContains(resposta, "Marque apenas um telefone principal por contato")

    def test_troca_de_principal(self):
        fornecedor, contato = self._criar_fornecedor_com_contato("Fornecedor Troca Principal")
        telefone_1 = FornecedorContatoTelefone.objects.create(contato=contato, numero="91993152627", whatsapp=True, principal=True, ordem=1)
        telefone_2 = FornecedorContatoTelefone.objects.create(contato=contato, numero="91991000720", whatsapp=True, principal=False, ordem=2)
        dados = self._dados_fornecedor(nome=fornecedor.nome, **{
            "contatos-INITIAL_FORMS": "1",
            "contatos-0-id": str(contato.pk),
            "contatos-0-nome": contato.nome,
            "contatos-0-ativo": "on",
            **self._telefone(0, 0, **{"contatos-0-telefones-0-id": str(telefone_1.pk), "contatos-0-telefones-0-principal": ""}),
            **self._telefone(0, 1, **{"contatos-0-telefones-1-id": str(telefone_2.pk), "contatos-0-telefones-1-numero": "91991000720", "contatos-0-telefones-1-principal": "on"}),
        })

        self.client.post(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), dados, secure=True)
        telefone_2.refresh_from_db()

        self.assertTrue(telefone_2.principal)

    def test_remocao_de_telefone(self):
        fornecedor, contato = self._criar_fornecedor_com_contato("Fornecedor Remove Telefone")
        telefone = FornecedorContatoTelefone.objects.create(contato=contato, numero="91993152627", whatsapp=True, principal=True)
        dados = self._dados_fornecedor(nome=fornecedor.nome, **{"contatos-INITIAL_FORMS": "1", "contatos-0-id": str(contato.pk), "contatos-0-nome": contato.nome, "contatos-0-ativo": "on", **self._telefone(0, 0, **{"contatos-0-telefones-0-id": str(telefone.pk), "contatos-0-telefones-0-DELETE": "on"})})

        self.client.post(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), dados, secure=True)
        telefone.refresh_from_db()

        self.assertFalse(telefone.ativo)

    def test_desativacao_de_telefone(self):
        fornecedor, contato = self._criar_fornecedor_com_contato("Fornecedor Desativa Telefone")
        telefone = FornecedorContatoTelefone.objects.create(contato=contato, numero="91993152627", whatsapp=True, principal=False)
        dados = self._dados_fornecedor(nome=fornecedor.nome, **{"contatos-INITIAL_FORMS": "1", "contatos-0-id": str(contato.pk), "contatos-0-nome": contato.nome, "contatos-0-ativo": "on", **self._telefone(0, 0, **{"contatos-0-telefones-0-id": str(telefone.pk), "contatos-0-telefones-0-ativo": ""})})

        self.client.post(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), dados, secure=True)
        telefone.refresh_from_db()

        self.assertFalse(telefone.ativo)

    def test_principal_removido_promove_outro_ativo(self):
        fornecedor, contato = self._criar_fornecedor_com_contato("Fornecedor Promove Principal")
        telefone_1 = FornecedorContatoTelefone.objects.create(contato=contato, numero="91993152627", whatsapp=True, principal=True, ordem=1)
        telefone_2 = FornecedorContatoTelefone.objects.create(contato=contato, numero="91991000720", whatsapp=True, principal=False, ordem=2)
        dados = self._dados_fornecedor(nome=fornecedor.nome, **{"contatos-INITIAL_FORMS": "1", "contatos-0-id": str(contato.pk), "contatos-0-nome": contato.nome, "contatos-0-ativo": "on", **self._telefone(0, 0, **{"contatos-0-telefones-0-id": str(telefone_1.pk), "contatos-0-telefones-0-DELETE": "on"}), **self._telefone(0, 1, **{"contatos-0-telefones-1-id": str(telefone_2.pk), "contatos-0-telefones-1-numero": "91991000720"})})

        self.client.post(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), dados, secure=True)
        telefone_2.refresh_from_db()

        self.assertTrue(telefone_2.principal)

    def test_numero_duplicado_mostra_erro(self):
        dados = self._dados_fornecedor(nome="Fornecedor Telefone Duplicado", **{"contatos-0-nome": "Ana", "contatos-0-ativo": "on", **self._telefone(0, 0), **self._telefone(0, 1, **{"contatos-0-telefones-1-numero": "(91) 99315-2627"})})

        resposta = self._post_novo(dados)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Este telefone ja esta cadastrado para este contato")

    def test_erro_de_telefone_volta_ao_formulario_sem_erro_500(self):
        dados = self._dados_fornecedor(nome="Fornecedor Erro Telefone Form", **{"contatos-0-nome": "Ana", "contatos-0-ativo": "on", **self._telefone(0, 0), **self._telefone(0, 1, **{"contatos-0-telefones-1-numero": "(91) 99315-2627"})})

        resposta = self.client.post(reverse("estoque:fornecedor_novo"), dados, secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Este telefone ja esta cadastrado para este contato")
        self.assertContains(resposta, "Telefones deste contato")

    def test_telefone_sem_nome_de_contato_mostra_erro(self):
        dados = self._dados_fornecedor(nome="Fornecedor Telefone Sem Nome", **self._telefone(0, 0))

        resposta = self._post_novo(dados)

        self.assertContains(resposta, "Informe o nome do responsavel")

    def test_telefone_principal_whatsapp_sincroniza_legado(self):
        dados = self._dados_fornecedor(nome="Fornecedor Sincroniza Principal", **{"contatos-0-nome": "Ana", "contatos-0-ativo": "on", **self._telefone(0, 0, **{"contatos-0-telefones-0-principal": "on"})})

        self._post_novo(dados)
        contato = self._fornecedor("Fornecedor Sincroniza Principal").contatos.get()

        self.assertEqual(contato.telefone_whatsapp, "91993152627")
        self.assertEqual(contato.telefone_whatsapp_normalizado, "91993152627")

    def test_primeiro_whatsapp_ativo_usado_quando_nenhum_whatsapp_principal(self):
        dados = self._dados_fornecedor(nome="Fornecedor Primeiro Whatsapp", **{"contatos-0-nome": "Ana", "contatos-0-ativo": "on", **self._telefone(0, 0, **{"contatos-0-telefones-0-principal": ""}), **self._telefone(0, 1, **{"contatos-0-telefones-1-numero": "91991000720", "contatos-0-telefones-1-principal": ""})})

        self._post_novo(dados)
        contato = self._fornecedor("Fornecedor Primeiro Whatsapp").contatos.get()

        self.assertEqual(contato.telefone_whatsapp, "91993152627")

    def test_telefone_nao_whatsapp_nao_sincroniza_legado(self):
        dados = self._dados_fornecedor(nome="Fornecedor Sem Sync Fixo", **{"contatos-0-nome": "Ana", "contatos-0-ativo": "on", **self._telefone(0, 0, **{"contatos-0-telefones-0-numero": "9132324444", "contatos-0-telefones-0-whatsapp": ""})})

        self._post_novo(dados)
        contato = self._fornecedor("Fornecedor Sem Sync Fixo").contatos.get()

        self.assertIsNone(contato.telefone_whatsapp)

    def test_remocao_de_todos_os_whatsapps_limpa_legado_intencionalmente(self):
        fornecedor, contato = self._criar_fornecedor_com_contato("Fornecedor Limpa Legado")
        contato.telefone_whatsapp = "91993152627"
        contato.telefone_whatsapp_normalizado = "91993152627"
        contato.save()
        telefone = FornecedorContatoTelefone.objects.create(contato=contato, numero="91993152627", whatsapp=True, principal=True)
        dados = self._dados_fornecedor(nome=fornecedor.nome, **{"contatos-INITIAL_FORMS": "1", "contatos-0-id": str(contato.pk), "contatos-0-nome": contato.nome, "contatos-0-ativo": "on", **self._telefone(0, 0, **{"contatos-0-telefones-0-id": str(telefone.pk), "contatos-0-telefones-0-DELETE": "on"})})

        self.client.post(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), dados, secure=True)
        contato.refresh_from_db()

        self.assertIsNone(contato.telefone_whatsapp)

    def test_post_invalido_nao_apaga_telefones_existentes(self):
        fornecedor, contato = self._criar_fornecedor_com_contato("Fornecedor Invalido Preserva")
        telefone = FornecedorContatoTelefone.objects.create(contato=contato, numero="91993152627", whatsapp=True, principal=True)
        dados = self._dados_fornecedor(nome="", **{"contatos-INITIAL_FORMS": "1", "contatos-0-id": str(contato.pk), "contatos-0-nome": contato.nome, "contatos-0-ativo": "on", **self._telefone(0, 0, **{"contatos-0-telefones-0-id": str(telefone.pk), "contatos-0-telefones-0-DELETE": "on"})})

        self.client.post(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), dados, secure=True)
        telefone.refresh_from_db()

        self.assertTrue(telefone.ativo)

    def test_post_invalido_nao_salva_parcialmente_fornecedor_contato_ou_telefone(self):
        fornecedor, contato = self._criar_fornecedor_com_contato("Fornecedor Rollback Telefones")
        telefone = FornecedorContatoTelefone.objects.create(contato=contato, numero="91993152627", whatsapp=True, principal=True)
        dados = self._dados_fornecedor(nome="", cidade="Cidade Nova", **{
            "contatos-INITIAL_FORMS": "1",
            "contatos-0-id": str(contato.pk),
            "contatos-0-nome": "Nome Alterado",
            "contatos-0-ativo": "on",
            **self._telefone(0, 0, **{"contatos-0-telefones-0-id": str(telefone.pk), "contatos-0-telefones-0-numero": "91991000720"}),
        })

        resposta = self.client.post(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), dados, secure=True)
        fornecedor.refresh_from_db()
        contato.refresh_from_db()
        telefone.refresh_from_db()

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(fornecedor.cidade or "", "")
        self.assertEqual(contato.nome, "Ana Paula")
        self.assertEqual(telefone.numero, "91993152627")

    def test_contato_2_continua_outra_pessoa(self):
        dados = self._dados_fornecedor(nome="Fornecedor Contato Dois", **{"contatos-0-nome": "Ana", "contatos-0-ativo": "on", "contatos-1-nome": "Bruno", "contatos-1-ativo": "on", **self._telefone(0, 0), **self._telefone(1, 0, **{"contatos-1-telefones-0-numero": "91991000720"})})

        self._post_novo(dados)

        self.assertEqual(self._fornecedor("Fornecedor Contato Dois").contatos.count(), 2)

    def test_telefone_2_pertence_ao_mesmo_contato(self):
        dados = self._dados_fornecedor(nome="Fornecedor Telefone Dois Mesmo Contato", **{"contatos-0-nome": "Ana", "contatos-0-ativo": "on", **self._telefone(0, 0), **self._telefone(0, 1, **{"contatos-0-telefones-1-numero": "91991000720"})})

        self._post_novo(dados)
        contato = self._fornecedor("Fornecedor Telefone Dois Mesmo Contato").contatos.get()

        self.assertEqual(contato.telefones.count(), 2)

    def test_contatos_vazios_continuam_descartados(self):
        dados = self._dados_fornecedor(nome="Fornecedor Contatos Vazios")

        self._post_novo(dados)

        self.assertEqual(self._fornecedor("Fornecedor Contatos Vazios").contatos.count(), 0)

    def test_produtos_do_fornecedor_continuam_preservados(self):
        produto = self._produto_teste("Produto Preservado Telefones")
        dados = self._dados_fornecedor(nome="Fornecedor Produto Preservado", produtos=[str(produto.pk)], **{"contatos-0-nome": "Ana", "contatos-0-ativo": "on", **self._telefone(0, 0)})

        self._post_novo(dados)

        fornecedor = self._fornecedor("Fornecedor Produto Preservado")
        self.assertTrue(ProdutoFornecedor.objects.filter(fornecedor=fornecedor, produto=produto, ativo=True).exists())

    def test_frequencia_de_visita_continua_salvando(self):
        dados = self._dados_fornecedor(
            nome="Fornecedor Frequencia Telefones",
            frequencia_visita_ativa="on",
            frequencia_visita_intervalo_dias="14",
            frequencia_visita_dia_semana=str(Fornecedor.DIA_SEMANA_TERCA),
            frequencia_visita_data_referencia="2026-07-07",
            **{"contatos-0-nome": "Ana", "contatos-0-ativo": "on", **self._telefone(0, 0)},
        )

        self._post_novo(dados)

        self.assertTrue(self._fornecedor("Fornecedor Frequencia Telefones").frequencia_visita_ativa)

    def test_mascara_funciona_nas_tres_linhas(self):
        resposta = self.client.get(reverse("estoque:fornecedor_novo"), secure=True)

        self.assertContains(resposta, 'data-fornecedor-telefone="1"', count=10)
        self.assertContains(resposta, 'maxlength="15"', count=10)

    def test_html_possui_estado_salvando_e_protecao_contra_duplo_submit(self):
        resposta = self.client.get(reverse("estoque:fornecedor_novo"), secure=True)

        self.assertContains(resposta, 'data-submit-loading-text="Salvando..."')
        self.assertContains(resposta, 'aria-busy')
        self.assertContains(resposta, "is-submitting")
        self.assertContains(resposta, "event.defaultPrevented")

    def test_layout_possui_regra_responsiva(self):
        resposta = self.client.get(reverse("estoque:fornecedor_novo"), secure=True)

        self.assertContains(resposta, "fornecedor-telefone-linha")
        self.assertContains(resposta, ".fornecedor-telefone-linha { grid-template-columns: 1fr;")

    def test_edicao_reabre_todos_os_numeros_salvos(self):
        fornecedor, contato = self._criar_fornecedor_com_contato("Fornecedor Reabre Numeros")
        FornecedorContatoTelefone.objects.create(contato=contato, numero="91993152627", whatsapp=True, principal=True, ordem=1)
        FornecedorContatoTelefone.objects.create(contato=contato, numero="91991000720", whatsapp=True, principal=False, ordem=2)
        FornecedorContatoTelefone.objects.create(contato=contato, numero="9132324444", whatsapp=False, principal=False, ordem=3)

        resposta = self.client.get(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), secure=True)

        self.assertContains(resposta, 'value="(91) 99315-2627"')
        self.assertContains(resposta, 'value="(91) 99100-0720"')
        self.assertContains(resposta, 'value="(91) 3232-4444"')

    def test_pagina_novo_fornecedor_exibe_destinatario_das_listas(self):
        resposta = self.client.get(reverse("estoque:fornecedor_novo"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Destinatário das listas")
        self.assertContains(resposta, 'name="destinatario_lista_contato"')
        self.assertContains(resposta, 'name="destinatario_lista_telefone"')

    def test_edicao_carrega_destinatario_padrao_configurado(self):
        fornecedor, contato = self._criar_fornecedor_com_contato("Fornecedor Destinatario Carrega")
        telefone = FornecedorContatoTelefone.objects.create(contato=contato, numero="91993152627", whatsapp=True, principal=True)
        FornecedorDestinatarioLista.objects.create(fornecedor=fornecedor, contato=contato, telefone=telefone)

        resposta = self.client.get(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), secure=True)
        payload = resposta.context["destinatario_lista_payload"]

        self.assertEqual(payload["selecionadoContato"], f"pk:{contato.pk}")
        self.assertEqual(payload["selecionadoTelefone"], f"pk:{telefone.pk}")
        self.assertTrue(payload["temConfiguracaoAtual"])

    def test_fornecedor_antigo_sem_destinatario_continua_abrindo(self):
        fornecedor = Fornecedor.objects.create(nome="Fornecedor Antigo Sem Destinatario")

        resposta = self.client.get(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertFalse(resposta.context["destinatario_lista_payload"]["temConfiguracaoAtual"])

    def test_edicao_sugere_contato_principal_sem_criar_destinatario(self):
        fornecedor, contato = self._criar_fornecedor_com_contato("Fornecedor Sugere Destinatario")
        contato.principal = True
        contato.save()
        telefone = FornecedorContatoTelefone.objects.create(contato=contato, numero="91993152627", whatsapp=True, principal=True)

        resposta = self.client.get(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), secure=True)

        payload = resposta.context["destinatario_lista_payload"]
        self.assertEqual(payload["selecionadoContato"], f"pk:{contato.pk}")
        self.assertEqual(payload["selecionadoTelefone"], f"pk:{telefone.pk}")
        self.assertFalse(FornecedorDestinatarioLista.objects.filter(fornecedor=fornecedor).exists())

    def test_salvamento_cria_destinatario_padrao(self):
        fornecedor, contato = self._criar_fornecedor_com_contato("Fornecedor Cria Destinatario")
        telefone = FornecedorContatoTelefone.objects.create(contato=contato, numero="91993152627", whatsapp=True, principal=True)
        dados = self._dados_fornecedor(nome=fornecedor.nome, **{
            "contatos-INITIAL_FORMS": "1",
            "contatos-0-id": str(contato.pk),
            "contatos-0-nome": contato.nome,
            "contatos-0-ativo": "on",
            **self._telefone(0, 0, **{"contatos-0-telefones-0-id": str(telefone.pk), "contatos-0-telefones-0-principal": "on"}),
            "destinatario_lista_contato": f"pk:{contato.pk}",
            "destinatario_lista_telefone": f"pk:{telefone.pk}",
        })

        resposta = self.client.post(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), dados, secure=True)

        self.assertEqual(resposta.status_code, 302)
        destinatario = FornecedorDestinatarioLista.objects.get(fornecedor=fornecedor)
        self.assertEqual(destinatario.contato, contato)
        self.assertEqual(destinatario.telefone, telefone)
        self.assertEqual(destinatario.tipo, FornecedorDestinatarioLista.TIPO_PADRAO)

    def test_novo_salvamento_atualiza_destinatario_sem_duplicar(self):
        fornecedor, contato = self._criar_fornecedor_com_contato("Fornecedor Atualiza Destinatario")
        telefone_1 = FornecedorContatoTelefone.objects.create(contato=contato, numero="91993152627", whatsapp=True, principal=True, ordem=1)
        telefone_2 = FornecedorContatoTelefone.objects.create(contato=contato, numero="91991000720", whatsapp=True, principal=False, ordem=2)
        FornecedorDestinatarioLista.objects.create(fornecedor=fornecedor, contato=contato, telefone=telefone_1)
        dados = self._dados_fornecedor(nome=fornecedor.nome, **{
            "contatos-INITIAL_FORMS": "1",
            "contatos-0-id": str(contato.pk),
            "contatos-0-nome": contato.nome,
            "contatos-0-ativo": "on",
            **self._telefone(0, 0, **{"contatos-0-telefones-0-id": str(telefone_1.pk), "contatos-0-telefones-0-principal": "on"}),
            **self._telefone(0, 1, **{"contatos-0-telefones-1-id": str(telefone_2.pk), "contatos-0-telefones-1-numero": telefone_2.numero}),
            "destinatario_lista_contato": f"pk:{contato.pk}",
            "destinatario_lista_telefone": f"pk:{telefone_2.pk}",
        })

        resposta = self.client.post(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), dados, secure=True)

        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(FornecedorDestinatarioLista.objects.filter(fornecedor=fornecedor, tipo=FornecedorDestinatarioLista.TIPO_PADRAO, ativo=True).count(), 1)
        self.assertEqual(FornecedorDestinatarioLista.objects.get(fornecedor=fornecedor).telefone, telefone_2)

    def test_destinatario_rejeita_contato_de_outro_fornecedor(self):
        fornecedor, contato = self._criar_fornecedor_com_contato("Fornecedor Rejeita Contato")
        telefone = FornecedorContatoTelefone.objects.create(contato=contato, numero="91993152627", whatsapp=True, principal=True)
        outro_fornecedor, outro_contato = self._criar_fornecedor_com_contato("Fornecedor Outro Contato")
        dados = self._dados_fornecedor(nome=fornecedor.nome, **{
            "contatos-INITIAL_FORMS": "1",
            "contatos-0-id": str(contato.pk),
            "contatos-0-nome": contato.nome,
            "contatos-0-ativo": "on",
            **self._telefone(0, 0, **{"contatos-0-telefones-0-id": str(telefone.pk), "contatos-0-telefones-0-principal": "on"}),
            "destinatario_lista_contato": f"pk:{outro_contato.pk}",
            "destinatario_lista_telefone": f"pk:{telefone.pk}",
        })

        resposta = self.client.post(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), dados, secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "O contato escolhido precisa pertencer a este fornecedor.")
        self.assertFalse(FornecedorDestinatarioLista.objects.filter(fornecedor=fornecedor).exists())
        self.assertTrue(outro_fornecedor.pk)

    def test_destinatario_rejeita_telefone_de_outro_contato(self):
        fornecedor, contato = self._criar_fornecedor_com_contato("Fornecedor Rejeita Telefone")
        telefone = FornecedorContatoTelefone.objects.create(contato=contato, numero="91993152627", whatsapp=True, principal=True)
        outro_contato = fornecedor.contatos.create(nome="Bruno", ativo=True)
        outro_telefone = FornecedorContatoTelefone.objects.create(contato=outro_contato, numero="91991000720", whatsapp=True, principal=True)
        dados = self._dados_fornecedor(nome=fornecedor.nome, **{
            "contatos-INITIAL_FORMS": "2",
            "contatos-0-id": str(contato.pk),
            "contatos-0-nome": contato.nome,
            "contatos-0-ativo": "on",
            **self._telefone(0, 0, **{"contatos-0-telefones-0-id": str(telefone.pk), "contatos-0-telefones-0-principal": "on"}),
            "contatos-1-id": str(outro_contato.pk),
            "contatos-1-nome": outro_contato.nome,
            "contatos-1-ativo": "on",
            **self._telefone(1, 0, **{"contatos-1-telefones-0-id": str(outro_telefone.pk), "contatos-1-telefones-0-numero": outro_telefone.numero, "contatos-1-telefones-0-principal": "on"}),
            "destinatario_lista_contato": f"pk:{contato.pk}",
            "destinatario_lista_telefone": f"pk:{outro_telefone.pk}",
        })

        resposta = self.client.post(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), dados, secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "O telefone escolhido precisa pertencer ao contato informado.")

    def test_destinatario_rejeita_telefone_nao_whatsapp(self):
        fornecedor, contato = self._criar_fornecedor_com_contato("Fornecedor Rejeita Nao Whatsapp")
        telefone = FornecedorContatoTelefone.objects.create(contato=contato, numero="9132324444", whatsapp=False, principal=True)
        dados = self._dados_fornecedor(nome=fornecedor.nome, **{
            "contatos-INITIAL_FORMS": "1",
            "contatos-0-id": str(contato.pk),
            "contatos-0-nome": contato.nome,
            "contatos-0-ativo": "on",
            **self._telefone(0, 0, **{
                "contatos-0-telefones-0-id": str(telefone.pk),
                "contatos-0-telefones-0-numero": telefone.numero,
                "contatos-0-telefones-0-whatsapp": "",
                "contatos-0-telefones-0-principal": "on",
            }),
            "destinatario_lista_contato": f"pk:{contato.pk}",
            "destinatario_lista_telefone": f"pk:{telefone.pk}",
        })

        resposta = self.client.post(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), dados, secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "O telefone escolhido precisa estar marcado como WhatsApp.")

    def test_destinatario_rejeita_telefone_inativo(self):
        fornecedor, contato = self._criar_fornecedor_com_contato("Fornecedor Rejeita Inativo")
        telefone = FornecedorContatoTelefone.objects.create(contato=contato, numero="91993152627", whatsapp=True, principal=False, ativo=True)
        dados = self._dados_fornecedor(nome=fornecedor.nome, **{
            "contatos-INITIAL_FORMS": "1",
            "contatos-0-id": str(contato.pk),
            "contatos-0-nome": contato.nome,
            "contatos-0-ativo": "on",
            **self._telefone(0, 0, **{
                "contatos-0-telefones-0-id": str(telefone.pk),
                "contatos-0-telefones-0-ativo": "",
            }),
            "destinatario_lista_contato": f"pk:{contato.pk}",
            "destinatario_lista_telefone": f"pk:{telefone.pk}",
        })

        resposta = self.client.post(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), dados, secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "O telefone escolhido precisa estar ativo.")

    def test_telefones_do_destinatario_sao_ordenados_com_principal_primeiro(self):
        fornecedor, contato = self._criar_fornecedor_com_contato("Fornecedor Ordena Telefones")
        telefone_secundario = FornecedorContatoTelefone.objects.create(contato=contato, numero="91991000720", whatsapp=True, principal=False, ordem=1)
        telefone_principal = FornecedorContatoTelefone.objects.create(contato=contato, numero="91993152627", whatsapp=True, principal=True, ordem=2)

        resposta = self.client.get(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), secure=True)

        telefones = resposta.context["destinatario_lista_payload"]["contatos"][0]["telefones"]
        self.assertEqual([item["id"] for item in telefones], [f"pk:{telefone_principal.pk}", f"pk:{telefone_secundario.pk}"])

    def test_fornecedor_sem_whatsapp_valido_continua_editavel(self):
        fornecedor, contato = self._criar_fornecedor_com_contato("Fornecedor Sem Whatsapp Valido")
        FornecedorContatoTelefone.objects.create(contato=contato, numero="9132324444", whatsapp=False, principal=False)
        dados = self._dados_fornecedor(nome=fornecedor.nome, **{
            "contatos-INITIAL_FORMS": "1",
            "contatos-0-id": str(contato.pk),
            "contatos-0-nome": contato.nome,
            "contatos-0-ativo": "on",
        })

        resposta = self.client.post(reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}), dados, secure=True)

        self.assertEqual(resposta.status_code, 302)
        self.assertFalse(FornecedorDestinatarioLista.objects.filter(fornecedor=fornecedor).exists())

    def test_cadastro_cria_destinatario_com_contato_e_telefone_novos(self):
        dados = self._dados_fornecedor(nome="Fornecedor Novo Destinatario", **{
            "contatos-0-nome": "Ana Paula",
            "contatos-0-ativo": "on",
            **self._telefone(0, 0, **{"contatos-0-telefones-0-principal": "on"}),
            "destinatario_lista_contato": "form:0",
            "destinatario_lista_telefone": "form:0:0",
        })

        resposta = self.client.post(reverse("estoque:fornecedor_novo"), dados, secure=True)

        self.assertEqual(resposta.status_code, 302)
        fornecedor = Fornecedor.objects.get(nome="Fornecedor Novo Destinatario")
        destinatario = FornecedorDestinatarioLista.objects.get(fornecedor=fornecedor)
        self.assertEqual(destinatario.contato.nome, "Ana Paula")
        self.assertEqual(destinatario.telefone.numero, "91993152627")


class FornecedorProdutosFormTests(TestCase):
    def _produto_teste(self, nome, excluido=False, excluido_em=None):
        return Produto.objects.create(
            nome=nome,
            preco_compra=Decimal("10.00"),
            preco_vista=Decimal("15.00"),
            preco_prazo=Decimal("16.00"),
            quantidade=Decimal("1.000"),
            excluido=excluido,
            excluido_em=excluido_em,
        )

    def _dados_fornecedor(self, fornecedor, produtos):
        dados = {
            "nome": fornecedor.nome,
            "nome_fantasia": fornecedor.nome_fantasia or "",
            "telefone_whatsapp": fornecedor.telefone_whatsapp or "",
            "cidade": fornecedor.cidade or "",
            "bairro": fornecedor.bairro or "",
            "prazos_pagamento_padrao": fornecedor.prazos_pagamento_padrao or "",
            "observacao": fornecedor.observacao or "",
            "ativo": "on",
            "produtos": [str(produto.id) for produto in produtos],
            "contatos-TOTAL_FORMS": "3",
            "contatos-INITIAL_FORMS": "0",
            "contatos-MIN_NUM_FORMS": "0",
            "contatos-MAX_NUM_FORMS": "1000",
        }
        for indice in range(3):
            dados.update({
                f"contatos-{indice}-id": "",
                f"contatos-{indice}-nome": "",
                f"contatos-{indice}-cargo": "",
                f"contatos-{indice}-telefone_whatsapp": "",
                f"contatos-{indice}-observacao": "",
            })
        return dados

    def test_fornecedor_form_mostra_apenas_produtos_fora_da_lixeira(self):
        produto_valido = self._produto_teste("Produto Valido", excluido=False, excluido_em=None)
        produto_lixeira = self._produto_teste(
            "Tmp Url Prod",
            excluido=True,
            excluido_em=timezone.now(),
        )

        form = FornecedorForm()

        self.assertTrue(form.fields["produtos"].queryset.filter(pk=produto_valido.pk).exists())
        self.assertFalse(form.fields["produtos"].queryset.filter(pk=produto_lixeira.pk).exists())

    def test_produto_da_lixeira_com_vinculo_ativo_nao_aparece_no_fornecedor(self):
        fornecedor = Fornecedor.objects.create(nome="Atacadao Br", ativo=True)
        produto_lixeira = self._produto_teste(
            "Pirakids Achoc 27/200Ml",
            excluido=True,
            excluido_em=timezone.now(),
        )
        ProdutoFornecedor.objects.create(
            fornecedor=fornecedor,
            produto=produto_lixeira,
            ativo=True,
        )

        resposta = self.client.get(
            reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertNotContains(resposta, "Pirakids Achoc 27/200Ml")
        form = resposta.context["form"]
        self.assertNotIn(produto_lixeira.pk, list(form.fields["produtos"].initial))
        self.assertFalse(form.fields["produtos"].queryset.filter(pk=produto_lixeira.pk).exists())

    def test_editar_fornecedor_salva_produto_valido_sem_erro_de_choice(self):
        fornecedor = Fornecedor.objects.create(nome="Atacadao Br", ativo=True)
        produto_valido = self._produto_teste("Produto Valido", excluido=False, excluido_em=None)

        resposta = self.client.post(
            reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}),
            self._dados_fornecedor(fornecedor, [produto_valido]),
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertNotContains(resposta, "Select a valid choice")
        vinculo = ProdutoFornecedor.objects.get(fornecedor=fornecedor, produto=produto_valido)
        self.assertTrue(vinculo.ativo)

        resposta_get = self.client.get(
            reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}),
            secure=True,
        )

        self.assertEqual(resposta_get.status_code, 200)
        form = resposta_get.context["form"]
        self.assertIn(produto_valido.pk, list(form.fields["produtos"].initial))

    def test_editar_fornecedor_ignora_produto_da_lixeira_enviado_no_post(self):
        fornecedor = Fornecedor.objects.create(nome="Atacadao Br", ativo=True)
        produto_valido = self._produto_teste("Produto Valido", excluido=False, excluido_em=None)
        produto_lixeira = self._produto_teste(
            "Sardinha Gc 50/78",
            excluido=True,
            excluido_em=timezone.now(),
        )
        ProdutoFornecedor.objects.create(
            fornecedor=fornecedor,
            produto=produto_lixeira,
            ativo=True,
        )

        resposta = self.client.post(
            reverse("estoque:fornecedor_editar", kwargs={"pk": fornecedor.pk}),
            self._dados_fornecedor(fornecedor, [produto_valido, produto_lixeira]),
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertNotContains(resposta, "Select a valid choice")
        self.assertTrue(
            ProdutoFornecedor.objects.get(fornecedor=fornecedor, produto=produto_valido).ativo
        )
        self.assertFalse(
            ProdutoFornecedor.objects.get(fornecedor=fornecedor, produto=produto_lixeira).ativo
        )


class ProdutosIncompletosTests(TestCase):
    def setUp(self):
        Categoria.objects.get_or_create(nome="Bebidas", defaults={"ativa": True})
        Unidade.objects.get_or_create(sigla="UN", defaults={"nome": "Unidade", "ativa": True})
        self.produto = Produto.objects.create(
            nome="Produto Incompleto",
            categoria="Bebidas",
            preco_compra=Decimal("3.33"),
            preco_vista=Decimal("3.33"),
            preco_prazo=Decimal("3.33"),
            quantidade=Decimal("0.000"),
            estoque_minimo=0,
            unidade_compra="UN",
            cadastro_incompleto=True,
            permitir_prejuizo=True,
            motivo_prejuizo="Cadastro rapido durante compra.",
        )

    def _url_edicao(self):
        return reverse("estoque:produto_editar", kwargs={"pk": self.produto.pk})

    def _valor_input_por_id(self, html, campo_id):
        tag = re.search(rf'<input(?=[^>]*id="{campo_id}")[^>]*>', html, re.S)
        self.assertIsNotNone(tag, f"Campo {campo_id} nao encontrado no HTML")
        valor = re.search(r'value="([^"]*)"', tag.group(0))
        return valor.group(1) if valor else ""

    def _dados_validos(self):
        return {
            "nome": "Produto Incompleto",
            "codigo": "",
            "categoria": "Bebidas",
            "preco_compra": "3.33",
            "unidade_compra": "UN",
            "fator_conversao": "",
            "preco_compra_fracionado": "",
            "unidade_venda_1": "UN",
            "preco_vista": "4.33",
            "unidade_venda_2": "",
            "preco_prazo": "4.99",
            "vende_fracionado": "False",
            "descricao_conversao": "",
            "quantidade": "0.000",
            "estoque_minimo": "0",
            "fornecedor": "",
            "percentual_vista_fracionado": "",
            "preco_vista_fracionado": "",
            "percentual_prazo_fracionado": "",
            "preco_prazo_fracionado": "",
        }

    def test_tela_completar_produto_incompleto_tem_campos_de_preco_e_percentual(self):
        resposta = self.client.get(self._url_edicao(), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'id="id_preco_compra"')
        self.assertContains(resposta, 'id="percentual_vista"')
        self.assertContains(resposta, 'id="id_preco_vista"')
        self.assertContains(resposta, 'id="percentual_prazo"')
        self.assertContains(resposta, 'id="id_preco_prazo"')

    def test_painel_produtos_exibe_fornecedores_e_nova_lista_separados(self):
        resposta = self.client.get(reverse("estoque:home"), secure=True)
        html = resposta.content.decode()

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Fornecedores")
        self.assertContains(resposta, f'href="{reverse("estoque:fornecedores")}"')
        self.assertContains(resposta, 'id="btnFornecedoresPainel"')
        self.assertContains(resposta, "Nova Lista por Fornecedor")
        self.assertContains(resposta, f'href="{reverse("estoque:sugestao_compra_fornecedor")}"')
        self.assertContains(resposta, 'id="btnNovaListaFornecedorPainel"')
        self.assertNotEqual(
            html.index('id="btnFornecedoresPainel"'),
            html.index('id="btnNovaListaFornecedorPainel"'),
        )

    def test_tela_completar_produto_incompleto_nao_inicializa_preco_final_com_compra(self):
        resposta = self.client.get(self._url_edicao(), secure=True)

        html = resposta.content.decode()
        self.assertEqual(self._valor_input_por_id(html, "id_preco_compra"), "3.33")
        self.assertEqual(self._valor_input_por_id(html, "id_preco_vista"), "")
        self.assertEqual(self._valor_input_por_id(html, "id_preco_prazo"), "")

    def test_tela_completar_produto_incompleto_tem_ordem_enter_de_precos(self):
        resposta = self.client.get(self._url_edicao(), secure=True)

        html = resposta.content.decode()
        self.assertIn('"#id_preco_compra"', html)
        self.assertIn('"#percentual_vista"', html)
        self.assertIn('"#id_preco_vista"', html)
        self.assertIn('"#percentual_prazo"', html)
        self.assertIn('"#id_preco_prazo"', html)
        self.assertLess(html.index('"#id_preco_compra"'), html.index('"#percentual_vista"'))
        self.assertLess(html.index('"#percentual_vista"'), html.index('"#id_preco_vista"'))
        self.assertLess(html.index('"#id_preco_vista"'), html.index('"#percentual_prazo"'))
        self.assertLess(html.index('"#percentual_prazo"'), html.index('"#id_preco_prazo"'))
        self.assertContains(resposta, "ev.stopImmediatePropagation();")
        self.assertContains(resposta, 'document.getElementById("form-produto")?.requestSubmit();')

    def test_tela_completar_produto_incompleto_calcula_percentual_e_preserva_preco_manual(self):
        resposta = self.client.get(self._url_edicao(), secure=True)

        self.assertContains(resposta, 'precoCampo.value = formatarDuasCasas(base * (1 + percentual / 100));')
        self.assertContains(resposta, "function marcarPrecoFinalManual")
        self.assertContains(resposta, 'precoCampo.dataset.precoFinalManual = "1";')
        self.assertContains(resposta, "function atualizarPrecoFinalAoAlterarCompra")
        self.assertContains(resposta, "precoFinalEditadoManual(precoCampo)")
        self.assertNotContains(resposta, "precoCampo.value = formatarDuasCasas(base);")

    def test_salvar_produto_incompleto_continua_funcionando(self):
        resposta = self.client.post(self._url_edicao(), self._dados_validos(), secure=True)

        self.produto.refresh_from_db()
        self.assertEqual(resposta.status_code, 302)
        self.assertFalse(self.produto.cadastro_incompleto)
        self.assertEqual(self.produto.preco_compra, Decimal("3.33"))
        self.assertEqual(self.produto.preco_vista, Decimal("4.33"))
        self.assertEqual(self.produto.preco_prazo, Decimal("4.99"))


class ComprasListaFinanceiroTests(TestCase):
    def test_listagem_exibe_situacao_financeira_sem_status_finalizada(self):
        fornecedor = Fornecedor.objects.create(nome="Fornecedor Lista Financeiro")
        hoje = timezone.localdate()

        def criar_compra(tipo="aprazo", **campos):
            return Compra.objects.create(
                fornecedor=fornecedor,
                data_compra=hoje,
                tipo_pagamento=tipo,
                total=Decimal("100.00"),
                status=campos.pop("status", Compra.STATUS_FINALIZADA),
                **campos,
            )

        avista = criar_compra(tipo="avista")
        aberta = criar_compra()
        vencida = criar_compra()
        parcial = criar_compra()
        quitada = criar_compra()
        criar_compra()  # Compra a prazo sem Conta a Pagar.
        criar_compra(status=Compra.STATUS_CANCELADA, cancelada=True)

        dados_conta = {
            "fornecedor": fornecedor,
            "data_emissao": hoje,
            "valor_original": Decimal("100.00"),
        }
        ContaPagar.objects.create(
            compra=aberta, data_vencimento=hoje + timedelta(days=5),
            valor_em_aberto=Decimal("100.00"), status=ContaPagar.STATUS_ABERTA,
            **dados_conta,
        )
        ContaPagar.objects.create(
            compra=vencida, data_vencimento=hoje - timedelta(days=2),
            valor_em_aberto=Decimal("100.00"), status=ContaPagar.STATUS_ABERTA,
            **dados_conta,
        )
        ContaPagar.objects.create(
            compra=parcial, data_vencimento=hoje + timedelta(days=3),
            valor_em_aberto=Decimal("40.00"), status=ContaPagar.STATUS_PARCIAL,
            **dados_conta,
        )
        ContaPagar.objects.create(
            compra=quitada, data_vencimento=hoje - timedelta(days=1),
            valor_em_aberto=Decimal("0.00"), status=ContaPagar.STATUS_PAGA,
            **dados_conta,
        )

        resposta = self.client.get(reverse("estoque:compras_lista"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Nota paga", count=4)  # Desktop e mobile, duas compras.
        self.assertContains(resposta, f"Vence em {(hoje + timedelta(days=5)):%d/%m/%Y}", count=2)
        self.assertContains(resposta, f"Vencida desde {(hoje - timedelta(days=2)):%d/%m/%Y}", count=2)
        self.assertContains(resposta, f"Parcial - vence em {(hoje + timedelta(days=3)):%d/%m/%Y}", count=2)
        self.assertContains(resposta, "Financeiro não localizado", count=2)
        self.assertContains(resposta, "Cancelada", count=2)
        self.assertNotContains(resposta, ">Finalizada<")
        self.assertContains(
            resposta,
            f'href="{reverse("estoque:compras_detalhe", kwargs={"pk": avista.pk})}"',
            count=2,
        )
        self.assertContains(
            resposta,
            f'href="{reverse("estoque:compra_corrigir_itens", kwargs={"pk": avista.pk})}"',
            count=2,
        )


class ComprasListaConferenciaTests(TestCase):
    def setUp(self):
        self.fornecedor = Fornecedor.objects.create(nome="Fornecedor Conferencia")
        self.lista = ListaCompraFornecedor.objects.create(
            fornecedor=self.fornecedor,
            data_lista=timezone.localdate(),
            data_inicio_periodo=timezone.localdate(),
            data_fim_periodo=timezone.localdate(),
            total_lista=Decimal("100.00"),
        )

    def criar_item(self, nome, quantidade_final):
        produto = Produto.objects.create(
            nome=nome,
            preco_compra=Decimal("10.00"),
            preco_vista=Decimal("15.00"),
            preco_prazo=Decimal("16.00"),
            quantidade=Decimal("5.000"),
        )
        item = ItemListaCompraFornecedor.objects.create(
            lista=self.lista,
            produto=produto,
            quantidade_final=Decimal(quantidade_final),
            unidade="UN",
            preco_compra=Decimal("10.00"),
            preco_unitario=Decimal("10.00"),
            total=Decimal("10.00"),
        )
        return item

    def test_salvar_conferencia_calcula_status_e_nao_altera_compra_financeiro_estoque(self):
        item_ok = self.criar_item("Produto Ok", "2.000")
        item_faltou = self.criar_item("Produto Faltou", "3.000")
        item_mais = self.criar_item("Produto Mais", "1.000")
        item_nao_veio = self.criar_item("Produto Nao Veio", "4.000")
        item_pendente = self.criar_item("Produto Pendente", "5.000")
        estoques_antes = {
            item.produto_id: item.produto.quantidade
            for item in [item_ok, item_faltou, item_mais, item_nao_veio, item_pendente]
        }
        compras_antes = Compra.objects.count()
        contas_antes = ContaPagar.objects.count()
        movimentos_antes = MovimentoFinanceiro.objects.count()

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_conferencia_salvar", kwargs={"pk": self.lista.pk}),
            {
                f"quantidade_recebida_{item_ok.id}": "2,000",
                f"observacao_conferencia_{item_ok.id}": "Tudo certo",
                f"quantidade_recebida_{item_faltou.id}": "1,000",
                f"observacao_conferencia_{item_faltou.id}": "Fornecedor entregou parcial",
                f"quantidade_recebida_{item_mais.id}": "2,000",
                f"observacao_conferencia_{item_mais.id}": "",
                f"quantidade_recebida_{item_nao_veio.id}": "0",
                f"observacao_conferencia_{item_nao_veio.id}": "Nao veio",
                f"quantidade_recebida_{item_pendente.id}": "",
                f"observacao_conferencia_{item_pendente.id}": "",
            },
            secure=True,
        )

        self.assertRedirects(
            resposta,
            reverse("estoque:compras_lista_fornecedor_detalhe", kwargs={"pk": self.lista.pk}),
            fetch_redirect_response=False,
        )
        item_ok.refresh_from_db()
        item_faltou.refresh_from_db()
        item_mais.refresh_from_db()
        item_nao_veio.refresh_from_db()
        item_pendente.refresh_from_db()
        self.assertEqual(item_ok.status_conferencia, ItemListaCompraFornecedor.STATUS_CONFERENCIA_OK)
        self.assertEqual(item_faltou.status_conferencia, ItemListaCompraFornecedor.STATUS_CONFERENCIA_FALTOU)
        self.assertEqual(item_mais.status_conferencia, ItemListaCompraFornecedor.STATUS_CONFERENCIA_VEIO_A_MAIS)
        self.assertEqual(item_nao_veio.status_conferencia, ItemListaCompraFornecedor.STATUS_CONFERENCIA_NAO_VEIO)
        self.assertEqual(item_pendente.status_conferencia, ItemListaCompraFornecedor.STATUS_CONFERENCIA_PENDENTE)
        self.assertTrue(item_ok.conferido)
        self.assertTrue(item_nao_veio.conferido)
        self.assertFalse(item_pendente.conferido)
        self.assertEqual(item_ok.observacao_conferencia, "Tudo certo")
        self.assertEqual(item_faltou.quantidade_recebida, Decimal("1.000"))
        self.assertIsNone(item_pendente.quantidade_recebida)
        self.assertEqual(self.lista.itens.count(), 5)
        self.assertEqual(Compra.objects.count(), compras_antes)
        self.assertEqual(ContaPagar.objects.count(), contas_antes)
        self.assertEqual(MovimentoFinanceiro.objects.count(), movimentos_antes)
        for produto_id, quantidade_antes in estoques_antes.items():
            self.assertEqual(Produto.objects.get(pk=produto_id).quantidade, quantidade_antes)


class ComprasListaFornecedorEnvioVendedorTests(TestCase):
    def setUp(self):
        self.usuario = get_user_model().objects.create_user(
            username="compras_whatsapp",
            password="senha-teste",
        )
        self.client.force_login(self.usuario)
        self.fornecedor = Fornecedor.objects.create(
            nome="Fornecedor Envio Vendedor",
            telefone_whatsapp="91999990000",
        )
        self.lista = ListaCompraFornecedor.objects.create(
            fornecedor=self.fornecedor,
            data_lista=date(2026, 7, 14),
            data_inicio_periodo=date(2026, 7, 1),
            data_fim_periodo=date(2026, 7, 14),
            total_sugerido_original=Decimal("100.00"),
            total_lista=Decimal("90.00"),
        )
        self.url = reverse(
            "estoque:compras_lista_fornecedor_whatsapp",
            kwargs={"pk": self.lista.pk},
        )

    def test_ver_lista_mostra_envio_interno(self):
        resposta = self.client.get(f"/compras/listas-fornecedor/{self.lista.pk}/ver/")

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Enviar ao Vendedor")
        self.assertContains(resposta, "Envio Interno")
        self.assertContains(resposta, f"/compras/listas-fornecedor/{self.lista.pk}/interno/")

    def test_ver_lista_mostra_resumo_do_ultimo_envio_interno(self):
        EnvioInternoListaCompraFornecedor.objects.create(
            lista=self.lista,
            fornecedor=self.fornecedor,
            versao=EnvioInternoListaCompraFornecedor.VERSAO_SINTETICA,
            nome_destinatario="Lincoln",
            telefone_destinatario="5591999999999",
            origem_destinatario=EnvioInternoListaCompraFornecedor.ORIGEM_FUNCIONARIO,
            registrado_em=timezone.now(),
            registrado_por=self.usuario,
        )

        resposta = self.client.get(f"/compras/listas-fornecedor/{self.lista.pk}/ver/")

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Envio interno")
        self.assertContains(resposta, "Ultimo envio")
        self.assertContains(resposta, "Lincoln")
        self.assertContains(resposta, "Sintetica")
        self.assertContains(resposta, "Ver historico completo")
        self.assertContains(resposta, f"/compras/listas-fornecedor/{self.lista.pk}/interno/")

    def test_consulta_listas_mostra_envio_interno(self):
        resposta = self.client.get("/compras/listas-fornecedor/")

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Ver")
        self.assertContains(resposta, "Interno")
        self.assertContains(resposta, "Editar")
        self.assertContains(resposta, "Conferir")
        self.assertContains(resposta, "Vendedor")
        self.assertContains(resposta, "Cancelar")
        self.assertContains(resposta, f"/compras/listas-fornecedor/{self.lista.pk}/interno/")

    def test_tela_interna_lista_fornecedor_abre_sem_confirmar_envio(self):
        status_original = self.lista.status

        resposta = self.client.get(f"/compras/listas-fornecedor/{self.lista.pk}/interno/")

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Lista interna")
        self.assertContains(resposta, "Analitica")
        self.assertContains(resposta, "Sintetica")
        self.assertContains(resposta, "Abrir imagem")
        self.assertContains(resposta, "Abrir WhatsApp")

        self.lista.refresh_from_db()
        self.assertEqual(self.lista.status, status_original)

    def _url_registrar_envio_interno(self, lista=None):
        return reverse(
            "estoque:compras_lista_fornecedor_registrar_envio_interno",
            kwargs={"pk": (lista or self.lista).pk},
        )

    def _post_registrar_envio_interno(self, dados=None, lista=None, client=None):
        cliente = client or self.client
        return cliente.post(
            self._url_registrar_envio_interno(lista),
            data=json.dumps(dados or {}),
            content_type="application/json",
            secure=True,
        )

    def test_tela_interna_lista_fornecedor_mostra_historico_de_envios_internos(self):
        EnvioInternoListaCompraFornecedor.objects.create(
            lista=self.lista,
            fornecedor=self.fornecedor,
            versao=EnvioInternoListaCompraFornecedor.VERSAO_ANALITICA,
            nome_destinatario="Lincoln",
            telefone_destinatario="5591999999999",
            origem_destinatario=EnvioInternoListaCompraFornecedor.ORIGEM_FUNCIONARIO,
            registrado_em=timezone.now(),
            registrado_por=self.usuario,
        )

        resposta = self.client.get(f"/compras/listas-fornecedor/{self.lista.pk}/interno/")

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Historico de envios internos")
        self.assertContains(resposta, "Lincoln")
        self.assertContains(resposta, "5591999999999")
        self.assertContains(resposta, "Analitica")
        self.assertContains(resposta, self.usuario.username)

    def test_registrar_envio_interno_com_funcionario_nao_confirma_envio_vendedor(self):
        funcionario = Funcionario.objects.create(
            nome="Lincoln",
            telefone_whatsapp="91955556666",
            ativo=True,
        )
        status_original = self.lista.status

        resposta = self._post_registrar_envio_interno({
            "versao": "sintetica",
            "origem": "funcionario",
            "funcionario_id": funcionario.id,
        })

        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json()["ok"])

        envio = EnvioInternoListaCompraFornecedor.objects.get(lista=self.lista)
        self.assertEqual(envio.fornecedor, self.fornecedor)
        self.assertEqual(envio.funcionario, funcionario)
        self.assertEqual(envio.versao, EnvioInternoListaCompraFornecedor.VERSAO_SINTETICA)
        self.assertEqual(envio.origem_destinatario, EnvioInternoListaCompraFornecedor.ORIGEM_FUNCIONARIO)
        self.assertEqual(envio.nome_destinatario, "Lincoln")
        self.assertTrue(envio.telefone_destinatario.endswith("91955556666"))
        self.assertEqual(envio.registrado_por, self.usuario)

        self.lista.refresh_from_db()
        self.assertEqual(self.lista.status, status_original)
        self.assertFalse(EnvioListaCompraFornecedor.objects.filter(lista=self.lista).exists())

    def test_registrar_envio_interno_com_numero_avulso_nao_muda_status_da_lista(self):
        status_original = self.lista.status

        resposta = self._post_registrar_envio_interno({
            "versao": "analitica",
            "origem": "avulso",
            "nome": "Roseli",
            "telefone": "+55 (91) 98888-7777",
        })

        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json()["ok"])

        envio = EnvioInternoListaCompraFornecedor.objects.get(lista=self.lista)
        self.assertEqual(envio.versao, EnvioInternoListaCompraFornecedor.VERSAO_ANALITICA)
        self.assertEqual(envio.origem_destinatario, EnvioInternoListaCompraFornecedor.ORIGEM_AVULSO)
        self.assertEqual(envio.nome_destinatario, "Roseli")
        self.assertTrue(envio.telefone_destinatario.endswith("91988887777"))

        self.lista.refresh_from_db()
        self.assertEqual(self.lista.status, status_original)
        self.assertFalse(EnvioListaCompraFornecedor.objects.filter(lista=self.lista).exists())

    def test_registrar_envio_interno_exige_autenticacao(self):
        self.client.logout()

        resposta = self._post_registrar_envio_interno({
            "versao": "analitica",
            "origem": "avulso",
            "telefone": "91988887777",
        })

        self.assertEqual(resposta.status_code, 403)
        self.assertFalse(EnvioInternoListaCompraFornecedor.objects.filter(lista=self.lista).exists())
        self.assertFalse(EnvioListaCompraFornecedor.objects.filter(lista=self.lista).exists())

    def test_imagem_interna_analitica_lista_fornecedor_retorna_png(self):
        resposta = self.client.get(
            f"/compras/listas-fornecedor/{self.lista.pk}/interno-imagem/?tipo=analitica"
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta["Content-Type"], "image/png")

        conteudo = b"".join(resposta.streaming_content)
        self.assertTrue(conteudo.startswith(b"\x89PNG"))

    def test_imagem_interna_sintetica_lista_fornecedor_retorna_png(self):
        resposta = self.client.get(
            f"/compras/listas-fornecedor/{self.lista.pk}/interno-imagem/?tipo=sintetica"
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta["Content-Type"], "image/png")

        conteudo = b"".join(resposta.streaming_content)
        self.assertTrue(conteudo.startswith(b"\x89PNG"))

    def test_payload_exige_reenvio_quando_lista_foi_alterada_depois_do_envio(self):
        from datetime import timedelta

        envio, criado = views._confirmar_envio_lista_fornecedor(
            lista=self.lista,
            usuario=self.usuario,
            telefone="91999990000",
            nome="Vendedor Teste",
            origem=views.EnvioListaCompraFornecedor.ORIGEM_PERSONALIZADO,
            chave_idempotencia="teste-payload-reenvio",
        )

        self.assertTrue(criado)

        ListaCompraFornecedor.objects.filter(pk=self.lista.pk).update(
            atualizado_em=envio.confirmado_em + timedelta(minutes=1)
        )
        self.lista.refresh_from_db()

        payload = views._payload_lista_fornecedor(self.lista)

        self.assertFalse(payload["envioVendedorConfirmado"])
        self.assertTrue(payload["envioVendedorRequerReenvio"])
        self.assertIsNone(payload["ultimoEnvioVendedor"])

    def test_confirmar_envio_nao_atualiza_atualizado_em_da_lista(self):
        atualizado_em_original = self.lista.atualizado_em

        envio, criado = views._confirmar_envio_lista_fornecedor(
            lista=self.lista,
            usuario=self.usuario,
            telefone="91999990000",
            nome="Vendedor Teste",
            origem=views.EnvioListaCompraFornecedor.ORIGEM_PERSONALIZADO,
            chave_idempotencia="teste-nao-atualiza-lista",
        )

        self.assertTrue(criado)
        self.assertIsNotNone(envio.pk)

        self.lista.refresh_from_db()
        self.assertEqual(
            self.lista.status,
            ListaCompraFornecedor.STATUS_ENVIADA,
        )
        self.assertEqual(self.lista.atualizado_em, atualizado_em_original)

    def test_payload_prioriza_destinatario_padrao_persistente(self):
        contato_principal = FornecedorContato.objects.create(
            fornecedor=self.fornecedor,
            nome="Contato Principal Antigo",
            principal=True,
            ativo=True,
        )
        FornecedorContatoTelefone.objects.create(
            contato=contato_principal,
            numero="91911112222",
            whatsapp=True,
            principal=True,
            ativo=True,
        )

        contato_escolhido = FornecedorContato.objects.create(
            fornecedor=self.fornecedor,
            nome="Vendedor Configurado",
            cargo="Representante",
            principal=False,
            ativo=True,
        )
        telefone_alternativo = FornecedorContatoTelefone.objects.create(
            contato=contato_escolhido,
            numero="91933334444",
            whatsapp=True,
            principal=True,
            ativo=True,
            ordem=1,
        )
        telefone_listas = FornecedorContatoTelefone.objects.create(
            contato=contato_escolhido,
            numero="91988887777",
            whatsapp=True,
            principal=False,
            ativo=True,
            ordem=2,
        )
        FornecedorDestinatarioLista.objects.create(
            fornecedor=self.fornecedor,
            contato=contato_escolhido,
            telefone=telefone_listas,
        )

        payload = views._payload_destinatarios_lista_fornecedor(
            self.fornecedor
        )

        self.assertTrue(payload["destinatarioConfigurado"])
        self.assertEqual(len(payload["opcoes"]), 2)
        self.assertEqual(
            payload["opcoes"][0]["numero"],
            telefone_listas.numero,
        )
        self.assertTrue(payload["opcoes"][0]["principal"])
        self.assertTrue(payload["opcoes"][0]["configurado"])
        self.assertEqual(
            payload["opcoes"][1]["numero"],
            telefone_alternativo.numero,
        )
        self.assertNotIn(
            "91911112222",
            [opcao["numero"] for opcao in payload["opcoes"]],
        )

    def test_payload_sem_configuracao_mantem_compatibilidade(self):
        contato = FornecedorContato.objects.create(
            fornecedor=self.fornecedor,
            nome="Vendedor Compatibilidade",
            principal=True,
            ativo=True,
        )
        telefone = FornecedorContatoTelefone.objects.create(
            contato=contato,
            numero="91955554444",
            whatsapp=True,
            principal=True,
            ativo=True,
        )

        payload = views._payload_destinatarios_lista_fornecedor(
            self.fornecedor
        )

        self.assertFalse(payload["destinatarioConfigurado"])
        self.assertEqual(len(payload["opcoes"]), 1)
        self.assertEqual(
            payload["opcoes"][0]["numero"],
            telefone.numero,
        )
        self.assertTrue(payload["opcoes"][0]["principal"])
        self.assertFalse(payload["opcoes"][0]["configurado"])

    def test_payload_lista_todos_whatsapps_ativos_do_vendedor_principal(self):
        contato = FornecedorContato.objects.create(
            fornecedor=self.fornecedor,
            nome="Vendedor Multiplos Telefones",
            principal=True,
            ativo=True,
        )
        telefone_secundario = FornecedorContatoTelefone.objects.create(
            contato=contato,
            numero="91922223333",
            tipo=FornecedorContatoTelefone.TIPO_CELULAR,
            whatsapp=True,
            principal=False,
            ativo=True,
            ordem=2,
        )
        telefone_principal = FornecedorContatoTelefone.objects.create(
            contato=contato,
            numero="91999998888",
            tipo=FornecedorContatoTelefone.TIPO_CELULAR,
            whatsapp=True,
            principal=True,
            ativo=True,
            ordem=1,
        )
        FornecedorContatoTelefone.objects.create(
            contato=contato,
            numero="9133334444",
            tipo=FornecedorContatoTelefone.TIPO_FIXO,
            whatsapp=False,
            principal=False,
            ativo=True,
            ordem=3,
        )

        payload = views._payload_destinatarios_lista_fornecedor(
            self.fornecedor
        )

        self.assertEqual(len(payload["opcoes"]), 2)
        self.assertEqual(
            [opcao["numero"] for opcao in payload["opcoes"]],
            [telefone_principal.numero, telefone_secundario.numero],
        )
        self.assertTrue(payload["opcoes"][0]["principal"])
        self.assertFalse(payload["opcoes"][1]["principal"])
        self.assertIn(
            "WhatsApp principal",
            payload["opcoes"][0]["nome"],
        )
        self.assertNotIn(
            "9133334444",
            [opcao["numero"] for opcao in payload["opcoes"]],
        )

    def test_payload_ignora_whatsapp_inativo_do_vendedor_principal(self):
        contato = FornecedorContato.objects.create(
            fornecedor=self.fornecedor,
            nome="Vendedor Com Telefone Inativo",
            principal=True,
            ativo=True,
        )
        telefone_ativo = FornecedorContatoTelefone.objects.create(
            contato=contato,
            numero="91955556666",
            whatsapp=True,
            principal=True,
            ativo=True,
        )
        FornecedorContatoTelefone.objects.create(
            contato=contato,
            numero="91977778888",
            whatsapp=True,
            principal=False,
            ativo=False,
        )

        payload = views._payload_destinatarios_lista_fornecedor(
            self.fornecedor
        )

        self.assertEqual(len(payload["opcoes"]), 1)
        self.assertEqual(
            payload["opcoes"][0]["numero"],
            telefone_ativo.numero,
        )
        self.assertNotEqual(
            payload["opcoes"][0]["numero"],
            "91977778888",
        )

    def test_payload_usa_somente_contato_principal_ativo(self):
        FornecedorContato.objects.create(
            fornecedor=self.fornecedor,
            nome="Contato Secundario",
            cargo="Financeiro",
            telefone_whatsapp="91911112222",
            principal=False,
            ativo=True,
        )
        principal = FornecedorContato.objects.create(
            fornecedor=self.fornecedor,
            nome="Vendedor Principal",
            cargo="Representante",
            telefone_whatsapp="91933334444",
            principal=True,
            ativo=True,
        )

        payload = views._payload_destinatarios_lista_fornecedor(self.fornecedor)

        self.assertTrue(payload["temContatoFornecedor"])
        self.assertEqual(len(payload["opcoes"]), 1)
        self.assertEqual(payload["opcoes"][0]["tipo"], "vendedor")
        self.assertEqual(
            payload["opcoes"][0]["nome"],
            "Vendedor Principal (Representante)",
        )
        self.assertEqual(
            payload["opcoes"][0]["numero"],
            principal.telefone_whatsapp_normalizado,
        )
        self.assertNotEqual(
            payload["opcoes"][0]["numero"],
            "91911112222",
        )

    def test_payload_nao_inclui_lincoln_roseli_nem_funcionarios(self):
        Funcionario.objects.create(
            nome="Lincoln",
            telefone_whatsapp="91955556666",
            ativo=True,
        )
        Funcionario.objects.create(
            nome="Roseli",
            telefone_whatsapp="91977778888",
            ativo=True,
        )
        FornecedorContato.objects.create(
            fornecedor=self.fornecedor,
            nome="Vendedor Fornecedor",
            telefone_whatsapp="91922223333",
            principal=True,
            ativo=True,
        )

        payload = views._payload_destinatarios_lista_fornecedor(self.fornecedor)
        nomes = [opcao["nome"] for opcao in payload["opcoes"]]
        numeros = [opcao["numero"] for opcao in payload["opcoes"]]

        self.assertEqual(nomes, ["Vendedor Fornecedor"])
        self.assertNotIn("Lincoln", nomes)
        self.assertNotIn("Roseli", nomes)
        self.assertNotIn("91955556666", numeros)
        self.assertNotIn("91977778888", numeros)

    def test_payload_nao_usa_telefone_geral_do_fornecedor_como_fallback(self):
        payload = views._payload_destinatarios_lista_fornecedor(self.fornecedor)

        self.assertFalse(payload["temContatoFornecedor"])
        self.assertEqual(payload["opcoes"], [])

    def test_contato_principal_sem_whatsapp_nao_eh_oferecido(self):
        FornecedorContato.objects.create(
            fornecedor=self.fornecedor,
            nome="Vendedor Sem Telefone",
            principal=True,
            ativo=True,
        )

        payload = views._payload_destinatarios_lista_fornecedor(self.fornecedor)

        self.assertFalse(payload["temContatoFornecedor"])
        self.assertEqual(payload["opcoes"], [])

    def test_tela_recebe_apenas_vendedor_principal_e_preenche_numero(self):
        FornecedorContato.objects.create(
            fornecedor=self.fornecedor,
            nome="Representante Padrao",
            telefone_whatsapp="91944445555",
            principal=True,
            ativo=True,
        )

        resposta = self.client.get(self.url, secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Representante Padrao")
        self.assertContains(resposta, "91944445555")
        self.assertContains(
            resposta,
            "Nenhum vendedor principal cadastrado para este fornecedor.",
        )
        self.assertContains(resposta, 'id="destinatarioStatus"')
        self.assertContains(resposta, 'data-destinatario-status')
        self.assertContains(resposta, 'destinatariosRapidos.hidden = true;')
        self.assertNotContains(resposta, 'class="destinatario-btn" data-origem="padrao"')
        self.assertContains(resposta, "const vendedorPadrao = opcoes.length ? opcoes[0] : null;")
        self.assertNotContains(resposta, '"nome": "Lincoln"')
        self.assertNotContains(resposta, '"nome": "Roseli"')

    def _url_destinatario_recente(self, lista=None):
        return reverse(
            "estoque:compras_lista_fornecedor_whatsapp_destinatario_recente",
            kwargs={"pk": (lista or self.lista).pk},
        )

    def _post_destinatario_recente(self, telefone, nome="", lista=None):
        return self.client.post(
            self._url_destinatario_recente(lista),
            data=json.dumps({"telefone": telefone, "nome": nome}),
            content_type="application/json",
            secure=True,
        )

    def _url_confirmar_envio_vendedor(self, lista=None):
        return reverse(
            "estoque:compras_lista_fornecedor_confirmar_envio_vendedor",
            kwargs={"pk": (lista or self.lista).pk},
        )

    def _post_confirmar_envio_vendedor(
        self,
        telefone="91988888888",
        nome="Carlos",
        origem="personalizado",
        chave="chave-confirmacao-1",
        lista=None,
        client=None,
    ):
        cliente = client or self.client
        return cliente.post(
            self._url_confirmar_envio_vendedor(lista),
            data=json.dumps({
                "telefone": telefone,
                "nome": nome,
                "origem": origem,
                "chaveConfirmacao": chave,
            }),
            content_type="application/json",
            secure=True,
        )

    def _criar_destinatario_padrao_envio(self, nome="Vendedor Padrao", telefone="91988888888", fornecedor=None):
        fornecedor = fornecedor or self.fornecedor
        contato = FornecedorContato.objects.create(
            fornecedor=fornecedor,
            nome=nome,
            principal=True,
            ativo=True,
        )
        telefone_obj = FornecedorContatoTelefone.objects.create(
            contato=contato,
            numero=telefone,
            whatsapp=True,
            principal=True,
            ativo=True,
        )
        FornecedorDestinatarioLista.objects.create(
            fornecedor=fornecedor,
            contato=contato,
            telefone=telefone_obj,
        )
        return contato, telefone_obj

    def test_cria_primeiro_destinatario_recente(self):
        resposta = self._post_destinatario_recente("(91) 98888-8888", "lincoln")

        self.assertEqual(resposta.status_code, 200)
        recente = FornecedorDestinatarioRecente.objects.get(fornecedor=self.fornecedor)
        self.assertEqual(recente.nome, "Lincoln")
        self.assertEqual(recente.telefone, "5591988888888")
        self.assertEqual(recente.quantidade_utilizacoes, 1)

    def test_destinatario_recente_normaliza_multiplos_nomes_e_espacos_extras(self):
        self._post_destinatario_recente("91977777777", "  ana   maria  ")

        recente = FornecedorDestinatarioRecente.objects.get(fornecedor=self.fornecedor)
        self.assertEqual(recente.nome, "Ana Maria")

    def test_reutiliza_mesmo_numero_incrementa_quantidade_atualiza_ultima_utilizacao_sem_duplicar(self):
        primeiro_momento = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.get_current_timezone())
        segundo_momento = datetime(2026, 7, 14, 11, 30, tzinfo=timezone.get_current_timezone())

        with patch("estoque.views.timezone.now", return_value=primeiro_momento):
            self._post_destinatario_recente("91988888888", "carlos")
        with patch("estoque.views.timezone.now", return_value=segundo_momento):
            self._post_destinatario_recente("(91) 98888-8888", "carlos silva")

        self.assertEqual(FornecedorDestinatarioRecente.objects.filter(fornecedor=self.fornecedor).count(), 1)
        recente = FornecedorDestinatarioRecente.objects.get(fornecedor=self.fornecedor)
        self.assertEqual(recente.quantidade_utilizacoes, 2)
        self.assertEqual(recente.ultima_utilizacao, segundo_momento)
        self.assertEqual(recente.nome, "Carlos Silva")

    def test_reutilizar_telefone_com_nome_em_caixa_diferente_atualiza_registro_existente(self):
        self._post_destinatario_recente("91988888888", "carlos silva")
        self._post_destinatario_recente("+55 (91) 98888-8888", "Carlos Silva")

        self.assertEqual(FornecedorDestinatarioRecente.objects.filter(fornecedor=self.fornecedor).count(), 1)
        recente = FornecedorDestinatarioRecente.objects.get(fornecedor=self.fornecedor)
        self.assertEqual(recente.nome, "Carlos Silva")
        self.assertEqual(recente.quantidade_utilizacoes, 2)

    def test_nome_ja_formatado_nao_sofre_transformacao_destrutiva(self):
        self._post_destinatario_recente("91988887777", "Joao McDonald LTDA")

        recente = FornecedorDestinatarioRecente.objects.get(fornecedor=self.fornecedor)
        self.assertEqual(recente.nome, "Joao McDonald LTDA")

    def test_historico_nao_duplica_mesmo_numero_por_diferenca_de_mascara(self):
        self._post_destinatario_recente("91993643215", "Leandro")
        self._post_destinatario_recente("(91) 99364-3215", "Leandro")
        self._post_destinatario_recente("+55 (91) 99364-3215", "Leandro")

        self.assertEqual(FornecedorDestinatarioRecente.objects.filter(fornecedor=self.fornecedor).count(), 1)
        recente = FornecedorDestinatarioRecente.objects.get(fornecedor=self.fornecedor)
        self.assertEqual(recente.telefone, "5591993643215")
        self.assertEqual(recente.quantidade_utilizacoes, 3)

    def test_lista_mostra_apenas_ultimos_cinco_recentes_ordenados(self):
        base = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.get_current_timezone())
        for indice in range(6):
            FornecedorDestinatarioRecente.objects.create(
                fornecedor=self.fornecedor,
                nome=f"Pessoa {indice}",
                telefone=f"9198888800{indice}",
                ultima_utilizacao=base + timedelta(minutes=indice),
                quantidade_utilizacoes=indice + 1,
            )

        payload = views._payload_lista_fornecedor(self.lista)

        recentes = payload["destinatariosRecentes"]
        self.assertEqual(len(recentes), 5)
        self.assertEqual(
            [item["nome"] for item in recentes],
            ["Pessoa 5", "Pessoa 4", "Pessoa 3", "Pessoa 2", "Pessoa 1"],
        )
        self.assertNotIn("Pessoa 0", [item["nome"] for item in recentes])

    def test_destinatarios_recentes_sao_separados_por_fornecedor(self):
        outro_fornecedor = Fornecedor.objects.create(nome="Outro Fornecedor Recente")
        outra_lista = ListaCompraFornecedor.objects.create(
            fornecedor=outro_fornecedor,
            data_lista=date(2026, 7, 14),
            data_inicio_periodo=date(2026, 7, 1),
            data_fim_periodo=date(2026, 7, 14),
            total_sugerido_original=Decimal("10.00"),
            total_lista=Decimal("10.00"),
        )
        FornecedorDestinatarioRecente.objects.create(
            fornecedor=self.fornecedor,
            nome="Carlos",
            telefone="91988888888",
            ultima_utilizacao=timezone.now(),
        )
        FornecedorDestinatarioRecente.objects.create(
            fornecedor=outro_fornecedor,
            nome="Ana",
            telefone="91977777777",
            ultima_utilizacao=timezone.now(),
        )

        payload_atual = views._payload_lista_fornecedor(self.lista)
        payload_outro = views._payload_lista_fornecedor(outra_lista)

        self.assertEqual([item["nome"] for item in payload_atual["destinatariosRecentes"]], ["Carlos"])
        self.assertEqual([item["nome"] for item in payload_outro["destinatariosRecentes"]], ["Ana"])

    def test_numero_manual_entra_no_historico(self):
        resposta = self._post_destinatario_recente("5591987654321", "")

        self.assertEqual(resposta.status_code, 200)
        recente = FornecedorDestinatarioRecente.objects.get(fornecedor=self.fornecedor)
        self.assertEqual(recente.telefone, "5591987654321")
        self.assertEqual(recente.nome, "")

    def test_numero_internacional_com_mais_nao_recebe_prefixo_brasileiro(self):
        resposta = self._post_destinatario_recente("+1 (415) 555-2671", "Fornecedor EUA")

        self.assertEqual(resposta.status_code, 200)
        recente = FornecedorDestinatarioRecente.objects.get(fornecedor=self.fornecedor)
        self.assertEqual(recente.telefone, "14155552671")
        self.assertFalse(recente.telefone.startswith("55"))

    def test_endpoint_destinatario_recente_rejeita_dados_grandes_sem_erro_tecnico(self):
        resposta = self._post_destinatario_recente("9" * 21, "A" * 141)

        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(resposta.json()["erro"], "Dados do destinatario invalidos.")
        self.assertFalse(FornecedorDestinatarioRecente.objects.filter(fornecedor=self.fornecedor).exists())

    def test_endpoint_destinatario_recente_aceita_apenas_post(self):
        resposta = self.client.get(self._url_destinatario_recente(), secure=True)

        self.assertEqual(resposta.status_code, 405)

    def test_endpoint_destinatario_recente_exige_usuario_autenticado(self):
        self.client.logout()

        resposta = self._post_destinatario_recente("91988888888", "Carlos")

        self.assertEqual(resposta.status_code, 403)
        self.assertEqual(resposta.json()["erro"], "Autenticacao necessaria.")
        self.assertFalse(FornecedorDestinatarioRecente.objects.filter(fornecedor=self.fornecedor).exists())

    def test_endpoint_confirmacao_envio_exige_usuario_autenticado(self):
        self.client.logout()

        resposta = self._post_confirmar_envio_vendedor()

        self.assertEqual(resposta.status_code, 403)
        self.assertEqual(resposta.json()["erro"], "Autenticacao necessaria.")
        self.assertFalse(EnvioListaCompraFornecedor.objects.filter(lista=self.lista).exists())

    def test_endpoint_confirmacao_envio_aceita_apenas_post(self):
        resposta = self.client.get(self._url_confirmar_envio_vendedor(), secure=True)

        self.assertEqual(resposta.status_code, 405)

    def test_endpoint_confirmacao_envio_mantem_csrf_protegido(self):
        cliente = Client(enforce_csrf_checks=True)
        cliente.force_login(self.usuario)

        resposta = self._post_confirmar_envio_vendedor(client=cliente)

        self.assertEqual(resposta.status_code, 403)
        self.assertFalse(EnvioListaCompraFornecedor.objects.filter(lista=self.lista).exists())

    def test_confirma_envio_com_destinatario_padrao(self):
        self._criar_destinatario_padrao_envio(nome="Vendedor Persistente", telefone="91988888888")

        resposta = self._post_confirmar_envio_vendedor(
            telefone="(91) 98888-8888",
            nome="Nome Alterado no Navegador",
            origem="padrao",
            chave="padrao-1",
        )

        self.assertEqual(resposta.status_code, 200)
        envio = EnvioListaCompraFornecedor.objects.get(lista=self.lista)
        self.assertEqual(envio.fornecedor, self.fornecedor)
        self.assertEqual(envio.nome_destinatario, "Vendedor Persistente")
        self.assertEqual(envio.telefone_destinatario, "5591988888888")
        self.assertEqual(envio.origem_destinatario, EnvioListaCompraFornecedor.ORIGEM_PADRAO)

    def test_confirmacao_rejeita_telefone_arbitrario_classificado_como_padrao(self):
        self._criar_destinatario_padrao_envio(nome="Vendedor Persistente", telefone="91988888888")

        resposta = self._post_confirmar_envio_vendedor(
            telefone="91977777777",
            nome="Outro",
            origem="padrao",
            chave="padrao-invalido-1",
        )

        self.assertEqual(resposta.status_code, 400)
        self.assertFalse(EnvioListaCompraFornecedor.objects.filter(lista=self.lista).exists())

    def test_confirmacao_rejeita_padrao_de_outro_fornecedor(self):
        outro_fornecedor = Fornecedor.objects.create(nome="Fornecedor Outro Padrao")
        self._criar_destinatario_padrao_envio(
            nome="Padrao Outro Fornecedor",
            telefone="91977777777",
            fornecedor=outro_fornecedor,
        )

        resposta = self._post_confirmar_envio_vendedor(
            telefone="91977777777",
            nome="Padrao Outro Fornecedor",
            origem="padrao",
            chave="padrao-outro-fornecedor-1",
        )

        self.assertEqual(resposta.status_code, 400)
        self.assertFalse(EnvioListaCompraFornecedor.objects.filter(lista=self.lista).exists())

    def test_confirma_envio_com_destinatario_recente(self):
        FornecedorDestinatarioRecente.objects.create(
            fornecedor=self.fornecedor,
            nome="Lincoln",
            telefone="5591999999999",
            ultima_utilizacao=timezone.now(),
        )

        resposta = self._post_confirmar_envio_vendedor(
            telefone="+55 (91) 99999-9999",
            nome="Nome Alterado no Navegador",
            origem="recente",
            chave="recente-1",
        )

        self.assertEqual(resposta.status_code, 200)
        envio = EnvioListaCompraFornecedor.objects.get(lista=self.lista)
        self.assertEqual(envio.origem_destinatario, EnvioListaCompraFornecedor.ORIGEM_RECENTE)
        self.assertEqual(envio.telefone_destinatario, "5591999999999")
        self.assertEqual(envio.nome_destinatario, "Lincoln")

    def test_confirmacao_rejeita_recente_de_outro_fornecedor(self):
        outro_fornecedor = Fornecedor.objects.create(nome="Fornecedor Outro Recente")
        FornecedorDestinatarioRecente.objects.create(
            fornecedor=outro_fornecedor,
            nome="Recente Outro Fornecedor",
            telefone="5591999999999",
            ultima_utilizacao=timezone.now(),
        )

        resposta = self._post_confirmar_envio_vendedor(
            telefone="+55 (91) 99999-9999",
            nome="Recente Outro Fornecedor",
            origem="recente",
            chave="recente-outro-fornecedor-1",
        )

        self.assertEqual(resposta.status_code, 400)
        self.assertFalse(EnvioListaCompraFornecedor.objects.filter(lista=self.lista).exists())

    def test_confirma_envio_com_destinatario_personalizado_e_nome_opcional(self):
        resposta = self._post_confirmar_envio_vendedor(
            telefone="91977777777",
            nome="",
            origem="personalizado",
            chave="personalizado-1",
        )

        self.assertEqual(resposta.status_code, 200)
        envio = EnvioListaCompraFornecedor.objects.get(lista=self.lista)
        self.assertEqual(envio.nome_destinatario, "")
        self.assertEqual(envio.origem_destinatario, EnvioListaCompraFornecedor.ORIGEM_PERSONALIZADO)

    def test_confirmacao_normaliza_telefone_e_equivale_numero_brasileiro_com_mais_55(self):
        self._post_confirmar_envio_vendedor(
            telefone="91993643215",
            nome="Leandro",
            origem="personalizado",
            chave="br-1",
        )
        self._post_confirmar_envio_vendedor(
            telefone="+55 (91) 99364-3215",
            nome="Leandro",
            origem="personalizado",
            chave="br-2",
        )

        telefones = list(
            EnvioListaCompraFornecedor.objects
            .filter(lista=self.lista)
            .order_by("id")
            .values_list("telefone_destinatario", flat=True)
        )
        self.assertEqual(telefones, ["5591993643215", "5591993643215"])

    def test_confirmacao_preserva_numero_internacional_explicito(self):
        resposta = self._post_confirmar_envio_vendedor(
            telefone="+1 (415) 555-2671",
            nome="Fornecedor EUA",
            origem="personalizado",
            chave="internacional-1",
        )

        self.assertEqual(resposta.status_code, 200)
        envio = EnvioListaCompraFornecedor.objects.get(lista=self.lista)
        self.assertEqual(envio.telefone_destinatario, "14155552671")
        self.assertFalse(envio.telefone_destinatario.startswith("55"))

    def test_confirmacao_rejeita_telefone_invalido(self):
        resposta = self._post_confirmar_envio_vendedor(
            telefone="123",
            nome="Carlos",
            origem="personalizado",
            chave="invalido-1",
        )

        self.assertEqual(resposta.status_code, 400)
        self.assertFalse(EnvioListaCompraFornecedor.objects.filter(lista=self.lista).exists())

    def test_confirmacao_rejeita_lista_cancelada(self):
        self.lista.status = ListaCompraFornecedor.STATUS_CANCELADA
        self.lista.save(update_fields=["status", "atualizado_em"])

        resposta = self._post_confirmar_envio_vendedor(chave="cancelada-1")

        self.assertEqual(resposta.status_code, 400)
        self.assertIn("cancelada", resposta.json()["erro"])
        self.assertFalse(EnvioListaCompraFornecedor.objects.filter(lista=self.lista).exists())

    def test_confirmacao_lista_aberta_passa_para_enviada(self):
        resposta = self._post_confirmar_envio_vendedor(chave="aberta-1")

        self.assertEqual(resposta.status_code, 200)
        self.lista.refresh_from_db()
        self.assertEqual(self.lista.status, ListaCompraFornecedor.STATUS_ENVIADA)

    def test_confirmacao_lista_finalizada_nao_e_rebaixada(self):
        self.lista.status = ListaCompraFornecedor.STATUS_FINALIZADA
        self.lista.save(update_fields=["status", "atualizado_em"])

        resposta = self._post_confirmar_envio_vendedor(chave="finalizada-1")

        self.assertEqual(resposta.status_code, 200)
        self.lista.refresh_from_db()
        self.assertEqual(self.lista.status, ListaCompraFornecedor.STATUS_FINALIZADA)

    def test_abrir_whatsapp_nao_marca_lista_como_enviada(self):
        resposta = self.client.get(self.url, secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "window.open(url")
        self.lista.refresh_from_db()
        self.assertEqual(self.lista.status, ListaCompraFornecedor.STATUS_ABERTA)
        self.assertFalse(EnvioListaCompraFornecedor.objects.filter(lista=self.lista).exists())

    def test_registrar_destinatario_recente_nao_marca_lista_como_enviada(self):
        resposta = self._post_destinatario_recente("91988888888", "Carlos")

        self.assertEqual(resposta.status_code, 200)
        self.lista.refresh_from_db()
        self.assertEqual(self.lista.status, ListaCompraFornecedor.STATUS_ABERTA)
        self.assertFalse(EnvioListaCompraFornecedor.objects.filter(lista=self.lista).exists())

    def test_gerar_imagem_ou_compartilhamento_visual_nao_marca_lista_como_enviada(self):
        resposta = self.client.get(
            reverse("estoque:compras_lista_fornecedor_whatsapp_imagem", kwargs={"pk": self.lista.pk}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.lista.refresh_from_db()
        self.assertEqual(self.lista.status, ListaCompraFornecedor.STATUS_ABERTA)
        self.assertFalse(EnvioListaCompraFornecedor.objects.filter(lista=self.lista).exists())

    def test_confirmacao_duplicada_com_mesma_chave_nao_cria_dois_registros(self):
        primeira = self._post_confirmar_envio_vendedor(chave="duplicada-1")
        segunda = self._post_confirmar_envio_vendedor(chave="duplicada-1")

        self.assertEqual(primeira.status_code, 200)
        self.assertEqual(segunda.status_code, 200)
        self.assertEqual(EnvioListaCompraFornecedor.objects.filter(lista=self.lista).count(), 1)
        self.assertTrue(primeira.json()["criado"])
        self.assertFalse(segunda.json()["criado"])
        self.assertEqual(primeira.json()["envio"]["id"], segunda.json()["envio"]["id"])

    def test_confirmacao_mesma_chave_em_outra_lista_nao_reaproveita_envio(self):
        outro_fornecedor = Fornecedor.objects.create(nome="Fornecedor Outra Lista")
        outra_lista = ListaCompraFornecedor.objects.create(
            fornecedor=outro_fornecedor,
            data_lista=date(2026, 7, 14),
            data_inicio_periodo=date(2026, 7, 1),
            data_fim_periodo=date(2026, 7, 14),
            total_sugerido_original=Decimal("20.00"),
            total_lista=Decimal("20.00"),
        )

        primeira = self._post_confirmar_envio_vendedor(chave="mesma-chave-listas")
        segunda = self._post_confirmar_envio_vendedor(
            telefone="91977777777",
            nome="Ana",
            origem="personalizado",
            chave="mesma-chave-listas",
            lista=outra_lista,
        )

        self.assertEqual(primeira.status_code, 200)
        self.assertEqual(segunda.status_code, 200)
        self.assertNotEqual(primeira.json()["envio"]["id"], segunda.json()["envio"]["id"])
        self.assertEqual(EnvioListaCompraFornecedor.objects.count(), 2)
        self.assertEqual(
            EnvioListaCompraFornecedor.objects.get(lista=outra_lista).fornecedor,
            outro_fornecedor,
        )

    def test_confirmacao_mesma_chave_mesma_lista_telefone_diferente_retorna_conflito(self):
        primeira = self._post_confirmar_envio_vendedor(chave="conflito-telefone")
        segunda = self._post_confirmar_envio_vendedor(
            telefone="91977777777",
            nome="Carlos",
            origem="personalizado",
            chave="conflito-telefone",
        )

        self.assertEqual(primeira.status_code, 200)
        self.assertEqual(segunda.status_code, 409)
        self.assertEqual(segunda.json()["erro"], "Confirmacao duplicada com dados divergentes.")
        self.assertEqual(EnvioListaCompraFornecedor.objects.filter(lista=self.lista).count(), 1)

    def test_confirmacao_mesma_chave_mesma_lista_origem_diferente_retorna_conflito(self):
        primeira = self._post_confirmar_envio_vendedor(chave="conflito-origem")
        self._criar_destinatario_padrao_envio(nome="Carlos", telefone="91988888888")
        segunda = self._post_confirmar_envio_vendedor(
            telefone="91988888888",
            nome="Carlos",
            origem="padrao",
            chave="conflito-origem",
        )

        self.assertEqual(primeira.status_code, 200)
        self.assertEqual(segunda.status_code, 409)
        self.assertEqual(EnvioListaCompraFornecedor.objects.filter(lista=self.lista).count(), 1)

    def test_confirmacao_idempotente_em_lista_enviada_permanece_enviada(self):
        primeira = self._post_confirmar_envio_vendedor(chave="enviada-idempotente")
        segunda = self._post_confirmar_envio_vendedor(chave="enviada-idempotente")

        self.assertEqual(primeira.status_code, 200)
        self.assertEqual(segunda.status_code, 200)
        self.lista.refresh_from_db()
        self.assertEqual(self.lista.status, ListaCompraFornecedor.STATUS_ENVIADA)
        self.assertEqual(EnvioListaCompraFornecedor.objects.filter(lista=self.lista).count(), 1)

    def test_confirmacao_registra_usuario_autenticado(self):
        self._post_confirmar_envio_vendedor(chave="usuario-1")

        envio = EnvioListaCompraFornecedor.objects.get(lista=self.lista)
        self.assertEqual(envio.confirmado_por, self.usuario)

    def test_payload_informa_ultimo_envio_confirmado(self):
        self._post_confirmar_envio_vendedor(
            telefone="91988888888",
            nome="Carlos",
            origem="personalizado",
            chave="payload-1",
        )

        payload = views._payload_lista_fornecedor(self.lista)

        self.assertTrue(payload["envioVendedorConfirmado"])
        self.assertEqual(payload["ultimoEnvioVendedor"]["nomeDestinatario"], "Carlos")
        self.assertEqual(payload["ultimoEnvioVendedor"]["telefoneDestinatario"], "5591988888888")
        self.assertEqual(payload["ultimoEnvioVendedor"]["origemDestinatario"], "personalizado")

    def test_tela_ja_confirmada_exibe_dados_do_envio(self):
        self._post_confirmar_envio_vendedor(
            telefone="91988888888",
            nome="Carlos",
            origem="personalizado",
            chave="tela-confirmada-1",
        )

        resposta = self.client.get(self.url, secure=True)

        self.assertContains(resposta, "Envio ao vendedor confirmado")
        self.assertContains(resposta, "Carlos")
        self.assertContains(resposta, "+55 (91) 98888-8888")
        self.assertContains(resposta, "confirmadoEmTexto")

    def test_javascript_nao_mostra_sucesso_quando_endpoint_retorna_erro(self):
        resposta = self.client.get(self.url, secure=True)

        self.assertContains(resposta, "throw new Error(resultado.erro")
        self.assertContains(resposta, "Falha de rede ao confirmar")
        self.assertContains(resposta, "Confirmacao registrada com sucesso.")

    def test_confirmacao_usa_transaction_atomic_e_select_for_update(self):
        from pathlib import Path

        conteudo = Path("estoque/views.py").read_text(encoding="utf-8")
        self.assertIn("with transaction.atomic():", conteudo)
        self.assertIn(".select_for_update()", conteudo)

    def test_fornecedor_da_confirmacao_corresponde_ao_fornecedor_da_lista(self):
        outro_fornecedor = Fornecedor.objects.create(nome="Fornecedor Incorreto")

        envio = EnvioListaCompraFornecedor(
            lista=self.lista,
            fornecedor=outro_fornecedor,
            telefone_destinatario="91988888888",
            confirmado_em=timezone.now(),
            origem_destinatario=EnvioListaCompraFornecedor.ORIGEM_PERSONALIZADO,
            chave_idempotencia="fornecedor-incorreto",
        )

        with self.assertRaises(ValidationError):
            envio.full_clean()

    def test_payload_formata_telefone_recente_com_codigo_do_pais(self):
        FornecedorDestinatarioRecente.objects.create(
            fornecedor=self.fornecedor,
            nome="Leandro",
            telefone="5591993643215",
            ultima_utilizacao=timezone.now(),
        )

        payload = views._payload_lista_fornecedor(self.lista)

        recente = payload["destinatariosRecentes"][0]
        self.assertEqual(recente["telefone"], "5591993643215")
        self.assertEqual(recente["telefoneFormatado"], "+55 (91) 99364-3215")

    def test_destinatario_padrao_permanece_inalterado_ao_registrar_recente(self):
        contato = FornecedorContato.objects.create(
            fornecedor=self.fornecedor,
            nome="Vendedor Padrao",
            principal=True,
            ativo=True,
        )
        telefone = FornecedorContatoTelefone.objects.create(
            contato=contato,
            numero="91955554444",
            whatsapp=True,
            principal=True,
            ativo=True,
        )
        destinatario = FornecedorDestinatarioLista.objects.create(
            fornecedor=self.fornecedor,
            contato=contato,
            telefone=telefone,
        )

        self._post_destinatario_recente("91988888888", "Carlos")

        destinatario.refresh_from_db()
        self.assertEqual(destinatario.contato, contato)
        self.assertEqual(destinatario.telefone, telefone)
        self.assertEqual(FornecedorDestinatarioLista.objects.filter(fornecedor=self.fornecedor).count(), 1)

    def test_tela_exibe_destinatarios_recentes_e_mantem_abertura_whatsapp(self):
        FornecedorDestinatarioRecente.objects.create(
            fornecedor=self.fornecedor,
            nome="Carlos",
            telefone="91988888888",
            ultima_utilizacao=timezone.now(),
            quantidade_utilizacoes=3,
        )

        resposta = self.client.get(self.url, secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Destinatários recentes")
        self.assertContains(resposta, "Carlos")
        self.assertContains(resposta, "+55 (91) 98888-8888")
        self.assertContains(resposta, 'class="destinatario-recente-btn"')
        self.assertContains(resposta, 'data-origem="recente"')
        self.assertContains(resposta, 'tabindex="0"')
        self.assertContains(resposta, 'aria-pressed="false"')
        self.assertContains(resposta, "registrarDestinatarioRecente")
        self.assertContains(resposta, "window.open(url")

    def test_tela_inclui_mascara_visual_e_abertura_normalizada_do_whatsapp(self):
        contato = FornecedorContato.objects.create(
            fornecedor=self.fornecedor,
            nome="Leandro",
            principal=True,
            ativo=True,
        )
        FornecedorContatoTelefone.objects.create(
            contato=contato,
            numero="91993643215",
            whatsapp=True,
            principal=True,
            ativo=True,
        )

        resposta = self.client.get(self.url, secure=True)

        self.assertContains(resposta, "formatarWhatsappCampo")
        self.assertContains(resposta, "aplicarMascaraWhatsappCampo")
        self.assertContains(resposta, "normalizarWhatsapp(whatsappDestinatario?.value")
        self.assertContains(resposta, 'textoOriginal.startsWith("+") && !textoOriginal.startsWith("+55")')
        self.assertContains(resposta, '"numero": "91993643215"')


class ComprasListaFornecedorGravarTests(TestCase):
    def setUp(self):
        self.fornecedor = Fornecedor.objects.create(nome="Fornecedor Lista Gravar")

    def criar_produto(self, nome, quantidade=Decimal("5.000")):
        return Produto.objects.create(
            nome=nome,
            preco_compra=Decimal("10.00"),
            preco_vista=Decimal("15.00"),
            preco_prazo=Decimal("16.00"),
            quantidade=quantidade,
        )

    def ativar_frequencia_fornecedor(self, fornecedor=None, referencia=date(2026, 7, 14), intervalo=7):
        fornecedor = fornecedor or self.fornecedor
        fornecedor.frequencia_visita_ativa = True
        fornecedor.frequencia_visita_intervalo_dias = intervalo
        fornecedor.frequencia_visita_dia_semana = referencia.weekday()
        fornecedor.frequencia_visita_data_referencia = referencia
        fornecedor.save()
        return fornecedor

    def payload(self, linhas):
        return {
            "fornecedorId": str(self.fornecedor.id),
            "dataInicio": "2026-06-01",
            "dataFim": "2026-06-15",
            "dataChegada": "2026-06-20",
            "totalOriginal": "0,00",
            "linhas": linhas,
        }

    def criar_linha(self, produto, sugestao, total=None):
        sugestao_decimal = Decimal(str(sugestao))
        preco_compra = Decimal("10.00")
        total_decimal = Decimal(str(total)) if total is not None else sugestao_decimal * preco_compra

        return {
            "produtoId": str(produto.id),
            "produtoNome": produto.nome,
            "estoque": "5.000",
            "minimo": "1.000",
            "vendido": "0.000",
            "pedidos": "0.000",
            "sugestaoOriginal": str(sugestao_decimal),
            "sugestao": str(sugestao_decimal),
            "sugestaoFinal": str(sugestao_decimal),
            "quantidadeFinal": str(sugestao_decimal),
            "unidade": "UN",
            "precoCompra": str(preco_compra),
            "precoUnitario": str(preco_compra),
            "total": str(total_decimal),
            "ativo": True,
            "removido": False,
        }

    def criar_linha_removida(self, produto, sugestao, total=None):
        linha = self.criar_linha(produto, sugestao, total=total)
        linha["removido"] = True
        return linha

    def criar_linha_ativo_false(self, produto, sugestao, total=None):
        linha = self.criar_linha(produto, sugestao, total=total)
        linha["ativo"] = False
        return linha

    def criar_lista_com_item(self, produto, quantidade=Decimal("2.000"), total=Decimal("20.00")):
        lista = ListaCompraFornecedor.objects.create(
            fornecedor=self.fornecedor,
            data_lista=timezone.localdate(),
            data_inicio_periodo=timezone.localdate(),
            data_fim_periodo=timezone.localdate(),
            total_lista=total,
        )
        ItemListaCompraFornecedor.objects.create(
            lista=lista,
            produto=produto,
            estoque_atual=produto.quantidade,
            estoque_minimo=Decimal("1.000"),
            quantidade_final=quantidade,
            unidade="UN",
            preco_compra=Decimal("10.00"),
            preco_unitario=Decimal("10.00"),
            total=total,
        )
        return lista

    def criar_compra_vinculada(self, lista, status=Compra.STATUS_RASCUNHO, cancelada=False):
        return Compra.objects.create(
            fornecedor=self.fornecedor,
            data_compra=timezone.localdate(),
            tipo_pagamento="",
            total=lista.total_lista,
            status=status,
            cancelada=cancelada,
            observacao=f"Gerada a partir da Lista de Compras #{lista.id}",
        )

    def criar_compra_historico_produto(
        self,
        produto,
        fornecedor=None,
        dias_atras=0,
        quantidade=Decimal("1.000"),
        unidade="UN",
        preco=Decimal("10.00"),
        status=Compra.STATUS_FINALIZADA,
        cancelada=False,
    ):
        compra = Compra.objects.create(
            fornecedor=fornecedor or self.fornecedor,
            data_compra=timezone.localdate() - timedelta(days=dias_atras),
            tipo_pagamento="avista",
            total=(quantidade * preco).quantize(Decimal("0.01")),
            status=status,
            cancelada=cancelada,
        )
        ItemCompra.objects.create(
            compra=compra,
            produto=produto,
            quantidade=quantidade,
            unidade=unidade,
            preco_unitario=preco,
            valor_total=(quantidade * preco).quantize(Decimal("0.01")),
        )
        return compra

    def assert_mensagem_resposta(self, resposta, trecho):
        mensagens = [str(mensagem) for mensagem in resposta.context["messages"]]
        self.assertTrue(
            any(trecho in mensagem for mensagem in mensagens),
            f"Mensagem com trecho {trecho!r} nao encontrada em {mensagens!r}",
        )

    def test_nova_lista_exibe_botao_mobile_gravar_lista(self):
        resposta = self.client.get(reverse("estoque:sugestao_compra_fornecedor"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'id="btnGravarListaFornecedor"')
        self.assertContains(resposta, 'id="btnGravarGerarCompraFornecedor"')
        self.assertContains(resposta, 'id="btnGravarListaMobile"')
        self.assertContains(resposta, 'id="btnGravarGerarCompraMobile"')
        self.assertContains(resposta, 'name="gerar_compra"')
        self.assertContains(resposta, "Gravar lista")
        self.assertContains(resposta, "Gravar e Gerar Compra")
        self.assertContains(resposta, "Revise os itens e toque em Gravar lista para salvar, ou em Gravar e Gerar Compra")
        self.assertContains(resposta, "focarCampoSeguroDepoisDeRemover")
        self.assertContains(resposta, "proximoItemSeguroAntesRemover")
        self.assertContains(resposta, "nextElementSibling")
        self.assertContains(resposta, "scrollIntoView")
        self.assertContains(resposta, 'document.getElementById("produtoManualBuscaSugestao")')
        self.assertContains(resposta, '.matches(".sugestao-remover-item")')

    def test_mobile_topo_mostra_consultar_listas_e_nova_lista_sem_alterar_desktop(self):
        resposta = self.client.get(reverse("estoque:sugestao_compra_fornecedor"), secure=True)
        url_nova = reverse("estoque:sugestao_compra_fornecedor")

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "sugestao-topo-mobile-hidden")
        self.assertContains(resposta, "sugestao-topo-mobile-only")
        self.assertContains(resposta, f'href="{url_nova}?nova=1"')
        self.assertContains(resposta, "Consultar Compras")
        self.assertContains(resposta, "Nova compra")
        self.assertContains(resposta, "@media (max-width: 860px)")

    def test_nova_lista_mobile_tem_hooks_para_abrir_limpa_e_calcular_periodo(self):
        hoje = timezone.localdate().isoformat()
        resposta = self.client.get(
            reverse("estoque:sugestao_compra_fornecedor"),
            {"nova": "1"},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'id="periodo" min="1" step="1" inputmode="numeric" value=""')
        self.assertContains(resposta, 'id="data_inicio" value=""')
        self.assertContains(resposta, 'id="data_fim" value=""')
        self.assertContains(resposta, f'id="data_chegada" value="{hoje}"')
        self.assertContains(resposta, "novaListaMobileLimpa")
        self.assertContains(resposta, 'params.get("nova") === "1"')
        self.assertContains(resposta, 'periodo.value = "";')
        self.assertContains(resposta, 'dataInicio.value = "";')
        self.assertContains(resposta, 'dataFim.value = "";')
        self.assertContains(resposta, "dataChegada.value = hojeIso();")
        self.assertContains(resposta, "fornecedorBusca?.focus();")

    def test_abrir_tela_pelo_aviso_preserva_fornecedor_e_data_visita(self):
        data_visita = date(2026, 7, 14)
        self.ativar_frequencia_fornecedor(referencia=data_visita)

        resposta = self.client.get(
            reverse("estoque:sugestao_compra_fornecedor"),
            {
                "fornecedor": str(self.fornecedor.id),
                "fornecedor_ciclo": str(self.fornecedor.id),
                "data_visita": data_visita.isoformat(),
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.context["fornecedor"], self.fornecedor)
        self.assertEqual(resposta.context["data_visita_fornecedor"], data_visita)
        self.assertContains(resposta, f'id="fornecedorCicloLista" value="{self.fornecedor.id}"')
        self.assertContains(resposta, f'id="dataVisitaFornecedor" value="{data_visita.isoformat()}"')
        self.assertContains(resposta, f'id="listaFornecedorDataVisita" value="{data_visita.isoformat()}"')

    def test_gravar_lista_pelo_aviso_persiste_data_visita_fornecedor(self):
        produto = self.criar_produto("Produto Ciclo")
        data_visita = date(2026, 7, 14)
        self.ativar_frequencia_fornecedor(referencia=data_visita)
        payload = self.payload([self.criar_linha(produto, "2.000")])
        payload["dataVisitaFornecedor"] = data_visita.isoformat()
        payload["fornecedorCicloId"] = str(self.fornecedor.id)

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {
                "lista_payload": json.dumps(payload),
                "fornecedor_ciclo": str(self.fornecedor.id),
                "data_visita_fornecedor": data_visita.isoformat(),
            },
            secure=True,
        )

        lista = ListaCompraFornecedor.objects.get()
        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(lista.fornecedor, self.fornecedor)
        self.assertEqual(lista.data_visita_fornecedor, data_visita)

    def test_erro_de_validacao_preserva_data_visita_no_redirecionamento(self):
        data_visita = date(2026, 7, 14)
        self.ativar_frequencia_fornecedor(referencia=data_visita)
        payload = self.payload([])
        payload["dataVisitaFornecedor"] = data_visita.isoformat()
        payload["fornecedorCicloId"] = str(self.fornecedor.id)

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {
                "lista_payload": json.dumps(payload),
                "fornecedor_ciclo": str(self.fornecedor.id),
                "data_visita_fornecedor": data_visita.isoformat(),
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        self.assertIn(f"fornecedor={self.fornecedor.id}", resposta["Location"])
        self.assertIn(f"fornecedor_ciclo={self.fornecedor.id}", resposta["Location"])
        self.assertIn(f"data_visita={data_visita.isoformat()}", resposta["Location"])
        self.assertFalse(ListaCompraFornecedor.objects.exists())

    def test_rascunho_mobile_preserva_data_visita_na_lista_salva(self):
        produto = self.criar_produto("Produto Rascunho Ciclo")
        data_visita = date(2026, 7, 14)
        self.ativar_frequencia_fornecedor(referencia=data_visita)
        payload = self.payload([self.criar_linha(produto, "1.000")])
        payload["dataVisitaFornecedor"] = data_visita.isoformat()
        payload["fornecedorCicloId"] = str(self.fornecedor.id)

        self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {
                "lista_payload": json.dumps(payload),
                "gerar_compra": "0",
                "fornecedor_ciclo": str(self.fornecedor.id),
                "data_visita_fornecedor": data_visita.isoformat(),
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            secure=True,
        )

        self.assertEqual(ListaCompraFornecedor.objects.get().data_visita_fornecedor, data_visita)

    def test_edicao_preserva_data_visita_existente(self):
        produto = self.criar_produto("Produto Editar Ciclo")
        data_visita = date(2026, 7, 14)
        lista = self.criar_lista_com_item(produto)
        lista.data_visita_fornecedor = data_visita
        lista.save(update_fields=["data_visita_fornecedor"])
        payload = self.payload([self.criar_linha(produto, "3.000")])

        self.client.post(
            reverse("estoque:compras_lista_fornecedor_editar", kwargs={"pk": lista.pk}),
            {"lista_payload": json.dumps(payload)},
            secure=True,
        )

        lista.refresh_from_db()
        self.assertEqual(lista.data_visita_fornecedor, data_visita)

    def test_data_visita_invalida_nao_e_gravada(self):
        produto = self.criar_produto("Produto Data Invalida")
        payload = self.payload([self.criar_linha(produto, "2.000")])
        payload["dataVisitaFornecedor"] = "2026-99-99"
        payload["fornecedorCicloId"] = str(self.fornecedor.id)

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {"lista_payload": json.dumps(payload)},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        self.assertFalse(ListaCompraFornecedor.objects.exists())

    def test_criacao_manual_sem_ciclo_continua_permitida(self):
        produto = self.criar_produto("Produto Manual Sem Ciclo")

        self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {"lista_payload": json.dumps(self.payload([self.criar_linha(produto, "2.000")]))},
            secure=True,
        )

        self.assertIsNone(ListaCompraFornecedor.objects.get().data_visita_fornecedor)

    def test_fornecedor_do_ciclo_nao_pode_ser_trocado_no_payload(self):
        produto = self.criar_produto("Produto Ciclo Trocado")
        outro = Fornecedor.objects.create(nome="Fornecedor Indevido")
        data_visita = date(2026, 7, 14)
        self.ativar_frequencia_fornecedor(referencia=data_visita)
        payload = self.payload([self.criar_linha(produto, "2.000")])
        payload["fornecedorId"] = str(outro.id)
        payload["dataVisitaFornecedor"] = data_visita.isoformat()
        payload["fornecedorCicloId"] = str(self.fornecedor.id)

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {
                "lista_payload": json.dumps(payload),
                "fornecedor_ciclo": str(self.fornecedor.id),
                "data_visita_fornecedor": data_visita.isoformat(),
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        self.assertFalse(ListaCompraFornecedor.objects.exists())

    def test_ids_adulterados_para_outro_fornecedor_nao_bastam_para_vincular_ciclo(self):
        produto = self.criar_produto("Produto Outro Fornecedor")
        outro = Fornecedor.objects.create(nome="Fornecedor Sem Frequencia")
        data_visita = date(2026, 7, 14)
        payload = self.payload([self.criar_linha(produto, "2.000")])
        payload["fornecedorId"] = str(outro.id)
        payload["dataVisitaFornecedor"] = data_visita.isoformat()
        payload["fornecedorCicloId"] = str(outro.id)

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {"lista_payload": json.dumps(payload)},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        self.assertFalse(ListaCompraFornecedor.objects.exists())

    def test_data_arbitraria_fora_da_frequencia_e_rejeitada(self):
        produto = self.criar_produto("Produto Data Fora Frequencia")
        data_visita = date(2026, 7, 15)
        self.ativar_frequencia_fornecedor(referencia=date(2026, 7, 14))
        payload = self.payload([self.criar_linha(produto, "2.000")])
        payload["dataVisitaFornecedor"] = data_visita.isoformat()
        payload["fornecedorCicloId"] = str(self.fornecedor.id)

        self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {"lista_payload": json.dumps(payload)},
            secure=True,
        )

        self.assertFalse(ListaCompraFornecedor.objects.exists())

    def test_data_distante_do_ciclo_e_rejeitada(self):
        produto = self.criar_produto("Produto Data Distante")
        data_visita = date(2026, 8, 25)
        self.ativar_frequencia_fornecedor(referencia=date(2026, 7, 14))
        payload = self.payload([self.criar_linha(produto, "2.000")])
        payload["dataVisitaFornecedor"] = data_visita.isoformat()
        payload["fornecedorCicloId"] = str(self.fornecedor.id)

        self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {"lista_payload": json.dumps(payload)},
            secure=True,
        )

        self.assertFalse(ListaCompraFornecedor.objects.exists())

    def test_data_do_proximo_ciclo_usada_pelo_servico_e_aceita(self):
        produto = self.criar_produto("Produto Proximo Ciclo")
        data_visita = date(2026, 7, 22)
        self.ativar_frequencia_fornecedor(referencia=date(2026, 7, 15))
        payload = self.payload([self.criar_linha(produto, "2.000")])
        payload["dataVisitaFornecedor"] = data_visita.isoformat()
        payload["fornecedorCicloId"] = str(self.fornecedor.id)

        self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {"lista_payload": json.dumps(payload)},
            secure=True,
        )

        self.assertEqual(ListaCompraFornecedor.objects.get().data_visita_fornecedor, data_visita)

    def test_somente_fornecedor_ciclo_sem_data_e_rejeitado(self):
        produto = self.criar_produto("Produto Ciclo Sem Data")
        payload = self.payload([self.criar_linha(produto, "2.000")])
        payload["fornecedorCicloId"] = str(self.fornecedor.id)

        self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {"lista_payload": json.dumps(payload)},
            secure=True,
        )

        self.assertFalse(ListaCompraFornecedor.objects.exists())

    def test_somente_data_sem_fornecedor_ciclo_e_rejeitada(self):
        produto = self.criar_produto("Produto Data Sem Ciclo")
        payload = self.payload([self.criar_linha(produto, "2.000")])
        payload["dataVisitaFornecedor"] = date(2026, 7, 14).isoformat()

        self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {"lista_payload": json.dumps(payload)},
            secure=True,
        )

        self.assertFalse(ListaCompraFornecedor.objects.exists())

    def test_alteracao_posterior_da_frequencia_nao_impede_edicao_historica(self):
        produto = self.criar_produto("Produto Historico Ciclo")
        data_visita = date(2026, 7, 14)
        lista = self.criar_lista_com_item(produto)
        lista.data_visita_fornecedor = data_visita
        lista.save(update_fields=["data_visita_fornecedor"])
        self.ativar_frequencia_fornecedor(referencia=date(2026, 7, 16))
        payload = self.payload([self.criar_linha(produto, "3.000")])

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_editar", kwargs={"pk": lista.pk}),
            {"lista_payload": json.dumps(payload)},
            secure=True,
        )

        lista.refresh_from_db()
        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(lista.data_visita_fornecedor, data_visita)

    def test_validacao_do_servico_e_da_view_usa_mesma_regra_de_calendario(self):
        data_visita = date(2026, 7, 14)
        self.ativar_frequencia_fornecedor(referencia=data_visita)

        datas_validas = datas_validas_ciclo_visita_fornecedor(self.fornecedor, data_referencia=date(2026, 7, 15))

        self.assertIn(data_visita, datas_validas)
        self.assertTrue(data_ciclo_visita_valida(self.fornecedor, data_visita, data_referencia=date(2026, 7, 15)))
        self.assertFalse(data_ciclo_visita_valida(self.fornecedor, date(2026, 7, 15), data_referencia=date(2026, 7, 15)))

    def test_edicao_lista_preserva_periodo_e_datas_salvos(self):
        produto = self.criar_produto("Produto Edicao Periodo")
        inicio = timezone.localdate() - timedelta(days=9)
        fim = timezone.localdate()
        chegada = timezone.localdate() + timedelta(days=2)
        lista = ListaCompraFornecedor.objects.create(
            fornecedor=self.fornecedor,
            data_lista=timezone.localdate(),
            data_inicio_periodo=inicio,
            data_fim_periodo=fim,
            data_chegada_prevista=chegada,
            total_lista=Decimal("20.00"),
        )
        ItemListaCompraFornecedor.objects.create(
            lista=lista,
            produto=produto,
            estoque_atual=Decimal("5.000"),
            estoque_minimo=Decimal("1.000"),
            quantidade_final=Decimal("2.000"),
            unidade="UN",
            preco_compra=Decimal("10.00"),
            preco_unitario=Decimal("10.00"),
            total=Decimal("20.00"),
        )

        resposta = self.client.get(
            reverse("estoque:compras_lista_fornecedor_editar", kwargs={"pk": lista.pk}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, f'id="periodo" min="1" step="1" inputmode="numeric" value="9"')
        self.assertContains(resposta, f'id="data_inicio" value="{inicio.isoformat()}"')
        self.assertContains(resposta, f'id="data_fim" value="{fim.isoformat()}"')
        self.assertContains(resposta, f'id="data_chegada" value="{chegada.isoformat()}"')

    def test_visualizacao_lista_aberta_mostra_botao_editar_lista(self):
        produto = self.criar_produto("Produto Botao Editar Ver Lista")
        lista = self.criar_lista_com_item(produto)
        url_edicao = reverse(
            "estoque:compras_lista_fornecedor_editar",
            kwargs={"pk": lista.pk},
        )

        resposta = self.client.get(
            reverse(
                "estoque:compras_lista_fornecedor_ver",
                kwargs={"pk": lista.pk},
            ),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(
            resposta,
            f'<a class="lista-ver-btn" href="{url_edicao}">Editar Lista</a>',
        )

    def test_visualizacao_lista_cancelada_nao_mostra_botao_editar_lista(self):
        produto = self.criar_produto("Produto Sem Botao Editar Ver Lista")
        lista = self.criar_lista_com_item(produto)
        lista.status = ListaCompraFornecedor.STATUS_CANCELADA
        lista.save(update_fields=["status"])

        url_edicao = reverse(
            "estoque:compras_lista_fornecedor_editar",
            kwargs={"pk": lista.pk},
        )

        resposta = self.client.get(
            reverse(
                "estoque:compras_lista_fornecedor_ver",
                kwargs={"pk": lista.pk},
            ),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertNotContains(
            resposta,
            f'<a class="lista-ver-btn" href="{url_edicao}">Editar Lista</a>',
        )

    def test_mobile_gravar_lista_vazia_usa_modal_padronizado(self):
        resposta = self.client.get(
            reverse("estoque:sugestao_compra_fornecedor"),
            secure=True,
        )
        html = resposta.content.decode()

        self.assertEqual(resposta.status_code, 200)
        self.assertIn(
            "const salvandoRascunho = opcoes?.rascunho === true;",
            html,
        )
        self.assertIn(
            r"Adicione pelo menos um produto \u00e0 lista antes de gravar.",
            html,
        )
        self.assertIn(
            "abrirModalListaVaziaRascunhoMobile(",
            html,
        )
        self.assertIn(
            '"Lista vazia",',
            html,
        )
        self.assertNotIn(
            'alert("Adicione pelo menos um produto ? lista antes de gravar.");',
            html,
        )

    def test_autocomplete_sem_selo_de_fornecedor(self):
        resposta = self.client.get(
            reverse("estoque:sugestao_compra_fornecedor"),
            secure=True,
        )
        html = resposta.content.decode()

        inicio = html.index("function tagFornecedor(produto)")
        fim = html.index("function tagJaNaLista(produto)", inicio)
        bloco_fornecedor = html[inicio:fim]

        self.assertEqual(resposta.status_code, 200)
        self.assertIn('return "";', bloco_fornecedor)
        self.assertNotIn("Fora deste fornecedor", bloco_fornecedor)
        self.assertNotIn(
            "sugestao-autocomplete-tag",
            bloco_fornecedor,
        )

    def test_mobile_produto_manual_entra_direto_em_itens_da_lista(self):
        resposta = self.client.get(
            reverse("estoque:sugestao_compra_fornecedor"),
            secure=True,
        )
        html = resposta.content.decode()

        inicio = html.index("function adicionarSelecionado()")
        fim = html.index("function selecionarTextoBusca()", inicio)
        bloco_adicionar = html[inicio:fim]

        self.assertEqual(resposta.status_code, 200)
        self.assertIn(
            'mobile.insertAdjacentHTML("beforeend", cardMobile(produto));',
            bloco_adicionar,
        )
        self.assertIn(
            "const mobileVisivel = Boolean(",
            bloco_adicionar,
        )
        self.assertIn(
            'typeof window.adicionarCardSugestaoNaLista === "function"',
            bloco_adicionar,
        )
        self.assertIn(
            "window.adicionarCardSugestaoNaLista(cardManual);",
            bloco_adicionar,
        )
        self.assertIn(
            "window.adicionarCardSugestaoNaLista = adicionarCardNaLista;",
            html,
        )

    def test_autocomplete_permite_consultar_produto_ja_presente_sem_duplicar(self):
        resposta = self.client.get(
            reverse("estoque:sugestao_compra_fornecedor"),
            secure=True,
        )
        html = resposta.content.decode()

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("function tagJaNaLista(produto)", html)
        self.assertIn("function atualizarEstadoBotaoProdutoManual(produto)", html)
        self.assertIn("j\\u00e1 est\\u00e1 na lista", html)
        self.assertIn('"J\\u00e1 est\\u00e1 na lista"', html)
        self.assertIn("botao.disabled = jaNaLista;", html)
        self.assertIn(
            "Dados e \\u00faltimas compras dispon\\u00edveis para consulta.",
            html,
        )
        self.assertNotIn(
            "if (existeNaLista(produto.id)) return false;",
            html,
        )
        self.assertNotIn(
            "!existeNaLista(produto.id) && String(produto.nome",
            html,
        )
        self.assertIn(
            "if (existeNaLista(produtoId)) {",
            html,
        )
        self.assertIn(
            "atualizarEstadoBotaoProdutoManual(produtoBase);",
            html,
        )

    def test_nova_lista_payload_manual_traz_historico_do_produto(self):
        fornecedor_compra = Fornecedor.objects.create(
            nome="Fornecedor Compra Manual Nova Lista"
        )
        produto = self.criar_produto(
            "Produto Manual Historico Nova Lista"
        )

        self.criar_compra_historico_produto(
            produto,
            fornecedor=fornecedor_compra,
            quantidade=Decimal("7.500"),
            unidade="CX",
            preco=Decimal("21.40"),
        )

        hoje = timezone.localdate()
        resposta = self.client.get(
            reverse("estoque:sugestao_compra_fornecedor"),
            {
                "fornecedor": self.fornecedor.pk,
                "periodo": "7",
                "data_inicio": (hoje - timedelta(days=7)).isoformat(),
                "data_fim": hoje.isoformat(),
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)

        produtos_payload = resposta.context["produtos_manual_payload"]
        payload_produto = next(
            item for item in produtos_payload
            if item["id"] == produto.id
        )
        historico = payload_produto["historico_ultimas_compras"]

        self.assertEqual(len(historico), 1)
        self.assertEqual(
            historico[0]["fornecedor"],
            fornecedor_compra.nome,
        )
        self.assertEqual(historico[0]["quantidade"], "7.500")
        self.assertEqual(historico[0]["unidade"], "CX")
        self.assertEqual(historico[0]["preco"], "21.40")

        self.assertContains(
            resposta,
            'id="produtoManualHistoricoRow"',
        )
        self.assertContains(
            resposta,
            'id="produtoManualHistoricoConteudo"',
        )
        self.assertContains(
            resposta,
            "renderizarHistoricoProdutoManual",
        )
        self.assertContains(
            resposta,
            "function historicoMobileProdutoHtml(produto)",
        )
        self.assertContains(
            resposta,
            "${historicoMobileProdutoHtml(produto)}",
        )
        self.assertContains(
            resposta,
            'data-historico-toggle aria-expanded="false"',
        )

    def test_historico_manual_nao_e_ocultado_no_mobile(self):
        resposta = self.client.get(
            reverse("estoque:sugestao_compra_fornecedor"),
            secure=True,
        )
        html = resposta.content.decode()

        inicio = html.index(
            "function renderizarHistoricoProdutoManual(produto)"
        )
        fim = html.index("function limparLinhaManual", inicio)
        bloco_historico = html[inicio:fim]

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("if (!produto) {", bloco_historico)
        self.assertNotIn("telaMobile", bloco_historico)
        self.assertIn(
            'id="produtoManualHistoricoMobile"',
            html,
        )
        self.assertLess(
            html.index('id="produtoManualHistoricoMobile"'),
            html.index('id="btnAdicionarProdutoSugestao"'),
        )
        self.assertIn(
            "manualHistoricoMobile.hidden = false;",
            html,
        )
        self.assertIn(
            "manualHistoricoMobile.hidden = true;",
            bloco_historico,
        )
        self.assertIn(
            "exibirHistoricoManual(comprasHtml);",
            bloco_historico,
        )

    def test_edicao_payload_manual_traz_historico_para_produto_novo(self):
        fornecedor_compra = Fornecedor.objects.create(
            nome="Fornecedor Compra Manual Edicao"
        )
        produto_lista = self.criar_produto(
            "Produto Existente na Lista Manual"
        )
        produto_adicionar = self.criar_produto(
            "Produto Novo com Historico Manual"
        )
        lista = self.criar_lista_com_item(produto_lista)

        self.criar_compra_historico_produto(
            produto_adicionar,
            fornecedor=fornecedor_compra,
            quantidade=Decimal("3.250"),
            unidade="UN",
            preco=Decimal("8.75"),
        )

        resposta = self.client.get(
            reverse(
                "estoque:compras_lista_fornecedor_editar",
                kwargs={"pk": lista.pk},
            ),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)

        produtos_payload = resposta.context["produtos_manual_payload"]
        payload_produto = next(
            item for item in produtos_payload
            if item["id"] == produto_adicionar.id
        )
        historico = payload_produto["historico_ultimas_compras"]

        self.assertEqual(len(historico), 1)
        self.assertEqual(
            historico[0]["fornecedor"],
            fornecedor_compra.nome,
        )
        self.assertEqual(historico[0]["quantidade"], "3.250")
        self.assertEqual(historico[0]["unidade"], "UN")
        self.assertEqual(historico[0]["preco"], "8.75")

        self.assertContains(
            resposta,
            'id="produtoManualHistoricoRow"',
        )
        self.assertContains(
            resposta,
            "Nenhuma compra anterior encontrada",
        )
        self.assertContains(
            resposta,
            "function historicoMobileProdutoHtml(produto)",
        )
        self.assertContains(
            resposta,
            'if (!historico.length) return "";',
        )
        self.assertContains(
            resposta,
            "${historicoMobileProdutoHtml(produto)}",
        )

    def test_edicao_manual_produto_novo_salva_historico_correto_e_nao_herda_sem_historico(self):
        fornecedor_compra = Fornecedor.objects.create(
            nome="Fornecedor Compra Manual Salvar Edicao"
        )
        produto_lista = self.criar_produto("Produto Ja Salvo Edicao Manual")
        produto_com_historico = self.criar_produto(
            "Produto Novo Edicao Com Historico"
        )
        produto_sem_historico = self.criar_produto(
            "Produto Novo Edicao Sem Historico"
        )
        lista = self.criar_lista_com_item(produto_lista)

        self.criar_compra_historico_produto(
            produto_com_historico,
            fornecedor=fornecedor_compra,
            quantidade=Decimal("4.750"),
            unidade="CX",
            preco=Decimal("13.60"),
        )

        resposta_get = self.client.get(
            reverse(
                "estoque:compras_lista_fornecedor_editar",
                kwargs={"pk": lista.pk},
            ),
            secure=True,
        )
        produtos_payload = resposta_get.context["produtos_manual_payload"]
        payload_com_historico = next(
            item for item in produtos_payload
            if item["id"] == produto_com_historico.id
        )
        payload_sem_historico = next(
            item for item in produtos_payload
            if item["id"] == produto_sem_historico.id
        )

        self.assertEqual(resposta_get.status_code, 200)
        self.assertEqual(
            payload_com_historico["historico_ultimas_compras"][0]["fornecedor"],
            fornecedor_compra.nome,
        )
        self.assertEqual(
            payload_com_historico["historico_ultimas_compras"][0]["quantidade"],
            "4.750",
        )
        self.assertEqual(payload_sem_historico["historico_ultimas_compras"], [])
        self.assertContains(
            resposta_get,
            'if (!historico.length) return "";',
        )

        resposta_post = self.client.post(
            reverse(
                "estoque:compras_lista_fornecedor_editar",
                kwargs={"pk": lista.pk},
            ),
            {
                "lista_payload": json.dumps(
                    self.payload([
                        self.criar_linha(produto_lista, 2),
                        self.criar_linha(
                            produto_com_historico,
                            5,
                            total=Decimal("50.00"),
                        ),
                    ])
                )
            },
            secure=True,
        )

        self.assertRedirects(
            resposta_post,
            reverse("estoque:compras_lista_fornecedor_ver", kwargs={"pk": lista.pk}),
            fetch_redirect_response=False,
        )
        lista.refresh_from_db()
        produtos_salvos = set(lista.itens.values_list("produto_id", flat=True))
        self.assertEqual(produtos_salvos, {produto_lista.id, produto_com_historico.id})
        item_adicionado = lista.itens.get(produto=produto_com_historico)
        self.assertEqual(item_adicionado.quantidade_final, Decimal("5.000"))
        self.assertEqual(item_adicionado.total, Decimal("50.00"))

        resposta_reabrir = self.client.get(
            reverse(
                "estoque:compras_lista_fornecedor_editar",
                kwargs={"pk": lista.pk},
            ),
            secure=True,
        )
        linha_adicionada = next(
            linha for linha in resposta_reabrir.context["linhas"]
            if linha["produto_id"] == produto_com_historico.id
        )

        self.assertEqual(resposta_reabrir.status_code, 200)
        self.assertEqual(
            linha_adicionada["historico_ultimas_compras"][0]["fornecedor"],
            fornecedor_compra.nome,
        )

    def test_historico_desktop_mostra_cabecalhos_claros(self):
        fornecedor = Fornecedor.objects.create(nome="Fornecedor Cabecalho Historico")
        produto = self.criar_produto("Produto Cabecalho Historico")
        lista = self.criar_lista_com_item(produto)
        self.criar_compra_historico_produto(
            produto,
            fornecedor=fornecedor,
            quantidade=Decimal("3.000"),
            unidade="CX",
            preco=Decimal("15.50"),
        )

        resposta_ver = self.client.get(
            reverse("estoque:compras_lista_fornecedor_ver", kwargs={"pk": lista.pk}),
            secure=True,
        )
        resposta_edicao = self.client.get(
            reverse("estoque:compras_lista_fornecedor_editar", kwargs={"pk": lista.pk}),
            secure=True,
        )

        for resposta in (resposta_ver, resposta_edicao):
            self.assertEqual(resposta.status_code, 200)
            self.assertContains(resposta, "Qde comprada")
            self.assertContains(resposta, "Pre&ccedil;o comprado", html=False)

        self.assertContains(resposta_ver, "lista-ver-historico-desktop-cabecalho")
        self.assertContains(resposta_edicao, "sugestao-historico-desktop-cabecalho")
        self.assertContains(resposta_ver, "lista-ver-historico-desktop-quantidade")
        self.assertContains(resposta_edicao, "sugestao-historico-desktop-quantidade")

    def test_mobile_historico_ultimas_compras_disponivel_na_edicao(self):
        fornecedor_recente = Fornecedor.objects.create(nome="Fornecedor Historico Recente")
        fornecedor_antigo = Fornecedor.objects.create(nome="Fornecedor Historico Antigo")
        produto = self.criar_produto("Produto Historico Edicao")
        lista = self.criar_lista_com_item(produto)
        self.criar_compra_historico_produto(
            produto,
            fornecedor=fornecedor_antigo,
            dias_atras=4,
            quantidade=Decimal("4.000"),
            unidade="CX",
            preco=Decimal("12.34"),
        )
        self.criar_compra_historico_produto(
            produto,
            fornecedor=fornecedor_recente,
            dias_atras=1,
            quantidade=Decimal("2.500"),
            unidade="UN",
            preco=Decimal("9.87"),
        )

        resposta = self.client.get(
            reverse("estoque:compras_lista_fornecedor_editar", kwargs={"pk": lista.pk}),
            secure=True,
        )
        html = resposta.content.decode()

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'class="sugestao-historico-toggle" data-historico-toggle aria-expanded="false"')
        self.assertContains(resposta, '<button type="button" class="sugestao-historico-toggle"')
        self.assertContains(resposta, "Últimas 3 compras deste produto")
        self.assertContains(resposta, fornecedor_recente.nome)
        self.assertContains(resposta, fornecedor_antigo.nome)
        self.assertContains(resposta, "2.500 UN")
        self.assertContains(resposta, "4.000 CX")
        self.assertContains(resposta, "R$ 9.87")
        self.assertContains(resposta, "R$ 12.34")
        self.assertLess(html.index(fornecedor_recente.nome), html.index(fornecedor_antigo.nome))
        self.assertContains(resposta, "function fecharHistoricosComprasMobile(exceto)")

    def test_mobile_historico_ultimas_compras_visualizacao_limita_ordena_e_ignora_cancelada(self):
        produto = self.criar_produto("Produto Historico Ver")
        lista = self.criar_lista_com_item(produto)
        fornecedores = [
            Fornecedor.objects.create(nome=f"Fornecedor Historico {indice}")
            for indice in range(1, 6)
        ]
        self.criar_compra_historico_produto(produto, fornecedor=fornecedores[0], dias_atras=5, preco=Decimal("5.00"))
        self.criar_compra_historico_produto(produto, fornecedor=fornecedores[1], dias_atras=3, preco=Decimal("6.00"))
        self.criar_compra_historico_produto(produto, fornecedor=fornecedores[2], dias_atras=2, preco=Decimal("7.00"), cancelada=True)
        self.criar_compra_historico_produto(produto, fornecedor=fornecedores[3], dias_atras=1, quantidade=Decimal("3.250"), unidade="FD", preco=Decimal("8.50"))
        self.criar_compra_historico_produto(produto, fornecedor=fornecedores[4], dias_atras=0, quantidade=Decimal("1.750"), unidade="UN", preco=Decimal("9.25"))

        resposta = self.client.get(reverse("estoque:compras_lista_fornecedor_ver", kwargs={"pk": lista.pk}), secure=True)
        html = resposta.content.decode()

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, '<button type="button" class="lista-ver-historico-toggle"')
        self.assertContains(resposta, "Últimas 3 compras deste produto")
        self.assertContains(resposta, fornecedores[4].nome)
        self.assertContains(resposta, fornecedores[3].nome)
        self.assertContains(resposta, fornecedores[1].nome)
        self.assertNotContains(resposta, fornecedores[0].nome)
        self.assertNotContains(resposta, fornecedores[2].nome)
        self.assertContains(resposta, "1.750 UN")
        self.assertContains(resposta, "3.250 FD")
        self.assertContains(resposta, "R$ 9.25")
        self.assertContains(resposta, "R$ 8.50")
        self.assertLess(html.index(fornecedores[4].nome), html.index(fornecedores[3].nome))
        self.assertLess(html.index(fornecedores[3].nome), html.index(fornecedores[1].nome))
        self.assertContains(resposta, "function fecharHistoricosListaVer(exceto)")

    def test_mobile_historico_produto_sem_compras_mostra_mensagem(self):
        produto = self.criar_produto("Produto Sem Historico")
        lista = self.criar_lista_com_item(produto)

        resposta_ver = self.client.get(reverse("estoque:compras_lista_fornecedor_ver", kwargs={"pk": lista.pk}), secure=True)
        resposta_edicao = self.client.get(
            reverse("estoque:compras_lista_fornecedor_editar", kwargs={"pk": lista.pk}),
            secure=True,
        )

        self.assertEqual(resposta_ver.status_code, 200)
        self.assertEqual(resposta_edicao.status_code, 200)
        self.assertContains(resposta_ver, "Nenhuma compra anterior encontrada para este produto.")
        self.assertContains(resposta_edicao, "Nenhuma compra anterior encontrada para este produto.")

    def test_historico_desktop_disponivel_sem_alterar_mobile_ou_campos_existentes(self):
        produto = self.criar_produto("Produto Historico Desktop")
        lista = self.criar_lista_com_item(produto)

        resposta_ver = self.client.get(
            reverse("estoque:compras_lista_fornecedor_ver", kwargs={"pk": lista.pk}),
            secure=True,
        )
        resposta_edicao = self.client.get(
            reverse("estoque:compras_lista_fornecedor_editar", kwargs={"pk": lista.pk}),
            secure=True,
        )

        html_ver = resposta_ver.content.decode()
        html_edicao = resposta_edicao.content.decode()

        tabela_ver = re.search(
            r'<table class="lista-ver-table lista-ver-tabela">(?P<conteudo>.*?)</table>',
            html_ver,
            re.S,
        )
        tabela_edicao = re.search(
            r'<div class="sugestao-table-wrap sugestao-desktop">(?P<conteudo>.*?)</div>\s*<div class="sugestao-mobile"',
            html_edicao,
            re.S,
        )

        self.assertEqual(resposta_ver.status_code, 200)
        self.assertEqual(resposta_edicao.status_code, 200)
        self.assertIsNotNone(tabela_ver)
        self.assertIsNotNone(tabela_edicao)

        self.assertIn("data-lista-ver-historico-desktop-toggle", tabela_ver.group("conteudo"))
        self.assertIn("data-lista-ver-historico-desktop-row", tabela_ver.group("conteudo"))
        self.assertIn('colspan="6"', tabela_ver.group("conteudo"))
        self.assertIn('type="button"', tabela_ver.group("conteudo"))

        self.assertIn("data-historico-desktop-toggle", tabela_edicao.group("conteudo"))
        self.assertIn("data-historico-desktop-row", tabela_edicao.group("conteudo"))
        self.assertIn('colspan="11"', tabela_edicao.group("conteudo"))
        self.assertIn('type="button"', tabela_edicao.group("conteudo"))

        self.assertContains(resposta_ver, 'class="lista-ver-mobile-itens"')
        self.assertContains(resposta_ver, 'data-lista-ver-historico-toggle')
        self.assertContains(resposta_edicao, 'id="mobileSugestaoProdutos"')
        self.assertContains(resposta_edicao, 'class="sugestao-mobile-card"')
        self.assertContains(resposta_edicao, 'data-historico-toggle')

        self.assertContains(resposta_edicao, 'class="sugestao-input sugestao-qtd-input"')
        self.assertContains(resposta_edicao, 'class="sugestao-input sugestao-preco-input"')
        self.assertContains(resposta_edicao, 'class="sugestao-input sugestao-preco-unitario-input"')
        self.assertContains(resposta_edicao, 'class="sugestao-input sugestao-total-input"')
        self.assertContains(resposta_edicao, 'class="sugestao-btn sugestao-btn-danger sugestao-remover-item"')

        self.assertContains(resposta_edicao, "function fecharHistoricosComprasDesktop(exceto)")
        self.assertContains(resposta_ver, "function fecharHistoricosListaVerDesktop(exceto)")

    def test_mobile_produto_sugerido_tem_area_itens_adicionar_sem_duplicar_editar_remover(self):
        produto = self.criar_produto("Produto Mobile Itens", quantidade=Decimal("0.000"))
        produto.estoque_minimo = Decimal("5.000")
        produto.unidade_compra = "UN"
        produto.save(update_fields=["estoque_minimo", "unidade_compra"])
        ProdutoFornecedor.objects.create(produto=produto, fornecedor=self.fornecedor, ativo=True)

        resposta = self.client.get(
            reverse("estoque:sugestao_compra_fornecedor"),
            {"fornecedor": str(self.fornecedor.id), "periodo": "7"},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'id="itensListaMobile"')
        self.assertContains(resposta, 'id="itensListaMobileLista"')
        self.assertContains(resposta, "sugestao-adicionar-lista-mobile")
        self.assertContains(resposta, "sugestao-item-lista-mobile")
        self.assertContains(resposta, "sugestao-item-lista-qtd-input")
        self.assertContains(resposta, "sugestao-item-lista-preco-input")
        self.assertContains(resposta, "sugestao-editar-lista-mobile")
        self.assertContains(resposta, "sugestao-remover-lista-mobile")
        self.assertContains(resposta, 'card.dataset.naLista === "1"')
        self.assertContains(resposta, "marcarBotao(card, true)")
        self.assertContains(resposta, "sincronizarItemComCard")
        self.assertContains(resposta, 'data-itens-lista-mobile-contador data-count="0"')
        self.assertContains(resposta, "Produtos vinculados ao fornecedor:")
        self.assertContains(resposta, "Itens adicionados na lista:")
        self.assertContains(resposta, "data-itens-lista-mobile-total")
        self.assertContains(resposta, "atualizarContadorItensLista")

    def test_mobile_payload_usa_itens_escolhidos_quando_existirem(self):
        resposta = self.client.get(reverse("estoque:sugestao_compra_fornecedor"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "window.sugestaoMobileCardsParaLista")
        self.assertContains(resposta, 'return colecao.filter((card) => card?.dataset?.naLista === "1");')
        self.assertContains(resposta, "const cardsLista = window.sugestaoMobileCardsParaLista")
        self.assertContains(resposta, "cardsMobile()")
        self.assertContains(resposta, "temItensEscolhidosMobile")
        self.assertContains(resposta, "orientarAdicionarItensMobile")
        self.assertContains(
            resposta,
            r"Adicione pelo menos um produto \u00e0 lista antes de gravar.",
        )
        self.assertContains(resposta, 'if (!usarItensDesktop() && !temItensEscolhidosMobile())')
        self.assertContains(resposta, 'orientarAdicionarItensMobile({ rascunho: salvarRascunhoAoGravar });')
        self.assertContains(
            resposta,
            "const focoRetorno = salvandoRascunho",
        )

    def test_mobile_gravar_e_gerar_compra_bloqueiam_sem_itens_escolhidos(self):
        resposta = self.client.get(reverse("estoque:sugestao_compra_fornecedor"), secure=True)
        html = resposta.content.decode()

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("orientarAdicionarItensMobile({ rascunho: salvarRascunhoAoGravar });", html)
        self.assertIn("novoClique(false, novoBtn);", html)
        self.assertIn("novoClique(true, btnGravarGerarCompraFornecedor);", html)
        self.assertIn(
            "abrirModalListaVaziaRascunhoMobile(",
            html,
        )
        self.assertEqual(
            html.count(r"Adicione pelo menos um produto \u00e0 lista antes de gravar."),
            1,
        )

    def test_mobile_salvar_rascunho_sem_itens_usa_modal_lista_vazia(self):
        resposta = self.client.get(reverse("estoque:sugestao_compra_fornecedor"), secure=True)
        html = resposta.content.decode()

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'id="modalListaVaziaRascunhoMobile"')
        self.assertContains(resposta, 'id="modalListaVaziaRascunhoMobileTitulo"')
        self.assertContains(resposta, 'id="modalListaVaziaRascunhoMobileMensagem"')
        self.assertContains(resposta, "Lista vazia")
        self.assertContains(resposta, "Adicione pelo menos um produto à lista antes de salvar o rascunho.")
        self.assertContains(resposta, 'id="btnFecharListaVaziaRascunhoMobile"')
        self.assertContains(resposta, "Entendi")
        self.assertContains(resposta, "function abrirModalListaVaziaRascunhoMobile(titulo, mensagem, focoRetorno)")
        self.assertContains(resposta, "function fecharModalListaVaziaRascunhoMobile()")
        self.assertContains(resposta, "btnFecharListaVaziaRascunhoMobile.addEventListener")
        self.assertContains(resposta, 'modalListaVaziaRascunhoMobileTitulo.textContent = titulo || "Lista vazia";')
        self.assertContains(resposta, "modalListaVaziaRascunhoMobileMensagem.textContent = mensagem")
        self.assertContains(
            resposta,
            "const salvandoRascunho = opcoes?.rascunho === true;",
        )
        self.assertNotIn('alert("Adicione pelo menos um produto à lista antes de salvar o rascunho.")', html)

    def test_mobile_sem_fornecedor_usa_modal_reaproveitado(self):
        resposta = self.client.get(reverse("estoque:sugestao_compra_fornecedor"), secure=True)
        html = resposta.content.decode()

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Fornecedor obrigatório")
        self.assertContains(resposta, "Selecione um fornecedor antes de salvar a lista.")
        self.assertContains(resposta, "listaFornecedorMobile() && abrirModalListaVaziaRascunhoMobile(")
        self.assertContains(resposta, "botaoAcionado")
        self.assertEqual(html.count('id="modalListaVaziaRascunhoMobile"'), 1)
        self.assertNotIn('alert("Selecione um fornecedor antes de salvar a lista.")', html)

    def test_mobile_lista_fornecedor_tem_salvar_rascunho_protegido(self):
        resposta = self.client.get(reverse("estoque:sugestao_compra_fornecedor"), secure=True)
        html = resposta.content.decode()

        self.assertEqual(resposta.status_code, 200)
        bloco_acoes = re.search(
            r'<section class="sugestao-visualizacao-acoes"[^>]*>(?P<conteudo>.*?)</section>',
            html,
            re.S,
        )
        self.assertIsNotNone(bloco_acoes)
        conteudo_acoes = bloco_acoes.group("conteudo")
        self.assertIn('id="btnSalvarRascunhoListaMobile"', conteudo_acoes)
        self.assertIn('id="avisoRascunhoProtegidoLista"', conteudo_acoes)
        self.assertIn('id="btnContinuarEditandoRascunhoLista"', conteudo_acoes)
        self.assertLess(
            conteudo_acoes.index('id="btnSalvarRascunhoListaMobile"'),
            conteudo_acoes.index('id="btnVisualizarAnalitica"'),
        )
        self.assertLess(
            conteudo_acoes.index('id="btnVisualizarAnalitica"'),
            conteudo_acoes.index('id="btnVisualizarSintetica"'),
        )
        self.assertLess(
            conteudo_acoes.index('id="btnVisualizarSintetica"'),
            conteudo_acoes.index('id="btnImagemWhatsapp"'),
        )
        self.assertLess(
            conteudo_acoes.index('id="btnImagemWhatsapp"'),
            conteudo_acoes.index('id="btnGravarListaFornecedor"'),
        )
        self.assertLess(
            conteudo_acoes.index('id="btnGravarListaFornecedor"'),
            conteudo_acoes.index('id="btnGravarGerarCompraFornecedor"'),
        )
        rodape_mobile = re.search(
            r'<div class="sugestao-salvar-mobile-rodape">(?P<conteudo>.*?)</div>',
            html,
            re.S,
        )
        self.assertIsNotNone(rodape_mobile)
        self.assertNotIn("btnSalvarRascunhoListaMobile", rodape_mobile.group("conteudo"))
        self.assertContains(resposta, "Salvar rascunho")
        self.assertContains(resposta, "sugestao-rascunho-mobile-only")
        self.assertContains(resposta, ".sugestao-rascunho-mobile-only {\n    display: none;\n  }")
        self.assertContains(resposta, '@media (max-width: 860px)')
        self.assertContains(resposta, ".sugestao-rascunho-mobile-only {\n      display: grid;\n    }")
        self.assertContains(resposta, 'id="btnGravarListaMobile"')
        self.assertContains(resposta, 'id="btnGravarGerarCompraMobile"')
        self.assertContains(resposta, "Gravar lista")
        self.assertContains(resposta, "Gravar e Gerar Compra")
        self.assertIn('novoClique(false, btnSalvarRascunhoListaMobile, { rascunho: true });', html)

    def test_mobile_rascunho_lista_tem_bloqueio_modal_e_ajax(self):
        resposta = self.client.get(reverse("estoque:sugestao_compra_fornecedor"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Rascunho salvo e protegido.")
        self.assertContains(resposta, 'id="btnContinuarEditandoRascunhoLista"')
        self.assertContains(resposta, 'id="modalContinuarEditandoRascunhoLista"')
        self.assertContains(resposta, "Continuar editando?")
        self.assertContains(resposta, "Manter protegida")
        self.assertContains(resposta, "Sim, continuar editando")
        self.assertContains(resposta, "function aplicarRascunhoProtegidoLista(bloqueado)")
        self.assertContains(resposta, "function abrirModalContinuarEditandoRascunhoLista()")
        self.assertContains(resposta, "function liberarEdicaoRascunhoProtegidoLista()")
        self.assertContains(resposta, "modalContinuarEditandoRascunhoLista.classList.contains(\"aberto\")")
        self.assertContains(resposta, "setControleRascunhoProtegidoLista(controle, rascunhoProtegidoLista);")
        self.assertContains(resposta, "#fornecedorBuscaSugestao")
        self.assertContains(resposta, "#periodo")
        self.assertContains(resposta, "#produtoManualBuscaSugestao")
        self.assertContains(resposta, "#mobileSugestaoProdutos input")
        self.assertContains(resposta, "#itensListaMobile button")
        self.assertContains(resposta, "fetch(formGravarListaFornecedor.action")
        self.assertContains(resposta, "listaFornecedorGerarCompra.value = \"0\";")
        self.assertContains(resposta, "history.replaceState")
        self.assertContains(resposta, "listaFornecedorRascunhoProtegido:")

    def test_salvar_rascunho_lista_nova_nao_gera_compra_estoque_ou_financeiro(self):
        produto = self.criar_produto("Produto Rascunho Lista", quantidade=Decimal("7.000"))
        estoque_antes = produto.quantidade
        compras_antes = Compra.objects.count()
        contas_antes = ContaPagar.objects.count()
        movimentos_antes = MovimentoFinanceiro.objects.count()

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {"lista_payload": json.dumps(self.payload([self.criar_linha(produto, 2)]))},
            secure=True,
        )

        produto.refresh_from_db()
        self.assertRedirects(resposta, reverse("estoque:compras_lista_fornecedor_detalhe", kwargs={"pk": ListaCompraFornecedor.objects.get().pk}), fetch_redirect_response=False)
        self.assertEqual(ListaCompraFornecedor.objects.count(), 1)
        self.assertEqual(ItemListaCompraFornecedor.objects.count(), 1)
        self.assertEqual(produto.quantidade, estoque_antes)
        self.assertEqual(Compra.objects.count(), compras_antes)
        self.assertEqual(ContaPagar.objects.count(), contas_antes)
        self.assertEqual(MovimentoFinanceiro.objects.count(), movimentos_antes)

    def test_salvar_rascunho_edicao_atualiza_mesma_lista_sem_compra(self):
        produto = self.criar_produto("Produto Rascunho Edicao", quantidade=Decimal("8.000"))
        lista = self.criar_lista_com_item(produto)
        estoque_antes = produto.quantidade
        compras_antes = Compra.objects.count()

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_editar", kwargs={"pk": lista.pk}),
            {"lista_payload": json.dumps(self.payload([self.criar_linha(produto, 5, total=Decimal("50.00"))]))},
            secure=True,
        )

        lista.refresh_from_db()
        produto.refresh_from_db()
        self.assertRedirects(resposta, reverse("estoque:compras_lista_fornecedor_ver", kwargs={"pk": lista.pk}), fetch_redirect_response=False)
        self.assertEqual(ListaCompraFornecedor.objects.count(), 1)
        self.assertEqual(lista.itens.get().quantidade_final, Decimal("5.000"))
        self.assertEqual(lista.total_lista, Decimal("50.00"))
        self.assertEqual(produto.quantidade, estoque_antes)
        self.assertEqual(Compra.objects.count(), compras_antes)

    def test_salvamento_lista_tem_trava_contra_clique_repetido(self):
        resposta = self.client.get(reverse("estoque:sugestao_compra_fornecedor"), secure=True)
        html = resposta.content.decode()

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("let salvamentoListaEmAndamento = false;", html)
        self.assertIn("if (salvamentoListaEmAndamento) return;", html)
        self.assertIn("function iniciarSalvamentoLista(botaoAcionado)", html)
        self.assertIn("salvamentoListaEmAndamento = true;", html)
        self.assertIn("bloquearBotoesSalvamentoLista(botaoAcionado);", html)
        self.assertIn("function bloquearBotoesSalvamentoLista(botaoAcionado)", html)
        self.assertIn("botao.disabled = true;", html)
        self.assertIn('botaoAcionado.textContent = "Salvando...";', html)
        self.assertIn('"#btnGravarListaFornecedor"', html)
        self.assertIn('"#btnGravarGerarCompraFornecedor"', html)
        self.assertIn('"#btnGravarListaMobile"', html)
        self.assertIn('"#btnSalvarAlteracoesListaMobile"', html)
        self.assertIn('"#btnGravarGerarCompraMobile"', html)
        self.assertIn('"#btnSalvarRascunhoListaMobile"', html)
        self.assertIn('"#btnConfirmarGravar"', html)
        self.assertIn("if (!iniciarSalvamentoLista(btnConfirmarGravar)) return;", html)
        self.assertIn("if (!iniciarSalvamentoLista(botaoAcionado)) return;", html)
        self.assertIn('modalConfirmacao.classList.contains("aberto")', html)

    def test_mobile_itens_lista_tem_edicao_confirmada_contador_e_validacoes(self):
        resposta = self.client.get(reverse("estoque:sugestao_compra_fornecedor"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "readonly")
        self.assertContains(resposta, "Salvar edição")
        self.assertContains(resposta, "Cancelar edição")
        self.assertContains(resposta, "itemEmEdicao")
        self.assertContains(resposta, "abrirEdicao")
        self.assertContains(resposta, "salvarEdicao")
        self.assertContains(resposta, "cancelarEdicao")
        self.assertContains(resposta, "numeroValidoObrigatorio")
        self.assertContains(resposta, "Informe uma quantidade maior que zero.")
        self.assertContains(resposta, "Informe um preço de compra válido.")
        self.assertContains(resposta, "Salve ou cancele a edição atual antes de editar outro item.")
        self.assertContains(resposta, "Salve ou cancele a edição do item antes de gravar a lista.")
        self.assertContains(resposta, "window.sugestaoMobileItemEmEdicaoAberta")
        self.assertContains(resposta, "itemMobileEmEdicaoAberta")
        self.assertContains(resposta, "orientarSalvarOuCancelarEdicao")
        self.assertContains(resposta, 'event.target === qtd')
        self.assertContains(resposta, 'event.target === preco')
        self.assertContains(resposta, "selecionarConteudoCampo")
        self.assertContains(resposta, "campo.setSelectionRange(0, String(campo.value || \"\").length)")
        self.assertContains(resposta, "focoPrecoPendentePorEnter")
        self.assertContains(resposta, "focoPrecoPendentePorEnter = { item, campo: preco };")
        self.assertContains(resposta, "preco?.focus({ preventScroll: true });")
        self.assertContains(resposta, "selecionarConteudoCampo(preco)")
        self.assertNotContains(resposta, "window.setTimeout(selecionar, 80)")
        self.assertContains(resposta, "precoValidado.valor < 0")
        self.assertContains(resposta, "qtdValidada.valor <= 0")
        self.assertContains(resposta, "campo.readOnly = adicionado")
        self.assertContains(resposta, "contador.dataset.count = String(total);")
        self.assertContains(resposta, 'contador.querySelector("[data-itens-lista-mobile-total]")')
        self.assertContains(resposta, "totalEl.textContent = String(total);")

    def test_mobile_remover_item_da_lista_usa_modal_de_confirmacao(self):
        resposta = self.client.get(reverse("estoque:sugestao_compra_fornecedor"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'id="modalRemoverItemListaMobile"')
        self.assertContains(resposta, "Remover item da lista?")
        self.assertContains(resposta, "O produto")
        self.assertContains(resposta, "será removido desta lista.")
        self.assertContains(resposta, 'id="btnCancelarRemoverItemListaMobile"')
        self.assertContains(resposta, 'id="btnConfirmarRemoverItemListaMobile"')
        self.assertContains(resposta, "abrirModalRemoverItem")
        self.assertContains(resposta, "cancelarRemocaoItem")
        self.assertContains(resposta, "confirmarRemocaoItem")
        self.assertContains(resposta, "itemPendenteRemocao")
        self.assertContains(resposta, "remocaoEmAndamento")
        self.assertContains(
            resposta,
            "if (itemPendenteRemocao || cardPendenteRemocao) return;",
        )
        self.assertContains(
            resposta,
            "window.abrirModalRemoverCardSugestao = function(card, botaoOrigem)",
        )
        self.assertContains(
            resposta,
            'botaoRemover.dataset.remocaoConfirmada !== "1"',
        )
        self.assertContains(
            resposta,
            'botaoCard.dataset.remocaoConfirmada = "1";',
        )
        self.assertContains(resposta, "removerItemDaLista(item);")
        self.assertContains(resposta, "if (itemEmEdicao === item) itemEmEdicao = null;")
        self.assertContains(resposta, "foco?.focus();")
        self.assertContains(resposta, "Escape")
        self.assertNotContains(resposta, 'event.target === modalRemover')

    def test_edicao_mobile_carrega_itens_salvos_em_itens_da_lista(self):
        produto = self.criar_produto("Produto Edicao Mobile Lista")
        lista = self.criar_lista_com_item(produto)

        resposta = self.client.get(
            reverse("estoque:compras_lista_fornecedor_editar", kwargs={"pk": lista.pk}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "const modoEdicaoLista = true;")
        self.assertContains(resposta, "cards().forEach(adicionarCardNaLista);")
        self.assertContains(resposta, 'id="itensListaMobile"')
        self.assertContains(resposta, "atualizarContadorItensLista")

    def test_gravar_lista_com_um_item_payload_salva_somente_esse_item(self):
        produto_escolhido = self.criar_produto("Produto Escolhido")
        produto_nao_escolhido = self.criar_produto("Produto Nao Escolhido")

        self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {"lista_payload": json.dumps(self.payload([self.criar_linha(produto_escolhido, 2)]))},
            secure=True,
        )

        lista = ListaCompraFornecedor.objects.get()
        self.assertEqual(lista.itens.count(), 1)
        self.assertEqual(lista.itens.get().produto, produto_escolhido)
        self.assertFalse(lista.itens.filter(produto=produto_nao_escolhido).exists())

    def test_gravar_lista_com_varios_itens_payload_salva_somente_escolhidos(self):
        produto_um = self.criar_produto("Produto Escolhido Um")
        produto_dois = self.criar_produto("Produto Escolhido Dois")
        produto_fora = self.criar_produto("Produto Fora da Lista")

        self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {"lista_payload": json.dumps(self.payload([
                self.criar_linha(produto_um, 1),
                self.criar_linha(produto_dois, 3),
            ]))},
            secure=True,
        )

        lista = ListaCompraFornecedor.objects.get()
        produtos_ids = set(lista.itens.values_list("produto_id", flat=True))
        self.assertEqual(lista.itens.count(), 2)
        self.assertEqual(produtos_ids, {produto_um.id, produto_dois.id})
        self.assertNotIn(produto_fora.id, produtos_ids)

    def test_desktop_preserva_gravacao_de_todos_os_itens_do_payload(self):
        produto_um = self.criar_produto("Produto Desktop Um")
        produto_dois = self.criar_produto("Produto Desktop Dois")

        self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {"lista_payload": json.dumps(self.payload([
                self.criar_linha(produto_um, 2),
                self.criar_linha(produto_dois, 4),
            ]))},
            secure=True,
        )

        lista = ListaCompraFornecedor.objects.get()
        self.assertEqual(lista.itens.count(), 2)
        self.assertEqual(
            set(lista.itens.values_list("produto_id", flat=True)),
            {produto_um.id, produto_dois.id},
        )

    def test_gravar_e_gerar_compra_cria_compra_rascunho_sem_finalizar(self):
        produto = self.criar_produto("Produto Gerar Compra", quantidade=Decimal("5.000"))
        estoque_antes = produto.quantidade
        contas_antes = ContaPagar.objects.count()
        movimentos_antes = MovimentoFinanceiro.objects.count()

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {
                "lista_payload": json.dumps(self.payload([self.criar_linha(produto, 3)])),
                "gerar_compra": "1",
            },
            secure=True,
        )

        lista = ListaCompraFornecedor.objects.get()
        compra = Compra.objects.get()
        produto.refresh_from_db()
        self.assertRedirects(
            resposta,
            f"{reverse('estoque:compra_editar', kwargs={'pk': compra.pk})}?continuar_itens=1",
            fetch_redirect_response=False,
        )
        self.assertEqual(lista.itens.count(), 1)
        self.assertEqual(compra.status, Compra.STATUS_RASCUNHO)
        self.assertEqual(compra.tipo_pagamento, "")
        self.assertEqual(compra.total, Decimal("30.00"))
        self.assertIn(f"Lista de Compras #{lista.id}", compra.observacao)
        self.assertEqual(compra.itens.count(), 1)
        self.assertEqual(compra.itens.get().produto, produto)
        self.assertEqual(produto.quantidade, estoque_antes)
        self.assertEqual(ContaPagar.objects.count(), contas_antes)
        self.assertEqual(MovimentoFinanceiro.objects.count(), movimentos_antes)

    def test_ver_lista_sem_compra_oferece_gerar_compra(self):
        produto = self.criar_produto("Produto Ver Gerar")
        lista = self.criar_lista_com_item(produto)

        resposta = self.client.get(reverse("estoque:compras_lista_fornecedor_ver", kwargs={"pk": lista.pk}), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Gerar Compra")
        self.assertContains(resposta, "Cancelar Lista")
        self.assertNotContains(resposta, "Excluir Lista")
        self.assertContains(resposta, 'data-gerar-compra-lista-form="1"')

    def test_ver_lista_com_compra_rascunho_oferece_continuar_sem_duplicar(self):
        produto = self.criar_produto("Produto Ver Continuar")
        lista = self.criar_lista_com_item(produto)
        compra = self.criar_compra_vinculada(lista)

        resposta = self.client.get(reverse("estoque:compras_lista_fornecedor_ver", kwargs={"pk": lista.pk}), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Continuar Compra")
        self.assertContains(resposta, reverse("estoque:compra_editar", kwargs={"pk": compra.pk}))
        self.assertNotContains(resposta, 'data-gerar-compra-lista-form="1"')
        self.assertNotContains(resposta, "Cancelar Lista")

    def test_consulta_listas_exibe_cancelar_para_lista_sem_compra(self):
        produto = self.criar_produto("Produto Cancelar Lista")
        lista = self.criar_lista_com_item(produto)

        resposta = self.client.get(reverse("estoque:compras_listas_fornecedor"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Cancelar Lista")
        self.assertNotContains(resposta, "Excluir Lista")
        self.assertContains(resposta, reverse("estoque:compras_lista_fornecedor_cancelar", kwargs={"pk": lista.pk}))
        self.assertContains(resposta, "Tem certeza que deseja cancelar esta lista?")
        self.assertContains(resposta, "Listas canceladas")

    def test_consulta_principal_mostra_aberta_e_esconde_cancelada(self):
        produto_aberto = self.criar_produto("Produto Lista Aberta")
        produto_cancelado = self.criar_produto("Produto Lista Cancelada")
        lista_aberta = self.criar_lista_com_item(produto_aberto)
        lista_cancelada = self.criar_lista_com_item(produto_cancelado)
        lista_cancelada.status = ListaCompraFornecedor.STATUS_CANCELADA
        lista_cancelada.save(update_fields=["status", "atualizado_em"])

        resposta = self.client.get(reverse("estoque:compras_listas_fornecedor"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        listas_ids = {lista.id for lista in resposta.context["listas"]}
        listas_mobile_ids = {lista.id for lista in resposta.context["listas_mobile"]}
        self.assertIn(lista_aberta.id, listas_ids)
        self.assertIn(lista_aberta.id, listas_mobile_ids)
        self.assertNotIn(lista_cancelada.id, listas_ids)
        self.assertNotIn(lista_cancelada.id, listas_mobile_ids)
        self.assertContains(resposta, "Listas canceladas")

    def test_consulta_canceladas_mostra_cancelada_sem_misturar_ativas(self):
        produto_aberto = self.criar_produto("Produto Historico Aberta")
        produto_cancelado = self.criar_produto("Produto Historico Cancelada")
        lista_aberta = self.criar_lista_com_item(produto_aberto)
        lista_cancelada = self.criar_lista_com_item(produto_cancelado)
        lista_cancelada.status = ListaCompraFornecedor.STATUS_CANCELADA
        lista_cancelada.save(update_fields=["status", "atualizado_em"])

        resposta = self.client.get(
            reverse("estoque:compras_listas_fornecedor"),
            {"status": ListaCompraFornecedor.STATUS_CANCELADA},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        listas_ids = {lista.id for lista in resposta.context["listas"]}
        self.assertIn(lista_cancelada.id, listas_ids)
        self.assertNotIn(lista_aberta.id, listas_ids)
        self.assertContains(resposta, "Listas ativas")

    def test_mobile_principal_mostra_lista_aberta_sem_compra(self):
        produto = self.criar_produto("Produto Mobile Pendente")
        lista = self.criar_lista_com_item(produto)

        resposta = self.client.get(reverse("estoque:compras_listas_fornecedor"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'data-mobile-modo="pendentes"')
        self.assertContains(resposta, f'data-mobile-lista-id="{lista.id}"')
        self.assertContains(resposta, "Historico")

    def test_mobile_principal_nao_mostra_lista_que_virou_compra(self):
        produto = self.criar_produto("Produto Mobile Compra")
        lista = self.criar_lista_com_item(produto)
        compra = self.criar_compra_vinculada(lista, status=Compra.STATUS_RASCUNHO)

        resposta = self.client.get(reverse("estoque:compras_listas_fornecedor"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, f"Lista #{lista.id} virou Compra #{compra.id}")
        self.assertNotContains(resposta, f'data-mobile-lista-id="{lista.id}"')
        self.assertNotContains(resposta, 'data-gerar-compra-lista-form="1"')

    def test_mobile_historico_mostra_lista_que_virou_compra(self):
        produto = self.criar_produto("Produto Mobile Historico")
        lista = self.criar_lista_com_item(produto)
        self.criar_compra_vinculada(lista, status=Compra.STATUS_FINALIZADA)

        resposta = self.client.get(
            reverse("estoque:compras_listas_fornecedor"),
            {"mobile": "historico"},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'data-mobile-modo="historico"')
        self.assertContains(resposta, f'data-mobile-lista-id="{lista.id}"')
        self.assertContains(resposta, "Listas pendentes")

    def test_mobile_principal_nao_mostra_lista_cancelada(self):
        produto = self.criar_produto("Produto Mobile Cancelada")
        lista = self.criar_lista_com_item(produto)
        lista.status = ListaCompraFornecedor.STATUS_CANCELADA
        lista.save(update_fields=["status", "atualizado_em"])

        resposta = self.client.get(reverse("estoque:compras_listas_fornecedor"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertNotContains(resposta, f'data-mobile-lista-id="{lista.id}"')

    def test_mobile_historico_mostra_lista_cancelada(self):
        produto = self.criar_produto("Produto Historico Cancelada Mobile")
        lista = self.criar_lista_com_item(produto)
        lista.status = ListaCompraFornecedor.STATUS_CANCELADA
        lista.save(update_fields=["status", "atualizado_em"])

        resposta = self.client.get(
            reverse("estoque:compras_listas_fornecedor"),
            {"mobile": "historico"},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, f'data-mobile-lista-id="{lista.id}"')

    def test_cancelar_lista_sem_compra_marca_cancelada_sem_efeitos_financeiros(self):
        produto = self.criar_produto("Produto Cancelar Sem Compra", quantidade=Decimal("8.000"))
        lista = self.criar_lista_com_item(produto)
        estoque_antes = produto.quantidade
        compras_antes = Compra.objects.count()
        contas_antes = ContaPagar.objects.count()
        movimentos_antes = MovimentoFinanceiro.objects.count()

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_cancelar", kwargs={"pk": lista.pk}),
            secure=True,
        )

        lista.refresh_from_db()
        produto.refresh_from_db()
        self.assertRedirects(resposta, reverse("estoque:compras_listas_fornecedor"), fetch_redirect_response=False)
        self.assertEqual(lista.status, ListaCompraFornecedor.STATUS_CANCELADA)
        self.assertEqual(lista.itens.count(), 1)
        self.assertEqual(produto.quantidade, estoque_antes)
        self.assertEqual(Compra.objects.count(), compras_antes)
        self.assertEqual(ContaPagar.objects.count(), contas_antes)
        self.assertEqual(MovimentoFinanceiro.objects.count(), movimentos_antes)

        resposta_principal = self.client.get(reverse("estoque:compras_listas_fornecedor"), secure=True)
        resposta_canceladas = self.client.get(
            reverse("estoque:compras_listas_fornecedor"),
            {"status": ListaCompraFornecedor.STATUS_CANCELADA},
            secure=True,
        )
        listas_principal_ids = {lista_contexto.id for lista_contexto in resposta_principal.context["listas"]}
        listas_mobile_principal_ids = {lista_contexto.id for lista_contexto in resposta_principal.context["listas_mobile"]}
        listas_canceladas_ids = {lista_contexto.id for lista_contexto in resposta_canceladas.context["listas"]}
        self.assertNotIn(lista.id, listas_principal_ids)
        self.assertNotIn(lista.id, listas_mobile_principal_ids)
        self.assertIn(lista.id, listas_canceladas_ids)

    def test_cancelar_lista_com_compra_rascunho_bloqueia(self):
        produto = self.criar_produto("Produto Bloqueio Rascunho")
        lista = self.criar_lista_com_item(produto)
        self.criar_compra_vinculada(lista, status=Compra.STATUS_RASCUNHO)

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_cancelar", kwargs={"pk": lista.pk}),
            secure=True,
            follow=True,
        )

        lista.refresh_from_db()
        self.assert_mensagem_resposta(resposta, "nao pode ser cancelada")
        self.assertNotEqual(lista.status, ListaCompraFornecedor.STATUS_CANCELADA)
        self.assertEqual(Compra.objects.count(), 1)

    def test_cancelar_lista_com_compra_finalizada_bloqueia(self):
        produto = self.criar_produto("Produto Bloqueio Finalizada")
        lista = self.criar_lista_com_item(produto)
        self.criar_compra_vinculada(lista, status=Compra.STATUS_FINALIZADA)

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_cancelar", kwargs={"pk": lista.pk}),
            secure=True,
            follow=True,
        )

        lista.refresh_from_db()
        self.assert_mensagem_resposta(resposta, "nao pode ser cancelada")
        self.assertNotEqual(lista.status, ListaCompraFornecedor.STATUS_CANCELADA)
        self.assertEqual(Compra.objects.count(), 1)

    def test_cancelar_lista_com_compra_cancelada_vinculada_bloqueia(self):
        produto = self.criar_produto("Produto Bloqueio Cancelada")
        lista = self.criar_lista_com_item(produto)
        self.criar_compra_vinculada(lista, status=Compra.STATUS_CANCELADA, cancelada=True)

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_cancelar", kwargs={"pk": lista.pk}),
            secure=True,
            follow=True,
        )

        lista.refresh_from_db()
        self.assert_mensagem_resposta(resposta, "nao pode ser cancelada")
        self.assertNotEqual(lista.status, ListaCompraFornecedor.STATUS_CANCELADA)
        self.assertEqual(Compra.objects.count(), 1)

    def test_consulta_listas_nao_exibe_cancelar_para_lista_com_compra_gerada(self):
        produto = self.criar_produto("Produto Sem Cancelar Com Compra")
        lista = self.criar_lista_com_item(produto)
        self.criar_compra_vinculada(lista, status=Compra.STATUS_RASCUNHO)

        resposta = self.client.get(reverse("estoque:compras_listas_fornecedor"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Lista #")
        self.assertNotContains(resposta, "Cancelar Lista")

    def test_lista_cancelada_nao_oferece_gerar_compra(self):
        produto = self.criar_produto("Produto Cancelado Sem Gerar")
        lista = self.criar_lista_com_item(produto)
        lista.status = ListaCompraFornecedor.STATUS_CANCELADA
        lista.save(update_fields=["status", "atualizado_em"])

        resposta = self.client.get(reverse("estoque:compras_lista_fornecedor_ver", kwargs={"pk": lista.pk}), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertNotContains(resposta, "Gerar Compra")
        self.assertNotContains(resposta, "Cancelar Lista")

    def test_item_removido_nao_e_gravado_no_post(self):
        produto_valido = self.criar_produto("Produto Valido")
        produto_removido = self.criar_produto("Produto Removido")

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {"lista_payload": json.dumps(self.payload([
                self.criar_linha(produto_valido, 2),
                self.criar_linha_removida(produto_removido, 3),
            ]))},
            secure=True,
            follow=True,
        )

        self.assertContains(resposta, "gravada com sucesso.")
        self.assertEqual(ListaCompraFornecedor.objects.count(), 1)
        lista = ListaCompraFornecedor.objects.get()
        self.assertEqual(lista.itens.count(), 1)
        self.assertEqual(lista.itens.get().produto, produto_valido)
        self.assertFalse(lista.itens.filter(produto=produto_removido).exists())

    def test_item_removido_com_quantidade_maior_que_zero_nao_e_gravado(self):
        produto_ativo = self.criar_produto("Produto Ativo")
        produto_removido = self.criar_produto("Produto Removido")

        self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {"lista_payload": json.dumps(self.payload([
                self.criar_linha(produto_ativo, 2),
                self.criar_linha_removida(produto_removido, 5),
            ]))},
            secure=True,
        )

        lista = ListaCompraFornecedor.objects.get()
        self.assertEqual(lista.itens.count(), 1)
        self.assertEqual(lista.itens.get().produto, produto_ativo)
        self.assertFalse(lista.itens.filter(produto=produto_removido).exists())

    def test_item_ativo_false_nao_e_gravado(self):
        produto_ativo = self.criar_produto("Produto Ativo")
        produto_inativo = self.criar_produto("Produto Inativo")

        self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {"lista_payload": json.dumps(self.payload([
                self.criar_linha(produto_ativo, 2),
                self.criar_linha_ativo_false(produto_inativo, 5),
            ]))},
            secure=True,
        )

        lista = ListaCompraFornecedor.objects.get()
        self.assertEqual(lista.itens.count(), 1)
        self.assertEqual(lista.itens.get().produto, produto_ativo)
        self.assertFalse(lista.itens.filter(produto=produto_inativo).exists())

    def test_itens_removidos_e_zerados_nao_criam_lista(self):
        produto_removido = self.criar_produto("Produto Removido")
        produto_zerado = self.criar_produto("Produto Zerado")

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {"lista_payload": json.dumps(self.payload([
                self.criar_linha_removida(produto_removido, 5),
                self.criar_linha(produto_zerado, 0),
            ]))},
            secure=True,
            follow=True,
        )

        self.assertContains(resposta, "Nenhum item com quantidade maior que zero para gravar. Ajuste as quantidades antes de gravar a lista.")
        self.assertEqual(ListaCompraFornecedor.objects.count(), 0)
        self.assertEqual(ItemListaCompraFornecedor.objects.count(), 0)

    def test_item_com_quantidade_final_maior_que_zero_e_gravado_normalmente(self):
        produto = self.criar_produto("Produto Positivo")
        estoque_antes = produto.quantidade
        compras_antes = Compra.objects.count()
        contas_antes = ContaPagar.objects.count()
        movimentos_antes = MovimentoFinanceiro.objects.count()

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {"lista_payload": json.dumps(self.payload([self.criar_linha(produto, 3)]))},
            secure=True,
            follow=True,
        )

        self.assertContains(resposta, "gravada com sucesso.")
        self.assertEqual(ListaCompraFornecedor.objects.count(), 1)
        self.assertEqual(ItemListaCompraFornecedor.objects.count(), 1)
        item = ItemListaCompraFornecedor.objects.get()
        self.assertEqual(item.produto, produto)
        self.assertEqual(item.quantidade_final, Decimal("3.000"))
        produto.refresh_from_db()
        self.assertEqual(produto.quantidade, estoque_antes)
        self.assertEqual(Compra.objects.count(), compras_antes)
        self.assertEqual(ContaPagar.objects.count(), contas_antes)
        self.assertEqual(MovimentoFinanceiro.objects.count(), movimentos_antes)

    def test_lista_com_itens_zerados_cria_apenas_itens_validos(self):
        produto_positivo = self.criar_produto("Produto Positivo")
        produto_zerado = self.criar_produto("Produto Zerado")

        self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {"lista_payload": json.dumps(self.payload([
                self.criar_linha(produto_positivo, 1),
                self.criar_linha(produto_zerado, 0),
            ]))},
            secure=True,
        )

        lista = ListaCompraFornecedor.objects.get()
        self.assertEqual(lista.itens.count(), 1)
        self.assertEqual(lista.itens.get().produto, produto_positivo)
        self.assertFalse(lista.itens.filter(produto=produto_zerado).exists())

    def test_lista_com_item_zerado_e_total_preenchido_cria_apenas_itens_validos(self):
        produto_positivo = self.criar_produto("Produto Positivo")
        produto_zerado = self.criar_produto("Produto Zerado")

        self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {"lista_payload": json.dumps(self.payload([
                self.criar_linha(produto_positivo, 2),
                self.criar_linha(produto_zerado, 0, total=Decimal("20.00")),
            ]))},
            secure=True,
        )

        lista = ListaCompraFornecedor.objects.get()
        self.assertEqual(lista.itens.count(), 1)
        self.assertEqual(lista.itens.get().produto, produto_positivo)
        self.assertFalse(lista.itens.filter(produto=produto_zerado).exists())

    def test_lista_com_todos_itens_zerados_nao_e_criada(self):
        produto_zerado = self.criar_produto("Produto Zerado")

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_gravar"),
            {"lista_payload": json.dumps(self.payload([self.criar_linha(produto_zerado, 0)]))},
            secure=True,
            follow=True,
        )

        self.assertContains(resposta, "Nenhum item com quantidade maior que zero para gravar. Ajuste as quantidades antes de gravar a lista.")
        self.assertEqual(ListaCompraFornecedor.objects.count(), 0)
        self.assertEqual(ItemListaCompraFornecedor.objects.count(), 0)
        self.assertEqual(Compra.objects.count(), 0)

    def test_edicao_lista_exibe_botao_salvar_alteracoes(self):
        produto = self.criar_produto("Produto Edicao Botao")
        lista = ListaCompraFornecedor.objects.create(
            fornecedor=self.fornecedor,
            data_lista=timezone.localdate(),
            data_inicio_periodo=timezone.localdate(),
            data_fim_periodo=timezone.localdate(),
            total_lista=Decimal("20.00"),
        )
        ItemListaCompraFornecedor.objects.create(
            lista=lista,
            produto=produto,
            estoque_atual=Decimal("5.000"),
            estoque_minimo=Decimal("1.000"),
            quantidade_final=Decimal("2.000"),
            unidade="UN",
            preco_compra=Decimal("10.00"),
            preco_unitario=Decimal("10.00"),
            total=Decimal("20.00"),
        )

        resposta = self.client.get(
            reverse("estoque:compras_lista_fornecedor_editar", kwargs={"pk": lista.pk}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Salvar alterações")
        self.assertContains(resposta, "Salvar alterações e Gerar Compra")
        self.assertContains(resposta, 'id="btnGravarGerarCompraFornecedor"')
        self.assertContains(resposta, 'id="btnGravarGerarCompraMobile"')
        self.assertContains(resposta, 'id="btnSalvarAlteracoesListaMobile"')
        self.assertContains(resposta, "As alteracoes dos campos so sao gravadas depois de salvar.")

    def test_edicao_lista_salva_itens_sem_gerar_compra_estoque_ou_financeiro(self):
        produto = self.criar_produto("Produto Edicao Lista", quantidade=Decimal("5.000"))
        lista = ListaCompraFornecedor.objects.create(
            fornecedor=self.fornecedor,
            data_lista=timezone.localdate(),
            data_inicio_periodo=timezone.localdate(),
            data_fim_periodo=timezone.localdate(),
            total_lista=Decimal("20.00"),
        )
        ItemListaCompraFornecedor.objects.create(
            lista=lista,
            produto=produto,
            estoque_atual=produto.quantidade,
            estoque_minimo=Decimal("1.000"),
            quantidade_final=Decimal("2.000"),
            unidade="UN",
            preco_compra=Decimal("10.00"),
            preco_unitario=Decimal("10.00"),
            total=Decimal("20.00"),
        )
        estoque_antes = produto.quantidade
        compras_antes = Compra.objects.count()
        contas_antes = ContaPagar.objects.count()
        movimentos_antes = MovimentoFinanceiro.objects.count()

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_editar", kwargs={"pk": lista.pk}),
            {"lista_payload": json.dumps(self.payload([self.criar_linha(produto, 3, total=Decimal("36.00"))]))},
            secure=True,
        )

        lista.refresh_from_db()
        produto.refresh_from_db()
        item = lista.itens.get()
        self.assertRedirects(resposta, reverse("estoque:compras_lista_fornecedor_ver", kwargs={"pk": lista.pk}), fetch_redirect_response=False)
        self.assertEqual(item.produto, produto)
        self.assertEqual(item.quantidade_final, Decimal("3.000"))
        self.assertEqual(item.preco_compra, Decimal("10.00"))
        self.assertEqual(item.preco_unitario, Decimal("10.00"))
        self.assertEqual(item.total, Decimal("36.00"))
        self.assertEqual(lista.total_lista, Decimal("36.00"))
        self.assertEqual(produto.quantidade, estoque_antes)
        self.assertEqual(Compra.objects.count(), compras_antes)
        self.assertEqual(ContaPagar.objects.count(), contas_antes)
        self.assertEqual(MovimentoFinanceiro.objects.count(), movimentos_antes)

    def test_edicao_lista_salva_alteracoes_e_gera_compra_rascunho(self):
        produto = self.criar_produto("Produto Edicao Gera Compra", quantidade=Decimal("5.000"))
        lista = self.criar_lista_com_item(produto)
        estoque_antes = produto.quantidade
        contas_antes = ContaPagar.objects.count()
        movimentos_antes = MovimentoFinanceiro.objects.count()

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_editar", kwargs={"pk": lista.pk}),
            {
                "lista_payload": json.dumps(self.payload([self.criar_linha(produto, 3, total=Decimal("36.00"))])),
                "gerar_compra": "1",
            },
            secure=True,
        )

        lista.refresh_from_db()
        produto.refresh_from_db()
        compra = Compra.objects.get()
        item_lista = lista.itens.get()
        item_compra = compra.itens.get()
        self.assertRedirects(
            resposta,
            f"{reverse('estoque:compra_editar', kwargs={'pk': compra.pk})}?continuar_itens=1",
            fetch_redirect_response=False,
        )
        self.assertEqual(item_lista.quantidade_final, Decimal("3.000"))
        self.assertEqual(item_lista.total, Decimal("36.00"))
        self.assertEqual(compra.status, Compra.STATUS_RASCUNHO)
        self.assertEqual(compra.tipo_pagamento, "")
        self.assertEqual(item_compra.produto, produto)
        self.assertEqual(item_compra.quantidade, Decimal("3.000"))
        self.assertEqual(item_compra.valor_total, Decimal("36.00"))
        self.assertEqual(produto.quantidade, estoque_antes)
        self.assertEqual(ContaPagar.objects.count(), contas_antes)
        self.assertEqual(MovimentoFinanceiro.objects.count(), movimentos_antes)

    def test_edicao_lista_com_compra_existente_nao_gera_duplicada(self):
        produto = self.criar_produto("Produto Edicao Sem Duplicar", quantidade=Decimal("5.000"))
        lista = self.criar_lista_com_item(produto)
        compra = Compra.objects.create(
            fornecedor=self.fornecedor,
            data_compra=timezone.localdate(),
            tipo_pagamento="",
            total=Decimal("20.00"),
            status=Compra.STATUS_RASCUNHO,
            observacao=f"Gerada a partir da Lista de Compras #{lista.id}",
        )

        resposta = self.client.post(
            reverse("estoque:compras_lista_fornecedor_editar", kwargs={"pk": lista.pk}),
            {
                "lista_payload": json.dumps(self.payload([self.criar_linha(produto, 4, total=Decimal("40.00"))])),
                "gerar_compra": "1",
            },
            secure=True,
        )

        lista.refresh_from_db()
        self.assertRedirects(
            resposta,
            f"{reverse('estoque:compra_editar', kwargs={'pk': compra.pk})}?continuar_itens=1",
            fetch_redirect_response=False,
        )
        self.assertEqual(Compra.objects.count(), 1)
        self.assertEqual(Compra.objects.get(), compra)
        self.assertEqual(lista.itens.get().quantidade_final, Decimal("4.000"))


class ComprasSugestaoFornecedorGeracaoTests(TestCase):
    def setUp(self):
        self.fornecedor = Fornecedor.objects.create(nome="Fornecedor Sugestao")
        self.url = reverse("estoque:sugestao_compra_fornecedor")

    def criar_produto(self, nome, quantidade, estoque_minimo):
        produto = Produto.objects.create(
            nome=nome,
            preco_compra=Decimal("10.00"),
            preco_vista=Decimal("15.00"),
            preco_prazo=Decimal("16.00"),
            quantidade=Decimal(str(quantidade)),
            estoque_minimo=Decimal(str(estoque_minimo)),
        )
        ProdutoFornecedor.objects.create(
            fornecedor=self.fornecedor,
            produto=produto,
            ativo=True,
        )
        return produto

    def produtos_da_lista_inicial(self, resposta):
        return {linha["produto"] for linha in resposta.context["linhas"]}

    def test_produto_com_estoque_maior_ou_igual_ao_minimo_e_sugestao_zero_nao_aparece(self):
        produto_sem_necessidade = self.criar_produto("Cafe Estoque Ok", "14.000", "12.000")
        produto_com_necessidade = self.criar_produto("Cafe Comprar", "8.000", "12.000")

        resposta = self.client.get(
            self.url,
            {"fornecedor": str(self.fornecedor.id)},
            secure=True,
        )

        produtos = self.produtos_da_lista_inicial(resposta)
        self.assertIn(produto_com_necessidade, produtos)
        self.assertNotIn(produto_sem_necessidade, produtos)
        self.assertEqual(resposta.context["total_produtos_vinculados"], 2)
        self.assertEqual(resposta.context["total_produtos_sugeridos"], 1)

    def test_geracao_nao_traz_produtos_apenas_por_estarem_vinculados_ao_fornecedor(self):
        produto_sem_necessidade = self.criar_produto("Produto Apenas Vinculado", "17.000", "15.000")

        resposta = self.client.get(
            self.url,
            {"fornecedor": str(self.fornecedor.id)},
            secure=True,
        )

        self.assertNotIn(produto_sem_necessidade, self.produtos_da_lista_inicial(resposta))
        self.assertEqual(resposta.context["total_produtos_vinculados"], 1)
        self.assertEqual(resposta.context["total_produtos_sugeridos"], 0)


class CorrecaoItensCompraTests(TestCase):
    def setUp(self):
        self.fornecedor = Fornecedor.objects.create(nome="Fornecedor Correcao")
        self.produto_a = self.criar_produto("Produto A", "20.000")
        self.produto_b = self.criar_produto("Produto B", "12.000")
        self.produto_c = self.criar_produto("Produto C", "3.000")
        self.compra = Compra.objects.create(
            fornecedor=self.fornecedor,
            data_compra=timezone.localdate(),
            tipo_pagamento="avista",
            total=Decimal("110.00"),
            status=Compra.STATUS_FINALIZADA,
            estoque_entrada_realizada=True,
        )
        self.item_a = ItemCompra.objects.create(
            compra=self.compra, produto=self.produto_a, quantidade=Decimal("10.000"),
            unidade="UN", preco_unitario=Decimal("10.00"), valor_total=Decimal("100.00"),
        )
        self.item_b = ItemCompra.objects.create(
            compra=self.compra, produto=self.produto_b, quantidade=Decimal("2.000"),
            unidade="UN", preco_unitario=Decimal("5.00"), valor_total=Decimal("10.00"),
        )
        self.conta_pagar = ContaPagar.objects.create(
            compra=self.compra, fornecedor=self.fornecedor, data_emissao=timezone.localdate(),
            valor_original=Decimal("110.00"), valor_em_aberto=Decimal("110.00"),
        )
        self.movimento = MovimentoFinanceiro.objects.create(
            conta=views._conta_financeira_padrao("caixa"),
            tipo=MovimentoFinanceiro.TIPO_SAIDA,
            valor=Decimal("110.00"),
            data=timezone.localdate(),
            origem="compra_a_vista",
            compra=self.compra,
        )
        self.url = f"/estoque/compras/{self.compra.id}/corrigir-itens/"

    def criar_produto(self, nome, quantidade):
        return Produto.objects.create(
            nome=nome, preco_compra=Decimal("2.00"), preco_vista=Decimal("3.00"),
            preco_prazo=Decimal("4.00"), quantidade=Decimal(quantidade), unidade_compra="UN",
        )

    def dados(self, **alteracoes):
        dados = {
            "item_id[]": [str(self.item_a.id), str(self.item_b.id)],
            "quantidade[]": ["10", "2"],
            "preco_unitario[]": ["10,00", "5,00"],
            "novo_produto_id[]": [""],
            "nova_quantidade[]": [""],
            "novo_preco_unitario[]": [""],
        }
        dados.update(alteracoes)
        return dados

    def assert_financeiro_inalterado(self):
        self.conta_pagar.refresh_from_db()
        self.movimento.refresh_from_db()
        self.assertEqual(self.conta_pagar.valor_original, Decimal("110.00"))
        self.assertEqual(self.conta_pagar.valor_em_aberto, Decimal("110.00"))
        self.assertEqual(self.movimento.valor, Decimal("110.00"))
        self.assertEqual(self.compra.movimentos_financeiros.count(), 1)

    def test_compra_a_vista_total_alterado_redireciona_para_corrigir_origem(self):
        resposta = self.client.post(
            self.url,
            self.dados(**{"quantidade[]": ["7", "2"]}),
            follow=True,
            secure=True,
        )
        self.produto_a.refresh_from_db(); self.compra.refresh_from_db(); self.item_a.refresh_from_db()
        self.assertTrue(resposta.redirect_chain)
        self.assertEqual(
            urlsplit(resposta.redirect_chain[0][0]).path,
            reverse("estoque:compra_corrigir_origem_pagamento", kwargs={"pk": self.compra.id}),
        )
        self.assertContains(resposta, "Agora ajuste a origem do pagamento")
        self.assertEqual(self.produto_a.quantidade, Decimal("17.000"))
        self.assertEqual(self.item_a.quantidade, Decimal("7.000"))
        self.assertEqual(self.compra.total, Decimal("80.00"))
        self.assertIn("Total anterior R$ 110,00", self.compra.observacao)
        self.assertIn("Financeiro nao alterado", self.compra.observacao)
        detalhe = self.client.get(f"/estoque/compras/{self.compra.id}/", secure=True)
        self.assertContains(detalhe, "Histórico da compra")
        self.assertContains(detalhe, "Mostrar histórico da compra")
        conteudo = detalhe.content.decode()
        titulo_itens = '<div class="nota-compra-card-titulo">Itens</div>'
        titulo_financeiro = '<div class="nota-compra-card-titulo">Financeiro</div>'
        titulo_historico = '<div class="nota-compra-card-titulo">Histórico da compra</div>'
        self.assertLess(conteudo.index(titulo_itens), conteudo.index(titulo_financeiro))
        self.assertLess(conteudo.index(titulo_financeiro), conteudo.index(titulo_historico))
        self.assert_financeiro_inalterado()

    def test_compra_a_prazo_total_alterado_redireciona_para_corrigir_financeiro(self):
        self.compra.tipo_pagamento = "aprazo"
        self.compra.save(update_fields=["tipo_pagamento"])

        resposta = self.client.post(
            self.url,
            self.dados(**{"quantidade[]": ["7", "2"]}),
            follow=True,
            secure=True,
        )

        self.compra.refresh_from_db()
        self.produto_a.refresh_from_db()
        self.assertTrue(resposta.redirect_chain)
        self.assertEqual(
            urlsplit(resposta.redirect_chain[0][0]).path,
            reverse("estoque:compra_corrigir_financeiro", kwargs={"pk": self.compra.id}),
        )
        self.assertContains(resposta, "Agora ajuste a Conta a Pagar")
        self.assertContains(resposta, "Corrigir financeiro da Compra")
        self.assertEqual(self.compra.total, Decimal("80.00"))
        self.assertEqual(self.produto_a.quantidade, Decimal("17.000"))
        self.assert_financeiro_inalterado()

    def test_aumentar_quantidade_aumenta_estoque_pela_diferenca(self):
        self.client.post(self.url, self.dados(**{"quantidade[]": ["15", "2"]}), secure=True)
        self.produto_a.refresh_from_db(); self.compra.refresh_from_db()
        self.assertEqual(self.produto_a.quantidade, Decimal("25.000"))
        self.assertEqual(self.compra.total, Decimal("160.00"))
        self.assert_financeiro_inalterado()

    def test_alterar_preco_recalcula_total_sem_alterar_estoque(self):
        self.client.post(self.url, self.dados(**{"preco_unitario[]": ["12,00", "5,00"]}), secure=True)
        self.produto_a.refresh_from_db(); self.compra.refresh_from_db()
        self.assertEqual(self.produto_a.quantidade, Decimal("20.000"))
        self.assertEqual(self.compra.total, Decimal("130.00"))
        self.assert_financeiro_inalterado()

    def test_remover_item_desfaz_sua_entrada_no_estoque(self):
        self.client.post(self.url, self.dados(**{"remover_item[]": [str(self.item_a.id)]}), secure=True)
        self.produto_a.refresh_from_db(); self.compra.refresh_from_db()
        self.assertFalse(ItemCompra.objects.filter(pk=self.item_a.id).exists())
        self.assertEqual(self.produto_a.quantidade, Decimal("10.000"))
        self.assertEqual(self.compra.total, Decimal("10.00"))
        self.assert_financeiro_inalterado()

    def test_adicionar_item_aumenta_estoque(self):
        self.client.post(self.url, self.dados(**{
            "novo_produto_id[]": [str(self.produto_c.id)],
            "nova_quantidade[]": ["5"],
            "novo_preco_unitario[]": ["4,00"],
        }), secure=True)
        self.produto_c.refresh_from_db(); self.compra.refresh_from_db()
        novo = self.compra.itens.get(produto=self.produto_c)
        self.assertEqual(self.produto_c.quantidade, Decimal("8.000"))
        self.assertEqual(novo.valor_total, Decimal("20.00"))
        self.assertEqual(self.compra.total, Decimal("130.00"))
        self.assert_financeiro_inalterado()

    def test_salvar_sem_alteracao_nao_muda_dados_nem_registra_historico(self):
        self.compra.observacao = "Historico existente."
        self.compra.save(update_fields=["observacao"])

        resposta = self.client.post(self.url, self.dados(), follow=True, secure=True)

        self.compra.refresh_from_db()
        self.produto_a.refresh_from_db()
        self.produto_b.refresh_from_db()
        self.item_a.refresh_from_db()
        self.item_b.refresh_from_db()
        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.redirect_chain)
        self.assertEqual(
            urlsplit(resposta.redirect_chain[0][0]).path,
            reverse("estoque:compras_detalhe", kwargs={"pk": self.compra.id}),
        )
        self.assertContains(resposta, "Nenhuma alteração de itens foi feita.")
        self.assertEqual(self.compra.total, Decimal("110.00"))
        self.assertEqual(self.compra.observacao, "Historico existente.")
        self.assertEqual(self.produto_a.quantidade, Decimal("20.000"))
        self.assertEqual(self.produto_b.quantidade, Decimal("12.000"))
        self.assertEqual(self.item_a.quantidade, Decimal("10.000"))
        self.assertEqual(self.item_a.preco_unitario, Decimal("10.00"))
        self.assertEqual(self.item_b.quantidade, Decimal("2.000"))
        self.assertEqual(self.item_b.preco_unitario, Decimal("5.00"))
        self.assertEqual(self.compra.itens.count(), 2)
        self.assert_financeiro_inalterado()

    def test_compra_antiga_e_tela_de_correcao_continuam_abrindo(self):
        detalhe = self.client.get(f"/estoque/compras/{self.compra.id}/", secure=True)
        correcao = self.client.get(self.url, secure=True)
        self.assertEqual(detalhe.status_code, 200)
        self.assertContains(correcao, "Salvar correção dos itens")
        self.assertContains(correcao, "Novo total")
        self.assertContains(correcao, "Caixa/Banco e Conta a Pagar não serão alterados")
        self.assertContains(correcao, 'form.addEventListener("keydown"')
        self.assertContains(correcao, 'event.key !== "Enter"')
        self.assertContains(correcao, 'event.preventDefault()')
        self.assertContains(correcao, '.linha-correcao-item .campo-quantidade-correcao')
        self.assertContains(correcao, 'campo.select()')
        self.assertContains(correcao, 'campoNavegavel(event.target)')
        self.assertContains(correcao, '.campo-produto-novo-correcao, .campo-quantidade-correcao, .campo-preco-correcao')
        self.assertContains(correcao, 'tbodyNovos.lastElementChild?.querySelector(".campo-produto-novo-correcao")')


class CorrecaoFinanceiroCompraTests(TestCase):
    def setUp(self):
        self.fornecedor = Fornecedor.objects.create(nome="Fornecedor Financeiro")
        self.produto = Produto.objects.create(
            nome="Produto Financeiro", preco_compra=Decimal("10.00"),
            preco_vista=Decimal("12.00"), preco_prazo=Decimal("13.00"),
            quantidade=Decimal("20.000"), unidade_compra="UN",
        )
        self.compra = Compra.objects.create(
            fornecedor=self.fornecedor, data_compra=timezone.localdate(),
            tipo_pagamento="aprazo", total=Decimal("433.60"),
            status=Compra.STATUS_FINALIZADA, estoque_entrada_realizada=True,
            operador="Operador Teste",
        )
        self.item = ItemCompra.objects.create(
            compra=self.compra, produto=self.produto, quantidade=Decimal("2.000"),
            unidade="UN", preco_unitario=Decimal("216.80"), valor_total=Decimal("433.60"),
        )
        self.conta = ContaPagar.objects.create(
            compra=self.compra, fornecedor=self.fornecedor,
            data_emissao=timezone.localdate(), valor_original=Decimal("1013.95"),
            valor_em_aberto=Decimal("1013.95"), status=ContaPagar.STATUS_ABERTA,
        )
        self.url = f"/estoque/compras/{self.compra.id}/corrigir-financeiro/"

    def criar_pagamento(self, valor):
        return PagamentoContaPagar.objects.create(
            conta=self.conta, data_pagamento=timezone.localdate(),
            valor=Decimal(valor), forma_pagamento="Boleto",
        )

    def assert_estoque_itens_caixa_inalterados(self):
        self.produto.refresh_from_db(); self.item.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("20.000"))
        self.assertEqual(self.item.quantidade, Decimal("2.000"))
        self.assertEqual(self.item.valor_total, Decimal("433.60"))
        self.assertEqual(MovimentoFinanceiro.objects.count(), 0)

    def test_sem_pagamento_ajusta_original_e_aberto(self):
        detalhe = self.client.get(f"/estoque/compras/{self.compra.id}/", secure=True)
        self.assertContains(detalhe, "Esta compra foi corrigida e o financeiro ainda está diferente.")
        self.assertContains(detalhe, "Corrigir financeiro")

        resposta = self.client.post(self.url, {"confirmar": "1"}, follow=True, secure=True)
        self.conta.refresh_from_db(); self.compra.refresh_from_db()
        self.assertEqual(self.conta.valor_original, Decimal("433.60"))
        self.assertEqual(self.conta.valor_em_aberto, Decimal("433.60"))
        self.assertEqual(self.conta.status, ContaPagar.STATUS_ABERTA)
        self.assertIn("Ajuste financeiro apos correcao de itens", self.conta.observacao)
        self.assertIn("Operador Teste", self.conta.observacao)
        self.assertContains(resposta, "Financeiro da compra corrigido com sucesso.")
        self.assert_estoque_itens_caixa_inalterados()

    def test_pagamento_parcial_recalcula_aberto_pelo_total_pago(self):
        self.criar_pagamento("100.00")
        self.conta.valor_em_aberto = Decimal("913.95")
        self.conta.status = ContaPagar.STATUS_PARCIAL
        self.conta.save(update_fields=["valor_em_aberto", "status"])

        self.client.post(self.url, {"confirmar": "1"}, secure=True)
        self.conta.refresh_from_db()
        self.assertEqual(self.conta.valor_original, Decimal("433.60"))
        self.assertEqual(self.conta.valor_em_aberto, Decimal("333.60"))
        self.assertEqual(self.conta.status, ContaPagar.STATUS_PARCIAL)
        self.assert_estoque_itens_caixa_inalterados()

    def test_pagamento_maior_que_novo_total_bloqueia(self):
        self.criar_pagamento("500.00")
        self.conta.valor_em_aberto = Decimal("513.95")
        self.conta.status = ContaPagar.STATUS_PARCIAL
        self.conta.save(update_fields=["valor_em_aberto", "status"])

        resposta = self.client.post(self.url, {"confirmar": "1"}, follow=True, secure=True)
        self.conta.refresh_from_db()
        self.assertEqual(self.conta.valor_original, Decimal("1013.95"))
        self.assertEqual(self.conta.valor_em_aberto, Decimal("513.95"))
        self.assertContains(resposta, "O total ja pago e maior que o novo total da compra.")
        self.assert_estoque_itens_caixa_inalterados()

    def test_conta_quitada_bloqueia_correcao_sem_motivo(self):
        self.criar_pagamento("1013.95")
        self.conta.valor_em_aberto = Decimal("0.00")
        self.conta.status = ContaPagar.STATUS_PAGA
        self.conta.save(update_fields=["valor_em_aberto", "status"])
        resposta = self.client.post(self.url, {"confirmar": "1"}, follow=True, secure=True)
        self.conta.refresh_from_db()
        self.assertEqual(self.conta.valor_original, Decimal("1013.95"))
        self.assertContains(resposta, "A conta ja esta quitada")
        self.assert_estoque_itens_caixa_inalterados()

    def test_conta_quitada_permite_ajustar_como_erro_de_lancamento(self):
        self.criar_pagamento("1013.95")
        self.conta.valor_em_aberto = Decimal("0.00")
        self.conta.status = ContaPagar.STATUS_PAGA
        self.conta.save(update_fields=["valor_em_aberto", "status"])

        resposta_get = self.client.get(self.url, secure=True)
        self.assertContains(resposta_get, "Erro de lancamento da nota")
        self.assertContains(resposta_get, "Fornecedor devolveu dinheiro")
        self.assertContains(resposta_get, "Ficar como credito com fornecedor")
        self.assertNotContains(resposta_get, "Pagar diferenca agora")

        resposta = self.client.post(
            self.url,
            {"confirmar": "1", "motivo_correcao": "erro_lancamento"},
            follow=True,
            secure=True,
        )

        self.conta.refresh_from_db()
        self.assertEqual(self.conta.valor_original, Decimal("433.60"))
        self.assertEqual(self.conta.valor_em_aberto, Decimal("0.00"))
        self.assertEqual(self.conta.status, ContaPagar.STATUS_PAGA)
        self.assertIn("erro de lancamento", self.conta.observacao)
        self.assertContains(resposta, "Financeiro ajustado como erro de lancamento")
        self.assertContains(resposta, "Esta compra foi ajustada como erro de lancamento da nota")
        self.assertContains(resposta, "Os pagamentos abaixo foram preservados apenas como historico")
        self.assertContains(resposta, "Caixa/Banco nao foi alterado")
        self.assertContains(resposta, "Total dos pagamentos preservados no historico")
        self.assert_estoque_itens_caixa_inalterados()

    def test_conta_quitada_permite_ajustar_com_devolucao_do_fornecedor(self):
        self.criar_pagamento("1013.95")
        self.conta.valor_em_aberto = Decimal("0.00")
        self.conta.status = ContaPagar.STATUS_PAGA
        self.conta.save(update_fields=["valor_em_aberto", "status"])
        conta_banco = views._conta_financeira_padrao("banco")
        movimentos_antes = MovimentoFinanceiro.objects.count()

        resposta = self.client.post(
            self.url,
            {
                "confirmar": "1",
                "motivo_correcao": "devolucao_dinheiro",
                "conta_devolucao": str(conta_banco.id),
            },
            follow=True,
            secure=True,
        )

        self.conta.refresh_from_db()
        self.assertEqual(self.conta.valor_original, Decimal("433.60"))
        self.assertEqual(self.conta.valor_em_aberto, Decimal("0.00"))
        self.assertEqual(self.conta.status, ContaPagar.STATUS_PAGA)
        movimento = MovimentoFinanceiro.objects.get(origem="compra_devolucao_fornecedor")
        self.assertEqual(MovimentoFinanceiro.objects.count(), movimentos_antes + 1)
        self.assertEqual(movimento.tipo, MovimentoFinanceiro.TIPO_ENTRADA)
        self.assertEqual(movimento.valor, Decimal("580.35"))
        self.assertEqual(movimento.conta, conta_banco)
        self.compra.refresh_from_db()
        self.assertContains(resposta, "Financeiro ajustado com devolucao do fornecedor")
        self.assertIn("Devolucao registrada", self.compra.observacao or "")

    def test_conta_quitada_permite_registrar_credito_com_fornecedor(self):
        self.criar_pagamento("1013.95")
        self.conta.valor_em_aberto = Decimal("0.00")
        self.conta.status = ContaPagar.STATUS_PAGA
        self.conta.save(update_fields=["valor_em_aberto", "status"])
        movimentos_antes = MovimentoFinanceiro.objects.count()

        resposta = self.client.post(
            self.url,
            {"confirmar": "1", "motivo_correcao": "credito_fornecedor"},
            follow=True,
            secure=True,
        )

        self.conta.refresh_from_db()
        self.compra.refresh_from_db()
        self.assertEqual(self.conta.valor_original, Decimal("433.60"))
        self.assertEqual(self.conta.valor_em_aberto, Decimal("0.00"))
        self.assertEqual(self.conta.status, ContaPagar.STATUS_PAGA)
        self.assertEqual(MovimentoFinanceiro.objects.count(), movimentos_antes)
        self.assertIn("credito junto ao fornecedor", self.conta.observacao)
        self.assertIn("Credito registrado em historico", self.compra.observacao)
        self.assertContains(resposta, "credito com fornecedor registrado no historico")
        self.assert_estoque_itens_caixa_inalterados()

    def test_conta_quitada_com_total_maior_mostra_opcoes_de_diferenca_a_pagar(self):
        self.criar_pagamento("1013.95")
        self.conta.valor_em_aberto = Decimal("0.00")
        self.conta.status = ContaPagar.STATUS_PAGA
        self.conta.save(update_fields=["valor_em_aberto", "status"])
        self.compra.total = Decimal("1200.00")
        self.compra.save(update_fields=["total"])

        resposta = self.client.get(self.url, secure=True)

        self.assertContains(resposta, "Faltou pagar uma diferenca")
        self.assertContains(resposta, "Pagar diferenca agora")
        self.assertContains(resposta, "Deixar diferenca em aberto")
        self.assertNotContains(resposta, "Fornecedor devolveu dinheiro")
        self.assertNotContains(resposta, "Ficar como credito com fornecedor")

    def test_conta_quitada_com_total_maior_paga_diferenca_agora(self):
        self.criar_pagamento("1013.95")
        self.conta.valor_em_aberto = Decimal("0.00")
        self.conta.status = ContaPagar.STATUS_PAGA
        self.conta.save(update_fields=["valor_em_aberto", "status"])
        self.compra.total = Decimal("1200.00")
        self.compra.save(update_fields=["total"])
        conta_banco = views._conta_financeira_padrao("banco")
        MovimentoFinanceiro.objects.create(
            conta=conta_banco,
            tipo=MovimentoFinanceiro.TIPO_ENTRADA,
            valor=Decimal("500.00"),
            data=timezone.localdate(),
            descricao="Saldo para teste",
            origem="teste",
        )
        movimentos_antes = MovimentoFinanceiro.objects.count()
        pagamentos_antes = PagamentoContaPagar.objects.count()

        resposta = self.client.post(
            self.url,
            {
                "confirmar": "1",
                "motivo_correcao": "pagar_diferenca",
                "conta_pagamento_diferenca": str(conta_banco.id),
            },
            follow=True,
            secure=True,
        )

        self.conta.refresh_from_db()
        self.assertEqual(self.conta.valor_original, Decimal("1200.00"))
        self.assertEqual(self.conta.valor_em_aberto, Decimal("0.00"))
        self.assertEqual(self.conta.status, ContaPagar.STATUS_PAGA)
        self.assertEqual(PagamentoContaPagar.objects.count(), pagamentos_antes + 1)
        movimento = MovimentoFinanceiro.objects.get(origem="compra_pagamento_diferenca")
        self.assertEqual(MovimentoFinanceiro.objects.count(), movimentos_antes + 1)
        self.assertEqual(movimento.tipo, MovimentoFinanceiro.TIPO_SAIDA)
        self.assertEqual(movimento.valor, Decimal("186.05"))
        self.assertContains(resposta, "diferenca paga")

    def test_conta_quitada_com_total_maior_deixa_diferenca_em_aberto(self):
        self.criar_pagamento("1013.95")
        self.conta.valor_em_aberto = Decimal("0.00")
        self.conta.status = ContaPagar.STATUS_PAGA
        self.conta.save(update_fields=["valor_em_aberto", "status"])
        self.compra.total = Decimal("1200.00")
        self.compra.save(update_fields=["total"])
        movimentos_antes = MovimentoFinanceiro.objects.count()

        resposta = self.client.post(
            self.url,
            {"confirmar": "1", "motivo_correcao": "deixar_em_aberto"},
            follow=True,
            secure=True,
        )

        self.conta.refresh_from_db()
        self.assertEqual(self.conta.valor_original, Decimal("1200.00"))
        self.assertEqual(self.conta.valor_em_aberto, Decimal("186.05"))
        self.assertEqual(self.conta.status, ContaPagar.STATUS_PARCIAL)
        self.assertEqual(MovimentoFinanceiro.objects.count(), movimentos_antes)
        self.assertContains(resposta, "diferenca deixada em aberto")

    def test_compra_a_vista_nao_pode_usar_esta_etapa(self):
        self.compra.tipo_pagamento = "avista"
        self.compra.save(update_fields=["tipo_pagamento"])
        resposta = self.client.post(self.url, {"confirmar": "1"}, follow=True, secure=True)
        self.conta.refresh_from_db()
        self.assertEqual(self.conta.valor_original, Decimal("1013.95"))
        self.assertContains(resposta, "Esta etapa corrige somente compras a prazo.")
        self.assert_estoque_itens_caixa_inalterados()


class CorrecaoOrigemCompraTests(TestCase):
    def setUp(self):
        self.fornecedor = Fornecedor.objects.create(nome="Fornecedor Origem")
        self.produto = Produto.objects.create(
            nome="Produto Origem", preco_compra=Decimal("80.00"),
            preco_vista=Decimal("100.00"), preco_prazo=Decimal("110.00"),
            quantidade=Decimal("15.000"), unidade_compra="UN",
        )
        self.compra = Compra.objects.create(
            fornecedor=self.fornecedor, data_compra=timezone.localdate(),
            tipo_pagamento="avista", total=Decimal("500.00"),
            status=Compra.STATUS_FINALIZADA, estoque_entrada_realizada=True,
            operador="Operador Origem",
        )
        self.item = ItemCompra.objects.create(
            compra=self.compra, produto=self.produto, quantidade=Decimal("5.000"),
            unidade="UN", preco_unitario=Decimal("100.00"), valor_total=Decimal("500.00"),
        )
        self.conta_caixa = views._conta_financeira_padrao("caixa")
        self.conta_reserva = views._conta_financeira_padrao("reserva")
        self.conta_banco = views._conta_financeira_padrao("banco")
        self.movimento_original = MovimentoFinanceiro.objects.create(
            conta=self.conta_caixa, tipo=MovimentoFinanceiro.TIPO_SAIDA,
            valor=Decimal("500.00"), data=timezone.localdate(),
            descricao=f"Pagamento da compra #{self.compra.id}",
            origem="compra_a_vista", compra=self.compra,
        )
        self.conta_pagar = ContaPagar.objects.create(
            compra=self.compra, fornecedor=self.fornecedor, data_emissao=timezone.localdate(),
            valor_original=Decimal("500.00"), valor_em_aberto=Decimal("500.00"),
        )
        self.url = f"/estoque/compras/{self.compra.id}/corrigir-origem-pagamento/"

    def dados(self, caixa, reserva, banco):
        return {"origem_caixa": caixa, "origem_reserva": reserva, "origem_banco": banco}

    def assert_dados_compra_inalterados(self):
        self.produto.refresh_from_db(); self.item.refresh_from_db(); self.conta_pagar.refresh_from_db()
        self.assertEqual(self.produto.quantidade, Decimal("15.000"))
        self.assertEqual(self.item.quantidade, Decimal("5.000"))
        self.assertEqual(self.item.valor_total, Decimal("500.00"))
        self.assertEqual(self.conta_pagar.valor_original, Decimal("500.00"))
        self.assertEqual(self.conta_pagar.valor_em_aberto, Decimal("500.00"))

    def test_compra_a_vista_abre_tela_com_distribuicao_atual(self):
        resposta = self.client.get(self.url, secure=True)
        detalhe = self.client.get(f"/estoque/compras/{self.compra.id}/", secure=True)
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Corrigir origem do dinheiro")
        self.assertContains(resposta, "Caixa atual")
        self.assertContains(resposta, "Tudo distribuído")
        self.assertContains(detalhe, "Corrigir origem")

    def test_detalhe_orienta_corrigir_origem_apos_mudanca_do_total(self):
        self.compra.total = Decimal("400.00")
        self.compra.save(update_fields=["total"])
        resposta = self.client.get(f"/estoque/compras/{self.compra.id}/", secure=True)
        self.assertContains(resposta, "O total da compra e as origens do dinheiro estão diferentes.")
        self.assertContains(resposta, "Corrigir origem")

    def test_soma_menor_que_total_bloqueia(self):
        resposta = self.client.post(self.url, self.dados("100", "100", "100"), follow=True, secure=True)
        self.assertEqual(self.compra.movimentos_financeiros.count(), 1)
        self.assertContains(resposta, "Distribua o total da compra entre Caixa, Sangria e Banco/Pix.")
        self.assert_dados_compra_inalterados()

    def test_soma_maior_que_total_bloqueia(self):
        self.client.post(self.url, self.dados("300", "200", "100"), secure=True)
        self.assertEqual(self.compra.movimentos_financeiros.count(), 1)
        self.assert_dados_compra_inalterados()

    def test_soma_correta_cria_ajustes_e_corrige_saldos(self):
        resposta = self.client.post(self.url, self.dados("100", "200", "200"), follow=True, secure=True)
        self.compra.refresh_from_db()
        movimentos = self.compra.movimentos_financeiros.order_by("id")
        self.assertEqual(movimentos.count(), 4)
        ajustes = movimentos.filter(origem="compra_correcao_origem")
        self.assertEqual(ajustes.count(), 3)
        self.assertEqual(views._saldo_conta_financeira(self.conta_caixa), Decimal("-100.00"))
        self.assertEqual(views._saldo_conta_financeira(self.conta_reserva), Decimal("-200.00"))
        self.assertEqual(views._saldo_conta_financeira(self.conta_banco), Decimal("-200.00"))
        self.assertEqual(views._alocacao_financeira_compra(self.compra), {
            "caixa": Decimal("100.00"), "reserva": Decimal("200.00"), "banco": Decimal("200.00"),
        })
        self.assertTrue(all("Correcao de origem da compra" in movimento.descricao for movimento in ajustes))
        self.assertIn("Origem anterior", self.compra.observacao)
        self.assertIn("Nova origem", self.compra.observacao)
        self.assertIn("Operador Origem", self.compra.observacao)
        self.assertContains(resposta, "Origem do pagamento corrigida")
        self.assert_dados_compra_inalterados()

    def test_compra_a_prazo_nao_permite_corrigir_origem(self):
        self.compra.tipo_pagamento = "aprazo"
        self.compra.save(update_fields=["tipo_pagamento"])
        resposta = self.client.get(self.url, follow=True, secure=True)
        self.assertContains(resposta, "se aplica apenas a compra a vista")
        self.assertEqual(self.compra.movimentos_financeiros.count(), 1)
        self.assert_dados_compra_inalterados()


class FuncionarioTests(TestCase):
    def test_exige_whatsapp_para_receber_checklist(self):
        funcionario = Funcionario(
            nome="Joao Entregador",
            pode_receber_checklist=True,
        )

        with self.assertRaises(ValidationError):
            funcionario.full_clean()

    def test_habilitados_para_checklist_filtra_ativos_e_marcados(self):
        habilitado = Funcionario.objects.create(
            nome="Maria Entregadora",
            telefone_whatsapp="85999990000",
            pode_receber_checklist=True,
        )
        Funcionario.objects.create(
            nome="Funcionario Inativo",
            telefone_whatsapp="85999991111",
            ativo=False,
            pode_receber_checklist=True,
        )
        Funcionario.objects.create(
            nome="Funcionario Sem Checklist",
            telefone_whatsapp="85999992222",
            pode_receber_checklist=False,
        )

        self.assertEqual(list(Funcionario.habilitados_para_checklist()), [habilitado])

    def test_operadores_do_sistema_filtra_ativos_e_marcados(self):
        operador = Funcionario.objects.create(
            nome="Bruna Operadora",
            pode_operar_sistema=True,
        )
        Funcionario.objects.create(
            nome="Funcionario Inativo",
            ativo=False,
            pode_operar_sistema=True,
        )
        Funcionario.objects.create(
            nome="Funcionario Sem Operador",
            pode_operar_sistema=False,
        )

        self.assertEqual(list(Funcionario.operadores_do_sistema()), [operador])

    def test_form_valida_whatsapp_quando_checklist_marcado(self):
        form = FuncionarioForm(data={
            "nome": "Carlos Entregador",
            "telefone_whatsapp": "",
            "pode_receber_checklist": "on",
            "ativo": "on",
        })

        self.assertFalse(form.is_valid())
        self.assertIn("telefone_whatsapp", form.errors)

    def test_view_cria_busca_edita_e_alterna_status(self):
        url = reverse("estoque:funcionarios")

        resposta_criar = self.client.post(url, data={
            "nome": "Ana Entregadora",
            "telefone_whatsapp": "85999990000",
            "pode_receber_checklist": "on",
            "pode_operar_sistema": "on",
            "ativo": "on",
            "observacoes": "Rota centro",
        }, secure=True)
        self.assertEqual(resposta_criar.status_code, 302)

        funcionario = Funcionario.objects.get(nome="Ana Entregadora")
        self.assertTrue(funcionario.ativo)
        self.assertTrue(funcionario.pode_receber_checklist)
        self.assertTrue(funcionario.pode_operar_sistema)
        self.assertEqual(funcionario.telefone_whatsapp_normalizado, "85999990000")

        resposta_busca = self.client.get(url, {"q": "99999"}, secure=True)
        self.assertContains(resposta_busca, "Ana Entregadora")

        resposta_editar = self.client.post(url, data={
            "funcionario_id": funcionario.id,
            "nome": "Ana Silva",
            "telefone_whatsapp": "85988887777",
            "pode_receber_checklist": "on",
            "pode_operar_sistema": "on",
            "ativo": "on",
        }, secure=True)
        self.assertEqual(resposta_editar.status_code, 302)
        funcionario.refresh_from_db()
        self.assertEqual(funcionario.nome, "Ana Silva")
        self.assertEqual(funcionario.telefone_whatsapp_normalizado, "85988887777")

        resposta_status = self.client.post(url, data={
            "acao": "alternar_status",
            "funcionario_id": funcionario.id,
            "ativo": "0",
        }, secure=True)
        self.assertEqual(resposta_status.status_code, 302)
        funcionario.refresh_from_db()
        self.assertFalse(funcionario.ativo)
        self.assertFalse(funcionario.pode_receber_checklist)
        self.assertFalse(funcionario.pode_operar_sistema)


class PixRecebidoTests(TestCase):
    def _produto_teste(self, nome, quantidade=None):
        return Produto.objects.create(
            nome=nome,
            preco_compra=Decimal("1.00"),
            preco_vista=Decimal("2.00"),
            preco_prazo=Decimal("3.00"),
            quantidade=quantidade,
            permitir_prejuizo=False,
        )

    def _post_cancelar_venda(self, venda, motivo="Pedido duplicado", destino_financeiro=""):
        dados = {
            "motivo_padrao": motivo,
            "observacao_cancelamento": "",
            "confirmacao_cancelamento": "CANCELAR",
            "ciencia_cancelamento": "1",
        }
        if destino_financeiro:
            dados["destino_financeiro"] = destino_financeiro
        return self.client.post(
            reverse("estoque:venda_cancelar", kwargs={"pk": venda.id}),
            dados,
            secure=True,
            follow=True,
        )

    def _post_gravar_venda(self, produto, quantidade="1"):
        return self.client.post(
            reverse("estoque:gravar_venda"),
            data=json.dumps({
                "cliente_id": "",
                "data_venda": timezone.localdate().isoformat(),
                "data_vencimento": "",
                "tipo_pagamento": "A vista",
                "operador": "Operador Teste",
                "itens": [
                    {
                        "produto_nome": produto.nome,
                        "quantidade": quantidade,
                        "unidade": "un",
                        "preco_unitario": "2.00",
                    }
                ],
            }),
            content_type="application/json",
            secure=True,
        )

    def test_cancelamento_manual_preserva_venda_itens_total_e_registra_historico(self):
        cliente = Cliente.objects.create(nome="Cliente Cancelamento Manual", ativo=True)
        produto = self._produto_teste("Produto Cancelamento Manual")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            operador="Operador Teste",
            total=Decimal("42.50"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("21.25"),
            valor_total=Decimal("42.50"),
        )

        resposta = self._post_cancelar_venda(venda)

        self.assertEqual(resposta.status_code, 200)
        venda.refresh_from_db()
        self.assertTrue(venda.cancelada)
        self.assertEqual(venda.motivo_cancelamento, "Pedido duplicado")
        self.assertIsNotNone(venda.cancelada_em)
        self.assertTrue(ItemVenda.objects.filter(pk=item.pk, venda=venda).exists())
        self.assertEqual(venda.total, Decimal("42.50"))
        self.assertTrue(
            EventoVenda.objects.filter(
                venda=venda,
                tipo_evento="venda_cancelada",
                descricao__icontains="Motivo: Pedido duplicado",
                usuario="Operador Teste",
            ).exists()
        )

    def test_cancelamento_manual_exige_confirmacao_cancelar(self):
        cliente = Cliente.objects.create(nome="Cliente Confirmacao Errada", ativo=True)
        produto = self._produto_teste("Produto Confirmacao Errada")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("20.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("20.00"),
            valor_total=Decimal("20.00"),
        )

        resposta = self.client.post(
            reverse("estoque:venda_cancelar", kwargs={"pk": venda.id}),
            {
                "motivo_padrao": "Pedido duplicado",
                "observacao_cancelamento": "",
                "confirmacao_cancelamento": "CANCELA",
                "ciencia_cancelamento": "1",
            },
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        venda.refresh_from_db()
        self.assertFalse(venda.cancelada)
        self.assertIsNone(venda.cancelada_em)
        self.assertEqual(venda.motivo_cancelamento, "")
        self.assertTrue(ItemVenda.objects.filter(pk=item.pk, venda=venda).exists())
        self.assertFalse(EventoVenda.objects.filter(venda=venda, tipo_evento="venda_cancelada").exists())
        self.assertContains(resposta, "Digite CANCELAR exatamente")

    def test_cancelamento_manual_exige_ciencia_de_preservacao_historica(self):
        cliente = Cliente.objects.create(nome="Cliente Ciencia Cancelamento", ativo=True)
        produto = self._produto_teste("Produto Ciencia Cancelamento")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("18.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("18.00"),
            valor_total=Decimal("18.00"),
        )

        resposta = self.client.post(
            reverse("estoque:venda_cancelar", kwargs={"pk": venda.id}),
            {
                "motivo_padrao": "Pedido duplicado",
                "observacao_cancelamento": "",
                "confirmacao_cancelamento": "CANCELAR",
            },
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        venda.refresh_from_db()
        self.assertFalse(venda.cancelada)
        self.assertTrue(ItemVenda.objects.filter(pk=item.pk, venda=venda).exists())
        self.assertFalse(EventoVenda.objects.filter(venda=venda, tipo_evento="venda_cancelada").exists())
        self.assertContains(resposta, "Marque a ciencia")

    def test_cancelamento_manual_de_venda_ja_cancelada_nao_registra_novo_cancelamento(self):
        cliente = Cliente.objects.create(nome="Cliente Ja Cancelada", ativo=True)
        produto = self._produto_teste("Produto Ja Cancelada")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("32.00"),
            cancelada=True,
            cancelada_em=timezone.now(),
            motivo_cancelamento="Cancelamento anterior",
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("32.00"),
            valor_total=Decimal("32.00"),
        )
        EventoVenda.objects.create(
            venda=venda,
            tipo_evento="venda_cancelada",
            descricao="Cancelamento anterior.",
            canal="sistema",
        )

        resposta = self._post_cancelar_venda(venda)

        self.assertEqual(resposta.status_code, 200)
        venda.refresh_from_db()
        self.assertTrue(venda.cancelada)
        self.assertEqual(venda.motivo_cancelamento, "Cancelamento anterior")
        self.assertEqual(EventoVenda.objects.filter(venda=venda, tipo_evento="venda_cancelada").count(), 1)

    def test_cancelamento_manual_cancela_conta_aberta_sem_excluir_venda_ou_itens(self):
        cliente = Cliente.objects.create(nome="Cliente Conta Aberta Cancelamento", ativo=True)
        produto = self._produto_teste("Produto Conta Aberta Cancelamento")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("75.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("3.000"),
            unidade="un",
            preco_unitario=Decimal("25.00"),
            valor_total=Decimal("75.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("75.00"),
            valor_em_aberto=Decimal("75.00"),
            status=ContaReceber.STATUS_ABERTA,
        )

        resposta = self._post_cancelar_venda(venda)

        self.assertEqual(resposta.status_code, 200)
        venda.refresh_from_db()
        conta.refresh_from_db()
        self.assertTrue(venda.cancelada)
        self.assertTrue(ItemVenda.objects.filter(pk=item.pk, venda=venda).exists())
        self.assertEqual(venda.total, Decimal("75.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_CANCELADA)
        self.assertEqual(conta.valor_original, Decimal("75.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("0.00"))
        self.assertIn("Cancelada por venda nao realizada", conta.observacao)

    def test_cancelamento_manual_preserva_conta_parcial_e_recebimentos(self):
        cliente = Cliente.objects.create(nome="Cliente Conta Parcial Cancelamento", ativo=True)
        produto = self._produto_teste("Produto Conta Parcial Cancelamento")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("100.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("100.00"),
            valor_total=Decimal("100.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("100.00"),
            valor_em_aberto=Decimal("40.00"),
            status=ContaReceber.STATUS_PARCIAL,
        )
        recebimento = RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("60.00"),
            forma_pagamento="PIX",
        )

        resposta = self._post_cancelar_venda(venda, destino_financeiro="credito_cliente")

        self.assertEqual(resposta.status_code, 200)
        venda.refresh_from_db()
        conta.refresh_from_db()
        recebimento.refresh_from_db()
        self.assertTrue(venda.cancelada)
        self.assertEqual(conta.status, ContaReceber.STATUS_CANCELADA)
        self.assertEqual(conta.valor_original, Decimal("100.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("0.00"))
        self.assertEqual(recebimento.valor, Decimal("60.00"))
        self.assertTrue(RecebimentoContaReceber.objects.filter(pk=recebimento.pk, conta=conta).exists())
        credito = CreditoCliente.objects.get(cliente=cliente)
        self.assertEqual(credito.valor, Decimal("60.00"))
        self.assertEqual(credito.origem_conta_receber, conta)
        self.assertEqual(credito.origem_recebimento, recebimento)
        self.assertIn("cancelamento da venda", credito.observacao)
        self.assertTrue(
            EventoVenda.objects.filter(
                venda=venda,
                tipo_evento="venda_cancelada",
                descricao__icontains="virou credito do cliente",
            ).exists()
        )

    def test_cancelamento_manual_com_recebimento_sem_destino_financeiro_bloqueia_cancelamento(self):
        cliente = Cliente.objects.create(nome="Cliente Sem Destino Financeiro", ativo=True)
        produto = self._produto_teste("Produto Sem Destino Financeiro")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("100.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("100.00"),
            valor_total=Decimal("100.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("100.00"),
            valor_em_aberto=Decimal("30.00"),
            status=ContaReceber.STATUS_PARCIAL,
        )
        recebimento = RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("70.00"),
            forma_pagamento="PIX",
        )

        resposta = self._post_cancelar_venda(venda)

        self.assertEqual(resposta.status_code, 200)
        venda.refresh_from_db()
        conta.refresh_from_db()
        self.assertFalse(venda.cancelada)
        self.assertEqual(conta.status, ContaReceber.STATUS_PARCIAL)
        self.assertEqual(conta.valor_em_aberto, Decimal("30.00"))
        self.assertTrue(RecebimentoContaReceber.objects.filter(pk=recebimento.pk, conta=conta).exists())
        self.assertEqual(CreditoCliente.objects.count(), 0)
        self.assertContains(resposta, "Escolha o destino financeiro")

    def test_cancelamento_manual_com_devolucao_manual_ou_pendencia_nao_cria_credito(self):
        cenarios = (
            ("devolucao_manual", "devolucao manual ao cliente"),
            ("pendencia_financeira", "pendencia financeira"),
        )
        for destino, texto_evento in cenarios:
            with self.subTest(destino=destino):
                cliente = Cliente.objects.create(nome=f"Cliente {destino}", ativo=True)
                produto = self._produto_teste(f"Produto {destino}")
                venda = Venda.objects.create(
                    cliente=cliente,
                    data_venda=timezone.localdate(),
                    tipo_pagamento="A prazo",
                    total=Decimal("80.00"),
                )
                ItemVenda.objects.create(
                    venda=venda,
                    produto=produto,
                    quantidade=Decimal("1.000"),
                    unidade="un",
                    preco_unitario=Decimal("80.00"),
                    valor_total=Decimal("80.00"),
                )
                conta = ContaReceber.objects.create(
                    venda=venda,
                    cliente=cliente,
                    data_emissao=timezone.localdate(),
                    valor_original=Decimal("80.00"),
                    valor_em_aberto=Decimal("20.00"),
                    status=ContaReceber.STATUS_PARCIAL,
                )
                recebimento = RecebimentoContaReceber.objects.create(
                    conta=conta,
                    data_recebimento=timezone.localdate(),
                    valor=Decimal("60.00"),
                    forma_pagamento="PIX",
                )

                resposta = self._post_cancelar_venda(venda, destino_financeiro=destino)

                self.assertEqual(resposta.status_code, 200)
                venda.refresh_from_db()
                conta.refresh_from_db()
                recebimento.refresh_from_db()
                self.assertTrue(venda.cancelada)
                self.assertEqual(conta.status, ContaReceber.STATUS_CANCELADA)
                self.assertEqual(conta.valor_em_aberto, Decimal("0.00"))
                self.assertEqual(recebimento.valor, Decimal("60.00"))
                self.assertFalse(CreditoCliente.objects.filter(cliente=cliente).exists())
                self.assertIn(texto_evento, conta.observacao)
                self.assertTrue(
                    EventoVenda.objects.filter(
                        venda=venda,
                        tipo_evento="venda_cancelada",
                        descricao__icontains=texto_evento,
                    ).exists()
                )

    def test_cancelamento_manual_com_credito_sem_cliente_bloqueia_cancelamento(self):
        produto = self._produto_teste("Produto Credito Sem Cliente")
        venda = Venda.objects.create(
            cliente=None,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("50.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("50.00"),
            valor_total=Decimal("50.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=None,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("50.00"),
            valor_em_aberto=Decimal("10.00"),
            status=ContaReceber.STATUS_PARCIAL,
        )
        RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("40.00"),
            forma_pagamento="PIX",
        )

        resposta = self._post_cancelar_venda(venda, destino_financeiro="credito_cliente")

        self.assertEqual(resposta.status_code, 200)
        venda.refresh_from_db()
        conta.refresh_from_db()
        self.assertFalse(venda.cancelada)
        self.assertEqual(conta.status, ContaReceber.STATUS_PARCIAL)
        self.assertEqual(conta.valor_em_aberto, Decimal("10.00"))
        self.assertEqual(CreditoCliente.objects.count(), 0)
        self.assertContains(resposta, "Nao e possivel gerar credito")

    def test_cancelamento_manual_preserva_conta_paga_e_recebimentos(self):
        cliente = Cliente.objects.create(nome="Cliente Conta Paga Cancelamento", ativo=True)
        produto = self._produto_teste("Produto Conta Paga Cancelamento")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("90.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("90.00"),
            valor_total=Decimal("90.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("90.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )
        recebimento = RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("90.00"),
            forma_pagamento="PIX",
        )

        resposta = self._post_cancelar_venda(venda, destino_financeiro="credito_cliente")

        self.assertEqual(resposta.status_code, 200)
        venda.refresh_from_db()
        conta.refresh_from_db()
        recebimento.refresh_from_db()
        self.assertTrue(venda.cancelada)
        self.assertEqual(conta.status, ContaReceber.STATUS_CANCELADA)
        self.assertEqual(conta.valor_original, Decimal("90.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("0.00"))
        self.assertEqual(recebimento.valor, Decimal("90.00"))
        self.assertTrue(RecebimentoContaReceber.objects.filter(pk=recebimento.pk, conta=conta).exists())
        credito = CreditoCliente.objects.get(cliente=cliente)
        self.assertEqual(credito.valor, Decimal("90.00"))
        self.assertEqual(credito.origem_conta_receber, conta)
        self.assertEqual(credito.origem_recebimento, recebimento)
        self.assertTrue(
            EventoVenda.objects.filter(
                venda=venda,
                tipo_evento="venda_cancelada",
                descricao__icontains="virou credito do cliente",
            ).exists()
        )

    def test_cancelamento_manual_move_venda_para_consulta_de_canceladas(self):
        cliente = Cliente.objects.create(nome="Cliente Consulta Cancelada Manual", ativo=True)
        produto = self._produto_teste("Produto Consulta Cancelada Manual")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("30.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("30.00"),
            valor_total=Decimal("30.00"),
        )

        self._post_cancelar_venda(venda)

        resposta_ativas = self.client.get(reverse("estoque:consultar_vendas"), secure=True)
        self.assertNotContains(resposta_ativas, "Cliente Consulta Cancelada Manual")
        resposta_canceladas = self.client.get(reverse("estoque:consultar_vendas_canceladas"), secure=True)
        self.assertContains(resposta_canceladas, "Cliente Consulta Cancelada Manual")

    def test_gravar_venda_baixa_estoque(self):
        produto = self._produto_teste("Produto Baixa Estoque Venda", quantidade=5)

        resposta = self._post_gravar_venda(produto, quantidade="2")

        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json()["sucesso"])
        produto.refresh_from_db()
        self.assertEqual(produto.quantidade, 3)
        venda = Venda.objects.get(pk=resposta.json()["venda_id"])
        self.assertEqual(venda.itens.get().quantidade, Decimal("2.000"))
        self.assertTrue(
            EventoVenda.objects.filter(
                venda=venda,
                tipo_evento="venda_gravada",
                descricao__icontains="Estoque baixado",
            ).exists()
        )

    def test_gravar_venda_bloqueia_estoque_insuficiente(self):
        produto = self._produto_teste("Produto Estoque Insuficiente Venda", quantidade=1)

        resposta = self._post_gravar_venda(produto, quantidade="2")

        self.assertEqual(resposta.status_code, 400)
        self.assertFalse(resposta.json()["sucesso"])
        self.assertIn("Estoque insuficiente", resposta.json()["mensagem"])
        produto.refresh_from_db()
        self.assertEqual(produto.quantidade, 1)
        self.assertEqual(Venda.objects.count(), 0)
        self.assertEqual(ItemVenda.objects.count(), 0)

    def test_adicionar_item_na_nota_baixa_estoque(self):
        cliente = Cliente.objects.create(nome="Cliente Adicao Estoque", ativo=True)
        produto = self._produto_teste("Produto Adicao Estoque", quantidade=5)
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("0.00"),
        )

        resposta = self.client.post(
            reverse("estoque:venda_adicionar_produto_item", kwargs={"pk": venda.id}),
            data={
                "produto_id": str(produto.id),
                "quantidade": "2",
                "preco_unitario": "2.00",
            },
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        produto.refresh_from_db()
        venda.refresh_from_db()
        self.assertEqual(produto.quantidade, 3)
        self.assertEqual(venda.total, Decimal("4.00"))
        self.assertTrue(ItemVenda.objects.filter(venda=venda, produto=produto, quantidade=Decimal("2.000")).exists())

    def test_remover_item_da_nota_devolve_estoque_do_item(self):
        cliente = Cliente.objects.create(nome="Cliente Remove Estoque", ativo=True)
        produto = self._produto_teste("Produto Remove Estoque", quantidade=3)
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("4.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("2.00"),
            valor_total=Decimal("4.00"),
        )

        resposta = self.client.post(
            reverse("estoque:venda_revisar_remocao_item", kwargs={"pk": venda.id, "item_id": item.id}),
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        produto.refresh_from_db()
        venda.refresh_from_db()
        remocao = ItemVendaRemovido.objects.get(venda=venda)
        self.assertEqual(produto.quantidade, 5)
        self.assertTrue(remocao.estoque_devolvido)
        self.assertIsNotNone(remocao.estoque_devolvido_em)
        self.assertFalse(ItemVenda.objects.filter(pk=item.pk).exists())
        self.assertTrue(
            EventoVenda.objects.filter(
                venda=venda,
                tipo_evento="item_removido_da_nota",
                descricao__icontains="Estoque devolvido",
            ).exists()
        )

    def test_desfazer_remocao_baixa_estoque_novamente(self):
        cliente = Cliente.objects.create(nome="Cliente Desfaz Estoque", ativo=True)
        produto = self._produto_teste("Produto Desfaz Estoque", quantidade=3)
        produto_extra = self._produto_teste("Produto Extra Desfaz Estoque", quantidade=4)
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("6.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("2.00"),
            valor_total=Decimal("4.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_extra,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("2.00"),
            valor_total=Decimal("2.00"),
        )
        self.client.post(
            reverse("estoque:venda_revisar_remocao_item", kwargs={"pk": venda.id, "item_id": item.id}),
            secure=True,
            follow=True,
        )
        produto.refresh_from_db()
        self.assertEqual(produto.quantidade, 5)
        remocao = ItemVendaRemovido.objects.get(venda=venda)

        resposta = self.client.post(
            reverse("estoque:venda_desfazer_remocao_item", kwargs={"pk": venda.id, "remocao_id": remocao.id}),
            data={"confirmacao_desfazer": "DESFAZER"},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        produto.refresh_from_db()
        remocao.refresh_from_db()
        self.assertEqual(produto.quantidade, 3)
        self.assertEqual(remocao.status, ItemVendaRemovido.STATUS_REVERTIDO)
        self.assertFalse(remocao.estoque_devolvido)
        self.assertTrue(ItemVenda.objects.filter(venda=venda, produto=produto, quantidade=Decimal("2.000")).exists())

    def test_desfazer_remocao_bloqueia_estoque_insuficiente_sem_alterar_nota(self):
        cliente = Cliente.objects.create(nome="Cliente Desfaz Sem Estoque", ativo=True)
        produto = self._produto_teste("Produto Desfaz Sem Estoque", quantidade=0)
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("0.00"),
        )
        remocao = ItemVendaRemovido.objects.create(
            venda=venda,
            produto=produto,
            produto_nome_snapshot=produto.nome,
            quantidade_snapshot=Decimal("2.000"),
            unidade_snapshot="un",
            preco_unitario_snapshot=Decimal("2.00"),
            valor_total_snapshot=Decimal("4.00"),
            status=ItemVendaRemovido.STATUS_REMOVIDO,
            estoque_devolvido=True,
            estoque_devolvido_em=timezone.now(),
        )

        resposta = self.client.post(
            reverse("estoque:venda_desfazer_remocao_item", kwargs={"pk": venda.id, "remocao_id": remocao.id}),
            data={"confirmacao_desfazer": "DESFAZER"},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        produto.refresh_from_db()
        remocao.refresh_from_db()
        venda.refresh_from_db()
        self.assertEqual(produto.quantidade, 0)
        self.assertEqual(remocao.status, ItemVendaRemovido.STATUS_REMOVIDO)
        self.assertTrue(remocao.estoque_devolvido)
        self.assertEqual(venda.total, Decimal("0.00"))
        self.assertEqual(ItemVenda.objects.count(), 0)
        self.assertContains(resposta, "Estoque insuficiente")

    def test_cancelamento_manual_devolve_estoque_dos_itens_ativos(self):
        cliente = Cliente.objects.create(nome="Cliente Cancela Estoque", ativo=True)
        produto = self._produto_teste("Produto Cancela Estoque", quantidade=2)
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("6.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("3.000"),
            unidade="un",
            preco_unitario=Decimal("2.00"),
            valor_total=Decimal("6.00"),
        )

        resposta = self._post_cancelar_venda(venda)

        self.assertEqual(resposta.status_code, 200)
        produto.refresh_from_db()
        venda.refresh_from_db()
        self.assertEqual(produto.quantidade, 5)
        self.assertTrue(venda.cancelada)
        self.assertTrue(venda.estoque_devolvido_cancelamento)
        self.assertIsNotNone(venda.estoque_devolvido_cancelamento_em)
        self.assertTrue(
            EventoVenda.objects.filter(
                venda=venda,
                tipo_evento="venda_cancelada",
                descricao__icontains="Estoque devolvido dos itens ativos",
            ).exists()
        )

    def test_cancelamento_manual_nao_devolve_item_ja_removido_duas_vezes(self):
        cliente = Cliente.objects.create(nome="Cliente Cancela Item Removido Estoque", ativo=True)
        produto_ativo = self._produto_teste("Produto Ativo Cancelamento Estoque", quantidade=8)
        produto_removido = self._produto_teste("Produto Removido Cancelamento Estoque", quantidade=7)
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("4.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_ativo,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("2.00"),
            valor_total=Decimal("4.00"),
        )
        ItemVendaRemovido.objects.create(
            venda=venda,
            produto=produto_removido,
            produto_nome_snapshot=produto_removido.nome,
            quantidade_snapshot=Decimal("3.000"),
            unidade_snapshot="un",
            preco_unitario_snapshot=Decimal("2.00"),
            valor_total_snapshot=Decimal("6.00"),
            status=ItemVendaRemovido.STATUS_REMOVIDO,
            estoque_devolvido=True,
            estoque_devolvido_em=timezone.now(),
        )

        resposta = self._post_cancelar_venda(venda)

        self.assertEqual(resposta.status_code, 200)
        produto_ativo.refresh_from_db()
        produto_removido.refresh_from_db()
        self.assertEqual(produto_ativo.quantidade, 10)
        self.assertEqual(produto_removido.quantidade, 7)

    def test_cancelamento_manual_com_credito_cliente_cria_credito_e_devolve_estoque(self):
        cliente = Cliente.objects.create(nome="Cliente Credito Estoque", ativo=True)
        produto = self._produto_teste("Produto Credito Estoque", quantidade=5)
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("4.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("2.00"),
            valor_total=Decimal("4.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("4.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )
        recebimento = RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("4.00"),
            forma_pagamento="PIX",
        )

        resposta = self._post_cancelar_venda(venda, destino_financeiro="credito_cliente")

        self.assertEqual(resposta.status_code, 200)
        produto.refresh_from_db()
        venda.refresh_from_db()
        conta.refresh_from_db()
        credito = CreditoCliente.objects.get(cliente=cliente)
        self.assertEqual(produto.quantidade, 7)
        self.assertTrue(venda.cancelada)
        self.assertTrue(venda.estoque_devolvido_cancelamento)
        self.assertEqual(conta.status, ContaReceber.STATUS_CANCELADA)
        self.assertEqual(credito.valor, Decimal("4.00"))
        self.assertEqual(credito.origem_conta_receber, conta)
        self.assertEqual(credito.origem_recebimento, recebimento)

    def test_venda_ja_cancelada_nao_devolve_estoque_novamente(self):
        cliente = Cliente.objects.create(nome="Cliente Cancelada Sem Dobrar Estoque", ativo=True)
        produto = self._produto_teste("Produto Cancelada Sem Dobrar Estoque", quantidade=5)
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("4.00"),
            cancelada=True,
            cancelada_em=timezone.now(),
            motivo_cancelamento="Cancelamento anterior",
            estoque_devolvido_cancelamento=True,
            estoque_devolvido_cancelamento_em=timezone.now(),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("2.00"),
            valor_total=Decimal("4.00"),
        )

        resposta = self._post_cancelar_venda(venda)

        self.assertEqual(resposta.status_code, 200)
        produto.refresh_from_db()
        self.assertEqual(produto.quantidade, 5)

    def test_historico_cliente_produto_retorna_ultima_venda_ativa(self):
        cliente = Cliente.objects.create(nome="Cliente Historico Produto", ativo=True)
        produto = self._produto_teste("Produto Historico Ultima Compra", quantidade=10)
        venda_antiga = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate() - timedelta(days=3),
            tipo_pagamento="A prazo",
            total=Decimal("8.00"),
        )
        ItemVenda.objects.create(
            venda=venda_antiga,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("8.00"),
            valor_total=Decimal("8.00"),
        )
        venda_recente = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate() - timedelta(days=1),
            tipo_pagamento="A prazo",
            total=Decimal("30.00"),
        )
        ItemVenda.objects.create(
            venda=venda_recente,
            produto=produto,
            quantidade=Decimal("3.000"),
            unidade="un",
            preco_unitario=Decimal("10.00"),
            valor_total=Decimal("30.00"),
        )

        resposta = self.client.get(
            reverse("estoque:vendas_cliente_produto_historico"),
            {"cliente_id": cliente.id, "produto_id": produto.id},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["sucesso"])
        self.assertEqual(dados["historico"]["venda_id"], venda_recente.id)
        self.assertEqual(dados["historico"]["data_venda"], venda_recente.data_venda.isoformat())
        self.assertEqual(dados["historico"]["preco_unitario"], "10.00")
        self.assertEqual(dados["historico"]["quantidade"], "3.000")
        self.assertEqual(dados["historico"]["unidade"], "un")

    def test_historico_cliente_produto_ignora_venda_cancelada(self):
        cliente = Cliente.objects.create(nome="Cliente Historico Ignora Cancelada", ativo=True)
        produto = self._produto_teste("Produto Historico Ignora Cancelada", quantidade=10)
        venda_ativa = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate() - timedelta(days=5),
            tipo_pagamento="A prazo",
            total=Decimal("12.00"),
        )
        ItemVenda.objects.create(
            venda=venda_ativa,
            produto=produto,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("6.00"),
            valor_total=Decimal("12.00"),
        )
        venda_cancelada = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("40.00"),
            cancelada=True,
            cancelada_em=timezone.now(),
        )
        ItemVenda.objects.create(
            venda=venda_cancelada,
            produto=produto,
            quantidade=Decimal("4.000"),
            unidade="un",
            preco_unitario=Decimal("10.00"),
            valor_total=Decimal("40.00"),
        )

        resposta = self.client.get(
            reverse("estoque:vendas_cliente_produto_historico"),
            {"cliente_id": cliente.id, "produto_id": produto.id},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json()["historico"]["venda_id"], venda_ativa.id)

    def test_historico_cliente_produto_ignora_outro_cliente(self):
        cliente = Cliente.objects.create(nome="Cliente Historico Alvo", ativo=True)
        outro_cliente = Cliente.objects.create(nome="Cliente Historico Outro", ativo=True)
        produto = self._produto_teste("Produto Historico Outro Cliente", quantidade=10)
        Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate() - timedelta(days=4),
            tipo_pagamento="A prazo",
            total=Decimal("5.00"),
        )
        venda_outro_cliente = Venda.objects.create(
            cliente=outro_cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("20.00"),
        )
        ItemVenda.objects.create(
            venda=venda_outro_cliente,
            produto=produto,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("10.00"),
            valor_total=Decimal("20.00"),
        )

        resposta = self.client.get(
            reverse("estoque:vendas_cliente_produto_historico"),
            {"cliente_id": cliente.id, "produto_id": produto.id},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertIsNone(resposta.json()["historico"])

    def test_historico_cliente_produto_sem_compra_retorna_vazio(self):
        cliente = Cliente.objects.create(nome="Cliente Sem Historico Produto", ativo=True)
        produto = self._produto_teste("Produto Nunca Comprado", quantidade=10)

        resposta = self.client.get(
            reverse("estoque:vendas_cliente_produto_historico"),
            {"cliente_id": cliente.id, "produto_id": produto.id},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json()["sucesso"])
        self.assertIsNone(resposta.json()["historico"])

    def test_tela_vendas_carrega_com_bloco_de_historico(self):
        resposta = self.client.get(reverse("estoque:vendas"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "historicoProdutoCliente")
        self.assertContains(resposta, reverse("estoque:vendas_cliente_produto_historico"))

    def test_venda_com_conta_paga_mostra_aviso_de_quitada_no_detalhe(self):
        cliente = Cliente.objects.create(nome="Cliente Aviso Conta Paga", ativo=True)
        produto = self._produto_teste("Produto Aviso Conta Paga")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("80.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("80.00"),
            valor_total=Decimal("80.00"),
        )
        ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("80.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "VENDA J&Aacute; QUITADA / RECEBIMENTOS REGISTRADOS")
        self.assertContains(resposta, "Esta nota j&aacute; possui pagamento registrado")
        self.assertContains(resposta, "Evite editar produtos ou valores")

    def test_acesso_direto_edicao_de_venda_quitada_e_bloqueado(self):
        cliente = Cliente.objects.create(nome="Cliente Aviso Recebimento", ativo=True)
        produto = self._produto_teste("Produto Aviso Recebimento")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("120.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("120.00"),
            valor_total=Decimal("120.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("120.00"),
            valor_em_aberto=Decimal("70.00"),
            status=ContaReceber.STATUS_PARCIAL,
        )
        RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("50.00"),
            forma_pagamento="PIX",
        )

        resposta = self.client.get(
            reverse("estoque:venda_editar_revisao", kwargs={"pk": venda.id}),
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        redirect_url = urlsplit(resposta.redirect_chain[0][0])
        self.assertEqual(
            f"{redirect_url.path}?{redirect_url.query}",
            reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}) + "?edicao_bloqueada=1",
        )
        self.assertContains(resposta, "Venda quitada: edicao comum bloqueada")
        self.assertNotContains(resposta, "Editar nota - Venda")

    def test_venda_quitada_nao_mostra_botoes_de_edicao_comum_no_detalhe(self):
        cliente = Cliente.objects.create(nome="Cliente Botoes Quitada", ativo=True)
        produto = self._produto_teste("Produto Botoes Quitada")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("85.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("85.00"),
            valor_total=Decimal("85.00"),
        )
        ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("85.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Venda quitada: edicao comum bloqueada")
        self.assertNotContains(resposta, reverse("estoque:venda_editar_revisao", kwargs={"pk": venda.id}))
        self.assertNotContains(resposta, reverse("estoque:venda_editar_cabecalho", kwargs={"pk": venda.id}))
        self.assertNotContains(resposta, reverse("estoque:venda_adicionar_produto_item", kwargs={"pk": venda.id}))
        self.assertContains(resposta, reverse("estoque:venda_cancelar", kwargs={"pk": venda.id}))
        self.assertContains(resposta, reverse("estoque:preparar_whatsapp_venda", kwargs={"pk": venda.id}))
        self.assertContains(resposta, reverse("estoque:venda_whatsapp_pdf", kwargs={"pk": venda.id}))
        self.assertContains(resposta, reverse("estoque:venda_criar_entrega", kwargs={"pk": venda.id}))
        self.assertContains(resposta, 'id="btnImprimir"')
        self.assertEqual(
            self.client.get(reverse("estoque:preparar_whatsapp_venda", kwargs={"pk": venda.id}), secure=True).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(reverse("estoque:venda_whatsapp_pdf", kwargs={"pk": venda.id}), secure=True).status_code,
            200,
        )

    def test_acesso_direto_adicionar_produto_em_venda_quitada_e_bloqueado(self):
        cliente = Cliente.objects.create(nome="Cliente Add Quitada", ativo=True)
        produto = self._produto_teste("Produto Add Quitada")
        produto_novo = self._produto_teste("Produto Add Bloqueado")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("40.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("40.00"),
            valor_total=Decimal("40.00"),
        )
        ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("40.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )

        resposta = self.client.post(
            reverse("estoque:venda_adicionar_produto_item", kwargs={"pk": venda.id}),
            {
                "produto_id": str(produto_novo.id),
                "quantidade": "1",
                "preco_unitario": "10.00",
            },
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        redirect_url = urlsplit(resposta.redirect_chain[0][0])
        self.assertEqual(
            f"{redirect_url.path}?{redirect_url.query}",
            reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}) + "?edicao_bloqueada=1",
        )
        self.assertContains(resposta, "Venda quitada: edicao comum bloqueada")
        self.assertFalse(ItemVenda.objects.filter(venda=venda, produto=produto_novo).exists())

    def test_venda_aberta_continua_permitindo_edicao_normal(self):
        cliente = Cliente.objects.create(nome="Cliente Aberta Editavel", ativo=True)
        produto = self._produto_teste("Produto Aberta Editavel")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("45.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("45.00"),
            valor_total=Decimal("45.00"),
        )
        ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("45.00"),
            valor_em_aberto=Decimal("45.00"),
            status=ContaReceber.STATUS_ABERTA,
        )

        resposta_detalhe = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)
        resposta_edicao = self.client.get(reverse("estoque:venda_editar_revisao", kwargs={"pk": venda.id}), secure=True)

        self.assertEqual(resposta_detalhe.status_code, 200)
        self.assertContains(resposta_detalhe, reverse("estoque:venda_editar_revisao", kwargs={"pk": venda.id}))
        self.assertContains(resposta_detalhe, reverse("estoque:venda_adicionar_produto_item", kwargs={"pk": venda.id}))
        self.assertEqual(resposta_edicao.status_code, 200)
        self.assertContains(resposta_edicao, "Editar nota - Venda")

    def test_cancelamento_de_venda_paga_mostra_aviso_de_recebimentos_preservados(self):
        cliente = Cliente.objects.create(nome="Cliente Cancelar Conta Paga Aviso", ativo=True)
        produto = self._produto_teste("Produto Cancelar Conta Paga Aviso")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("60.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("60.00"),
            valor_total=Decimal("60.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("60.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )
        RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("60.00"),
            forma_pagamento="PIX",
        )

        resposta = self.client.get(reverse("estoque:venda_cancelar", kwargs={"pk": venda.id}), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "VENDA JA QUITADA / RECEBIMENTOS REGISTRADOS")
        self.assertContains(resposta, "Recebimentos nao serao apagados")
        self.assertContains(resposta, "historico financeiro sera preservado")
        self.assertContains(resposta, "Conta quitada / recebimentos registrados")

    def test_venda_com_conta_aberta_sem_recebimento_nao_mostra_aviso_de_quitada(self):
        cliente = Cliente.objects.create(nome="Cliente Sem Aviso Quitada", ativo=True)
        produto = self._produto_teste("Produto Sem Aviso Quitada")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("45.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("45.00"),
            valor_total=Decimal("45.00"),
        )
        ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("45.00"),
            valor_em_aberto=Decimal("45.00"),
            status=ContaReceber.STATUS_ABERTA,
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertNotContains(resposta, "VENDA J&Aacute; QUITADA / RECEBIMENTOS REGISTRADOS")
        self.assertNotContains(resposta, "Esta nota j&aacute; possui pagamento registrado")

    def test_ajuste_item_venda_quitada_cria_snapshot_sem_alterar_dados_existentes(self):
        cliente = Cliente.objects.create(nome="Cliente Ajuste Quitado", ativo=True)
        produto = self._produto_teste("Coca Cola Ajuste Quitado")
        produto.quantidade = 10
        produto.save()
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            operador="Operador Ajuste",
            total=Decimal("25.50"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("3.000"),
            unidade="un",
            preco_unitario=Decimal("8.50"),
            valor_total=Decimal("25.50"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("25.50"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )
        recebimento = RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("25.50"),
            forma_pagamento="PIX",
        )

        ajuste = views.criar_ajuste_item_venda_quitada(
            venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
            observacao="Cliente nao recebeu o item.",
        )

        venda.refresh_from_db()
        item.refresh_from_db()
        conta.refresh_from_db()
        produto.refresh_from_db()
        self.assertTrue(ItemVenda.objects.filter(pk=item.pk, venda=venda).exists())
        self.assertEqual(venda.total, Decimal("25.50"))
        self.assertEqual(conta.valor_original, Decimal("25.50"))
        self.assertEqual(conta.valor_em_aberto, Decimal("0.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_PAGA)
        self.assertTrue(RecebimentoContaReceber.objects.filter(pk=recebimento.pk, conta=conta).exists())
        self.assertEqual(produto.quantidade, 10)
        self.assertEqual(CreditoCliente.objects.count(), 0)
        self.assertEqual(ajuste.venda, venda)
        self.assertEqual(ajuste.item_venda, item)
        self.assertEqual(ajuste.cliente, cliente)
        self.assertEqual(ajuste.produto, produto)
        self.assertEqual(ajuste.produto_nome_snapshot, "Coca Cola Ajuste Quitado")
        self.assertEqual(ajuste.quantidade_snapshot, Decimal("3.000"))
        self.assertEqual(ajuste.unidade_snapshot, "un")
        self.assertEqual(ajuste.preco_unitario_snapshot, Decimal("8.50"))
        self.assertEqual(ajuste.valor_total_snapshot, Decimal("25.50"))
        self.assertEqual(ajuste.diferenca_financeira, Decimal("25.50"))
        self.assertEqual(ajuste.resolucao_financeira, AjusteItemVendaQuitada.RESOLUCAO_NAO_DEFINIDA)
        self.assertEqual(ajuste.status, AjusteItemVendaQuitada.STATUS_PENDENTE)
        self.assertEqual(ajuste.operador, "Operador Ajuste")

    def test_ajuste_item_venda_quitada_nao_permite_item_de_outra_venda(self):
        cliente = Cliente.objects.create(nome="Cliente Item Outra Venda", ativo=True)
        produto = self._produto_teste("Produto Item Outra Venda")
        venda_quitada = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("10.00"),
        )
        outra_venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("10.00"),
        )
        item_outra_venda = ItemVenda.objects.create(
            venda=outra_venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("10.00"),
            valor_total=Decimal("10.00"),
        )

        with self.assertRaisesMessage(ValueError, "O item informado nao pertence a venda do ajuste."):
            views.criar_ajuste_item_venda_quitada(
                venda_quitada,
                item_outra_venda,
                AjusteItemVendaQuitada.MOTIVO_CLIENTE_RECUSOU,
            )

        self.assertEqual(AjusteItemVendaQuitada.objects.count(), 0)

    def test_ajuste_item_venda_quitada_exige_venda_quitada(self):
        cliente = Cliente.objects.create(nome="Cliente Ajuste Venda Aberta", ativo=True)
        produto = self._produto_teste("Produto Ajuste Venda Aberta")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("10.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("10.00"),
            valor_total=Decimal("10.00"),
        )
        ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("10.00"),
            valor_em_aberto=Decimal("10.00"),
            status=ContaReceber.STATUS_ABERTA,
        )

        with self.assertRaisesMessage(ValueError, "Ajuste de item quitado permitido apenas para venda quitada."):
            views.criar_ajuste_item_venda_quitada(
                venda,
                item,
                AjusteItemVendaQuitada.MOTIVO_PRODUTO_FALTOU,
            )

        venda.refresh_from_db()
        self.assertEqual(venda.total, Decimal("10.00"))
        self.assertEqual(AjusteItemVendaQuitada.objects.count(), 0)

    def test_detalhe_venda_quitada_mostra_entrada_para_ajuste_de_item(self):
        cliente = Cliente.objects.create(nome="Cliente Entrada Ajuste", ativo=True)
        produto = self._produto_teste("Produto Entrada Ajuste")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("18.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("18.00"),
            valor_total=Decimal("18.00"),
        )
        ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("18.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)

        self.assertContains(resposta, "Resolver item nao entregue/nao aceito")
        self.assertContains(resposta, reverse("estoque:venda_ajuste_item_quitado", kwargs={"pk": venda.id}))

    def test_detalhe_venda_aberta_nao_mostra_entrada_para_ajuste_de_item(self):
        cliente = Cliente.objects.create(nome="Cliente Sem Entrada Ajuste", ativo=True)
        produto = self._produto_teste("Produto Sem Entrada Ajuste")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("18.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("18.00"),
            valor_total=Decimal("18.00"),
        )
        ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("18.00"),
            valor_em_aberto=Decimal("18.00"),
            status=ContaReceber.STATUS_ABERTA,
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)

        self.assertNotContains(resposta, "Resolver item nao entregue/nao aceito")
        self.assertNotContains(resposta, reverse("estoque:venda_ajuste_item_quitado", kwargs={"pk": venda.id}))

    def test_detalhe_venda_com_ajuste_pendente_mostra_bloco_sem_alterar_dados(self):
        cliente = Cliente.objects.create(nome="Cliente Bloco Ajuste", ativo=True)
        produto = self._produto_teste("Produto Bloco Ajuste")
        produto.quantidade = 9
        produto.save()
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("24.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("12.00"),
            valor_total=Decimal("24.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("24.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )
        recebimento = RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("24.00"),
            forma_pagamento="PIX",
        )
        views.criar_ajuste_item_venda_quitada(
            venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)

        self.assertContains(resposta, "AJUSTE PENDENTE EM VENDA QUITADA")
        self.assertContains(resposta, "Existe item registrado como n&atilde;o entregue/n&atilde;o aceito")
        self.assertContains(resposta, "Produto Bloco Ajuste")
        self.assertContains(resposta, "2.000 un")
        self.assertContains(resposta, "R$ 24.00")
        self.assertContains(resposta, "Motivo: Item nao entregue")
        self.assertContains(resposta, "Resolu&ccedil;&atilde;o: Nao definida")
        self.assertContains(resposta, "Status: Pendente")
        self.assertContains(resposta, "Resolver como cr&eacute;dito do cliente")
        venda.refresh_from_db()
        conta.refresh_from_db()
        produto.refresh_from_db()
        self.assertEqual(venda.total, Decimal("24.00"))
        self.assertEqual(conta.valor_original, Decimal("24.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("0.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_PAGA)
        self.assertTrue(RecebimentoContaReceber.objects.filter(pk=recebimento.pk, conta=conta).exists())
        self.assertEqual(produto.quantidade, 9)

    def test_detalhe_venda_sem_ajuste_pendente_nao_mostra_bloco(self):
        cliente = Cliente.objects.create(nome="Cliente Sem Bloco Ajuste", ativo=True)
        produto = self._produto_teste("Produto Sem Bloco Ajuste")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("24.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("12.00"),
            valor_total=Decimal("24.00"),
        )
        ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("24.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)

        self.assertNotContains(resposta, "AJUSTE PENDENTE EM VENDA QUITADA")
        self.assertNotContains(resposta, "Existe item registrado como n&atilde;o entregue/n&atilde;o aceito")

    def test_detalhe_separa_item_ajustado_da_tabela_principal_sem_alterar_dados(self):
        cliente = Cliente.objects.create(nome="Cliente Separa Ajuste", ativo=True)
        produto_normal = self._produto_teste("Produto Normal Separa")
        produto_ajustado = self._produto_teste("Produto Ajustado Separa")
        produto_ajustado.quantidade = 8
        produto_ajustado.save()
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("70.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_normal,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("40.00"),
            valor_total=Decimal("40.00"),
        )
        item_ajustado = ItemVenda.objects.create(
            venda=venda,
            produto=produto_ajustado,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("15.00"),
            valor_total=Decimal("30.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("70.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )
        recebimento = RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("70.00"),
            forma_pagamento="PIX",
        )
        views.criar_ajuste_item_venda_quitada(
            venda,
            item_ajustado,
            AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)
        conteudo = resposta.content.decode("utf-8")
        tabela_principal = conteudo.split('<table class="tabela-itens-principais">', 1)[1].split("</table>", 1)[0]

        self.assertIn("Produto Normal Separa", tabela_principal)
        self.assertNotIn("Produto Ajustado Separa", tabela_principal)
        self.assertContains(resposta, "Itens n&atilde;o entregues / n&atilde;o aceitos")
        self.assertContains(resposta, "Produto Ajustado Separa")
        self.assertContains(resposta, "2.000")
        self.assertContains(resposta, "R$ 30.00")
        self.assertContains(resposta, "Resolu&ccedil;&atilde;o financeira pendente")
        self.assertContains(resposta, "Total original preservado")
        self.assertContains(resposta, "R$ 70.00")
        self.assertContains(resposta, "Itens n&atilde;o entregues/n&atilde;o aceitos")
        self.assertContains(resposta, "R$ 30.00")
        self.assertContains(resposta, "Total ajustado/entregue")
        self.assertContains(resposta, "R$ 40.00")
        venda.refresh_from_db()
        conta.refresh_from_db()
        produto_ajustado.refresh_from_db()
        self.assertEqual(venda.total, Decimal("70.00"))
        self.assertEqual(conta.valor_original, Decimal("70.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("0.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_PAGA)
        self.assertTrue(RecebimentoContaReceber.objects.filter(pk=recebimento.pk, conta=conta).exists())
        self.assertEqual(produto_ajustado.quantidade, 8)
        self.assertTrue(ItemVenda.objects.filter(pk=item_ajustado.pk, venda=venda).exists())

    def test_detalhe_item_resolvido_com_credito_mostra_credito_na_secao_ajustada(self):
        cliente = Cliente.objects.create(nome="Cliente Ajuste Credito Secao", ativo=True)
        produto = self._produto_teste("Produto Ajuste Credito Secao")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("22.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("22.00"),
            valor_total=Decimal("22.00"),
        )
        ajuste = views.criar_ajuste_item_venda_quitada(
            venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_CLIENTE_RECUSOU,
        )
        ajuste.status = AjusteItemVendaQuitada.STATUS_RESOLVIDO
        ajuste.resolucao_financeira = AjusteItemVendaQuitada.RESOLUCAO_CREDITO_CLIENTE
        ajuste.save(update_fields=["status", "resolucao_financeira", "atualizado_em"])

        resposta = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)
        conteudo = resposta.content.decode("utf-8")
        tabela_principal = conteudo.split('<table class="tabela-itens-principais">', 1)[1].split("</table>", 1)[0]

        self.assertNotIn("Produto Ajuste Credito Secao", tabela_principal)
        self.assertContains(resposta, "Itens n&atilde;o entregues / n&atilde;o aceitos")
        self.assertContains(resposta, "Cr&eacute;dito gerado para o cliente: R$ 22.00")
        self.assertContains(resposta, "Total ajustado/entregue")
        self.assertContains(resposta, "R$ 0.00")

    def test_detalhe_venda_sem_ajuste_mantem_item_na_tabela_principal_e_sem_secao_ajustada(self):
        cliente = Cliente.objects.create(nome="Cliente Layout Sem Ajuste", ativo=True)
        produto = self._produto_teste("Produto Layout Sem Ajuste")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("18.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("18.00"),
            valor_total=Decimal("18.00"),
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)
        conteudo = resposta.content.decode("utf-8")
        tabela_principal = conteudo.split('<table class="tabela-itens-principais">', 1)[1].split("</table>", 1)[0]

        self.assertIn("Produto Layout Sem Ajuste", tabela_principal)
        self.assertNotContains(resposta, "Itens n&atilde;o entregues / n&atilde;o aceitos")
        self.assertNotContains(resposta, "Total ajustado/entregue")

    def test_get_confirmacao_credito_mostra_dados_do_ajuste(self):
        cliente = Cliente.objects.create(nome="Cliente GET Credito", ativo=True)
        produto = self._produto_teste("Produto GET Credito")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("15.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("15.00"),
            valor_total=Decimal("15.00"),
        )
        ajuste = views.criar_ajuste_item_venda_quitada(
            venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_CLIENTE_RECUSOU,
        )

        resposta = self.client.get(
            reverse("estoque:venda_ajuste_item_quitado_credito", kwargs={"pk": venda.id, "ajuste_id": ajuste.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Resolver ajuste como crédito do cliente")
        self.assertContains(resposta, "Venda #")
        self.assertContains(resposta, "Cliente Get Credito")
        self.assertContains(resposta, "Produto Get Credito")
        self.assertContains(resposta, "1.000 un")
        self.assertContains(resposta, "R$ 15.00")
        self.assertContains(resposta, "Item, venda, conta a receber e recebimentos não serão apagados")

    def test_post_credito_sem_confirmacao_forte_nao_gera_credito(self):
        cliente = Cliente.objects.create(nome="Cliente Credito Sem Confirmacao", ativo=True)
        produto = self._produto_teste("Produto Credito Sem Confirmacao")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("15.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("15.00"),
            valor_total=Decimal("15.00"),
        )
        ajuste = views.criar_ajuste_item_venda_quitada(
            venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
        )

        resposta = self.client.post(
            reverse("estoque:venda_ajuste_item_quitado_credito", kwargs={"pk": venda.id, "ajuste_id": ajuste.id}),
            data={"confirmacao_credito": "CRED", "ciencia_credito": "1"},
            secure=True,
            follow=True,
        )

        self.assertContains(resposta, "Digite CREDITO exatamente")
        ajuste.refresh_from_db()
        self.assertEqual(CreditoCliente.objects.count(), 0)
        self.assertEqual(ajuste.status, AjusteItemVendaQuitada.STATUS_PENDENTE)
        self.assertEqual(ajuste.resolucao_financeira, AjusteItemVendaQuitada.RESOLUCAO_NAO_DEFINIDA)

    def test_post_credito_resolve_ajuste_sem_alterar_venda_financeiro_estoque_ou_item(self):
        cliente = Cliente.objects.create(nome="Cliente Credito Resolve", ativo=True)
        produto = self._produto_teste("Produto Credito Resolve")
        produto.quantidade = 11
        produto.save()
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            operador="Operador Credito",
            total=Decimal("28.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("14.00"),
            valor_total=Decimal("28.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("28.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )
        recebimento = RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("28.00"),
            forma_pagamento="PIX",
        )
        ajuste = views.criar_ajuste_item_venda_quitada(
            venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_PRODUTO_FALTOU,
        )

        resposta = self.client.post(
            reverse("estoque:venda_ajuste_item_quitado_credito", kwargs={"pk": venda.id, "ajuste_id": ajuste.id}),
            data={"confirmacao_credito": "CREDITO", "ciencia_credito": "1"},
            secure=True,
        )

        self.assertRedirects(
            resposta,
            reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}) + "?credito_resolvido=1",
            fetch_redirect_response=False,
        )
        credito = CreditoCliente.objects.get(cliente=cliente)
        self.assertEqual(credito.valor, Decimal("28.00"))
        self.assertEqual(credito.origem_conta_receber, conta)
        self.assertIn(f"ajuste #{ajuste.id}", credito.observacao)
        ajuste.refresh_from_db()
        self.assertEqual(ajuste.status, AjusteItemVendaQuitada.STATUS_RESOLVIDO)
        self.assertEqual(ajuste.resolucao_financeira, AjusteItemVendaQuitada.RESOLUCAO_CREDITO_CLIENTE)
        self.assertTrue(
            EventoVenda.objects.filter(
                venda=venda,
                tipo_evento="ajuste_item_quitado_resolvido_credito",
                descricao__icontains="credito do cliente",
            ).exists()
        )
        venda.refresh_from_db()
        conta.refresh_from_db()
        produto.refresh_from_db()
        self.assertTrue(ItemVenda.objects.filter(pk=item.pk, venda=venda).exists())
        self.assertEqual(venda.total, Decimal("28.00"))
        self.assertEqual(conta.valor_original, Decimal("28.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("0.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_PAGA)
        self.assertTrue(RecebimentoContaReceber.objects.filter(pk=recebimento.pk, conta=conta).exists())
        self.assertEqual(produto.quantidade, 11)

    def test_post_credito_nao_permite_resolver_duas_vezes(self):
        cliente = Cliente.objects.create(nome="Cliente Credito Duplo", ativo=True)
        produto = self._produto_teste("Produto Credito Duplo")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("19.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("19.00"),
            valor_total=Decimal("19.00"),
        )
        ajuste = views.criar_ajuste_item_venda_quitada(
            venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
        )
        url = reverse("estoque:venda_ajuste_item_quitado_credito", kwargs={"pk": venda.id, "ajuste_id": ajuste.id})

        primeira = self.client.post(
            url,
            data={"confirmacao_credito": "CREDITO", "ciencia_credito": "1"},
            secure=True,
        )
        segunda = self.client.post(
            url,
            data={"confirmacao_credito": "CREDITO", "ciencia_credito": "1"},
            secure=True,
        )

        self.assertEqual(primeira.status_code, 302)
        self.assertRedirects(
            segunda,
            reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}) + "?credito_bloqueado=1",
            fetch_redirect_response=False,
        )
        self.assertEqual(CreditoCliente.objects.filter(cliente=cliente).count(), 1)

    def test_post_credito_nao_resolve_ajuste_de_outra_venda(self):
        cliente = Cliente.objects.create(nome="Cliente Credito Outra Venda", ativo=True)
        produto = self._produto_teste("Produto Credito Outra Venda")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("12.00"),
        )
        outra_venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("12.00"),
        )
        item = ItemVenda.objects.create(
            venda=outra_venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("12.00"),
            valor_total=Decimal("12.00"),
        )
        ajuste = views.criar_ajuste_item_venda_quitada(
            outra_venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
        )

        resposta = self.client.post(
            reverse("estoque:venda_ajuste_item_quitado_credito", kwargs={"pk": venda.id, "ajuste_id": ajuste.id}),
            data={"confirmacao_credito": "CREDITO", "ciencia_credito": "1"},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 404)
        self.assertEqual(CreditoCliente.objects.count(), 0)
        ajuste.refresh_from_db()
        self.assertEqual(ajuste.status, AjusteItemVendaQuitada.STATUS_PENDENTE)

    def test_tela_ajuste_item_quitado_so_permite_venda_quitada(self):
        cliente = Cliente.objects.create(nome="Cliente GET Ajuste Aberto", ativo=True)
        produto = self._produto_teste("Produto GET Ajuste Aberto")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("18.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("18.00"),
            valor_total=Decimal("18.00"),
        )
        ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("18.00"),
            valor_em_aberto=Decimal("18.00"),
            status=ContaReceber.STATUS_ABERTA,
        )

        resposta = self.client.get(
            reverse("estoque:venda_ajuste_item_quitado", kwargs={"pk": venda.id}),
            secure=True,
        )

        self.assertRedirects(
            resposta,
            reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}) + "?ajuste_bloqueado=1",
            fetch_redirect_response=False,
        )
        self.assertEqual(AjusteItemVendaQuitada.objects.count(), 0)

    def test_post_ajuste_item_quitado_cria_auditoria_evento_sem_alterar_financeiro_estoque_ou_item(self):
        cliente = Cliente.objects.create(nome="Cliente Fluxo Ajuste", ativo=True)
        produto = self._produto_teste("Produto Fluxo Ajuste")
        produto.quantidade = 7
        produto.save()
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            operador="Operador Fluxo",
            total=Decimal("32.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("2.000"),
            unidade="un",
            preco_unitario=Decimal("16.00"),
            valor_total=Decimal("32.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("32.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )
        recebimento = RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("32.00"),
            forma_pagamento="PIX",
        )

        resposta = self.client.post(
            reverse("estoque:venda_ajuste_item_quitado", kwargs={"pk": venda.id}),
            data={
                "item_id": str(item.id),
                "motivo": AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
                "observacao": "Nao saiu na entrega.",
            },
            secure=True,
        )

        self.assertRedirects(
            resposta,
            reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}) + "?ajuste_registrado=1",
            fetch_redirect_response=False,
        )
        ajuste = AjusteItemVendaQuitada.objects.get(venda=venda, item_venda=item)
        self.assertEqual(ajuste.produto_nome_snapshot, "Produto Fluxo Ajuste")
        self.assertEqual(ajuste.quantidade_snapshot, Decimal("2.000"))
        self.assertEqual(ajuste.unidade_snapshot, "un")
        self.assertEqual(ajuste.preco_unitario_snapshot, Decimal("16.00"))
        self.assertEqual(ajuste.valor_total_snapshot, Decimal("32.00"))
        self.assertEqual(ajuste.diferenca_financeira, Decimal("32.00"))
        self.assertEqual(ajuste.resolucao_financeira, AjusteItemVendaQuitada.RESOLUCAO_NAO_DEFINIDA)
        self.assertEqual(ajuste.status, AjusteItemVendaQuitada.STATUS_PENDENTE)
        self.assertTrue(
            EventoVenda.objects.filter(
                venda=venda,
                tipo_evento="ajuste_item_quitado_registrado",
                descricao__icontains="Resolucao financeira pendente",
            ).exists()
        )
        venda.refresh_from_db()
        conta.refresh_from_db()
        produto.refresh_from_db()
        self.assertTrue(ItemVenda.objects.filter(pk=item.pk, venda=venda).exists())
        self.assertEqual(venda.total, Decimal("32.00"))
        self.assertEqual(conta.valor_original, Decimal("32.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("0.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_PAGA)
        self.assertTrue(RecebimentoContaReceber.objects.filter(pk=recebimento.pk, conta=conta).exists())
        self.assertEqual(produto.quantidade, 7)
        self.assertEqual(CreditoCliente.objects.count(), 0)
        remocao = ItemVendaRemovido.objects.get(venda=venda, ajuste_origem=ajuste)
        self.assertEqual(remocao.produto_nome_snapshot, "Produto Fluxo Ajuste")
        self.assertEqual(remocao.valor_total_snapshot, Decimal("32.00"))
        self.assertEqual(remocao.status, ItemVendaRemovido.STATUS_REMOVIDO)

        detalhe = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)
        self.assertContains(detalhe, reverse("estoque:venda_desfazer_remocao_item", kwargs={"pk": venda.id, "remocao_id": remocao.id}))

    def test_post_ajuste_item_quitado_bloqueia_item_de_outra_venda(self):
        cliente = Cliente.objects.create(nome="Cliente Bloqueia Outra Venda", ativo=True)
        produto = self._produto_teste("Produto Bloqueia Outra Venda")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("20.00"),
        )
        outra_venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("20.00"),
        )
        item_outra_venda = ItemVenda.objects.create(
            venda=outra_venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("20.00"),
            valor_total=Decimal("20.00"),
        )

        resposta = self.client.post(
            reverse("estoque:venda_ajuste_item_quitado", kwargs={"pk": venda.id}),
            data={
                "item_id": str(item_outra_venda.id),
                "motivo": AjusteItemVendaQuitada.MOTIVO_CLIENTE_RECUSOU,
                "observacao": "",
            },
            secure=True,
            follow=True,
        )

        self.assertContains(resposta, "Selecione um item valido desta venda.")
        self.assertEqual(AjusteItemVendaQuitada.objects.count(), 0)

    def test_post_ajuste_item_quitado_exige_observacao_para_motivo_outro(self):
        cliente = Cliente.objects.create(nome="Cliente Motivo Outro", ativo=True)
        produto = self._produto_teste("Produto Motivo Outro")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("20.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("20.00"),
            valor_total=Decimal("20.00"),
        )

        resposta = self.client.post(
            reverse("estoque:venda_ajuste_item_quitado", kwargs={"pk": venda.id}),
            data={
                "item_id": str(item.id),
                "motivo": AjusteItemVendaQuitada.MOTIVO_OUTRO,
                "observacao": "",
            },
            secure=True,
            follow=True,
        )

        self.assertContains(resposta, "Informe uma observacao quando o motivo for outro.")
        self.assertEqual(AjusteItemVendaQuitada.objects.count(), 0)

    def test_post_ajuste_item_quitado_evita_duplicidade_pendente_do_mesmo_item(self):
        cliente = Cliente.objects.create(nome="Cliente Duplicidade Ajuste", ativo=True)
        produto = self._produto_teste("Produto Duplicidade Ajuste")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("20.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("20.00"),
            valor_total=Decimal("20.00"),
        )
        views.criar_ajuste_item_venda_quitada(
            venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
        )

        resposta = self.client.post(
            reverse("estoque:venda_ajuste_item_quitado", kwargs={"pk": venda.id}),
            data={
                "item_id": str(item.id),
                "motivo": AjusteItemVendaQuitada.MOTIVO_CLIENTE_RECUSOU,
                "observacao": "",
            },
            secure=True,
            follow=True,
        )

        self.assertContains(
            resposta,
            "Este item já possui ajuste registrado nesta venda. Desfaça ou resolva o ajuste existente antes de registrar outro.",
        )
        self.assertEqual(AjusteItemVendaQuitada.objects.filter(venda=venda, item_venda=item).count(), 1)

    def test_post_ajuste_item_quitado_bloqueia_duplicidade_resolvida_com_credito(self):
        cliente = Cliente.objects.create(nome="Cliente Duplicidade Resolvida", ativo=True)
        produto = self._produto_teste("Produto Duplicidade Resolvida")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("67.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("67.00"),
            valor_total=Decimal("67.00"),
        )
        ajuste = views.criar_ajuste_item_venda_quitada(
            venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
        )
        CreditoCliente.objects.create(
            cliente=cliente,
            valor=Decimal("67.00"),
            observacao="Credito do ajuste.",
        )
        ajuste.status = AjusteItemVendaQuitada.STATUS_RESOLVIDO
        ajuste.resolucao_financeira = AjusteItemVendaQuitada.RESOLUCAO_CREDITO_CLIENTE
        ajuste.save(update_fields=["status", "resolucao_financeira", "atualizado_em"])

        resposta = self.client.post(
            reverse("estoque:venda_ajuste_item_quitado", kwargs={"pk": venda.id}),
            data={
                "item_id": str(item.id),
                "motivo": AjusteItemVendaQuitada.MOTIVO_CLIENTE_RECUSOU,
                "observacao": "",
            },
            secure=True,
            follow=True,
        )

        self.assertContains(
            resposta,
            "Este item já possui ajuste registrado nesta venda. Desfaça ou resolva o ajuste existente antes de registrar outro.",
        )
        self.assertEqual(AjusteItemVendaQuitada.objects.filter(venda=venda, item_venda=item).count(), 1)

    def test_post_ajuste_item_quitado_bloqueia_mesmo_produto_da_mesma_venda(self):
        cliente = Cliente.objects.create(nome="Cliente Duplicidade Produto", ativo=True)
        produto = self._produto_teste("Produto Mesmo Ajuste")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("40.00"),
        )
        item_1 = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("20.00"),
            valor_total=Decimal("20.00"),
        )
        item_2 = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("20.00"),
            valor_total=Decimal("20.00"),
        )
        views.criar_ajuste_item_venda_quitada(
            venda,
            item_1,
            AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
        )

        resposta = self.client.post(
            reverse("estoque:venda_ajuste_item_quitado", kwargs={"pk": venda.id}),
            data={
                "item_id": str(item_2.id),
                "motivo": AjusteItemVendaQuitada.MOTIVO_CLIENTE_RECUSOU,
                "observacao": "",
            },
            secure=True,
            follow=True,
        )

        self.assertContains(
            resposta,
            "Este item já possui ajuste registrado nesta venda. Desfaça ou resolva o ajuste existente antes de registrar outro.",
        )
        self.assertEqual(AjusteItemVendaQuitada.objects.filter(venda=venda).count(), 1)

    def test_detalhe_ajuste_antigo_sem_snapshot_mostra_mensagem_sem_quebrar(self):
        cliente = Cliente.objects.create(nome="Cliente Ajuste Antigo", ativo=True)
        produto = self._produto_teste("Produto Ajuste Antigo")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("25.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("25.00"),
            valor_total=Decimal("25.00"),
        )
        views.criar_ajuste_item_venda_quitada(
            venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)

        self.assertContains(
            resposta,
            "Este ajuste foi criado antes do controle de reversão automática. Não é possível desfazer automaticamente.",
        )
        self.assertNotContains(resposta, "Reverse for &#x27;venda_desfazer_remocao_item&#x27;")

    def test_detalhe_nao_duplica_total_de_ajustes_repetidos_do_mesmo_produto(self):
        cliente = Cliente.objects.create(nome="Cliente Total Duplicado", ativo=True)
        produto = self._produto_teste("Coca Cola Total Duplicado")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("290.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("67.00"),
            valor_total=Decimal("67.00"),
        )
        views.criar_ajuste_item_venda_quitada(
            venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
        )
        views.criar_ajuste_item_venda_quitada(
            venda,
            item,
            AjusteItemVendaQuitada.MOTIVO_CLIENTE_RECUSOU,
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)

        self.assertContains(resposta, "Itens n&atilde;o entregues/n&atilde;o aceitos")
        self.assertContains(resposta, "R$ 67.00")
        self.assertNotContains(resposta, "R$ 134.00")

    def test_remover_item_da_nota_resolve_pendencia_de_entrega_do_item(self):
        cliente = Cliente.objects.create(nome="Cliente Entrega", ativo=True)
        produto_entregue = self._produto_teste("Agua Teste")
        produto_pendente = self._produto_teste("Coca Cola 2L Teste")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("25.00"),
        )
        item_entregue = ItemVenda.objects.create(
            venda=venda,
            produto=produto_entregue,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("10.00"),
            valor_total=Decimal("10.00"),
        )
        item_pendente = ItemVenda.objects.create(
            venda=venda,
            produto=produto_pendente,
            quantidade=Decimal("5.000"),
            unidade="pct",
            preco_unitario=Decimal("3.00"),
            valor_total=Decimal("15.00"),
        )
        rota = EntregaRota.objects.create(data=timezone.localdate(), tipo=EntregaRota.TIPO_UNITARIA)
        item_rota = EntregaRotaItem.objects.create(
            rota=rota,
            venda=venda,
            status=EntregaRotaItem.STATUS_PARCIAL,
            observacao="[checklist_entrega_salva]",
        )
        EntregaChecklistItem.objects.create(
            rota_item=item_rota,
            item_venda=item_entregue,
            carregado=True,
            entregue=True,
        )
        checklist_pendente = EntregaChecklistItem.objects.create(
            rota_item=item_rota,
            item_venda=item_pendente,
            carregado=False,
            entregue=False,
        )

        pendencias_antes = views.listar_pendencias_entrega()
        self.assertTrue(any(pendencia["item_venda_id"] == item_pendente.id for pendencia in pendencias_antes))

        revisao_url = reverse(
            "estoque:revisar_remocao_pendencia_da_nota",
            kwargs={"checklist_id": checklist_pendente.id},
        )
        resposta_revisao = self.client.get(revisao_url, secure=True)
        self.assertContains(resposta_revisao, "Agua Teste")
        self.assertContains(resposta_revisao, "Coca Cola 2L Teste")
        self.assertContains(resposta_revisao, "Sera removido")
        self.assertContains(resposta_revisao, "Permanece na nota")

        resposta = self.client.post(
            revisao_url,
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Item removido da nota e pendencia resolvida com sucesso.")
        self.assertFalse(ItemVenda.objects.filter(pk=item_pendente.id).exists())
        item_rota.refresh_from_db()
        self.assertEqual(item_rota.status, EntregaRotaItem.STATUS_ENTREGUE)
        self.assertTrue(item_rota.entrega_concluida)
        evento_historico = EventoVenda.objects.get(
            venda=venda,
            tipo_evento="pendencia_removida_da_nota",
        )
        self.assertIn("Pendencia resolvida por resolucao de pendencia de entrega", evento_historico.descricao)
        self.assertIn("removido 5 pct de Coca Cola 2L Teste da nota", evento_historico.descricao)
        self.assertIn("motivo: item nao entregue", evento_historico.descricao)
        self.assertIn("Total alterado de R$ 25,00 para R$ 10,00", evento_historico.descricao)
        pendencias_depois = views.listar_pendencias_entrega()
        self.assertFalse(any(pendencia["venda"].id == venda.id for pendencia in pendencias_depois))

        resposta_abertas = self.client.get(reverse("estoque:pendencias_entrega"), secure=True)
        self.assertContains(resposta_abertas, "Ver pendencias resolvidas")
        self.assertNotContains(resposta_abertas, "Coca Cola 2L Teste")
        resposta_resolvidas = self.client.get(
            reverse("estoque:pendencias_entrega"),
            {"status": "resolvidas"},
            secure=True,
        )
        self.assertContains(resposta_resolvidas, "Ver pendencias em aberto")
        self.assertContains(resposta_resolvidas, "Coca Cola 2L Teste")
        self.assertContains(resposta_resolvidas, "Item removido da nota - venda continuou ativa")
        self.assertContains(resposta_resolvidas, "Abrir nota")
        self.assertContains(
            resposta_resolvidas,
            f'{reverse("estoque:venda_detalhe", kwargs={"pk": venda.id})}?entrega={rota.id}&origem=pendencias_resolvidas&evento={evento_historico.id}',
        )

        resposta_nota_resolvida = self.client.get(
            f'{reverse("estoque:venda_detalhe", kwargs={"pk": venda.id})}?entrega={rota.id}&origem=pendencias_resolvidas&evento={evento_historico.id}',
            secure=True,
        )
        self.assertContains(resposta_nota_resolvida, "Pendencia de entrega resolvida.")
        self.assertContains(resposta_nota_resolvida, "5 pct de Coca Cola 2L Teste")
        self.assertContains(resposta_nota_resolvida, "item nao entregue removido da nota")
        self.assertContains(resposta_nota_resolvida, "Total alterado de R$ 25,00 para R$ 10,00")
        self.assertContains(resposta_nota_resolvida, "A venda continuou ativa com os itens restantes.")
        self.assertContains(resposta_nota_resolvida, "Voltar para pendencias resolvidas")
        self.assertNotContains(resposta_nota_resolvida, "Editar nota")
        self.assertNotContains(resposta_nota_resolvida, "Cancelar venda")
        self.assertNotContains(resposta_nota_resolvida, "Imagem / WhatsApp")
        self.assertNotContains(resposta_nota_resolvida, ">PDF</a>")
        self.assertNotContains(resposta_nota_resolvida, "Imprimir</button>")
        self.assertNotContains(resposta_nota_resolvida, "Entrega / checklist")

    def test_remover_item_pela_edicao_da_nota_lista_pendencia_resolvida(self):
        cliente = Cliente.objects.create(nome="Cliente Edicao", ativo=True)
        produto_entregue = self._produto_teste("Agua Edicao Teste")
        produto_pendente = self._produto_teste("Guarana Pendente Teste")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("30.00"),
        )
        item_entregue = ItemVenda.objects.create(
            venda=venda,
            produto=produto_entregue,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("12.00"),
            valor_total=Decimal("12.00"),
        )
        item_pendente = ItemVenda.objects.create(
            venda=venda,
            produto=produto_pendente,
            quantidade=Decimal("6.000"),
            unidade="un",
            preco_unitario=Decimal("3.00"),
            valor_total=Decimal("18.00"),
        )
        rota = EntregaRota.objects.create(data=timezone.localdate(), tipo=EntregaRota.TIPO_UNITARIA)
        item_rota = EntregaRotaItem.objects.create(
            rota=rota,
            venda=venda,
            status=EntregaRotaItem.STATUS_PARCIAL,
            observacao="[checklist_entrega_salva]",
        )
        EntregaChecklistItem.objects.create(
            rota_item=item_rota,
            item_venda=item_entregue,
            carregado=True,
            entregue=True,
        )
        EntregaChecklistItem.objects.create(
            rota_item=item_rota,
            item_venda=item_pendente,
            carregado=False,
            entregue=False,
        )

        pendencias_antes = views.listar_pendencias_entrega()
        self.assertTrue(any(pendencia["item_venda_id"] == item_pendente.id for pendencia in pendencias_antes))

        resposta = self.client.post(
            reverse("estoque:venda_revisar_remocao_item", kwargs={"pk": venda.id, "item_id": item_pendente.id}),
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertFalse(ItemVenda.objects.filter(pk=item_pendente.id).exists())
        pendencias_depois = views.listar_pendencias_entrega()
        self.assertFalse(any(pendencia["venda"].id == venda.id for pendencia in pendencias_depois))

        resolvidas = views.listar_pendencias_resolvidas_entrega()
        resolvidas_item = [
            pendencia
            for pendencia in resolvidas
            if pendencia["venda"].id == venda.id and pendencia["produto"] == "Guarana Pendente Teste"
        ]
        self.assertEqual(len(resolvidas_item), 1)
        self.assertEqual(resolvidas_item[0]["resolucao"], "Resolvida removendo item da nota pela edicao da venda")

        resposta_resolvidas = self.client.get(
            reverse("estoque:pendencias_entrega"),
            {"status": "resolvidas"},
            secure=True,
        )
        self.assertContains(resposta_resolvidas, "Guarana Pendente Teste")
        self.assertContains(resposta_resolvidas, "Item removido da nota - venda continuou ativa")
        self.assertContains(
            resposta_resolvidas,
            f'{reverse("estoque:venda_detalhe", kwargs={"pk": venda.id})}?entrega={rota.id}&origem=pendencias_resolvidas',
        )

    def test_remover_item_por_pendencia_atualiza_conta_receber_aberta(self):
        cliente = Cliente.objects.create(nome="Cliente Pendencia Conta Aberta", ativo=True)
        produto_entregue = self._produto_teste("Agua Pendencia Conta")
        produto_pendente = self._produto_teste("Refri Pendencia Conta")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("100.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_entregue,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("70.00"),
            valor_total=Decimal("70.00"),
        )
        item_pendente = ItemVenda.objects.create(
            venda=venda,
            produto=produto_pendente,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("30.00"),
            valor_total=Decimal("30.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("100.00"),
            valor_em_aberto=Decimal("100.00"),
            status=ContaReceber.STATUS_ABERTA,
        )
        rota = EntregaRota.objects.create(data=timezone.localdate(), tipo=EntregaRota.TIPO_UNITARIA)
        item_rota = EntregaRotaItem.objects.create(
            rota=rota,
            venda=venda,
            status=EntregaRotaItem.STATUS_PARCIAL,
            observacao="[checklist_entrega_salva]",
        )
        checklist_pendente = EntregaChecklistItem.objects.create(
            rota_item=item_rota,
            item_venda=item_pendente,
            carregado=False,
            entregue=False,
        )

        resposta = self.client.post(
            reverse(
                "estoque:revisar_remocao_pendencia_da_nota",
                kwargs={"checklist_id": checklist_pendente.id},
            ),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        venda.refresh_from_db()
        conta.refresh_from_db()
        self.assertEqual(venda.total, Decimal("70.00"))
        self.assertEqual(conta.valor_original, Decimal("70.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("70.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_ABERTA)

    def test_remover_ultimo_item_por_pendencia_cancela_conta_receber_aberta(self):
        cliente = Cliente.objects.create(nome="Cliente Pendencia Conta Cancelada", ativo=True)
        produto_pendente = self._produto_teste("Produto Pendencia Cancela Conta")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("50.00"),
        )
        item_pendente = ItemVenda.objects.create(
            venda=venda,
            produto=produto_pendente,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("50.00"),
            valor_total=Decimal("50.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("50.00"),
            valor_em_aberto=Decimal("50.00"),
            status=ContaReceber.STATUS_ABERTA,
        )
        rota = EntregaRota.objects.create(data=timezone.localdate(), tipo=EntregaRota.TIPO_UNITARIA)
        item_rota = EntregaRotaItem.objects.create(
            rota=rota,
            venda=venda,
            status=EntregaRotaItem.STATUS_PARCIAL,
            observacao="[checklist_entrega_salva]",
        )
        checklist_pendente = EntregaChecklistItem.objects.create(
            rota_item=item_rota,
            item_venda=item_pendente,
            carregado=False,
            entregue=False,
        )

        resposta = self.client.post(
            reverse(
                "estoque:revisar_remocao_pendencia_da_nota",
                kwargs={"checklist_id": checklist_pendente.id},
            ),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        venda.refresh_from_db()
        conta.refresh_from_db()
        self.assertTrue(venda.cancelada)
        self.assertEqual(venda.total, Decimal("0.00"))
        self.assertEqual(conta.valor_original, Decimal("50.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("0.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_CANCELADA)
        self.assertIn("Cancelada por venda nao realizada", conta.observacao)

    def test_remover_ultimo_item_por_pendencia_anula_venda_sem_itens(self):
        cliente = Cliente.objects.create(nome="Cliente Venda Anulada Pendencia", ativo=True)
        produto_pendente = self._produto_teste("Skol 24/600ml Teste")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("50.00"),
        )
        item_pendente = ItemVenda.objects.create(
            venda=venda,
            produto=produto_pendente,
            quantidade=Decimal("2.000"),
            unidade="CX",
            preco_unitario=Decimal("25.00"),
            valor_total=Decimal("50.00"),
        )
        rota = EntregaRota.objects.create(data=timezone.localdate(), tipo=EntregaRota.TIPO_UNITARIA)
        item_rota = EntregaRotaItem.objects.create(
            rota=rota,
            venda=venda,
            status=EntregaRotaItem.STATUS_PARCIAL,
            observacao="[checklist_entrega_salva]",
        )
        EntregaChecklistItem.objects.create(
            rota_item=item_rota,
            item_venda=item_pendente,
            carregado=False,
            entregue=False,
        )

        resposta = self.client.post(
            reverse("estoque:venda_revisar_remocao_item", kwargs={"pk": venda.id, "item_id": item_pendente.id}),
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        venda.refresh_from_db()
        self.assertFalse(ItemVenda.objects.filter(venda=venda).exists())
        self.assertEqual(venda.total, Decimal("0.00"))
        self.assertTrue(venda.cancelada)
        self.assertIsNotNone(venda.cancelada_em)
        self.assertIn("Remocao de pendencia deixou a nota sem itens", venda.motivo_cancelamento)
        self.assertFalse(any(pendencia["venda"].id == venda.id for pendencia in views.listar_pendencias_entrega()))

        resolvidas = views.listar_pendencias_resolvidas_entrega()
        self.assertTrue(
            any(
                pendencia["venda"].id == venda.id
                and pendencia["produto"] == produto_pendente.nome
                and pendencia["resolucao"] == "Resolvida removendo item da nota pela edicao da venda"
                and pendencia["resumo_resolucao"] == "Item removido da nota - venda anulada porque ficou sem itens"
                for pendencia in resolvidas
            )
        )
        resposta_resolvidas = self.client.get(
            reverse("estoque:pendencias_entrega"),
            {"status": "resolvidas"},
            secure=True,
        )
        self.assertContains(resposta_resolvidas, "Item removido da nota - venda anulada porque ficou sem itens")
        self.assertTrue(
            EventoVenda.objects.filter(
                venda=venda,
                tipo_evento="venda_anulada_sem_itens_por_pendencia",
                descricao__icontains="Venda anulada porque a remocao da pendencia deixou a nota sem itens",
            ).exists()
        )

        resposta_ativas = self.client.get(reverse("estoque:consultar_vendas"), secure=True)
        self.assertNotContains(resposta_ativas, "Cliente Venda Anulada Pendencia")
        resposta_canceladas = self.client.get(reverse("estoque:consultar_vendas_canceladas"), secure=True)
        self.assertContains(resposta_canceladas, "Cliente Venda Anulada Pendencia")

    def test_filtros_de_pendencias_resolvidas(self):
        cliente_lincoln = Cliente.objects.create(nome="Lincoln Cliente", ativo=True)
        cliente_camila = Cliente.objects.create(nome="Camila Cliente", ativo=True)
        produto_aberto = self._produto_teste("Produto Aberto Teste")
        produto_coca = self._produto_teste("Coca Filtro Teste")
        produto_fanta = self._produto_teste("Fanta Filtro Teste")
        venda_lincoln = Venda.objects.create(
            cliente=cliente_lincoln,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("10.00"),
        )
        venda_camila = Venda.objects.create(
            cliente=cliente_camila,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("12.00"),
        )
        venda_aberta = Venda.objects.create(
            cliente=cliente_lincoln,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("5.00"),
        )
        item_aberto = ItemVenda.objects.create(
            venda=venda_aberta,
            produto=produto_aberto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("5.00"),
            valor_total=Decimal("5.00"),
        )
        rota_aberta = EntregaRota.objects.create(data=timezone.localdate(), tipo=EntregaRota.TIPO_UNITARIA)
        item_rota_aberto = EntregaRotaItem.objects.create(
            rota=rota_aberta,
            venda=venda_aberta,
            status=EntregaRotaItem.STATUS_PARCIAL,
            observacao="[checklist_entrega_salva]",
        )
        EntregaChecklistItem.objects.create(
            rota_item=item_rota_aberto,
            item_venda=item_aberto,
            carregado=False,
            entregue=False,
        )

        rota_lincoln = EntregaRota.objects.create(data=timezone.localdate(), tipo=EntregaRota.TIPO_UNITARIA)
        rota_camila = EntregaRota.objects.create(data=timezone.localdate() - timedelta(days=2), tipo=EntregaRota.TIPO_UNITARIA)
        evento_lincoln = EventoVenda.objects.create(
            venda=venda_lincoln,
            tipo_evento="pendencia_removida_da_nota",
            descricao=(
                f"Pendencia da rota #{rota_lincoln.id} resolvida por remocao da nota. "
                "Item removido: Coca Filtro Teste - 2.000 un (R$ 10.00). Novo total: R$ 0.00."
            ),
            canal="sistema",
        )
        evento_camila = EventoVenda.objects.create(
            venda=venda_camila,
            tipo_evento="pendencia_removida_da_nota",
            descricao=(
                f"Pendencia da rota #{rota_camila.id} resolvida pela edicao da nota. "
                "Item removido: Fanta Filtro Teste - 3.000 un (R$ 12.00). Novo total: R$ 0.00."
            ),
            canal="sistema",
        )
        data_lincoln = timezone.make_aware(datetime(2026, 5, 10, 9, 0))
        data_camila = timezone.make_aware(datetime(2026, 5, 12, 9, 0))
        EventoVenda.objects.filter(pk=evento_lincoln.pk).update(criado_em=data_lincoln)
        EventoVenda.objects.filter(pk=evento_camila.pk).update(criado_em=data_camila)

        resposta_sem_filtro = self.client.get(
            reverse("estoque:pendencias_entrega"),
            {"status": "resolvidas"},
            secure=True,
        )
        self.assertContains(resposta_sem_filtro, "Coca Filtro Teste")
        self.assertContains(resposta_sem_filtro, "Fanta Filtro Teste")
        self.assertContains(resposta_sem_filtro, "Limpar")

        resposta_venda = self.client.get(
            reverse("estoque:pendencias_entrega"),
            {"status": "resolvidas", "venda": str(venda_lincoln.id)},
            secure=True,
        )
        self.assertContains(resposta_venda, "Coca Filtro Teste")
        self.assertNotContains(resposta_venda, "Fanta Filtro Teste")

        resposta_cliente = self.client.get(
            reverse("estoque:pendencias_entrega"),
            {"status": "resolvidas", "cliente": "Lincoln"},
            secure=True,
        )
        self.assertContains(resposta_cliente, "Coca Filtro Teste")
        self.assertNotContains(resposta_cliente, "Fanta Filtro Teste")

        resposta_produto = self.client.get(
            reverse("estoque:pendencias_entrega"),
            {"status": "resolvidas", "produto": "Fanta"},
            secure=True,
        )
        self.assertContains(resposta_produto, "Fanta Filtro Teste")
        self.assertNotContains(resposta_produto, "Coca Filtro Teste")

        resposta_data = self.client.get(
            reverse("estoque:pendencias_entrega"),
            {"status": "resolvidas", "data_inicial": "2026-05-10", "data_final": "2026-05-10"},
            secure=True,
        )
        self.assertContains(resposta_data, "Coca Filtro Teste")
        self.assertNotContains(resposta_data, "Fanta Filtro Teste")

        resposta_abertas = self.client.get(
            reverse("estoque:pendencias_entrega"),
            {"cliente": "Camila", "produto": "Fanta"},
            secure=True,
        )
        self.assertContains(resposta_abertas, "Produto Aberto Teste")
        self.assertContains(resposta_abertas, "Ver pendencias resolvidas")
        self.assertNotContains(resposta_abertas, "Coca Filtro Teste")

    def test_remover_item_da_nota_atualiza_conta_receber_aberta(self):
        cliente = Cliente.objects.create(nome="Cliente Conta Aberta", ativo=True)
        produto_base = self._produto_teste("Produto Base Conta")
        produto_removido = self._produto_teste("Produto Removido Conta")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("100.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_base,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("75.00"),
            valor_total=Decimal("75.00"),
        )
        item_removido = ItemVenda.objects.create(
            venda=venda,
            produto=produto_removido,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("25.00"),
            valor_total=Decimal("25.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("100.00"),
            valor_em_aberto=Decimal("100.00"),
            status=ContaReceber.STATUS_ABERTA,
        )

        resposta = self.client.post(
            reverse("estoque:venda_revisar_remocao_item", kwargs={"pk": venda.id, "item_id": item_removido.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        venda.refresh_from_db()
        conta.refresh_from_db()
        self.assertEqual(venda.total, Decimal("75.00"))
        self.assertEqual(conta.valor_original, Decimal("75.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("75.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_ABERTA)

    def test_remover_item_da_nota_atualiza_conta_receber_parcial_preservando_recebido(self):
        cliente = Cliente.objects.create(nome="Cliente Conta Parcial", ativo=True)
        produto_base = self._produto_teste("Produto Base Parcial")
        produto_removido = self._produto_teste("Produto Removido Parcial")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("100.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_base,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("80.00"),
            valor_total=Decimal("80.00"),
        )
        item_removido = ItemVenda.objects.create(
            venda=venda,
            produto=produto_removido,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("20.00"),
            valor_total=Decimal("20.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("100.00"),
            valor_em_aberto=Decimal("40.00"),
            status=ContaReceber.STATUS_PARCIAL,
        )
        recebimento = RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("60.00"),
            forma_pagamento="PIX",
        )

        resposta = self.client.post(
            reverse("estoque:venda_revisar_remocao_item", kwargs={"pk": venda.id, "item_id": item_removido.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        venda.refresh_from_db()
        conta.refresh_from_db()
        recebimento.refresh_from_db()
        self.assertEqual(venda.total, Decimal("80.00"))
        self.assertEqual(recebimento.valor, Decimal("60.00"))
        self.assertEqual(conta.valor_original, Decimal("80.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("20.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_PARCIAL)

    def test_adicionar_item_na_nota_atualiza_conta_receber_parcial_preservando_recebido(self):
        cliente = Cliente.objects.create(nome="Cliente Conta Aumento", ativo=True)
        produto_base = self._produto_teste("Produto Base Aumento")
        produto_novo = self._produto_teste(
            "Produto Novo Aumento",
            quantidade=Decimal("5.000"),
        )
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("100.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_base,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("100.00"),
            valor_total=Decimal("100.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("100.00"),
            valor_em_aberto=Decimal("40.00"),
            status=ContaReceber.STATUS_PARCIAL,
        )
        RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("60.00"),
            forma_pagamento="PIX",
        )

        resposta = self.client.post(
            reverse("estoque:venda_adicionar_produto_item", kwargs={"pk": venda.id}),
            {
                "produto_id": str(produto_novo.id),
                "quantidade": "1",
                "preco_unitario": "30.00",
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        venda.refresh_from_db()
        conta.refresh_from_db()
        self.assertEqual(venda.total, Decimal("130.00"))
        self.assertEqual(conta.valor_original, Decimal("130.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("70.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_PARCIAL)

    def test_remover_item_de_venda_a_vista_sem_conta_receber_nao_quebra(self):
        cliente = Cliente.objects.create(nome="Cliente Vista Sem Conta", ativo=True)
        produto_base = self._produto_teste("Produto Vista Base")
        produto_removido = self._produto_teste("Produto Vista Removido")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            total=Decimal("50.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_base,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("30.00"),
            valor_total=Decimal("30.00"),
        )
        item_removido = ItemVenda.objects.create(
            venda=venda,
            produto=produto_removido,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("20.00"),
            valor_total=Decimal("20.00"),
        )

        resposta = self.client.post(
            reverse("estoque:venda_revisar_remocao_item", kwargs={"pk": venda.id, "item_id": item_removido.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        venda.refresh_from_db()
        self.assertEqual(venda.total, Decimal("30.00"))
        self.assertFalse(ContaReceber.objects.filter(venda=venda).exists())

    def test_edicao_com_total_menor_que_recebido_mantem_conta_zerada(self):
        cliente = Cliente.objects.create(nome="Cliente Conta Quitada", ativo=True)
        produto_base = self._produto_teste("Produto Quitado Base")
        produto_removido = self._produto_teste("Produto Quitado Removido")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("100.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_base,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("80.00"),
            valor_total=Decimal("80.00"),
        )
        item_removido = ItemVenda.objects.create(
            venda=venda,
            produto=produto_removido,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("20.00"),
            valor_total=Decimal("20.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("100.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )
        RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("100.00"),
            forma_pagamento="PIX",
        )

        resposta = self.client.post(
            reverse("estoque:venda_revisar_remocao_item", kwargs={"pk": venda.id, "item_id": item_removido.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        venda.refresh_from_db()
        conta.refresh_from_db()
        self.assertEqual(venda.total, Decimal("80.00"))
        self.assertEqual(conta.valor_original, Decimal("80.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("0.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_PAGA)

    def test_desfazer_remocao_sem_credito_recoloca_item_e_sincroniza_conta(self):
        cliente = Cliente.objects.create(nome="Cliente Desfazer Simples", ativo=True)
        produto_base = self._produto_teste("Produto Desfazer Base")
        produto_removido = self._produto_teste("Produto Desfazer Removido")
        produto_removido.quantidade = 10
        produto_removido.save()
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("100.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_base,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("70.00"),
            valor_total=Decimal("70.00"),
        )
        item_removido_original = ItemVenda.objects.create(
            venda=venda,
            produto=produto_removido,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("30.00"),
            valor_total=Decimal("30.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("100.00"),
            valor_em_aberto=Decimal("100.00"),
            status=ContaReceber.STATUS_ABERTA,
        )

        self.client.post(
            reverse("estoque:venda_revisar_remocao_item", kwargs={"pk": venda.id, "item_id": item_removido_original.id}),
            secure=True,
        )
        remocao = ItemVendaRemovido.objects.get(venda=venda)
        resposta = self.client.post(
            reverse("estoque:venda_desfazer_remocao_item", kwargs={"pk": venda.id, "remocao_id": remocao.id}),
            {"confirmacao_desfazer": "DESFAZER"},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        venda.refresh_from_db()
        conta.refresh_from_db()
        produto_removido.refresh_from_db()
        remocao.refresh_from_db()
        self.assertTrue(ItemVenda.objects.filter(venda=venda, produto=produto_removido, valor_total=Decimal("30.00")).exists())
        self.assertEqual(venda.total, Decimal("100.00"))
        self.assertEqual(conta.valor_original, Decimal("100.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("100.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_ABERTA)
        self.assertEqual(produto_removido.quantidade, 10)
        self.assertEqual(remocao.status, ItemVendaRemovido.STATUS_REVERTIDO)
        self.assertTrue(EventoVenda.objects.filter(venda=venda, tipo_evento="remocao_item_desfeita").exists())

    def test_desfazer_remocao_com_credito_disponivel_cancela_credito_e_recoloca_item(self):
        cliente = Cliente.objects.create(nome="Cliente Desfazer Credito", ativo=True)
        produto_base = self._produto_teste("Produto Credito Base")
        produto_removido = self._produto_teste("Produto Credito Removido")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("100.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_base,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("70.00"),
            valor_total=Decimal("70.00"),
        )
        item_removido_original = ItemVenda.objects.create(
            venda=venda,
            produto=produto_removido,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("30.00"),
            valor_total=Decimal("30.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("100.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )
        RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("100.00"),
            forma_pagamento="PIX",
        )

        self.client.post(
            reverse("estoque:venda_revisar_remocao_item", kwargs={"pk": venda.id, "item_id": item_removido_original.id}),
            secure=True,
        )
        remocao = ItemVendaRemovido.objects.get(venda=venda)
        credito = CreditoCliente.objects.create(
            cliente=cliente,
            valor=Decimal("30.00"),
            origem_conta_receber=conta,
            observacao=f"Credito gerado pela remocao #{remocao.id}.",
        )
        remocao.credito_gerado = credito
        remocao.save(update_fields=["credito_gerado"])

        resposta = self.client.post(
            reverse("estoque:venda_desfazer_remocao_item", kwargs={"pk": venda.id, "remocao_id": remocao.id}),
            {"confirmacao_desfazer": "DESFAZER"},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        venda.refresh_from_db()
        conta.refresh_from_db()
        remocao.refresh_from_db()
        credito_total = CreditoCliente.objects.filter(cliente=cliente).aggregate(total=Sum("valor")).get("total")
        self.assertEqual(credito_total, Decimal("0.00"))
        self.assertTrue(ItemVenda.objects.filter(venda=venda, produto=produto_removido, valor_total=Decimal("30.00")).exists())
        self.assertEqual(venda.total, Decimal("100.00"))
        self.assertEqual(conta.valor_original, Decimal("100.00"))
        self.assertEqual(conta.valor_em_aberto, Decimal("0.00"))
        self.assertEqual(conta.status, ContaReceber.STATUS_PAGA)
        self.assertEqual(remocao.status, ItemVendaRemovido.STATUS_REVERTIDO)

    def test_desfazer_remocao_bloqueia_quando_credito_ja_foi_usado(self):
        cliente = Cliente.objects.create(nome="Cliente Credito Usado", ativo=True)
        produto_base = self._produto_teste("Produto Usado Base")
        produto_removido = self._produto_teste("Produto Usado Removido")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=Decimal("100.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_base,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("70.00"),
            valor_total=Decimal("70.00"),
        )
        item_removido_original = ItemVenda.objects.create(
            venda=venda,
            produto=produto_removido,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("30.00"),
            valor_total=Decimal("30.00"),
        )
        conta = ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=Decimal("100.00"),
            valor_em_aberto=Decimal("0.00"),
            status=ContaReceber.STATUS_PAGA,
        )

        self.client.post(
            reverse("estoque:venda_revisar_remocao_item", kwargs={"pk": venda.id, "item_id": item_removido_original.id}),
            secure=True,
        )
        remocao = ItemVendaRemovido.objects.get(venda=venda)
        credito = CreditoCliente.objects.create(
            cliente=cliente,
            valor=Decimal("30.00"),
            origem_conta_receber=conta,
            observacao=f"Credito gerado pela remocao #{remocao.id}.",
        )
        CreditoCliente.objects.create(
            cliente=cliente,
            valor=Decimal("-30.00"),
            origem_conta_receber=conta,
            observacao="Credito usado em abatimento.",
        )
        remocao.credito_gerado = credito
        remocao.save(update_fields=["credito_gerado"])

        detalhe = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)
        self.assertContains(detalhe, "Este crédito já foi usado e não pode ser desfeito automaticamente nesta etapa.")
        self.assertNotContains(detalhe, reverse("estoque:venda_desfazer_remocao_item", kwargs={"pk": venda.id, "remocao_id": remocao.id}))

        resposta = self.client.post(
            reverse("estoque:venda_desfazer_remocao_item", kwargs={"pk": venda.id, "remocao_id": remocao.id}),
            {"confirmacao_desfazer": "DESFAZER"},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Este crédito já foi usado e não pode ser desfeito automaticamente nesta etapa.")
        remocao.refresh_from_db()
        self.assertEqual(remocao.status, ItemVendaRemovido.STATUS_REMOVIDO)
        self.assertFalse(ItemVenda.objects.filter(venda=venda, produto=produto_removido).exists())

    def test_desfazer_ajuste_novo_com_credito_cancela_credito_sem_duplicar_item(self):
        cliente = Cliente.objects.create(nome="Cliente Desfazer Ajuste", ativo=True)
        produto = self._produto_teste("Produto Desfazer Ajuste")
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            operador="Operador Ajuste",
            total=Decimal("67.00"),
        )
        item = ItemVenda.objects.create(
            venda=venda,
            produto=produto,
            quantidade=Decimal("1.000"),
            unidade="un",
            preco_unitario=Decimal("67.00"),
            valor_total=Decimal("67.00"),
        )

        self.client.post(
            reverse("estoque:venda_ajuste_item_quitado", kwargs={"pk": venda.id}),
            data={
                "item_id": str(item.id),
                "motivo": AjusteItemVendaQuitada.MOTIVO_ITEM_NAO_ENTREGUE,
                "observacao": "Nao entregue.",
            },
            secure=True,
        )
        ajuste = AjusteItemVendaQuitada.objects.get(venda=venda, item_venda=item)
        remocao = ItemVendaRemovido.objects.get(venda=venda, ajuste_origem=ajuste)

        detalhe = self.client.get(reverse("estoque:venda_detalhe", kwargs={"pk": venda.id}), secure=True)
        self.assertContains(
            detalhe,
            reverse("estoque:venda_desfazer_remocao_item", kwargs={"pk": venda.id, "remocao_id": remocao.id}),
        )

        self.client.post(
            reverse("estoque:venda_ajuste_item_quitado_credito", kwargs={"pk": venda.id, "ajuste_id": ajuste.id}),
            data={"confirmacao_credito": "CREDITO", "ciencia_credito": "1"},
            secure=True,
        )
        remocao.refresh_from_db()
        self.assertIsNotNone(remocao.credito_gerado_id)

        resposta = self.client.post(
            reverse("estoque:venda_desfazer_remocao_item", kwargs={"pk": venda.id, "remocao_id": remocao.id}),
            {"confirmacao_desfazer": "DESFAZER"},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        venda.refresh_from_db()
        ajuste.refresh_from_db()
        remocao.refresh_from_db()
        credito_total = CreditoCliente.objects.filter(cliente=cliente).aggregate(total=Sum("valor")).get("total")
        self.assertEqual(credito_total, Decimal("0.00"))
        self.assertEqual(ajuste.status, AjusteItemVendaQuitada.STATUS_CANCELADO)
        self.assertEqual(remocao.status, ItemVendaRemovido.STATUS_REVERTIDO)
        self.assertEqual(venda.total, Decimal("67.00"))
        self.assertEqual(ItemVenda.objects.filter(venda=venda, produto=produto).count(), 1)
        self.assertTrue(EventoVenda.objects.filter(venda=venda, tipo_evento="remocao_item_desfeita").exists())

    def test_consulta_vendas_por_numero_ignora_datas_preenchidas(self):
        cliente = Cliente.objects.create(nome="Lincoln Neiva", ativo=True)
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=datetime(2026, 5, 11).date(),
            tipo_pagamento="A prazo",
            total=Decimal("1043.70"),
        )
        hoje = timezone.localdate().isoformat()

        resposta = self.client.get(
            reverse("estoque:consultar_vendas"),
            {
                "data_inicial": hoje,
                "data_final": hoje,
                "cliente": "",
                "numero": str(venda.id),
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, f"#{venda.id}")
        self.assertContains(resposta, "Lincoln Neiva")
        self.assertContains(resposta, "11/05/2026")

    def test_form_permite_cliente_opcional_e_status_pendente(self):
        form = PixRecebidoForm(data={
            "cliente": "",
            "nome_pagador": "Maria Silva",
            "valor": "50.00",
            "data_pagamento": timezone.localtime(timezone.now()).strftime("%Y-%m-%dT%H:%M"),
            "observacao": "Pix recebido no caixa",
            "status": PixRecebido.STATUS_PENDENTE,
        })

        self.assertTrue(form.is_valid(), form.errors)
        pix = form.save()
        self.assertIsNone(pix.cliente)
        self.assertEqual(pix.status, PixRecebido.STATUS_PENDENTE)

    def test_central_pix_registra_lista_e_nao_altera_fluxo_financeiro(self):
        cliente = Cliente.objects.create(nome="Cliente Pix", ativo=True)
        url = reverse("estoque:central_pix")
        contagens_antes = {
            "contas": ContaReceber.objects.count(),
            "recebimentos": RecebimentoContaReceber.objects.count(),
            "creditos": CreditoCliente.objects.count(),
            "vendas": Venda.objects.count(),
        }

        resposta_get = self.client.get(url, secure=True)
        self.assertEqual(resposta_get.status_code, 200)
        self.assertContains(resposta_get, "Central de Pix")

        resposta_post = self.client.post(url, data={
            "cliente": cliente.id,
            "nome_pagador": "Filho do cliente",
            "valor": "123.45",
            "data_pagamento": timezone.localtime(timezone.now()).strftime("%Y-%m-%dT%H:%M"),
            "instituicao_pix": "PicPay",
            "observacao": "Conferir manualmente depois",
            "status": PixRecebido.STATUS_PENDENTE,
        }, secure=True, follow=True)

        self.assertEqual(resposta_post.status_code, 200)
        self.assertContains(resposta_post, "Filho do cliente")
        self.assertContains(resposta_post, "PicPay")
        pix = PixRecebido.objects.get(nome_pagador="Filho do cliente")
        self.assertEqual(pix.cliente, cliente)
        self.assertEqual(pix.status, PixRecebido.STATUS_PENDENTE)
        self.assertEqual(pix.instituicao_pix, "PicPay")

        contagens_depois = {
            "contas": ContaReceber.objects.count(),
            "recebimentos": RecebimentoContaReceber.objects.count(),
            "creditos": CreditoCliente.objects.count(),
            "vendas": Venda.objects.count(),
        }
        self.assertEqual(contagens_depois, contagens_antes)

    def test_central_pix_pix_novo_aparece_com_linha_destacada(self):
        PixRecebido.objects.create(
            nome_pagador="Pix novo visual",
            valor="25.00",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.get(reverse("estoque:central_pix"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Pix novo visual")
        self.assertContains(resposta, "pix-row-novo")
        self.assertNotContains(resposta, "pix-novo-badge")
        self.assertNotContains(resposta, ">Novo<")
        pix = PixRecebido.objects.get(nome_pagador="Pix novo visual")
        self.assertIsNone(pix.visualizado_em)

    def test_central_pix_lista_mostra_id_e_busca_por_numero_pix(self):
        alvo = PixRecebido.objects.create(
            nome_pagador="Pix alvo busca id",
            valor="25.00",
            status=PixRecebido.STATUS_BAIXADO,
        )
        PixRecebido.objects.create(
            nome_pagador="Pix fora da busca id",
            valor="35.00",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.get(
            reverse("estoque:central_pix"),
            {"q": str(alvo.id)},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Nº Pix")
        self.assertContains(resposta, f"#{alvo.id}")
        self.assertContains(resposta, "Buscar por nº do Pix, pagador, cliente, instituicao, status ou data...")
        self.assertContains(resposta, "Pix alvo busca id")
        self.assertContains(resposta, "pix-status baixado")
        self.assertNotContains(resposta, "Pix fora da busca id")

    def test_central_pix_detalhe_marca_pix_como_visualizado(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix abre detalhe",
            valor="35.00",
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
        )

        resposta = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        pix.refresh_from_db()
        self.assertIsNotNone(pix.visualizado_em)

        resposta_lista = self.client.get(reverse("estoque:central_pix"), secure=True)
        self.assertContains(resposta_lista, "Pix abre detalhe")
        self.assertNotContains(resposta_lista, '<tr class="pix-row pix-row-novo">', html=True)

    def test_central_pix_marcar_visualizado_nao_altera_status_financeiro(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix status preservado",
            valor="45.00",
            status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
        )

        self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )

        pix.refresh_from_db()
        self.assertIsNotNone(pix.visualizado_em)
        self.assertEqual(pix.status, PixRecebido.STATUS_POSSIVEL_DUPLICADO)

    def test_central_pix_detalhe_baixado_mostra_id_status_e_sem_botao_excluir(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix detalhe baixado",
            valor="99.00",
            status=PixRecebido.STATUS_BAIXADO,
        )

        resposta = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, f"Detalhe do Pix baixado - Pix #{pix.id}")
        self.assertContains(resposta, "ID do Pix")
        self.assertContains(resposta, f"#{pix.id}")
        self.assertContains(resposta, "Este Pix ja foi baixado/usado financeiramente e nao pode ser excluido.")
        self.assertContains(resposta, "pix-detail-status-baixado")
        self.assertNotContains(resposta, ">Ignorar Pix sem baixa</button>")
        self.assertNotContains(resposta, "Excluir Pix enviado errado")
        self.assertNotContains(resposta, "Se voltar sem baixar, este Pix continuara pendente na Central de Pix.")

    def test_central_pix_nao_permite_ignorar_pix_baixado_por_post_direto(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix baixado protegido",
            valor="88.00",
            status=PixRecebido.STATUS_BAIXADO,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            {"acao": "ignorar"},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        pix.refresh_from_db()
        self.assertEqual(pix.status, PixRecebido.STATUS_BAIXADO)
        self.assertContains(resposta, "Pix baixado/usado financeiramente nao pode ser ignorado.")

    def test_central_pix_pendente_pode_ser_excluido_com_confirmacao_forte(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Excluir", ativo=True)
        pix = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Arquivo enviado errado",
            valor="12.34",
            status=PixRecebido.STATUS_PENDENTE,
        )
        url = reverse("estoque:central_pix_excluir", kwargs={"pix_id": pix.id})

        resposta_get = self.client.get(url, secure=True)
        self.assertEqual(resposta_get.status_code, 200)
        self.assertContains(resposta_get, f"#{pix.id}")
        self.assertContains(resposta_get, "Cliente Pix Excluir")
        self.assertContains(resposta_get, "Arquivo enviado errado")

        resposta_post = self.client.post(
            url,
            {"confirmacao": "EXCLUIR"},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta_post.status_code, 200)
        self.assertFalse(PixRecebido.objects.filter(pk=pix.id).exists())
        self.assertContains(resposta_post, f"Pix #{pix.id} excluido com sucesso.")

    def test_central_pix_nao_exclui_se_confirmacao_estiver_errada(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Confirmacao errada",
            valor="22.00",
            status=PixRecebido.STATUS_PENDENTE,
        )
        url = reverse("estoque:central_pix_excluir", kwargs={"pix_id": pix.id})

        resposta = self.client.post(
            url,
            {"confirmacao": "excluir"},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(PixRecebido.objects.filter(pk=pix.id).exists())
        self.assertContains(resposta, "Digite exatamente EXCLUIR para confirmar a exclusao do Pix.")

    def test_central_pix_baixado_nao_pode_ser_excluido(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix ja baixado",
            valor="33.00",
            status=PixRecebido.STATUS_BAIXADO,
        )
        url = reverse("estoque:central_pix_excluir", kwargs={"pix_id": pix.id})

        resposta_get = self.client.get(url, secure=True, follow=True)
        self.assertEqual(resposta_get.status_code, 200)
        self.assertTrue(PixRecebido.objects.filter(pk=pix.id).exists())
        self.assertContains(resposta_get, "Nao e possivel excluir este Pix porque ele ja tem vinculo financeiro/baixa.")

        resposta_post = self.client.post(
            url,
            {"confirmacao": "EXCLUIR"},
            secure=True,
            follow=True,
        )
        self.assertEqual(resposta_post.status_code, 200)
        self.assertTrue(PixRecebido.objects.filter(pk=pix.id).exists())

    def test_central_pix_bloqueia_salvar_sem_cliente_confirmado(self):
        url = reverse("estoque:central_pix")
        contagens_antes = {
            "contas": ContaReceber.objects.count(),
            "recebimentos": RecebimentoContaReceber.objects.count(),
            "creditos": CreditoCliente.objects.count(),
            "vendas": Venda.objects.count(),
        }
        comprovante = SimpleUploadedFile(
            "mercado-pago.jpg",
            b"comprovante pix mercado pago",
            content_type="image/jpeg",
        )

        resposta = self.client.post(url, data={
            "cliente": "",
            "nome_pagador": "",
            "valor": "645.00",
            "data_pagamento": "2026-05-23T18:55",
            "instituicao_pix": "Mercado Pago",
            "observacao": "",
            "status": PixRecebido.STATUS_PENDENTE,
            "comprovante": comprovante,
        }, secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Confirme o cliente antes de salvar o Pix.")
        self.assertContains(resposta, "Confirme um cliente antes de salvar o Pix.")
        self.assertContains(resposta, 'placeholder="Sem cliente identificado"')
        self.assertEqual(PixRecebido.objects.count(), 0)

        contagens_depois = {
            "contas": ContaReceber.objects.count(),
            "recebimentos": RecebimentoContaReceber.objects.count(),
            "creditos": CreditoCliente.objects.count(),
            "vendas": Venda.objects.count(),
        }
        self.assertEqual(contagens_depois, contagens_antes)

    def test_central_pix_usa_next_interno_e_ignora_next_externo(self):
        url = reverse("estoque:central_pix")

        resposta_interna = self.client.get(f"{url}?next=/vendas/", secure=True)
        self.assertContains(resposta_interna, 'href="/vendas/"')

        resposta_externa = self.client.get(f"{url}?next=https://example.com/", secure=True)
        self.assertContains(resposta_externa, f'href="{reverse("estoque:contas_receber")}"')
        self.assertNotContains(resposta_externa, "example.com")

    def test_botoes_central_pix_alertam_somente_pix_em_atencao(self):
        cliente = Cliente.objects.create(nome="Cliente Alerta Pix", ativo=True)

        resposta_sem_pix = self.client.get(reverse("estoque:contas_receber"), secure=True)
        self.assertNotContains(resposta_sem_pix, "(pendente)")

        PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Pix baixado",
            valor="10.00",
            status=PixRecebido.STATUS_BAIXADO,
        )
        resposta_baixado = self.client.get(reverse("estoque:vendas"), secure=True)
        self.assertNotContains(resposta_baixado, "(pendente)")

        PixRecebido.objects.create(
            nome_pagador="Pix pendente",
            valor="20.00",
            status=PixRecebido.STATUS_PENDENTE,
        )
        resposta_contas = self.client.get(reverse("estoque:contas_receber"), secure=True)
        self.assertContains(resposta_contas, "Central de Pix (pendente)")
        self.assertContains(resposta_contas, "contas-btn-pix-alerta")

        resposta_vendas = self.client.get(reverse("estoque:vendas"), secure=True)
        self.assertContains(resposta_vendas, "Central de Pix (pendente)")
        self.assertContains(resposta_vendas, "pix-alerta")

        PixRecebido.objects.filter(status=PixRecebido.STATUS_PENDENTE).update(
            status=PixRecebido.STATUS_BAIXADO
        )
        PixRecebido.objects.create(
            nome_pagador="Pix nao identificado",
            valor="30.00",
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
        )
        resposta_receber = self.client.get(
            reverse("estoque:receber_cliente", kwargs={"cliente_id": cliente.id}),
            secure=True,
        )
        self.assertContains(resposta_receber, "Central de Pix (pendente)")
        self.assertContains(resposta_receber, "rc-btn-pix-alerta")

    def test_analisar_comprovante_pix_preenche_dados_sem_confirmar_cliente(self):
        cliente = Cliente.objects.create(nome="Cicero Cristiano Silva Souza", ativo=True)
        conteudo = (
            "Comprovante Pix\n"
            "Origem\n"
            "Nome: Cicero Cristiano Silva Souza\n"
            "Valor R$ 20,00\n"
            "Data 16/05/2026 17:30\n"
            "Destino\n"
            "Nome: Loja Exemplo\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Cicero Cristiano Silva Souza")
        self.assertEqual(dados["valor"], "20.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T17:30")
        self.assertEqual(dados["cliente_sugerido_id"], cliente.id)
        self.assertEqual(dados["confianca_cliente"], "alta")
        self.assertEqual(dados["mensagem_cliente"], "")
        self.assertEqual(PixRecebido.objects.count(), 0)
        self.assertEqual(ContaReceber.objects.count(), 0)
        self.assertEqual(CreditoCliente.objects.count(), 0)

    def test_pagina_envio_comprovante_pix_inclui_manifest_pwa(self):
        resposta = self.client.get(reverse("estoque:central_pix_enviar_comprovante"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'rel="manifest"')
        self.assertContains(resposta, 'href="/static/manifest.json"')
        self.assertContains(resposta, 'name="theme-color" content="#16a34a"')

    @override_settings(
        PIX_LOCAL_URL="http://10.0.0.154:8000/central-pix/enviar-comprovante/",
        PIX_ONLINE_URL="https://sistema-de-vendas-e-estoque.onrender.com/central-pix/enviar-comprovante/",
    )
    def test_atalho_inteligente_pix_carrega_urls_configuradas(self):
        resposta = self.client.get(reverse("estoque:pix_enviar_inteligente"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Abrindo Enviar Pix")
        self.assertContains(resposta, "Tentando conexao local pelo Wi-Fi")
        self.assertContains(resposta, "http://10.0.0.154:8000/central-pix/enviar-comprovante/")
        self.assertContains(resposta, "https://sistema-de-vendas-e-estoque.onrender.com/central-pix/enviar-comprovante/")
        self.assertContains(resposta, "fetch(localUrl")
        self.assertContains(resposta, "window.location.replace(onlineUrl)")

    @override_settings(
        ALLOWED_HOSTS=["10.0.0.154"],
        PIX_LOCAL_URL="http://10.0.0.154:8000/central-pix/enviar-comprovante/",
        PIX_ONLINE_URL="https://sistema-de-vendas-e-estoque.onrender.com/central-pix/enviar-comprovante/",
    )
    def test_pagina_envio_comprovante_pix_mostra_ambiente_local(self):
        resposta = self.client.get(
            reverse("estoque:central_pix_enviar_comprovante"),
            secure=True,
            HTTP_HOST="10.0.0.154:8000",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "LOCAL / Wi-Fi")

    @override_settings(
        ALLOWED_HOSTS=["sistema-de-vendas-e-estoque.onrender.com"],
        PIX_LOCAL_URL="http://10.0.0.154:8000/central-pix/enviar-comprovante/",
        PIX_ONLINE_URL="https://sistema-de-vendas-e-estoque.onrender.com/central-pix/enviar-comprovante/",
    )
    def test_pagina_envio_comprovante_pix_mostra_ambiente_online(self):
        resposta = self.client.get(
            reverse("estoque:central_pix_enviar_comprovante"),
            secure=True,
            HTTP_HOST="sistema-de-vendas-e-estoque.onrender.com",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "ONLINE / Render")

    @override_settings(ALLOWED_HOSTS=["example.com"])
    def test_pagina_envio_comprovante_pix_mostra_ambiente_nao_identificado(self):
        resposta = self.client.get(
            reverse("estoque:central_pix_enviar_comprovante"),
            secure=True,
            HTTP_HOST="example.com",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "AMBIENTE NÃO IDENTIFICADO")

    @override_settings(ALLOWED_HOSTS=["10.0.0.154"])
    def test_pagina_sucesso_comprovante_pix_mostra_ambiente_local(self):
        pix = PixRecebido.objects.create(valor=Decimal("0.00"), data_pagamento=timezone.now())

        resposta = self.client.get(
            reverse("estoque:central_pix_envio_sucesso", kwargs={"pix_id": pix.id}),
            secure=True,
            HTTP_HOST="10.0.0.154:8000",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "LOCAL / Wi-Fi")

    @override_settings(ALLOWED_HOSTS=["sistema-de-vendas-e-estoque.onrender.com"])
    def test_pagina_sucesso_comprovante_pix_mostra_ambiente_online(self):
        pix = PixRecebido.objects.create(valor=Decimal("0.00"), data_pagamento=timezone.now())

        resposta = self.client.get(
            reverse("estoque:central_pix_envio_sucesso", kwargs={"pix_id": pix.id}),
            secure=True,
            HTTP_HOST="sistema-de-vendas-e-estoque.onrender.com",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "ONLINE / Render")

    @override_settings(ALLOWED_HOSTS=["10.0.0.154"])
    def test_rota_antiga_envio_pix_post_sem_arquivo_mantem_selo_local(self):
        resposta = self.client.post(
            reverse("estoque:central_pix_enviar_comprovante"),
            {"enviado_por": "Lincoln"},
            secure=True,
            HTTP_HOST="10.0.0.154:8000",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "LOCAL / Wi-Fi")
        self.assertContains(resposta, "Escolha uma imagem ou arquivo de comprovante Pix.")

    @override_settings(ALLOWED_HOSTS=["sistema-de-vendas-e-estoque.onrender.com"])
    def test_rota_antiga_envio_pix_sucesso_mantem_selo_online(self):
        arquivo = SimpleUploadedFile("comprovante.txt", b"Comprovante Pix", content_type="text/plain")

        with patch("estoque.views.analisar_comprovante_pix") as analisar_mock:
            resposta = self.client.post(
                reverse("estoque:central_pix_enviar_comprovante"),
                {"comprovante": arquivo, "enviado_por": "Lincoln"},
                secure=True,
                HTTP_HOST="sistema-de-vendas-e-estoque.onrender.com",
                follow=True,
            )

        self.assertEqual(resposta.status_code, 200)
        analisar_mock.assert_not_called()
        self.assertContains(resposta, "ONLINE / Render")
        self.assertContains(resposta, "Comprovante enviado com sucesso para a Central de Pix.")

    def test_enviar_comprovante_pix_cria_registro_pendente(self):
        cliente = Cliente.objects.create(nome="Cicero Cristiano Silva Souza", ativo=True)
        arquivo = SimpleUploadedFile(
            "comprovante.txt",
            (
                "Comprovante Pix\n"
                "Origem\n"
                "Nome: Cicero Cristiano Silva Souza\n"
                "Valor R$ 20,00\n"
                "Data 16/05/2026 17:30\n"
                "Banco do Brasil\n"
            ).encode("utf-8"),
            content_type="text/plain",
        )

        with patch("estoque.views.analisar_comprovante_pix") as analisar_mock:
            resposta = self.client.post(
                reverse("estoque:central_pix_enviar_comprovante"),
                {"comprovante": arquivo, "enviado_por": "Lincoln"},
                secure=True,
                follow=True,
            )

        self.assertEqual(resposta.status_code, 200)
        analisar_mock.assert_not_called()
        pix = PixRecebido.objects.get()
        sucesso_url = reverse("estoque:central_pix_envio_sucesso", kwargs={"pix_id": pix.id})
        self.assertTrue(resposta.redirect_chain[-1][0].endswith(sucesso_url))
        self.assertContains(resposta, "Comprovante enviado")
        self.assertContains(resposta, "Comprovante enviado com sucesso para a Central de Pix.")
        self.assertContains(resposta, "Confira depois no computador antes de baixar qualquer conta.")
        self.assertContains(resposta, "Enviado por")
        self.assertContains(resposta, "Lincoln")
        self.assertContains(resposta, f'href="{reverse("estoque:central_pix_enviar_comprovante")}"')
        self.assertContains(resposta, f'href="{reverse("estoque:central_pix")}"')
        self.assertContains(resposta, "Enviar outro comprovante")
        self.assertIsNone(pix.cliente)
        self.assertIsNone(pix.cliente_sugerido)
        self.assertEqual(pix.nome_pagador, "")
        self.assertEqual(pix.enviado_por_nome, "Lincoln")
        self.assertEqual(str(pix.valor), "0.00")
        self.assertEqual(pix.instituicao_pix, "")
        self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
        self.assertIn("OCR nao executado automaticamente no envio mobile", pix.texto_ocr_bruto)
        self.assertTrue(pix.comprovante)
        self.assertContains(resposta, "OCR pendente / Conferencia pendente")

        resposta_detalhe = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )
        self.assertEqual(resposta_detalhe.status_code, 200)
        self.assertContains(resposta_detalhe, "Texto OCR bruto")
        self.assertContains(resposta_detalhe, "Ler comprovante (OCR)")

    def test_enviar_comprovante_pix_erro_storage_nao_derruba_pagina(self):
        arquivo = SimpleUploadedFile("comprovante.txt", b"Comprovante Pix", content_type="text/plain")

        with patch("estoque.views.PixRecebido.objects.create", side_effect=RuntimeError("falha storage")):
            with self.assertLogs("estoque.views", level="ERROR") as logs:
                resposta = self.client.post(
                    reverse("estoque:central_pix_enviar_comprovante"),
                    {"comprovante": arquivo, "enviado_por": "Lincoln"},
                    secure=True,
                )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(
            resposta,
            "Erro ao salvar comprovante Pix. Verifique a configuração do armazenamento online.",
        )
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertIn("diagnostico_storage", "\n".join(logs.output))
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_enviar_comprovante_pix_sem_cliente_sugerido_salva_nao_identificado(self):
        arquivo = SimpleUploadedFile(
            "comprovante-sem-cliente.txt",
            (
                "Mercado Pago\n"
                "Comprovante de Pix\n"
                "16/maio/2026 as 16:33:29\n"
                "R$ 600\n"
                "@ De\n"
                "Ivanildo Ferraz Patricio Junior\n"
            ).encode("utf-8"),
            content_type="text/plain",
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_enviar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        pix = PixRecebido.objects.get()
        self.assertIsNone(pix.cliente)
        self.assertEqual(pix.nome_pagador, "")
        self.assertEqual(str(pix.valor), "0.00")
        self.assertEqual(pix.instituicao_pix, "")
        self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
        self.assertIn("OCR nao executado automaticamente no envio mobile", pix.texto_ocr_bruto)

    def test_enviar_comprovante_pix_com_outro_salva_nome_informado(self):
        arquivo = SimpleUploadedFile(
            "comprovante-outro.txt",
            (
                "Comprovante Pix\n"
                "Nome: Cliente Sem Cadastro\n"
                "Valor R$ 42,00\n"
                "Data 16/05/2026 17:30\n"
            ).encode("utf-8"),
            content_type="text/plain",
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_enviar_comprovante"),
            {
                "comprovante": arquivo,
                "enviado_por": "Outro",
                "enviado_por_outro": "  Ana Caixa  ",
            },
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        pix = PixRecebido.objects.get()
        sucesso_url = reverse("estoque:central_pix_envio_sucesso", kwargs={"pix_id": pix.id})
        self.assertTrue(resposta.redirect_chain[-1][0].endswith(sucesso_url))
        self.assertEqual(pix.enviado_por_nome, "Ana Caixa")
        self.assertContains(resposta, "Enviado por")
        self.assertContains(resposta, "Ana Caixa")
        self.assertContains(resposta, "Enviar outro comprovante")

    def test_enviar_comprovante_pix_sem_ocr_completo_salva_nao_identificado(self):
        arquivo = SimpleUploadedFile(
            "comprovante-sem-ocr.txt",
            b"texto sem dados de pix",
            content_type="text/plain",
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_enviar_comprovante"),
            {"comprovante": arquivo, "enviado_por": "Roseli"},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Comprovante enviado com sucesso para a Central de Pix.")
        self.assertContains(
            resposta,
            "Confira depois no computador antes de baixar qualquer conta.",
        )
        pix = PixRecebido.objects.get()
        self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
        self.assertEqual(pix.enviado_por_nome, "Roseli")

    def test_enviar_comprovante_pix_com_erro_ocr_salva_diagnostico_sem_quebrar(self):
        arquivo = SimpleUploadedFile(
            "comprovante-render.jpg",
            b"imagem recebida pelo celular",
            content_type="image/jpeg",
        )

        with patch("estoque.utils_pix._extrair_texto_comprovante", side_effect=RuntimeError("tesseract nao encontrado")):
            resposta = self.client.post(
                reverse("estoque:central_pix_enviar_comprovante"),
                {"comprovante": arquivo, "enviado_por": "Roseli"},
                secure=True,
                follow=True,
            )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Comprovante enviado com sucesso para a Central de Pix.")
        pix = PixRecebido.objects.get()
        self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
        self.assertEqual(pix.enviado_por_nome, "Roseli")
        self.assertIn("OCR nao executado automaticamente no envio mobile", pix.texto_ocr_bruto)

        resposta_detalhe = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )
        self.assertEqual(resposta_detalhe.status_code, 200)
        self.assertContains(resposta_detalhe, "Texto OCR bruto")
        self.assertContains(resposta_detalhe, "OCR nao executado automaticamente no envio mobile")

    def test_analisar_comprovante_pix_com_ocr_vazio_salva_diagnostico(self):
        arquivo = SimpleUploadedFile(
            "comprovante-vazio.jpg",
            b"imagem sem texto",
            content_type="image/jpeg",
        )

        with patch("estoque.utils_pix._extrair_texto_comprovante", return_value=""):
            resposta = self.client.post(
                reverse("estoque:central_pix_enviar_comprovante"),
                {"comprovante": arquivo},
                secure=True,
                follow=True,
            )

        self.assertEqual(resposta.status_code, 200)
        pix = PixRecebido.objects.get()
        self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
        self.assertIn("OCR nao executado automaticamente no envio mobile", pix.texto_ocr_bruto)

    def _imagem_pix_teste(self, tamanho=(900, 1600)):
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", tamanho, "white").save(buffer, format="PNG")
        return buffer.getvalue()

    def _imagem_linhas_ocr_teste(self):
        from PIL import Image, ImageDraw

        imagem = Image.new("RGB", (500, 900), "white")
        desenho = ImageDraw.Draw(imagem)
        for y in (80, 150, 220, 290, 360):
            desenho.rectangle((60, y, 430, y + 18), fill="black")
        buffer = io.BytesIO()
        imagem.save(buffer, format="PNG")
        return buffer.getvalue()

    def _imagem_linhas_nubank_teste(self):
        from PIL import Image, ImageDraw

        imagem = Image.new("RGB", (500, 900), "white")
        desenho = ImageDraw.Draw(imagem)
        for y, largura in zip((70, 135, 195, 260, 340), (80, 260, 320, 360, 340)):
            desenho.rectangle((70, y, 70 + largura, y + 18), fill="black")
        buffer = io.BytesIO()
        imagem.save(buffer, format="PNG")
        return buffer.getvalue()

    def _imagem_texto_denso_teste(self):
        from PIL import Image, ImageDraw

        imagem = Image.new("RGB", (700, 1500), "white")
        desenho = ImageDraw.Draw(imagem)
        for y in range(60, 1280, 28):
            desenho.rectangle((70, y, 610, y + 5), fill="black")
            desenho.rectangle((70, y + 10, 420, y + 14), fill="black")
        buffer = io.BytesIO()
        imagem.save(buffer, format="PNG")
        return buffer.getvalue()

    def _modulo_pytesseract_fake(self, respostas_por_recorte):
        def image_to_string(imagem, **kwargs):
            recorte = imagem.info.get("ocr_recorte", "inteira")
            resposta = respostas_por_recorte.get(recorte, "")
            if callable(resposta):
                return resposta(imagem, kwargs)
            if isinstance(resposta, Exception):
                raise resposta
            return resposta

        return types.SimpleNamespace(
            pytesseract=types.SimpleNamespace(tesseract_cmd=""),
            image_to_string=image_to_string,
        )

    def _mock_google_vision_texto(self, texto):
        class ImagemFake:
            def __init__(self, content):
                self.content = content

        cliente = types.SimpleNamespace(
            document_text_detection=lambda image: types.SimpleNamespace(
                full_text_annotation=types.SimpleNamespace(text=texto),
                text_annotations=[],
                error=types.SimpleNamespace(message=""),
            )
        )
        vision = types.SimpleNamespace(Image=ImagemFake)
        return cliente, vision

    def test_analisar_comprovante_pix_google_vision_banpara_extrai_dados(self):
        texto = (
            "BANCO DO ESTADO DO PARÁ S.A. - BANPARÁ\n"
            "COMPROVANTE DE PIX\n"
            "Data da Operação: 05/05/2026 16:45:50\n"
            "Dados de Origem\n"
            "Titular: RUBEM ARRUDA DE SOUZA\n"
            "Dados do Recebedor\n"
            "Instituição: NU PAGAMENTOS - IP\n"
            "Titular: Lincoln Albuquerque Neiva\n"
            "Valor: 847,70\n"
        )
        arquivo = SimpleUploadedFile("banpara.jpg", b"imagem", content_type="image/jpeg")

        with patch("estoque.utils_pix._criar_cliente_google_vision", return_value=self._mock_google_vision_texto(texto)):
            dados = analisar_comprovante_pix_google_vision(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "847.70")
        self.assertEqual(dados["data_pagamento"], "2026-05-05T16:45")
        self.assertEqual(dados["pagador"], "RUBEM ARRUDA DE SOUZA")
        self.assertEqual(dados["instituicao_pix"], "Banpará")
        self.assertIn("[Google Vision OCR]", dados["texto_ocr_bruto"])
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")

    def test_analisar_comprovante_pix_google_vision_nubank_extrai_valor_data(self):
        texto = "NU\nComprovante de transferência\n21 ABR 2026 - 13:05:01\nValor R$ 172,00\n"
        arquivo = SimpleUploadedFile("nubank.jpg", b"imagem", content_type="image/jpeg")

        with patch("estoque.utils_pix._criar_cliente_google_vision", return_value=self._mock_google_vision_texto(texto)):
            dados = analisar_comprovante_pix_google_vision(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "172.00")
        self.assertEqual(dados["data_pagamento"], "2026-04-21T13:05")
        self.assertIn("[Google Vision OCR]", dados["texto_ocr_bruto"])

    def test_analisar_comprovante_pix_google_vision_nubank_extrai_pagador_origem_por_contexto(self):
        texto = (
            "NU\n"
            "Comprovante de transferencia\n"
            "21 ABR 2026 - 13:05:01\n"
            "Valor R$ 172,00\n"
            "Destino\n"
            "Nome Lincoln Albuquerque Neiva\n"
            "Origem\n"
            "Juliana Cotrim Cardoso\n"
            "CPF ***.123.456-**\n"
            "Instituicao NU PAGAMENTOS - IP\n"
        )
        arquivo = SimpleUploadedFile("nubank.jpg", b"imagem", content_type="image/jpeg")

        with patch("estoque.utils_pix._criar_cliente_google_vision", return_value=self._mock_google_vision_texto(texto)):
            dados = analisar_comprovante_pix_google_vision(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Juliana Cotrim Cardoso")
        self.assertEqual(dados["valor"], "172.00")
        self.assertEqual(dados["data_pagamento"], "2026-04-21T13:05")
        self.assertEqual(dados["instituicao_pix"], "Nubank")
        self.assertIn("[Google Vision OCR]", dados["texto_ocr_bruto"])
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")

    def test_analisar_comprovante_pix_google_vision_mercado_pago_extrai_pagador_de(self):
        texto = (
            "Comprovante de Pix\n"
            "23/maio/2026 as 18:55:32\n"
            "R$ 645\n"
            "De\n"
            "Joao de Almeida E Silva\n"
            "CPF: ***.105.902-**\n"
            "Mercado Pago\n"
            "Para\n"
            "Lincoln Albuquerque Neiva\n"
        )
        arquivo = SimpleUploadedFile("mercado-pago.jpg", b"imagem", content_type="image/jpeg")

        with patch("estoque.utils_pix._criar_cliente_google_vision", return_value=self._mock_google_vision_texto(texto)):
            dados = analisar_comprovante_pix_google_vision(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Joao de Almeida E Silva")
        self.assertEqual(dados["valor"], "645.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-23T18:55")
        self.assertEqual(dados["instituicao_pix"], "Mercado Pago")
        self.assertIn("[Google Vision OCR]", dados["texto_ocr_bruto"])
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")

    def test_analisar_comprovante_pix_google_vision_debug_texto_bruto_env(self):
        texto = "Comprovante de Pix\nDe\nJoao de Almeida E Silva\nPara\nLincoln Albuquerque Neiva\n"
        arquivo = SimpleUploadedFile("mercado-pago.jpg", b"imagem", content_type="image/jpeg")

        with patch.dict(os.environ, {"PIX_OCR_DEBUG_TEXT": "True"}), patch(
            "estoque.utils_pix._criar_cliente_google_vision",
            return_value=self._mock_google_vision_texto(texto),
        ), self.assertLogs("estoque.utils_pix", level="WARNING") as logs:
            analisar_comprovante_pix_google_vision(arquivo)

        saida = "\n".join(logs.output)
        self.assertIn("[PIX OCR][Google Vision texto bruto]", saida)
        self.assertIn("--- INICIO GOOGLE VISION OCR ---", saida)
        self.assertIn("Joao de Almeida E Silva", saida)
        self.assertIn("--- FIM GOOGLE VISION OCR ---", saida)

    def test_analisar_comprovante_pix_google_vision_extrai_de_para_em_texto_achatado(self):
        casos = [
            (
                "pagbank.jpg",
                "Comprovante de envio de Pix 18/05/2026 as 07:08:52 Valor da transferencia R$ 5,00 De ROSELI DA COSTA GAMA CPF ***.115.912-** Instituicao PagBank Para Lincoln Albuquerque Neiva",
                "ROSELI DA COSTA GAMA",
                "5.00",
                "2026-05-18T07:08",
                "PagBank",
            ),
            (
                "itau.jpg",
                "Comprovante de devolucao de Pix R$ 5,00 Realizado em 19/05/2026 as 17:18:36 De EUCLIDES CARNEIRO NEIVA NETO CPF/CNPJ: 787.484.883-72 Instituicao: ITAU UNIBANCO S.A Para Lincoln Albuquerque Neiva",
                "EUCLIDES CARNEIRO NEIVA NETO",
                "5.00",
                "2026-05-19T17:18",
                "Ita\u00fa Unibanco",
            ),
        ]

        for nome_arquivo, texto, pagador, valor, data_pagamento, instituicao in casos:
            with self.subTest(nome=nome_arquivo):
                arquivo = SimpleUploadedFile(nome_arquivo, b"imagem", content_type="image/jpeg")
                with patch("estoque.utils_pix._criar_cliente_google_vision", return_value=self._mock_google_vision_texto(texto)):
                    dados = analisar_comprovante_pix_google_vision(arquivo)

                self.assertTrue(dados["ok"])
                self.assertEqual(dados["pagador"], pagador)
                self.assertEqual(dados["valor"], valor)
                self.assertEqual(dados["data_pagamento"], data_pagamento)
                self.assertEqual(dados["instituicao_pix"], instituicao)
                self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")

    def test_pix_google_vision_habilitado_le_variavel_de_ambiente_fora_dos_testes(self):
        with patch.dict(os.environ, {"PIX_USAR_GOOGLE_VISION": "True"}), patch("sys.argv", ["manage.py", "runserver"]):
            self.assertTrue(views.pix_google_vision_habilitado())

    def test_pix_google_vision_habilitado_usa_credencial_quando_flag_nao_definida(self):
        with patch.dict(os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": "google-vision.json"}, clear=True), patch(
            "sys.argv",
            ["manage.py", "runserver"],
        ):
            self.assertTrue(views.pix_google_vision_habilitado())

    def test_analisar_comprovante_pix_google_vision_falha_sem_500(self):
        arquivo = SimpleUploadedFile("erro.jpg", b"imagem", content_type="image/jpeg")

        with patch("estoque.utils_pix._criar_cliente_google_vision", side_effect=RuntimeError("sem credencial")):
            dados = analisar_comprovante_pix_google_vision(arquivo)

        self.assertFalse(dados["ok"])
        self.assertEqual(dados["valor"], "")
        self.assertIn("[Google Vision OCR erro]", dados["texto_ocr_bruto"])

    def test_analisar_comprovante_pix_ocr_parcial_nao_derruba_extracao(self):
        arquivo = SimpleUploadedFile(
            "comprovante-parcial.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )
        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": "Comprovante Pix\nValor R$ 156,50\n16/05/2026 17:51\nNubank\n",
            "pagador": RuntimeError("timeout no recorte"),
            "inteira": "Origem\nNome: Joelson Ferreira dos Santos\nDestino\nNome: Lincoln Albuquerque Neiva\n",
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Joelson Ferreira dos Santos")
        self.assertEqual(dados["valor"], "156.50")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T17:51")
        self.assertEqual(dados["instituicao_pix"], "Nubank")
        self.assertIn("[OCR avisos]", dados["texto_ocr_bruto"])

    def test_analisar_comprovante_pix_fallback_imagem_inteira_continua_funcionando(self):
        arquivo = SimpleUploadedFile(
            "comprovante-fallback.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )
        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": RuntimeError("falhou topo"),
            "pagador": RuntimeError("falhou pagador"),
            "inteira": (
                "Comprovante Pix\n"
                "Origem\n"
                "Nome: Maria Fallback Silva\n"
                "Valor R$ 88,00\n"
                "16/05/2026 17:30\n"
                "Banco do Brasil\n"
            ),
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Maria Fallback Silva")
        self.assertEqual(dados["valor"], "88.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T17:30")
        self.assertEqual(dados["instituicao_pix"], "Banco do Brasil")

    def test_analisar_comprovante_pix_tenta_eng_quando_por_traineddata_falha(self):
        arquivo = SimpleUploadedFile(
            "comprovante-sem-por.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )

        def texto_com_fallback(_imagem, kwargs):
            if kwargs.get("lang") == "por":
                raise RuntimeError("Error opening data file C:\\Program Files\\Tesseract-OCR/tessdata/por.traineddata")
            if kwargs.get("lang") == "eng":
                return "Pix enviado\nR$ 5,00\nData do pagamento\nSabado, 16/05/2026\nHorario\n23h41\n"
            return ""

        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": texto_com_fallback,
            "pagador": "",
            "inteira": "",
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ), self.assertLogs("estoque.utils_pix", level="WARNING") as logs:
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T23:41")
        self.assertIn("recorte=topo tentativa OCR falhou=por:", "\n".join(logs.output))
        self.assertIn("recorte=topo idioma OCR usado=eng", "\n".join(logs.output))

    def test_analisar_comprovante_pix_recortes_nao_usam_recebedor_como_pagador(self):
        arquivo = SimpleUploadedFile(
            "comprovante-recebedor.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )
        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": "Valor R$ 600,00\n16/05/2026 16:33\nPara\nLincoln Albuquerque Neiva\n",
            "pagador": "@ De\nIvanildo Ferraz Patricio Junior\nCPF ***.188.882-**\nMercado Pago\n",
            "inteira": "Para\nLA Neiva\nNu Pagamentos S.A.\n",
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Ivanildo Ferraz Patricio Junior")
        self.assertNotIn(dados["pagador"], ["Lincoln Albuquerque Neiva", "LA Neiva"])
        self.assertEqual(dados["valor"], "600.00")
        self.assertEqual(dados["instituicao_pix"], "Mercado Pago")

    def test_analisar_comprovante_pix_sem_banco_pagador_retorna_parcial_sem_erro(self):
        arquivo = SimpleUploadedFile(
            "comprovante-sem-pagador.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )
        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": "Comprovante Pix\nValor R$ 10,00\n21/05/2026 09:10\n",
            "pagador": "",
            "inteira": "Destino\nLincoln Albuquerque Neiva\n",
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "")
        self.assertEqual(dados["instituicao_pix"], "")
        self.assertEqual(dados["valor"], "10.00")

    def test_analisar_comprovante_pix_todos_recortes_falham_sem_excecao(self):
        arquivo = SimpleUploadedFile(
            "comprovante-falha.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )
        erro = RuntimeError("timeout")
        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": erro,
            "pagador": erro,
            "inteira": erro,
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertFalse(dados["ok"])
        self.assertEqual(dados["pagador"], "")
        self.assertEqual(dados["valor"], "")
        self.assertIn("preencha manualmente", dados["mensagem"].lower())

    def test_analisar_comprovante_pix_render_nao_usa_imagem_inteira_em_imagem_grande(self):
        arquivo = SimpleUploadedFile(
            "banpara-grande.png",
            self._imagem_pix_teste(tamanho=(900, 1800)),
            content_type="image/png",
        )
        recortes_chamados = []

        def falhar_recorte(imagem, _kwargs):
            recortes_chamados.append(imagem.info.get("ocr_recorte", "inteira"))
            raise RuntimeError("timeout render")

        pytesseract_fake = self._modulo_pytesseract_fake({
            "faixa_valor_principal": falhar_recorte,
            "faixa_valor_alternativa": falhar_recorte,
            "faixa_data_principal": falhar_recorte,
            "faixa_data_alternativa": falhar_recorte,
            "rapido_superior": falhar_recorte,
            "rapido_meio_superior": falhar_recorte,
            "inteira": AssertionError("nao deve usar imagem inteira no Render"),
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ), patch("estoque.utils_pix.OCR_RENDER_MODO_LEVE", True):
            dados = analisar_comprovante_pix(arquivo)

        self.assertFalse(dados["ok"])
        self.assertNotIn("inteira", recortes_chamados)
        self.assertIn("ERRO OCR", dados["texto_ocr_bruto"])

    def test_analisar_comprovante_pix_render_bloqueia_imagem_densa_antes_do_tesseract(self):
        arquivo = SimpleUploadedFile(
            "banpara-denso.png",
            self._imagem_texto_denso_teste(),
            content_type="image/png",
        )
        pytesseract_fake = self._modulo_pytesseract_fake({
            "inteira": AssertionError("nao deve chamar Tesseract para imagem densa no Render"),
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix.OCR_RENDER_MODO_LEVE",
            True,
        ), self.assertLogs("estoque.utils_pix", level="WARNING") as logs:
            dados = analisar_comprovante_pix(arquivo)

        self.assertFalse(dados["ok"])
        self.assertEqual(dados["valor"], "")
        self.assertIn("OCR bloqueado por seguran", dados["texto_ocr_bruto"])
        self.assertIn("OCR bloqueado por seguranca no Render antes do Tesseract", "\n".join(logs.output))

    def test_detalhe_pix_processar_ocr_render_bloqueado_volta_sem_500(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("banpara-denso.png", self._imagem_texto_denso_teste(), content_type="image/png"),
            )
            pytesseract_fake = self._modulo_pytesseract_fake({
                "inteira": AssertionError("nao deve chamar Tesseract para imagem densa no Render"),
            })

            with patch.dict(os.environ, {"PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True"}), patch.dict(
                "sys.modules",
                {"pytesseract": pytesseract_fake},
            ), patch(
                "estoque.utils_pix.OCR_RENDER_MODO_LEVE",
                True,
            ):
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertTrue(pix.comprovante)
            self.assertEqual(str(pix.valor), "0.00")
            self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
            self.assertIn("OCR bloqueado por seguran", pix.texto_ocr_bruto)
            self.assertContains(resposta, "OCR nao conseguiu ler todos os dados")

    def test_analisar_comprovante_pix_render_timeouts_sucessivos_retornam_falha_segura(self):
        arquivo = SimpleUploadedFile(
            "banpara-timeout.png",
            self._imagem_linhas_ocr_teste(),
            content_type="image/png",
        )

        def timeout(_imagem, _kwargs):
            raise RuntimeError("Tesseract process timeout")

        pytesseract_fake = self._modulo_pytesseract_fake({
            "linha_01": timeout,
            "linha_02": timeout,
            "linha_03": timeout,
            "linha_04": timeout,
            "linha_05": timeout,
            "rapido_superior": timeout,
            "rapido_meio_superior": timeout,
            "inteira": AssertionError("nao deve usar imagem inteira no Render"),
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ), patch("estoque.utils_pix.OCR_RENDER_MODO_LEVE", True):
            dados = analisar_comprovante_pix(arquivo)

        self.assertFalse(dados["ok"])
        self.assertEqual(dados["valor"], "")
        self.assertIn("ERRO OCR", dados["texto_ocr_bruto"])

    def test_analisar_comprovante_pix_inter_enviado_preenche_valor_data_sem_pagador(self):
        arquivo = SimpleUploadedFile(
            "comprovante-inter.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )
        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": (
                "Pix enviado\n"
                "R$ 5,00\n"
                "Data do pagamento\n"
                "Sabado, 16/05/2026\n"
                "Horario\n"
                "23h41\n"
            ),
            "pagador": (
                "ID da transacao\n"
                "E00416968202605170241GgMBKIN9FUL\n"
                "Quem recebeu\n"
                "Lincoln Albuquerque Neiva\n"
                "Instituicao do recebedor\n"
                "Nu Pagamentos\n"
            ),
            "inteira": "",
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "")
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T23:41")
        self.assertEqual(dados["instituicao_pix"], "")
        self.assertNotIn("Lincoln Albuquerque Neiva", dados["pagador"])

    def test_analisar_comprovante_pix_inter_nao_usa_instituicao_do_recebedor_como_banco(self):
        arquivo = SimpleUploadedFile(
            "comprovante-inter-recebedor-nubank.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )
        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": (
                "Inter\n"
                "Pix enviado\n"
                "R$ 5,00\n"
                "Data do pagamento\n"
                "Sabado, 16/05/2026\n"
                "Horario\n"
                "23h41\n"
            ),
            "pagador": (
                "Quem pagou\n"
                "Nome\n"
                "RONISE DO SOCORRO DOS SANTOS FERREIRA\n"
                "Quem recebeu\n"
                "Lincoln Albuquerque Neiva\n"
                "Instituicao do recebedor\n"
                "Nu Pagamentos\n"
            ),
            "inteira": "",
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "RONISE DO SOCORRO DOS SANTOS FERREIRA")
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T23:41")
        self.assertEqual(dados["instituicao_pix"], "Inter")
        self.assertNotEqual(dados["instituicao_pix"], "Nubank")

    def test_analisar_comprovante_pix_inter_nao_usa_la_neiva_como_pagador(self):
        arquivo = SimpleUploadedFile(
            "comprovante-inter-la-neiva.png",
            self._imagem_pix_teste(),
            content_type="image/png",
        )
        pytesseract_fake = self._modulo_pytesseract_fake({
            "topo": "Pix enviado\nRS 5,00\nData do pagamento Sabado, 16/05/2026 Horario 23h41\n",
            "pagador": "Quem recebeu\nLA Neiva\nInstituicao do recebedor\nNu Pagamentos\n",
            "inteira": "",
        })

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "")
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T23:41")

    def test_enviar_mesmo_comprovante_pix_duas_vezes_nao_processa_duplicidade_no_mobile(self):
        conteudo = (
            "Comprovante Pix\n"
            "Origem\n"
            "Nome: Maria Duplicada Silva\n"
            "Valor R$ 88,00\n"
            "Data 16/05/2026 17:30\n"
            "Banco do Brasil\n"
            "ID da transacao\n"
            "E00000000202605161730ABCDEF123456789\n"
        ).encode("utf-8")
        url = reverse("estoque:central_pix_enviar_comprovante")

        primeira = self.client.post(
            url,
            {"comprovante": SimpleUploadedFile("pix-1.txt", conteudo, content_type="text/plain")},
            secure=True,
            follow=True,
        )
        segunda = self.client.post(
            url,
            {"comprovante": SimpleUploadedFile("pix-2.txt", conteudo, content_type="text/plain")},
            secure=True,
            follow=True,
        )

        self.assertEqual(primeira.status_code, 200)
        self.assertEqual(segunda.status_code, 200)
        self.assertContains(segunda, "Comprovante enviado com sucesso para a Central de Pix.")
        pix_original, pix_duplicado = PixRecebido.objects.order_by("id")
        self.assertNotEqual(pix_original.status, PixRecebido.STATUS_POSSIVEL_DUPLICADO)
        self.assertEqual(pix_duplicado.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
        self.assertIsNone(pix_duplicado.pix_original)
        self.assertIsNone(pix_duplicado.cliente)

    def test_enviar_comprovante_mobile_nao_roda_duplicidade_por_ocr(self):
        PixRecebido.objects.create(
            nome_pagador="Jose Parecido da Silva",
            valor="55.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 16, 17, 30)),
            instituicao_pix="PicPay",
            status=PixRecebido.STATUS_PENDENTE,
        )
        arquivo = SimpleUploadedFile(
            "provavel-duplicado.txt",
            (
                "PicPay\n"
                "Comprovante Pix\n"
                "Pagador: José Parecido da Silva\n"
                "Valor R$ 55,00\n"
                "16/05/2026 17:30\n"
            ).encode("utf-8"),
            content_type="text/plain",
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_enviar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        novo_pix = PixRecebido.objects.order_by("-id").first()
        self.assertEqual(novo_pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
        self.assertIsNone(novo_pix.pix_original)
        self.assertIn("OCR pendente", novo_pix.observacao)

    def test_enviar_comprovante_pix_diferente_continua_pendente_normal(self):
        cliente = Cliente.objects.create(nome="Cliente Diferente Pix", ativo=True)
        PixRecebido.objects.create(
            nome_pagador="Outro Pagador",
            valor="55.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 16, 17, 30)),
            instituicao_pix="PicPay",
            status=PixRecebido.STATUS_PENDENTE,
        )
        arquivo = SimpleUploadedFile(
            "pix-diferente.txt",
            (
                "Comprovante Pix\n"
                "Origem\n"
                "Nome: Cliente Diferente Pix\n"
                "Valor R$ 75,00\n"
                "Data 16/05/2026 18:30\n"
                "Banco do Brasil\n"
            ).encode("utf-8"),
            content_type="text/plain",
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_enviar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        novo_pix = PixRecebido.objects.order_by("-id").first()
        self.assertIsNone(novo_pix.cliente_sugerido)
        self.assertEqual(novo_pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
        self.assertIsNone(novo_pix.pix_original)

    def test_status_possivel_duplicado_aparece_na_central_e_no_detalhe(self):
        original = PixRecebido.objects.create(
            nome_pagador="Pix original",
            valor="25.00",
            status=PixRecebido.STATUS_PENDENTE,
        )
        duplicado = PixRecebido.objects.create(
            nome_pagador="Pix duplicado",
            valor="25.00",
            status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
            pix_original=original,
            observacao="Possivel Pix duplicado do registro #1.",
        )

        resposta_lista = self.client.get(reverse("estoque:central_pix"), secure=True)
        self.assertContains(resposta_lista, "Possivel duplicado")
        self.assertContains(resposta_lista, "possivel_duplicado")

        resposta_detalhe = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": duplicado.id}),
            secure=True,
        )
        self.assertContains(resposta_detalhe, "Possivel Pix duplicado")
        self.assertContains(resposta_detalhe, f"Ver Pix parecido #{original.id}")
        self.assertContains(resposta_detalhe, "Comparação com Pix parecido")
        self.assertContains(resposta_detalhe, f"Pix atual #{duplicado.id}")
        self.assertContains(resposta_detalhe, f"Pix parecido #{original.id}")
        self.assertContains(resposta_detalhe, "Compare os dados antes de decidir se este envio é duplicado.")

    def test_enviado_por_aparece_na_central_detalhe_e_comparacao(self):
        original = PixRecebido.objects.create(
            nome_pagador="Pix original enviado",
            enviado_por_nome="Lincoln",
            valor="25.00",
            status=PixRecebido.STATUS_PENDENTE,
        )
        duplicado = PixRecebido.objects.create(
            nome_pagador="Pix duplicado enviado",
            enviado_por_nome="Roseli",
            valor="25.00",
            status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
            pix_original=original,
        )

        resposta_lista = self.client.get(reverse("estoque:central_pix"), secure=True)
        self.assertContains(resposta_lista, "Enviado por")
        self.assertContains(resposta_lista, "Lincoln")
        self.assertContains(resposta_lista, "Roseli")

        resposta_detalhe = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": duplicado.id}),
            secure=True,
        )
        self.assertContains(resposta_detalhe, "Enviado por")
        self.assertContains(resposta_detalhe, "Lincoln")
        self.assertContains(resposta_detalhe, "Roseli")

    def test_detalhe_pix_volta_para_resumo_de_envio_quando_recebe_next_seguro(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix do resumo",
            valor="40.00",
            status=PixRecebido.STATUS_PENDENTE,
        )
        resumo_url = f"{reverse('estoque:central_pix_enviar_comprovante')}?pix_recebido={pix.id}"
        detalhe_url = f"{reverse('estoque:central_pix_detalhe', kwargs={'pix_id': pix.id})}?next={resumo_url}"

        resposta = self.client.get(detalhe_url, secure=True)

        self.assertContains(resposta, f'href="{resumo_url}"')

    def test_detalhe_pix_ignora_next_externo_e_volta_para_central(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix next externo",
            valor="15.00",
            status=PixRecebido.STATUS_PENDENTE,
        )
        detalhe_url = (
            f"{reverse('estoque:central_pix_detalhe', kwargs={'pix_id': pix.id})}"
            "?next=https://example.com/roubo"
        )

        resposta = self.client.get(detalhe_url, secure=True)

        self.assertContains(resposta, f'href="{reverse("estoque:central_pix")}"')
        self.assertNotContains(resposta, "example.com")

    def test_central_pix_lista_link_de_detalhe_do_pix_pendente(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix pendente detalhe",
            valor="35.50",
            instituicao_pix="PicPay",
            status=PixRecebido.STATUS_PENDENTE,
            texto_ocr_bruto="Texto tecnico do OCR",
        )

        resposta_lista = self.client.get(reverse("estoque:central_pix"), secure=True)
        self.assertContains(resposta_lista, "Data do Pix")
        self.assertContains(resposta_lista, "Registrado em")
        self.assertNotContains(resposta_lista, "<th class=\"pix-col-observacao\">Observacao</th>")
        self.assertNotContains(resposta_lista, "Texto tecnico do OCR")
        self.assertContains(resposta_lista, "Pix pendente detalhe")
        self.assertContains(resposta_lista, 'class="pix-pagador" title="Pix pendente detalhe"')
        self.assertContains(resposta_lista, 'class="pix-sem-cliente">Sem cliente</span>')
        self.assertContains(resposta_lista, "Ver detalhe")
        self.assertContains(
            resposta_lista,
            f'{reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id})}?next={reverse("estoque:central_pix")}',
        )

        resposta_detalhe = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )
        self.assertContains(resposta_detalhe, f'href="{reverse("estoque:central_pix")}"')
        self.assertContains(resposta_detalhe, "Enviar outro comprovante")
        self.assertContains(resposta_detalhe, "Texto OCR bruto")
        self.assertContains(resposta_detalhe, "Texto tecnico do OCR")

    def test_central_pix_ver_detalhe_retorna_para_central_mesmo_com_next_externo(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix com retorno central",
            valor="18.00",
            instituicao_pix="PagBank",
            status=PixRecebido.STATUS_PENDENTE,
        )
        central_url = reverse("estoque:central_pix")
        contas_url = reverse("estoque:contas_receber")
        caminho_lista = f"{central_url}?next={contas_url}&q=pagbank"

        resposta_lista = self.client.get(caminho_lista, secure=True)

        detalhe_path = reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id})
        self.assertContains(resposta_lista, f"{detalhe_path}?next=")
        self.assertContains(resposta_lista, "central-pix")
        self.assertContains(resposta_lista, "q%3Dpagbank")

    def test_receber_cliente_voltar_ao_pix_preserva_next_original_do_detalhe(self):
        cliente = Cliente.objects.create(nome="Cliente retorno pix", ativo=True)
        pix = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Cliente retorno pix",
            valor="25.00",
            status=PixRecebido.STATUS_PENDENTE,
        )
        central_url = reverse("estoque:central_pix")
        detalhe_url = (
            f"{reverse('estoque:central_pix_detalhe', kwargs={'pix_id': pix.id})}"
            f"?{urlencode({'next': central_url})}"
        )
        receber_url = (
            f"{reverse('estoque:receber_cliente', kwargs={'cliente_id': cliente.id})}"
            f"?{urlencode({'pix_recebido': pix.id, 'next': detalhe_url})}"
        )

        resposta = self.client.get(receber_url, secure=True)

        self.assertContains(resposta, f'href="{detalhe_url}"')
        self.assertNotContains(resposta, f"{detalhe_url}?")
        self.assertNotContains(resposta, f"{reverse('estoque:receber_cliente', kwargs={'cliente_id': cliente.id})}?pix_recebido=")

    def test_central_pix_busca_filtra_pix_registrados(self):
        cliente = Cliente.objects.create(nome="Jo\u00e3o De Almeida E Silva", ativo=True)
        pix_joao = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Joao de Almeida e Silva",
            valor="42.50",
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
            data_pagamento=timezone.make_aware(datetime(2026, 5, 23, 18, 55)),
        )
        PixRecebido.objects.create(
            nome_pagador="Maria Ignorada",
            valor="12.00",
            instituicao_pix="Nubank",
            status=PixRecebido.STATUS_IGNORADO,
            data_pagamento=timezone.make_aware(datetime(2026, 5, 22, 8, 30)),
        )

        for termo in ("joao", "almeida", "mercado", "pendente", "23/05/2026", "42,50"):
            resposta = self.client.get(f"{reverse('estoque:central_pix')}?q={termo}", secure=True)
            self.assertEqual(resposta.status_code, 200)
            self.assertContains(resposta, "Joao de Almeida e Silva")
            self.assertContains(resposta, "Mercado Pago")
            self.assertNotContains(resposta, "Maria Ignorada")
            self.assertContains(
                resposta,
                f'{reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix_joao.id})}?next=',
            )
            self.assertContains(resposta, "q%3D")

        resposta_ignorado = self.client.get(f"{reverse('estoque:central_pix')}?q=ignorado", secure=True)
        self.assertContains(resposta_ignorado, "Maria Ignorada")
        self.assertNotContains(resposta_ignorado, "Joao de Almeida e Silva")
        self.assertContains(resposta_ignorado, "Limpar")

    def test_central_pix_filtro_por_data(self):
        PixRecebido.objects.create(
            nome_pagador="Pix dentro da data",
            valor="30.00",
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
            data_pagamento=timezone.make_aware(datetime(2026, 5, 4, 10, 0)),
        )
        PixRecebido.objects.create(
            nome_pagador="Pix fora da data",
            valor="30.00",
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
            data_pagamento=timezone.make_aware(datetime(2026, 5, 5, 10, 0)),
        )

        resposta = self.client.get(
            f"{reverse('estoque:central_pix')}?data_inicio=2026-05-04&data_fim=2026-05-04",
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Pix dentro da data")
        self.assertNotContains(resposta, "Pix fora da data")

    def test_central_pix_filtro_por_status(self):
        PixRecebido.objects.create(
            nome_pagador="Pix status pendente",
            valor="30.00",
            status=PixRecebido.STATUS_PENDENTE,
        )
        PixRecebido.objects.create(
            nome_pagador="Pix status baixado",
            valor="30.00",
            status=PixRecebido.STATUS_BAIXADO,
        )

        resposta = self.client.get(
            f"{reverse('estoque:central_pix')}?status={PixRecebido.STATUS_PENDENTE}",
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Pix status pendente")
        self.assertNotContains(resposta, "Pix status baixado")

    def test_central_pix_combina_texto_cliente_data_e_status(self):
        cliente_roseli = Cliente.objects.create(nome="Roseli Cliente", ativo=True)
        cliente_maria = Cliente.objects.create(nome="Maria Cliente", ativo=True)
        PixRecebido.objects.create(
            cliente=cliente_roseli,
            nome_pagador="Pix combo certo",
            valor="45.00",
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
            data_pagamento=timezone.make_aware(datetime(2026, 5, 4, 10, 0)),
        )
        PixRecebido.objects.create(
            cliente=cliente_roseli,
            nome_pagador="Pix combo baixado",
            valor="45.00",
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_BAIXADO,
            data_pagamento=timezone.make_aware(datetime(2026, 5, 4, 10, 0)),
        )
        PixRecebido.objects.create(
            cliente=cliente_maria,
            nome_pagador="Pix combo maria",
            valor="45.00",
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
            data_pagamento=timezone.make_aware(datetime(2026, 5, 4, 10, 0)),
        )

        resposta = self.client.get(
            (
                f"{reverse('estoque:central_pix')}?q=Mercado&cliente=Roseli"
                f"&data_inicio=2026-05-04&data_fim=2026-05-04&status={PixRecebido.STATUS_PENDENTE}"
            ),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Pix combo certo")
        self.assertNotContains(resposta, "Pix combo baixado")
        self.assertNotContains(resposta, "Pix combo maria")
        self.assertContains(resposta, "status%3Dpendente")

    def test_analisar_comprovante_pix_usa_google_vision_quando_habilitado(self):
        dados_vision = {
            "ok": True,
            "pagador": "Joao de Almeida e Silva",
            "valor": "42.50",
            "data_pagamento": "2026-05-23T18:55",
            "instituicao_pix": "Mercado Pago",
            "texto_ocr_bruto": "[Google Vision OCR]\nJoao de Almeida e Silva\nR$ 42,50",
            "mensagem": "Dados lidos pelo Google Vision. Confira antes de salvar.",
        }
        arquivo = SimpleUploadedFile("mercado-pago.jpg", b"imagem", content_type="image/jpeg")

        with override_settings(PIX_USAR_GOOGLE_VISION=True):
            with patch("estoque.views.analisar_comprovante_pix_google_vision", return_value=dados_vision) as vision_mock:
                with patch("estoque.views.analisar_comprovante_pix") as local_mock:
                    resposta = self.client.post(
                        reverse("estoque:central_pix_analisar_comprovante"),
                        {"comprovante": arquivo},
                        secure=True,
                    )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Joao de Almeida e Silva")
        self.assertEqual(dados["instituicao_pix"], "Mercado Pago")
        self.assertIn("[Google Vision OCR]", dados["texto_ocr_bruto"])
        vision_mock.assert_called_once()
        local_mock.assert_not_called()
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_nao_usa_ocr_local_sem_fallback_explicito(self):
        arquivo = SimpleUploadedFile("comprovante.jpg", b"imagem", content_type="image/jpeg")

        with patch("estoque.views.analisar_comprovante_pix") as local_mock, self.assertLogs("estoque.views", level="WARNING") as logs:
            resposta = self.client.post(
                reverse("estoque:central_pix_analisar_comprovante"),
                {"comprovante": arquivo},
                secure=True,
            )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertFalse(dados["ok"])
        self.assertEqual(dados["pagador"], "")
        self.assertIn("Leitura automatica nao realizada", dados["mensagem"])
        self.assertIn("[Google Vision indisponivel]", dados["texto_ocr_bruto"])
        self.assertIn("fallback local desativado", "\n".join(logs.output))
        local_mock.assert_not_called()

    def test_analisar_comprovante_pix_usa_ocr_local_quando_fallback_explicito(self):
        dados_local = {
            "ok": True,
            "pagador": "Pagador Local",
            "valor": "5.00",
            "data_pagamento": "2026-05-18T07:08",
            "instituicao_pix": "PagBank",
            "texto_ocr_bruto": "[OCR linhas detectadas]\nPagador Local",
            "mensagem": "OCR parcial concluido. Confira os dados antes de salvar.",
        }
        arquivo = SimpleUploadedFile("comprovante.jpg", b"imagem", content_type="image/jpeg")

        with patch.dict(os.environ, {"PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True"}), patch(
            "estoque.views.analisar_comprovante_pix",
            return_value=dados_local,
        ) as local_mock, self.assertLogs("estoque.views", level="WARNING") as logs:
            resposta = self.client.post(
                reverse("estoque:central_pix_analisar_comprovante"),
                {"comprovante": arquivo},
                secure=True,
            )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Pagador Local")
        self.assertIn("[OCR linhas detectadas]", dados["texto_ocr_bruto"])
        self.assertIn("OCR local fallback permitido", "\n".join(logs.output))
        local_mock.assert_called_once()

    def test_detalhe_pix_processar_ocr_agora_atualiza_dados_do_comprovante(self):
        cliente = Cliente.objects.create(nome="Cicero Cristiano Silva Souza", ativo=True)
        conteudo = (
            "Comprovante Pix\n"
            "Origem\n"
            "Nome: Cicero Cristiano Silva Souza\n"
            "Valor R$ 20,00\n"
            "Data 16/05/2026 17:30\n"
            "Banco do Brasil\n"
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain"),
            )

            resposta = self.client.post(
                reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                secure=True,
                follow=True,
            )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(pix.cliente_sugerido, cliente)
            self.assertEqual(pix.nome_pagador, "Cicero Cristiano Silva Souza")
            self.assertEqual(str(pix.valor), "20.00")
            self.assertEqual(pix.instituicao_pix, "Banco do Brasil")
            self.assertEqual(pix.status, PixRecebido.STATUS_PENDENTE)
            self.assertIn("Comprovante Pix", pix.texto_ocr_bruto)
            self.assertTrue(pix.comprovante)
            self.assertContains(resposta, "Texto OCR bruto")
            self.assertContains(resposta, "Reler comprovante (OCR)")

    def test_detalhe_pix_processar_google_vision_preenche_sem_criar_baixa(self):
        cliente = Cliente.objects.create(nome="Rubem Arruda de Souza", ativo=True)
        dados_vision = {
            "ok": True,
            "pagador": "RUBEM ARRUDA DE SOUZA",
            "valor": "847.70",
            "data_pagamento": "2026-05-05T16:45",
            "instituicao_pix": "Banpará",
            "texto_ocr_bruto": (
                "[Google Vision OCR]\n"
                "Dados de Origem\n"
                "Titular: RUBEM ARRUDA DE SOUZA\n"
                "Valor: 847,70\n"
            ),
        }
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root, PIX_USAR_GOOGLE_VISION=True):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("banpara.jpg", b"imagem", content_type="image/jpeg"),
            )

            with patch("estoque.views.analisar_comprovante_pix_google_vision", return_value=dados_vision):
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(pix.cliente_sugerido, cliente)
            self.assertEqual(pix.nome_pagador, "RUBEM ARRUDA DE SOUZA")
            self.assertEqual(str(pix.valor), "847.70")
            self.assertEqual(timezone.localtime(pix.data_pagamento).strftime("%Y-%m-%dT%H:%M"), "2026-05-05T16:45")
            self.assertEqual(pix.instituicao_pix, "Banpará")
            self.assertIn("[Google Vision OCR]", pix.texto_ocr_bruto)
            self.assertEqual(RecebimentoContaReceber.objects.count(), 0)

    def test_detalhe_pix_processar_google_vision_confirma_cliente_exato_normalizado(self):
        cliente = Cliente.objects.create(nome="João De Almeida E Silva", ativo=True)
        dados_vision = {
            "ok": True,
            "pagador": "Joao de Almeida E Silva",
            "valor": "5.00",
            "data_pagamento": "2026-05-18T07:08",
            "instituicao_pix": "Mercado Pago",
            "texto_ocr_bruto": (
                "[Google Vision OCR]\n"
                "Mercado Pago\n"
                "@ De\n"
                "Joao de Almeida E Silva\n"
                "Valor: R$ 5,00\n"
            ),
        }
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root, PIX_USAR_GOOGLE_VISION=True):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("mercado-pago.jpg", b"imagem", content_type="image/jpeg"),
            )

            with patch("estoque.views.analisar_comprovante_pix_google_vision", return_value=dados_vision):
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(pix.cliente, cliente)
            self.assertEqual(pix.cliente_sugerido, cliente)
            self.assertEqual(pix.nome_pagador, "Joao de Almeida E Silva")
            self.assertEqual(pix.status, PixRecebido.STATUS_PENDENTE)
            self.assertEqual(RecebimentoContaReceber.objects.count(), 0)

    def test_detalhe_pix_processar_google_vision_cliente_inexistente_continua_sem_confirmacao(self):
        dados_vision = {
            "ok": True,
            "pagador": "Cliente Inexistente Silva",
            "valor": "5.00",
            "data_pagamento": "2026-05-18T07:08",
            "instituicao_pix": "Mercado Pago",
            "texto_ocr_bruto": "[Google Vision OCR]\nCliente Inexistente Silva\nValor: R$ 5,00\n",
        }
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root, PIX_USAR_GOOGLE_VISION=True):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("mercado-pago.jpg", b"imagem", content_type="image/jpeg"),
            )

            with patch("estoque.views.analisar_comprovante_pix_google_vision", return_value=dados_vision):
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertIsNone(pix.cliente)
            self.assertIsNone(pix.cliente_sugerido)
            self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
            self.assertEqual(RecebimentoContaReceber.objects.count(), 0)

    def test_central_pix_cliente_confirmado_autocomplete_lista_ao_focar(self):
        Cliente.objects.create(nome="João De Almeida E Silva", ativo=True)
        Cliente.objects.create(nome="Maria Cliente Ativa", ativo=True)
        Cliente.objects.create(nome="Cliente Inativo", ativo=False)

        resposta = self.client.get(
            f"{reverse('estoque:clientes_autocomplete')}?contexto=pix_detalhe",
            secure=True,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(resposta.status_code, 200)
        nomes = [cliente["nome"] for cliente in resposta.json()["clientes"]]
        self.assertIn("João De Almeida E Silva", nomes)
        self.assertIn("Maria Cliente Ativa", nomes)
        self.assertNotIn("Cliente Inativo", nomes)
        self.assertLessEqual(len(nomes), 12)

    def test_central_pix_detalhe_carrega_clientes_para_autocomplete_local(self):
        cliente = Cliente.objects.create(
            nome="Jo\u00e3o De Almeida E Silva",
            apelido_nome_conhecido="Almeida",
            cpf_cnpj="123.456.789-00",
            whatsapp="(85) 99999-0000",
            ativo=True,
        )
        Cliente.objects.create(nome="Cliente Inativo", ativo=False)
        pix = PixRecebido.objects.create(
            nome_pagador="Joao de Almeida E Silva",
            valor=Decimal("5.00"),
            data_pagamento=timezone.now(),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "pixClientesAutocompleteData")
        clientes = resposta.context["clientes_pix_autocomplete"]
        item = next(cliente_local for cliente_local in clientes if cliente_local["id"] == cliente.id)
        self.assertEqual(item["nome"], "Jo\u00e3o De Almeida E Silva")
        self.assertIn("joao", item["busca"])
        self.assertIn("almeida", item["busca"])
        self.assertIn("12345678900", item["busca"])
        self.assertIn("85999990000", item["busca"])
        self.assertNotIn("Cliente Inativo", [cliente_local["nome"] for cliente_local in clientes])

    def test_central_pix_cliente_confirmado_autocomplete_busca_normalizada(self):
        cliente = Cliente.objects.create(
            nome="João De Almeida E Silva",
            apelido_nome_conhecido="Joao Almeida",
            cpf_cnpj="123.456.789-00",
            whatsapp="(85) 99999-0000",
            ativo=True,
        )

        for termo in ("alm", "joao", "123456", "999990000"):
            resposta = self.client.get(
                f"{reverse('estoque:clientes_autocomplete')}?contexto=pix_detalhe&q={termo}",
                secure=True,
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
            self.assertEqual(resposta.status_code, 200)
            ids = [item["id"] for item in resposta.json()["clientes"]]
            self.assertIn(cliente.id, ids)

    def test_central_pix_cliente_confirmado_autocomplete_sem_resultado_nao_confirma_cliente(self):
        Cliente.objects.create(nome="João De Almeida E Silva", ativo=True)

        resposta = self.client.get(
            f"{reverse('estoque:clientes_autocomplete')}?contexto=pix_detalhe&q=clienteinexistente",
            secure=True,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json()["clientes"], [])
        self.assertEqual(PixRecebido.objects.count(), 0)
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
        self.assertEqual(CreditoCliente.objects.count(), 0)

    def test_central_pix_pendente_pode_remover_cliente_confirmado_sem_baixa(self):
        cliente = Cliente.objects.create(nome="João De Almeida E Silva", ativo=True)
        conteudo = b"comprovante pix"
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                cliente=cliente,
                cliente_sugerido=cliente,
                nome_pagador="Joao de Almeida E Silva",
                valor=Decimal("5.00"),
                data_pagamento=timezone.now(),
                instituicao_pix="Mercado Pago",
                status=PixRecebido.STATUS_PENDENTE,
                texto_ocr_bruto="[Google Vision OCR]\nJoao de Almeida E Silva",
                comprovante=SimpleUploadedFile("mercado-pago.jpg", conteudo, content_type="image/jpeg"),
            )
            comprovante_nome = pix.comprovante.name

            resposta = self.client.post(
                reverse("estoque:central_pix_remover_cliente_confirmado", kwargs={"pix_id": pix.id}),
                secure=True,
                follow=True,
            )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertIsNone(pix.cliente)
            self.assertIsNone(pix.cliente_sugerido)
            self.assertEqual(pix.status, PixRecebido.STATUS_PENDENTE)
            self.assertEqual(pix.nome_pagador, "Joao de Almeida E Silva")
            self.assertEqual(str(pix.valor), "5.00")
            self.assertEqual(pix.instituicao_pix, "Mercado Pago")
            self.assertEqual(pix.comprovante.name, comprovante_nome)
            self.assertIn("[Google Vision OCR]", pix.texto_ocr_bruto)
            self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
            self.assertEqual(CreditoCliente.objects.count(), 0)
            self.assertContains(resposta, "Cliente confirmado removido")

    def test_central_pix_baixado_nao_remove_cliente_confirmado(self):
        cliente = Cliente.objects.create(nome="João De Almeida E Silva", ativo=True)
        pix = PixRecebido.objects.create(
            cliente=cliente,
            cliente_sugerido=cliente,
            nome_pagador="Joao de Almeida E Silva",
            valor=Decimal("5.00"),
            data_pagamento=timezone.now(),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_BAIXADO,
            texto_ocr_bruto="[Google Vision OCR]\nJoao de Almeida E Silva",
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_remover_cliente_confirmado", kwargs={"pix_id": pix.id}),
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        pix.refresh_from_db()
        self.assertEqual(pix.cliente, cliente)
        self.assertEqual(pix.cliente_sugerido, cliente)
        self.assertEqual(pix.status, PixRecebido.STATUS_BAIXADO)
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
        self.assertEqual(CreditoCliente.objects.count(), 0)
        self.assertContains(resposta, "Este Pix ja foi baixado/inativado")

    def test_receber_cliente_com_pix_sem_contas_abertas_oferece_saida_segura(self):
        cliente = Cliente.objects.create(nome="Cliente Sem Conta", ativo=True)
        pix = PixRecebido.objects.create(
            cliente=cliente,
            cliente_sugerido=cliente,
            nome_pagador="Cliente Sem Conta",
            valor=Decimal("5.00"),
            data_pagamento=timezone.now(),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
            texto_ocr_bruto="[Google Vision OCR]\nCliente Sem Conta",
        )

        resposta = self.client.get(
            f"{reverse('estoque:receber_cliente', kwargs={'cliente_id': cliente.id})}?pix_recebido={pix.id}",
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Este cliente nao tem contas abertas")
        self.assertContains(resposta, "Voltar ao Pix")
        self.assertContains(resposta, "Trocar cliente")
        self.assertContains(resposta, "foco_cliente=1")
        self.assertContains(resposta, "rc-btn-pix-voltar")
        self.assertContains(resposta, "rc-btn-pix-trocar")
        self.assertContains(resposta, "rc-btn-pix-remover")
        self.assertContains(resposta, "Remover cliente confirmado e manter pendente")
        self.assertContains(
            resposta,
            reverse("estoque:central_pix_remover_cliente_confirmado", kwargs={"pix_id": pix.id}),
        )
        conteudo = resposta.content.decode()
        self.assertLess(
            conteudo.rindex("Saldo atual em aberto"),
            conteudo.rindex("Este cliente nao tem contas abertas"),
        )
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
        self.assertEqual(CreditoCliente.objects.count(), 0)

    def test_detalhe_pix_com_foco_cliente_foca_sem_abrir_autocomplete(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Cliente Teste",
            valor=Decimal("5.00"),
            data_pagamento=timezone.now(),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta_sem_foco = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )
        resposta_com_foco = self.client.get(
            f"{reverse('estoque:central_pix_detalhe', kwargs={'pix_id': pix.id})}?foco_cliente=1",
            secure=True,
        )

        self.assertEqual(resposta_sem_foco.status_code, 200)
        self.assertEqual(resposta_com_foco.status_code, 200)
        self.assertContains(resposta_sem_foco, "const focarClienteConfirmado = true;")
        self.assertContains(resposta_com_foco, "const focarClienteConfirmado = true;")
        self.assertContains(resposta_com_foco, "clienteConfirmadoBusca.select();")
        self.assertContains(resposta_com_foco, 'addEventListener("click"')
        self.assertContains(resposta_com_foco, 'addEventListener("input"')
        conteudo = resposta_com_foco.content.decode()
        bloco_foco = conteudo.split("if (focarClienteConfirmado && clienteConfirmadoBusca)", 1)[1]
        bloco_foco = bloco_foco.split('document.querySelectorAll("form")', 1)[0]
        self.assertNotIn("buscarClientes", bloco_foco)
        self.assertNotIn('addEventListener("focus"', conteudo)

    def test_detalhe_pix_com_cliente_confirmado_destaca_sem_focar_digitacao(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Confirmado", ativo=True)
        pix = PixRecebido.objects.create(
            cliente=cliente,
            cliente_sugerido=cliente,
            nome_pagador="Cliente Pix Confirmado",
            valor=Decimal("5.00"),
            data_pagamento=timezone.now(),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "const focarClienteConfirmado = false;")
        self.assertContains(resposta, "pix-client-confirmed-visible")
        self.assertContains(resposta, "Cliente encontrado com seguranca")

    def test_detalhe_pix_com_dados_lidos_sem_cliente_orienta_vincular_pagador(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Lisandra De Oliveira Da Silva",
            valor=Decimal("548.30"),
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 14, 9, 15)),
            instituicao_pix="Stone",
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
        )

        resposta = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Este Pix j\u00e1 tem dados lidos")
        self.assertContains(resposta, "Vincule o pagador a um cliente para usar este Pix na baixa")
        self.assertContains(resposta, "const focarClienteConfirmado = true;")
        pix.refresh_from_db()
        self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)

    def test_detalhe_pix_processar_ocr_com_erro_mantem_comprovante_salvo(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante.jpg", b"imagem", content_type="image/jpeg"),
            )

            with patch.dict(os.environ, {"PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True"}), patch(
                "estoque.views.analisar_comprovante_pix",
                side_effect=RuntimeError("timeout render"),
            ):
                with self.assertLogs("estoque.views", level="WARNING") as logs:
                    resposta = self.client.post(
                        reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                        secure=True,
                        follow=True,
                    )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
            self.assertTrue(pix.comprovante)
            self.assertIn("[OCR erro]", pix.texto_ocr_bruto)
            self.assertIn("RuntimeError", pix.texto_ocr_bruto)
            self.assertIn(f"pix_id={pix.id}", "\n".join(logs.output))
            self.assertIn("arquivo=comprovante_", "\n".join(logs.output))
            self.assertIn("timeout render", "\n".join(logs.output))
            self.assertContains(resposta, "Texto OCR bruto")

    def test_detalhe_pix_processar_ocr_excecao_inesperada_nao_retorna_500(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("banpara.jpg", b"imagem", content_type="image/jpeg"),
            )

            with patch.dict(os.environ, {"PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True"}), patch(
                "estoque.views.analisar_comprovante_pix",
                side_effect=ValueError("falha banpara"),
            ):
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertTrue(pix.comprovante)
            self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
            self.assertEqual(str(pix.valor), "0.00")
            self.assertIn("[OCR erro]", pix.texto_ocr_bruto)
            self.assertIn("ValueError", pix.texto_ocr_bruto)
            self.assertContains(resposta, "OCR nao conseguiu ler todos os dados")

    def test_detalhe_pix_processar_ocr_com_timeout_retornado_mantem_comprovante_salvo(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante.jpg", b"imagem", content_type="image/jpeg"),
            )
            dados_timeout = {
                "ok": False,
                "pagador": "",
                "valor": "",
                "data_pagamento": "",
                "instituicao_pix": "",
                "texto_ocr_bruto": "ERRO OCR: RuntimeError: Tesseract process timeout",
            }

            with patch.dict(os.environ, {"PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True"}), patch(
                "estoque.views.analisar_comprovante_pix",
                return_value=dados_timeout,
            ):
                with self.assertLogs("estoque.views", level="WARNING") as logs:
                    resposta = self.client.post(
                        reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                        secure=True,
                        follow=True,
                    )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
            self.assertEqual(str(pix.valor), "0.00")
            self.assertTrue(pix.comprovante)
            self.assertIn("[OCR erro]", pix.texto_ocr_bruto)
            self.assertIn(f"pix_id={pix.id}", "\n".join(logs.output))
            self.assertIn("Tesseract process timeout", "\n".join(logs.output))
            self.assertContains(resposta, "Texto OCR bruto")

    def test_detalhe_pix_processar_ocr_aproveita_texto_parcial_com_valor_e_data(self):
        conteudo = (
            "ERRO OCR: timeout parcial no Render\n"
            "21 ABR 2026 - 13:05:01\n"
            "Valor R$ 172,00\n"
            "Tipo de transferencia Pix\n"
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante-parcial.txt", conteudo, content_type="text/plain"),
            )

            with self.assertLogs("estoque.views", level="INFO") as logs:
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
            self.assertEqual(pix.nome_pagador, "")
            self.assertEqual(str(pix.valor), "172.00")
            self.assertEqual(timezone.localtime(pix.data_pagamento).strftime("%Y-%m-%dT%H:%M"), "2026-04-21T13:05")
            self.assertIn("Valor R$ 172,00", pix.texto_ocr_bruto)
            self.assertContains(resposta, "OCR parcial concluido. Confira os dados antes de qualquer baixa.")
            self.assertContains(resposta, "Reler comprovante (OCR)")
            self.assertIn("extraiu_valor=True", "\n".join(logs.output))
            self.assertIn("extraiu_data=True", "\n".join(logs.output))

    def test_detalhe_pix_processar_ocr_render_para_apos_topo_com_valor_e_data(self):
        chamadas = []
        configs = []

        def image_to_string(imagem, **kwargs):
            recorte = imagem.info.get("ocr_recorte", "inteira")
            chamadas.append(recorte)
            configs.append(kwargs.get("config"))
            if recorte == "faixa_valor_principal":
                return "R$ 172,00"
            if recorte == "faixa_data_principal":
                return "21 ABR 2026 - 13:05:01"
            raise RuntimeError(f"recorte inesperado: {recorte}")

        pytesseract_fake = types.SimpleNamespace(
            pytesseract=types.SimpleNamespace(tesseract_cmd=""),
            image_to_string=image_to_string,
        )

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante-topo.png", self._imagem_pix_teste((485, 1600)), content_type="image/png"),
            )

            with patch.dict(os.environ, {"PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True"}), patch.dict(
                "sys.modules",
                {"pytesseract": pytesseract_fake},
            ), patch(
                "estoque.utils_pix._resolver_tesseract_cmd",
                return_value="tesseract",
            ), patch("estoque.utils_pix.OCR_RENDER_MODO_LEVE", True), self.assertLogs("estoque.utils_pix", level="WARNING") as logs:
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(chamadas, ["faixa_valor_principal", "faixa_data_principal"])
            self.assertEqual(
                configs,
                [
                    "--oem 1 --psm 7 -c tessedit_char_whitelist=0123456789R$r$.,,",
                    "--oem 1 --psm 7",
                ],
            )
            self.assertEqual(str(pix.valor), "172.00")
            self.assertEqual(timezone.localtime(pix.data_pagamento).strftime("%Y-%m-%dT%H:%M"), "2026-04-21T13:05")
            self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)
            self.assertIn("R$ 172,00", pix.texto_ocr_bruto)
            self.assertContains(resposta, "OCR parcial concluido. Confira os dados antes de qualquer baixa.")
            self.assertIn("modo leve parou apos faixas", "\n".join(logs.output))
            self.assertIn("config=--oem 1 --psm 7", "\n".join(logs.output))
            self.assertIn("extraiu_valor=True", "\n".join(logs.output))
            self.assertIn("extraiu_data=True", "\n".join(logs.output))

    def test_detalhe_pix_processar_ocr_render_faixas_evita_bloco_grande_com_timeout(self):
        chamadas = []
        timeouts = []

        def image_to_string(imagem, **kwargs):
            recorte = imagem.info.get("ocr_recorte", "inteira")
            chamadas.append(recorte)
            timeouts.append(kwargs.get("timeout"))
            if recorte == "faixa_valor_principal":
                return "R$ 172,00"
            if recorte == "faixa_data_principal":
                return "21 ABR 2026 - 13:05:01"
            if recorte in {"rapido_superior", "rapido_meio_superior"}:
                raise RuntimeError("Tesseract process timeout")
            raise RuntimeError(f"recorte inesperado: {recorte}")

        pytesseract_fake = types.SimpleNamespace(
            pytesseract=types.SimpleNamespace(tesseract_cmd=""),
            image_to_string=image_to_string,
        )

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante-render.png", self._imagem_pix_teste((485, 1600)), content_type="image/png"),
            )

            with patch.dict(os.environ, {"PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True"}), patch.dict(
                "sys.modules",
                {"pytesseract": pytesseract_fake},
            ), patch(
                "estoque.utils_pix._resolver_tesseract_cmd",
                return_value="tesseract",
            ), patch("estoque.utils_pix.OCR_RENDER_MODO_LEVE", True), self.assertLogs("estoque.utils_pix", level="WARNING") as logs:
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(chamadas, ["faixa_valor_principal", "faixa_data_principal"])
            self.assertEqual(timeouts, [4, 4])
            self.assertTrue(pix.comprovante)
            self.assertEqual(str(pix.valor), "172.00")
            self.assertEqual(timezone.localtime(pix.data_pagamento).strftime("%Y-%m-%dT%H:%M"), "2026-04-21T13:05")
            self.assertIn("R$ 172,00", pix.texto_ocr_bruto)
            self.assertContains(resposta, "OCR parcial concluido. Confira os dados antes de qualquer baixa.")
            self.assertIn("recorte=faixa_valor_principal", "\n".join(logs.output))
            self.assertIn("recorte=faixa_data_principal", "\n".join(logs.output))
            self.assertIn("texto=R$ 172,00", "\n".join(logs.output))
            self.assertIn("modo leve parou apos faixas", "\n".join(logs.output))

    def test_analisar_comprovante_pix_nubank_faixas_calculadas_na_imagem_700(self):
        with patch("estoque.utils_pix.OCR_RENDER_MODO_LEVE", True):
            _original, _reduzido, recortes = _preparar_recortes_ocr(self._imagem_pix_teste((485, 1600)))

        por_nome = {nome: (imagem, caixa) for nome, imagem, caixa in recortes}
        imagem_valor, caixa_valor = por_nome["faixa_valor_principal"]
        imagem_data, caixa_data = por_nome["faixa_data_principal"]

        self.assertEqual(imagem_valor.info["ocr_base_nome"], "nubank_700")
        self.assertEqual(imagem_valor.info["ocr_base_tamanho"][0], 700)
        self.assertEqual(imagem_valor.info["ocr_caixa_percentual"], (0.35, 0.38, 1.00, 0.47))
        self.assertEqual(imagem_data.info["ocr_caixa_percentual"], (0.05, 0.25, 0.95, 0.33))
        self.assertLess(caixa_data[1], int(imagem_data.info["ocr_base_tamanho"][1] * 0.34))
        self.assertLess(caixa_valor[1], int(imagem_valor.info["ocr_base_tamanho"][1] * 0.48))
        self.assertEqual(imagem_valor.info["ocr_tamanho_depois"][0], imagem_valor.info["ocr_tamanho_antes"][0] * 3)

    def test_analisar_comprovante_pix_ocr_por_linhas_extrai_valor_e_data(self):
        arquivo = SimpleUploadedFile(
            "comprovante-linhas.png",
            self._imagem_linhas_nubank_teste(),
            content_type="image/png",
        )
        timeouts = {}

        respostas = {
            "linha_01": "NU",
            "linha_02": "Comprovante de",
            "linha_03": "transferencia",
            "linha_04": "21 ABR 2026 - 13:05:01",
            "linha_05": "Valor R$ 172,00",
        }

        def image_to_string(imagem, **kwargs):
            recorte = imagem.info.get("ocr_recorte")
            timeouts[recorte] = kwargs.get("timeout")
            return respostas.get(recorte, "")

        pytesseract_fake = types.SimpleNamespace(
            pytesseract=types.SimpleNamespace(tesseract_cmd=""),
            image_to_string=image_to_string,
        )

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "172.00")
        self.assertEqual(dados["data_pagamento"], "2026-04-21T13:05")
        self.assertEqual(timeouts["linha_04"], 4)
        self.assertIn("[OCR linhas detectadas]", dados["texto_ocr_bruto"])
        self.assertIn("04 candidata_data: 21 ABR 2026 - 13:05:01", dados["texto_ocr_bruto"])

    def test_analisar_comprovante_pix_ocr_por_linhas_continua_ate_pagador(self):
        arquivo = SimpleUploadedFile(
            "comprovante-linhas-santander.png",
            self._imagem_linhas_ocr_teste(),
            content_type="image/png",
        )
        chamadas = []
        respostas = {
            "linha_01": "RS 500,00",
            "linha_02": "18/05/2026 as 17:45:00",
            "linha_03": "Dados do pagador",
            "linha_04": "De",
            "linha_05": "lvanildo Ferraz Patricio Junior",
        }

        def image_to_string(imagem, **_kwargs):
            recorte = imagem.info.get("ocr_recorte")
            chamadas.append(recorte)
            return respostas.get(recorte, "")

        pytesseract_fake = types.SimpleNamespace(
            pytesseract=types.SimpleNamespace(tesseract_cmd=""),
            image_to_string=image_to_string,
        )

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "500.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-18T17:45")
        self.assertEqual(dados["pagador"], "Ivanildo Ferraz Patricio Junior")
        self.assertIn("linha_05", chamadas)
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")

    def test_analisar_comprovante_pix_ocr_por_linhas_timeout_continua(self):
        arquivo = SimpleUploadedFile(
            "comprovante-linhas-timeout.png",
            self._imagem_linhas_ocr_teste(),
            content_type="image/png",
        )

        def image_to_string(imagem, **_kwargs):
            recorte = imagem.info.get("ocr_recorte")
            if recorte == "linha_01":
                raise RuntimeError("Tesseract process timeout")
            if recorte == "linha_02":
                return "21 ABR 2026 - 13:05:01"
            if recorte == "linha_04":
                return "R$ 172,00"
            return ""

        pytesseract_fake = types.SimpleNamespace(
            pytesseract=types.SimpleNamespace(tesseract_cmd=""),
            image_to_string=image_to_string,
        )

        with patch.dict("sys.modules", {"pytesseract": pytesseract_fake}), patch(
            "estoque.utils_pix._resolver_tesseract_cmd",
            return_value="tesseract",
        ):
            dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "172.00")
        self.assertEqual(dados["data_pagamento"], "2026-04-21T13:05")

    def test_analisar_comprovante_pix_ids_de_transacao_nao_viram_valor(self):
        conteudo = (
            "Comprovante de transferencia\n"
            "ID da transacao 14605308174802dd\n"
            "E182361202604211305\n"
            "NU PAGAMENTOS - IP\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante-id.txt", conteudo, content_type="text/plain")

        dados = analisar_comprovante_pix(arquivo)

        self.assertEqual(dados["valor"], "")
        self.assertEqual(dados["data_pagamento"], "")

    def test_detalhe_pix_processar_ocr_salva_debug_recortes_com_variavel(self):
        chamadas = []

        def image_to_string(imagem, **_kwargs):
            recorte = imagem.info.get("ocr_recorte", "inteira")
            chamadas.append(recorte)
            if recorte == "faixa_valor_principal":
                return "R$ 172,00"
            if recorte == "faixa_data_principal":
                return "21 ABR 2026 - 13:05:01"
            return ""

        pytesseract_fake = types.SimpleNamespace(
            pytesseract=types.SimpleNamespace(tesseract_cmd=""),
            image_to_string=image_to_string,
        )

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root, DEBUG=False):
            pix = PixRecebido.objects.create(
                valor="0.00",
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante-debug.png", self._imagem_pix_teste((485, 1600)), content_type="image/png"),
            )

            with patch.dict(os.environ, {
                "PIX_OCR_DEBUG_CROPS": "True",
                "PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True",
            }), patch.dict(
                "sys.modules",
                {"pytesseract": pytesseract_fake},
            ), patch(
                "estoque.utils_pix._resolver_tesseract_cmd",
                return_value="tesseract",
            ), patch("estoque.utils_pix.OCR_RENDER_MODO_LEVE", True):
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertIn("[OCR debug recortes]", pix.texto_ocr_bruto)
            self.assertIn(f"debug_ocr/pix_{pix.id}_faixa_valor_principal.jpg", pix.texto_ocr_bruto)
            self.assertIn(f"debug_ocr/pix_{pix.id}_faixa_data_principal.jpg", pix.texto_ocr_bruto)
            self.assertEqual(str(pix.valor), "172.00")

    def test_detalhe_pix_processar_ocr_sem_data_preserva_data_existente(self):
        data_original = timezone.make_aware(timezone.datetime(2026, 4, 21, 13, 5))
        dados_sem_data = {
            "ok": True,
            "pagador": "",
            "valor": "172.00",
            "data_pagamento": "",
            "instituicao_pix": "",
            "texto_ocr_bruto": "[OCR faixa_valor_principal]\nR$ 172,00",
        }

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                valor="0.00",
                data_pagamento=data_original,
                status=PixRecebido.STATUS_NAO_IDENTIFICADO,
                texto_ocr_bruto="OCR pendente",
                comprovante=SimpleUploadedFile("comprovante-sem-data.jpg", b"imagem", content_type="image/jpeg"),
            )

            with patch.dict(os.environ, {"PIX_PERMITIR_OCR_LOCAL_FALLBACK": "True"}), patch(
                "estoque.views.analisar_comprovante_pix",
                return_value=dados_sem_data,
            ):
                resposta = self.client.post(
                    reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id}),
                    secure=True,
                    follow=True,
                )

            self.assertEqual(resposta.status_code, 200)
            pix.refresh_from_db()
            self.assertEqual(str(pix.valor), "172.00")
            self.assertEqual(pix.data_pagamento, data_original)
            self.assertTrue(pix.comprovante)
            self.assertContains(resposta, "OCR parcial concluido. Confira os dados antes de qualquer baixa.")

    def test_detalhe_pix_usa_rota_propria_do_comprovante(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix_original = PixRecebido.objects.create(
                nome_pagador="Pix original com imagem",
                valor="35.50",
                status=PixRecebido.STATUS_PENDENTE,
                comprovante=SimpleUploadedFile("pix-original.jpg", b"imagem original", content_type="image/jpeg"),
            )
            pix = PixRecebido.objects.create(
                nome_pagador="Pix com imagem",
                valor="35.50",
                status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
                pix_original=pix_original,
                comprovante=SimpleUploadedFile("pix-render.jpg", b"imagem pix", content_type="image/jpeg"),
            )
            comprovante_url = reverse("estoque:central_pix_comprovante", kwargs={"pix_id": pix.id})
            comprovante_original_url = reverse("estoque:central_pix_comprovante", kwargs={"pix_id": pix_original.id})

            resposta = self.client.get(
                reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
                secure=True,
            )

            self.assertContains(resposta, f'href="{comprovante_url}"')
            self.assertContains(resposta, f'src="{comprovante_url}"')
            self.assertContains(resposta, f'src="{comprovante_original_url}"')
            self.assertNotContains(resposta, "/media/")

    def test_rota_comprovante_pix_retorna_arquivo_salvo(self):
        conteudo = b"imagem pix"
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            pix = PixRecebido.objects.create(
                nome_pagador="Pix com comprovante",
                valor="20.00",
                status=PixRecebido.STATUS_PENDENTE,
                comprovante=SimpleUploadedFile("comprovante-render.jpg", conteudo, content_type="image/jpeg"),
            )

            resposta = self.client.get(
                reverse("estoque:central_pix_comprovante", kwargs={"pix_id": pix.id}),
                secure=True,
            )

            self.assertEqual(resposta.status_code, 200)
            self.assertEqual(resposta["Content-Type"], "image/jpeg")
            self.assertIn('inline; filename="comprovante-render', resposta["Content-Disposition"])
            self.assertEqual(b"".join(resposta.streaming_content), conteudo)

    def test_abrir_comprovante_pix_usa_fallback_media_root_para_arquivo_antigo(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            caminho_antigo = Path(media_root) / "pix" / "comprovantes" / "antigo.txt"
            caminho_antigo.parent.mkdir(parents=True, exist_ok=True)
            caminho_antigo.write_bytes(b"arquivo antigo")
            pix = PixRecebido(valor=Decimal("0.00"), data_pagamento=timezone.now())
            pix.comprovante.name = "pix/comprovantes/antigo.txt"

            with patch.object(pix.comprovante, "open", side_effect=FileNotFoundError):
                arquivo = views._abrir_comprovante_pix(pix)

            try:
                self.assertEqual(arquivo.read(), b"arquivo antigo")
            finally:
                arquivo.close()

    def test_rota_comprovante_pix_retorna_404_sem_comprovante(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix sem comprovante",
            valor="10.00",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.get(
            reverse("estoque:central_pix_comprovante", kwargs={"pix_id": pix.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 404)

    def test_detalhe_pix_pendente_permite_marcar_como_ignorado(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix para ignorar",
            valor="10.00",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            {"acao": "ignorar"},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        self.assertRedirects(resposta, reverse("estoque:central_pix"), fetch_redirect_response=False)
        pix.refresh_from_db()
        self.assertEqual(pix.status, PixRecebido.STATUS_IGNORADO)

    def test_ignorar_pix_preserva_dados_conferidos(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Ignorado", ativo=True)
        pix_original = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Ivanildo Ferraz Patricio Junior",
            valor="500.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_BAIXADO,
        )
        data_pagamento = timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45))
        pix = PixRecebido.objects.create(
            cliente=cliente,
            cliente_sugerido=cliente,
            pix_original=pix_original,
            nome_pagador="Ivanildo Ferraz Patricio Junior",
            valor="500.00",
            data_pagamento=data_pagamento,
            instituicao_pix="Mercado Pago",
            observacao="Conferido antes de ignorar.",
            status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            {
                "acao": "ignorar",
                "valor": "",
                "nome_pagador": "",
                "instituicao_pix": "",
                "cliente": "",
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        self.assertRedirects(resposta, reverse("estoque:central_pix"), fetch_redirect_response=False)
        pix.refresh_from_db()
        self.assertEqual(pix.status, PixRecebido.STATUS_IGNORADO)
        self.assertEqual(str(pix.valor), "500.00")
        self.assertEqual(pix.data_pagamento, data_pagamento)
        self.assertEqual(pix.instituicao_pix, "Mercado Pago")
        self.assertEqual(pix.nome_pagador, "Ivanildo Ferraz Patricio Junior")
        self.assertEqual(pix.cliente, cliente)
        self.assertEqual(pix.cliente_sugerido, cliente)
        self.assertEqual(pix.pix_original, pix_original)
        self.assertIn("Conferido antes de ignorar.", pix.observacao)
        self.assertIn("Pix ignorado sem baixa pelo operador", pix.observacao)

    def _criar_conta_receber_pix(self, cliente, valor="100.00"):
        venda = Venda.objects.create(
            cliente=cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A prazo",
            total=valor,
        )
        return ContaReceber.objects.create(
            venda=venda,
            cliente=cliente,
            data_emissao=timezone.localdate(),
            valor_original=valor,
            valor_em_aberto=valor,
            status=ContaReceber.STATUS_ABERTA,
        )

    def _post_receber_cliente(self, cliente, valor, destino_diferenca="troco", rota="", next_url="", follow=False):
        url = reverse("estoque:receber_cliente", kwargs={"cliente_id": cliente.id})
        parametros = {}
        if rota:
            parametros["rota"] = rota
        if next_url:
            parametros["next"] = next_url
        if parametros:
            url = f"{url}?{urlencode(parametros)}"
        return self.client.post(
            url,
            {
                "data_recebimento": timezone.localdate().isoformat(),
                "valor": valor,
                "forma_pagamento": "PIX",
                "destino_diferenca": destino_diferenca,
            },
            secure=True,
            follow=follow,
        )

    def _criar_operacao_recebimento_cliente(self, cliente, status=OperacaoRecebimentoCliente.STATUS_RECIBO_PENDENTE):
        return OperacaoRecebimentoCliente.objects.create(
            cliente=cliente,
            cliente_nome_snapshot=cliente.nome,
            valor_recebido=Decimal("100.00"),
            valor_aplicado=Decimal("100.00"),
            credito_gerado=Decimal("0.00"),
            saldo_anterior=Decimal("100.00"),
            saldo_atual=Decimal("0.00"),
            data_recebimento=timezone.localdate(),
            forma_pagamento="PIX",
            status_recibo=status,
        )

    def _url_confirmar_recibo(self, cliente, operacao):
        return reverse(
            "estoque:receber_cliente_confirmar_recibo",
            kwargs={"cliente_id": cliente.id, "operacao_id": operacao.id},
        )

    def _url_recebimento_confirmado(self, cliente, operacao):
        return reverse(
            "estoque:receber_cliente_confirmado",
            kwargs={"cliente_id": cliente.id, "operacao_id": operacao.id},
        )

    def _url_recibos_pendentes(self):
        return reverse("estoque:recebimentos_recibos_pendentes")

    def test_confirmar_recibo_valido_marca_enviado_com_usuario(self):
        usuario = get_user_model().objects.create_user(username="confirmador", password="senha")
        self.client.force_login(usuario)
        cliente = Cliente.objects.create(nome="Cliente Recibo Confirmado", ativo=True)
        operacao = self._criar_operacao_recebimento_cliente(cliente)

        resposta = self.client.post(self._url_confirmar_recibo(cliente, operacao), secure=True)

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["status_recibo"], OperacaoRecebimentoCliente.STATUS_RECIBO_ENVIADO)
        self.assertEqual(dados["mensagem"], "Recibo confirmado como enviado.")
        self.assertIn("confirmado_em", dados)
        self.assertEqual(dados["confirmado_por"], "confirmador")
        operacao.refresh_from_db()
        self.assertEqual(operacao.status_recibo, OperacaoRecebimentoCliente.STATUS_RECIBO_ENVIADO)
        self.assertIsNotNone(operacao.recibo_confirmado_em)
        self.assertEqual(operacao.recibo_confirmado_por, usuario)

    def test_confirmar_recibo_anonimo_marca_enviado_sem_usuario(self):
        cliente = Cliente.objects.create(nome="Cliente Recibo Anonimo", ativo=True)
        operacao = self._criar_operacao_recebimento_cliente(cliente)

        resposta = self.client.post(self._url_confirmar_recibo(cliente, operacao), secure=True)

        self.assertEqual(resposta.status_code, 200)
        operacao.refresh_from_db()
        self.assertEqual(operacao.status_recibo, OperacaoRecebimentoCliente.STATUS_RECIBO_ENVIADO)
        self.assertIsNotNone(operacao.recibo_confirmado_em)
        self.assertIsNone(operacao.recibo_confirmado_por)
        self.assertEqual(resposta.json()["confirmado_por"], "")

    def test_confirmar_recibo_idempotente_preserva_confirmacao_original(self):
        usuario = get_user_model().objects.create_user(username="primeiro", password="senha")
        self.client.force_login(usuario)
        cliente = Cliente.objects.create(nome="Cliente Recibo Idempotente", ativo=True)
        operacao = self._criar_operacao_recebimento_cliente(cliente)

        primeira = self.client.post(self._url_confirmar_recibo(cliente, operacao), secure=True)
        operacao.refresh_from_db()
        confirmado_em = operacao.recibo_confirmado_em
        confirmado_por = operacao.recibo_confirmado_por
        segunda = self.client.post(self._url_confirmar_recibo(cliente, operacao), secure=True)

        self.assertEqual(primeira.status_code, 200)
        self.assertEqual(segunda.status_code, 200)
        dados = segunda.json()
        self.assertTrue(dados["ok"])
        self.assertTrue(dados["ja_confirmado"])
        self.assertEqual(dados["mensagem"], "Este recibo ja estava confirmado como enviado.")
        operacao.refresh_from_db()
        self.assertEqual(operacao.recibo_confirmado_em, confirmado_em)
        self.assertEqual(operacao.recibo_confirmado_por, confirmado_por)

    def test_confirmar_recibo_operacao_de_outro_cliente_retorna_404(self):
        cliente_a = Cliente.objects.create(nome="Cliente Operacao A", ativo=True)
        cliente_b = Cliente.objects.create(nome="Cliente Operacao B", ativo=True)
        operacao = self._criar_operacao_recebimento_cliente(cliente_a)

        resposta = self.client.post(self._url_confirmar_recibo(cliente_b, operacao), secure=True)

        self.assertEqual(resposta.status_code, 404)
        operacao.refresh_from_db()
        self.assertEqual(operacao.status_recibo, OperacaoRecebimentoCliente.STATUS_RECIBO_PENDENTE)
        self.assertIsNone(operacao.recibo_confirmado_em)

    def test_confirmar_recibo_operacao_inexistente_retorna_404(self):
        cliente = Cliente.objects.create(nome="Cliente Operacao Inexistente", ativo=True)

        resposta = self.client.post(
            reverse(
                "estoque:receber_cliente_confirmar_recibo",
                kwargs={"cliente_id": cliente.id, "operacao_id": 999999},
            ),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 404)

    def test_confirmar_recibo_get_retorna_405_sem_alterar_status(self):
        cliente = Cliente.objects.create(nome="Cliente Recibo GET", ativo=True)
        operacao = self._criar_operacao_recebimento_cliente(cliente)

        resposta = self.client.get(self._url_confirmar_recibo(cliente, operacao), secure=True)

        self.assertEqual(resposta.status_code, 405)
        operacao.refresh_from_db()
        self.assertEqual(operacao.status_recibo, OperacaoRecebimentoCliente.STATUS_RECIBO_PENDENTE)
        self.assertIsNone(operacao.recibo_confirmado_em)

    def test_confirmar_recibo_dispensado_nao_marca_enviado(self):
        cliente = Cliente.objects.create(nome="Cliente Recibo Dispensado", ativo=True)
        operacao = self._criar_operacao_recebimento_cliente(
            cliente,
            status=OperacaoRecebimentoCliente.STATUS_RECIBO_DISPENSADO,
        )

        resposta = self.client.post(self._url_confirmar_recibo(cliente, operacao), secure=True)

        self.assertEqual(resposta.status_code, 409)
        dados = resposta.json()
        self.assertFalse(dados["ok"])
        self.assertEqual(dados["status_recibo"], OperacaoRecebimentoCliente.STATUS_RECIBO_DISPENSADO)
        operacao.refresh_from_db()
        self.assertEqual(operacao.status_recibo, OperacaoRecebimentoCliente.STATUS_RECIBO_DISPENSADO)
        self.assertIsNone(operacao.recibo_confirmado_em)
        self.assertIsNone(operacao.recibo_confirmado_por)

    def test_receber_cliente_uma_conta_cria_operacao_e_relaciona_baixa(self):
        usuario = get_user_model().objects.create_user(username="operador", password="senha")
        self.client.force_login(usuario)
        cliente = Cliente.objects.create(nome="Cliente Operacao Unica", ativo=True)
        self._criar_conta_receber_pix(cliente, "100.00")

        resposta = self._post_receber_cliente(cliente, "60,00")

        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(OperacaoRecebimentoCliente.objects.count(), 1)
        self.assertEqual(RecebimentoContaReceber.objects.count(), 1)
        operacao = OperacaoRecebimentoCliente.objects.get()
        recebimento = RecebimentoContaReceber.objects.get()
        self.assertEqual(recebimento.operacao, operacao)
        self.assertEqual(operacao.status_recibo, OperacaoRecebimentoCliente.STATUS_RECIBO_PENDENTE)
        self.assertEqual(operacao.cliente, cliente)
        self.assertEqual(operacao.cliente_nome_snapshot, "Cliente Operacao Unica")
        self.assertEqual(operacao.valor_recebido, Decimal("60.00"))
        self.assertEqual(operacao.valor_aplicado, Decimal("60.00"))
        self.assertEqual(operacao.credito_gerado, Decimal("0.00"))
        self.assertEqual(operacao.saldo_anterior, Decimal("100.00"))
        self.assertEqual(operacao.saldo_atual, Decimal("40.00"))
        self.assertEqual(operacao.forma_pagamento, "PIX")
        self.assertEqual(operacao.criado_por, usuario)

    def test_receber_cliente_post_redireciona_para_tela_confirmada_da_operacao(self):
        cliente = Cliente.objects.create(nome="Cliente Redireciona Confirmado", ativo=True)
        self._criar_conta_receber_pix(cliente, "100.00")

        resposta = self._post_receber_cliente(cliente, "100,00")

        operacao = OperacaoRecebimentoCliente.objects.get()
        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(resposta["Location"], self._url_recebimento_confirmado(cliente, operacao))
        self.assertIn(f"/cliente/{cliente.id}/operacao/{operacao.id}/recebimento-confirmado/", resposta["Location"])

    def test_recibos_pendentes_lista_apenas_status_pendente(self):
        cliente_pendente = Cliente.objects.create(nome="Cliente Recibo Pendente", ativo=True)
        cliente_enviado = Cliente.objects.create(nome="Cliente Recibo Enviado", ativo=True)
        cliente_dispensado = Cliente.objects.create(nome="Cliente Recibo Dispensado", ativo=True)
        self._criar_operacao_recebimento_cliente(cliente_pendente)
        self._criar_operacao_recebimento_cliente(
            cliente_enviado,
            status=OperacaoRecebimentoCliente.STATUS_RECIBO_ENVIADO,
        )
        self._criar_operacao_recebimento_cliente(
            cliente_dispensado,
            status=OperacaoRecebimentoCliente.STATUS_RECIBO_DISPENSADO,
        )

        resposta = self.client.get(self._url_recibos_pendentes(), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertTemplateUsed(resposta, "estoque/recebimentos_recibos_pendentes.html")
        self.assertContains(resposta, "Cliente Recibo Pendente")
        self.assertNotContains(resposta, "Cliente Recibo Enviado")
        self.assertNotContains(resposta, "Cliente Recibo Dispensado")
        self.assertContains(resposta, 'id="recibosPendentesQtd">1</strong>')

    def test_recibos_pendentes_mostra_dados_e_acoes_da_operacao(self):
        cliente = Cliente.objects.create(
            nome="Cliente Recibo Acao",
            whatsapp="(85) 97777-1111",
            ativo=True,
        )
        operacao = self._criar_operacao_recebimento_cliente(cliente)
        operacao.rota_snapshot = "Furo da Marinha"
        operacao.comprovante_dados = {
            "cliente_id": cliente.id,
            "cliente_nome": cliente.nome,
            "valor_pago": "100.00",
            "saldo_atual": "0.00",
            "contas": [{"conta_id": 10, "valor_aplicado": "100.00"}],
            "contas_abertas": [],
        }
        operacao.save(update_fields=["rota_snapshot", "comprovante_dados"])

        resposta = self.client.get(self._url_recibos_pendentes(), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Cliente Recibo Acao")
        self.assertContains(resposta, "R$ 100,00")
        self.assertContains(resposta, "PIX")
        self.assertContains(resposta, "Furo da Marinha")
        self.assertContains(resposta, "Pendente")
        self.assertContains(resposta, "Contas abatidas")
        self.assertContains(resposta, ">1</strong>", html=False)
        self.assertContains(resposta, 'href="{}"'.format(self._url_recebimento_confirmado(cliente, operacao)))
        self.assertContains(
            resposta,
            reverse(
                "estoque:receber_cliente_operacao_recibo_card_imagem",
                kwargs={"cliente_id": cliente.id, "operacao_id": operacao.id},
            ),
        )
        self.assertContains(resposta, "Enviar WhatsApp")
        self.assertContains(resposta, f'data-confirmar-recibo-url="{self._url_confirmar_recibo(cliente, operacao)}"')

    def test_recibos_pendentes_whatsapp_usa_mensagem_curta_e_nao_altera_status(self):
        cliente = Cliente.objects.create(
            nome="Cliente Recibo WhatsApp Curto",
            whatsapp="(85) 96666-2222",
            ativo=True,
        )
        operacao = self._criar_operacao_recebimento_cliente(cliente)
        operacao.comprovante_dados = {
            "cliente_id": cliente.id,
            "cliente_nome": cliente.nome,
            "valor_pago": "100.00",
            "saldo_atual": "0.00",
            "contas": [{"conta_id": 10, "venda_id": 99, "valor_aplicado": "100.00"}],
            "contas_abertas": [{"conta_id": 11, "venda_id": 100, "saldo_atual": "40.00"}],
        }
        operacao.save(update_fields=["comprovante_dados"])

        resposta = self.client.get(self._url_recibos_pendentes(), secure=True)

        self.assertEqual(resposta.status_code, 200)
        conteudo = resposta.content.decode()
        match = re.search(r'<a(?=[^>]+data-whatsapp-pendente)(?=[^>]+href="([^"]+)")[^>]+>', conteudo)
        self.assertIsNotNone(match)
        url_whatsapp = match.group(1).replace("&amp;", "&")
        mensagem = parse_qs(urlsplit(url_whatsapp).query)["text"][0]
        self.assertIn("Segue seu comprovante de pagamento.", mensagem)
        self.assertIn("Total pago:", mensagem)
        self.assertIn("Saldo atual:", mensagem)
        self.assertNotIn("Venda/Nota", mensagem)
        self.assertNotIn("Contas que ainda faltam pagar", mensagem)
        operacao.refresh_from_db()
        self.assertEqual(operacao.status_recibo, OperacaoRecebimentoCliente.STATUS_RECIBO_PENDENTE)

    def test_recibos_pendentes_confirmar_usa_endpoint_existente_e_template_remove_card(self):
        cliente = Cliente.objects.create(nome="Cliente Recibo Confirmar Lista", ativo=True)
        operacao = self._criar_operacao_recebimento_cliente(cliente)
        pagina = self.client.get(self._url_recibos_pendentes(), secure=True)

        resposta = self.client.post(self._url_confirmar_recibo(cliente, operacao), secure=True)

        self.assertContains(pagina, "card.remove()")
        self.assertContains(pagina, "atualizarQuantidade(-1)")
        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json()["ok"])
        operacao.refresh_from_db()
        self.assertEqual(operacao.status_recibo, OperacaoRecebimentoCliente.STATUS_RECIBO_ENVIADO)
        pagina_atualizada = self.client.get(self._url_recibos_pendentes(), secure=True)
        self.assertNotContains(pagina_atualizada, "Cliente Recibo Confirmar Lista")
        self.assertContains(pagina_atualizada, "Nenhum recibo pendente.")

    def test_recibos_pendentes_estado_vazio(self):
        resposta = self.client.get(self._url_recibos_pendentes(), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Nenhum recibo pendente.")
        self.assertContains(resposta, "Receber de cliente")
        self.assertContains(resposta, "Voltar ao painel")

    def test_home_mostra_atalho_recibos_pendentes_com_contador(self):
        cliente_pendente = Cliente.objects.create(nome="Cliente Painel Pendente", ativo=True)
        cliente_enviado = Cliente.objects.create(nome="Cliente Painel Enviado", ativo=True)
        self._criar_operacao_recebimento_cliente(cliente_pendente)
        self._criar_operacao_recebimento_cliente(
            cliente_enviado,
            status=OperacaoRecebimentoCliente.STATUS_RECIBO_ENVIADO,
        )

        resposta = self.client.get(reverse("estoque:home"), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, reverse("estoque:recebimentos_recibos_pendentes"))
        self.assertContains(resposta, "Recibos pendentes (1)")

    def test_receber_cliente_anonimo_cria_operacao_sem_criado_por(self):
        cliente = Cliente.objects.create(nome="Cliente Operacao Anonima", ativo=True)
        self._criar_conta_receber_pix(cliente, "100.00")

        resposta = self._post_receber_cliente(cliente, "60,00")

        self.assertEqual(resposta.status_code, 302)
        self.assertIsNone(OperacaoRecebimentoCliente.objects.get().criado_por)

    def test_receber_cliente_varias_contas_cria_uma_operacao_para_todas_baixas(self):
        cliente = Cliente.objects.create(nome="Cliente Varias Contas", ativo=True)
        self._criar_conta_receber_pix(cliente, "80.00")
        self._criar_conta_receber_pix(cliente, "70.00")

        resposta = self._post_receber_cliente(cliente, "120,00")

        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(OperacaoRecebimentoCliente.objects.count(), 1)
        operacao = OperacaoRecebimentoCliente.objects.get()
        recebimentos = list(RecebimentoContaReceber.objects.order_by("id"))
        self.assertEqual(len(recebimentos), 2)
        self.assertEqual({recebimento.operacao_id for recebimento in recebimentos}, {operacao.id})
        self.assertEqual(operacao.valor_recebido, Decimal("120.00"))
        self.assertEqual(operacao.valor_aplicado, Decimal("120.00"))
        self.assertEqual(operacao.saldo_anterior, Decimal("150.00"))
        self.assertEqual(operacao.saldo_atual, Decimal("30.00"))
        self.assertEqual(sum((recebimento.valor for recebimento in recebimentos), Decimal("0.00")), Decimal("120.00"))

    def test_receber_cliente_com_sobra_em_credito_registra_operacao(self):
        cliente = Cliente.objects.create(nome="Cliente Sobra Credito", ativo=True)
        self._criar_conta_receber_pix(cliente, "100.00")

        resposta = self._post_receber_cliente(cliente, "125,00", destino_diferenca="credito")

        self.assertEqual(resposta.status_code, 302)
        operacao = OperacaoRecebimentoCliente.objects.get()
        self.assertEqual(operacao.valor_recebido, Decimal("125.00"))
        self.assertEqual(operacao.valor_aplicado, Decimal("100.00"))
        self.assertEqual(operacao.credito_gerado, Decimal("25.00"))
        self.assertEqual(operacao.saldo_atual, Decimal("0.00"))
        self.assertEqual(CreditoCliente.objects.get().valor, Decimal("25.00"))

    def test_receber_cliente_com_sobra_em_troco_nao_registra_credito(self):
        cliente = Cliente.objects.create(nome="Cliente Sobra Troco", ativo=True)
        self._criar_conta_receber_pix(cliente, "100.00")

        resposta = self._post_receber_cliente(cliente, "125,00", destino_diferenca="troco")

        self.assertEqual(resposta.status_code, 302)
        operacao = OperacaoRecebimentoCliente.objects.get()
        recebimento = RecebimentoContaReceber.objects.get()
        self.assertEqual(operacao.valor_recebido, Decimal("125.00"))
        self.assertEqual(operacao.valor_aplicado, Decimal("100.00"))
        self.assertEqual(operacao.credito_gerado, Decimal("0.00"))
        self.assertEqual(operacao.saldo_atual, Decimal("0.00"))
        self.assertEqual(recebimento.valor, Decimal("100.00"))
        self.assertEqual(CreditoCliente.objects.count(), 0)

    def test_receber_cliente_erro_durante_baixa_nao_deixa_operacao_orfa(self):
        cliente = Cliente.objects.create(nome="Cliente Rollback Operacao", ativo=True)
        self._criar_conta_receber_pix(cliente, "100.00")

        with patch("estoque.views._aplicar_recebimento_conta", side_effect=RuntimeError("falha baixa")):
            with self.assertRaises(RuntimeError):
                self._post_receber_cliente(cliente, "50,00")

        self.assertEqual(OperacaoRecebimentoCliente.objects.count(), 0)
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
        self.assertEqual(MovimentoFinanceiro.objects.count(), 0)

    def test_receber_cliente_persiste_comprovante_dados_e_mantem_feedback(self):
        cliente = Cliente.objects.create(nome="Cliente Comprovante Operacao", ativo=True)
        self._criar_conta_receber_pix(cliente, "100.00")

        resposta = self._post_receber_cliente(cliente, "100,00")

        self.assertEqual(resposta.status_code, 302)
        operacao = OperacaoRecebimentoCliente.objects.get()
        dados = operacao.comprovante_dados
        self.assertEqual(dados["cliente_id"], cliente.id)
        self.assertEqual(dados["cliente_nome"], "Cliente Comprovante Operacao")
        self.assertEqual(dados["valor_pago"], "100.00")
        self.assertEqual(dados["forma_pagamento"], "PIX")
        self.assertEqual(dados["saldo_atual"], "0.00")
        self.assertEqual(len(dados["contas"]), 1)
        self.assertEqual(dados["contas"][0]["valor_aplicado"], "100.00")
        feedback = self.client.session["receber_cliente_feedback"]
        self.assertEqual(feedback["operacao_id"], operacao.id)
        self.assertEqual(feedback["status_recibo"], OperacaoRecebimentoCliente.STATUS_RECIBO_PENDENTE)
        self.assertEqual(feedback["confirmar_recibo_url"], self._url_confirmar_recibo(cliente, operacao))
        self.assertIn("comprovante_imagem_url", feedback)
        self.assertIn("whatsapp_confirmacao", feedback)
        comprovantes_sessao = self.client.session["receber_cliente_comprovantes"]
        self.assertTrue(comprovantes_sessao)
        comprovante_sessao = next(iter(comprovantes_sessao.values()))
        self.assertEqual(dados["operacao_id"], operacao.id)
        self.assertEqual(comprovante_sessao["operacao_id"], operacao.id)

    def test_receber_cliente_feedback_referencia_operacao_nova_mesmo_com_operacao_anterior(self):
        cliente = Cliente.objects.create(nome="Cliente Operacao Atual", ativo=True)
        self._criar_conta_receber_pix(cliente, "100.00")
        operacao_anterior = OperacaoRecebimentoCliente.objects.create(
            cliente=cliente,
            cliente_nome_snapshot=cliente.nome,
            valor_recebido=Decimal("10.00"),
            valor_aplicado=Decimal("10.00"),
            credito_gerado=Decimal("0.00"),
            saldo_anterior=Decimal("110.00"),
            saldo_atual=Decimal("100.00"),
            data_recebimento=timezone.localdate(),
            forma_pagamento="PIX",
        )

        resposta = self._post_receber_cliente(cliente, "40,00")

        self.assertEqual(resposta.status_code, 302)
        operacao_nova = OperacaoRecebimentoCliente.objects.exclude(pk=operacao_anterior.pk).get()
        feedback = self.client.session["receber_cliente_feedback"]
        self.assertEqual(feedback["operacao_id"], operacao_nova.id)
        self.assertNotEqual(feedback["operacao_id"], operacao_anterior.id)
        self.assertEqual(operacao_nova.comprovante_dados["operacao_id"], operacao_nova.id)

    def test_receber_cliente_confirmado_renderiza_dados_persistidos_da_operacao(self):
        cliente = Cliente.objects.create(
            nome="Cliente Data Operacao",
            whatsapp="(85) 99999-0000",
            ativo=True,
        )
        self._criar_conta_receber_pix(cliente, "100.00")

        resposta = self._post_receber_cliente(cliente, "100,00", follow=True)

        self.assertEqual(resposta.status_code, 200)
        operacao = OperacaoRecebimentoCliente.objects.get()
        self.assertTemplateUsed(resposta, "estoque/receber_cliente_confirmado.html")
        self.assertContains(resposta, f'data-operacao-id="{operacao.id}"')
        self.assertContains(resposta, "max-width: 900px")
        self.assertContains(resposta, "Recebimento confirmado")
        self.assertContains(resposta, "Cliente Data Operacao")
        self.assertContains(resposta, "R$ 100.00")
        self.assertContains(resposta, "Saldo anterior")
        self.assertContains(resposta, "Saldo atual")
        self.assertContains(resposta, "Contas abatidas")
        self.assertContains(resposta, "Contas ainda abertas")
        self.assertContains(resposta, "<details", html=False)
        self.assertContains(resposta, "Visualizar comprovante")
        self.assertContains(resposta, "Abrir card do recibo")
        self.assertContains(
            resposta,
            reverse(
                "estoque:receber_cliente_operacao_recibo_card_imagem",
                kwargs={"cliente_id": cliente.id, "operacao_id": operacao.id},
            ),
        )
        self.assertContains(resposta, "Enviar recibo pelo WhatsApp")
        self.assertContains(resposta, "Confirmar recibo enviado")
        self.assertContains(resposta, f'data-confirmar-recibo-url="{self._url_confirmar_recibo(cliente, operacao)}"')
        self.assertContains(resposta, 'id="btn-confirmar-recibo-enviado"')
        self.assertContains(resposta, "hidden")
        self.assertContains(resposta, 'btnConfirmar.classList.add("pulsando")')
        self.assertContains(resposta, 'btnConfirmar.classList.remove("pulsando", "primary")')
        self.assertContains(resposta, "@media (prefers-reduced-motion: reduce)")
        self.assertContains(resposta, "#btn-enviar-confirmacao-whatsapp { flex: 0 1 300px; max-width: 300px; }")
        self.assertContains(resposta, "#btn-confirmar-recibo-enviado { flex: 0 1 250px; max-width: 250px; }")
        self.assertContains(resposta, "#btn-ver-recibo-card { flex: 0 1 210px; max-width: 210px; }")
        self.assertNotContains(resposta, "grid-template-columns: 1fr 1.35fr auto")
        conteudo = resposta.content.decode()
        self.assertLess(conteudo.find("Acoes do recibo"), conteudo.find("Contas abatidas"))
        self.assertNotContains(resposta, 'id="formReceberCliente"')
        self.assertNotContains(resposta, 'id="clienteBuscaReceberDireto"')

    def test_receber_cliente_confirmado_resume_contas_abertas_em_details(self):
        cliente = Cliente.objects.create(nome="Cliente Details Contas", ativo=True)
        self._criar_conta_receber_pix(cliente, "100.00")
        self._criar_conta_receber_pix(cliente, "80.00")
        self._criar_conta_receber_pix(cliente, "70.00")

        resposta = self._post_receber_cliente(cliente, "100,00", follow=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Contas abatidas")
        self.assertContains(resposta, "Contas ainda abertas")
        self.assertContains(resposta, '<span class="rcp-count">2 &middot; R$ 150,00</span>', html=True)
        self.assertContains(resposta, "Saldo restante resumido: R$ 150,00")
        self.assertContains(resposta, "<details", count=2, html=False)

    def test_receber_cliente_whatsapp_usa_mensagem_curta_sem_listagem_de_contas(self):
        cliente = Cliente.objects.create(
            nome="Cliente Mensagem Curta",
            whatsapp="(85) 98888-7777",
            ativo=True,
        )
        self._criar_conta_receber_pix(cliente, "100.00")
        self._criar_conta_receber_pix(cliente, "70.00")

        resposta = self._post_receber_cliente(cliente, "100,00", follow=True)

        self.assertEqual(resposta.status_code, 200)
        conteudo = resposta.content.decode()
        match = re.search(r'<a[^>]+id="btn-enviar-confirmacao-whatsapp"[^>]+href="([^"]+)"', conteudo)
        self.assertIsNotNone(match)
        url_whatsapp = match.group(1).replace("&amp;", "&")
        mensagem = parse_qs(urlsplit(url_whatsapp).query)["text"][0]
        self.assertIn("Segue seu comprovante de pagamento.", mensagem)
        self.assertIn("Total pago:", mensagem)
        self.assertIn("Saldo atual:", mensagem)
        self.assertNotIn("Contas que ainda faltam pagar", mensagem)
        self.assertNotIn("Venda/Nota", mensagem)
        self.assertContains(resposta, 'btnWhatsapp?.addEventListener("click", revelarConfirmacao)')

    def test_receber_cliente_confirmado_reload_mantem_previa_sem_sessao(self):
        cliente = Cliente.objects.create(nome="Cliente Reload Confirmado", ativo=True)
        self._criar_conta_receber_pix(cliente, "100.00")
        resposta_post = self._post_receber_cliente(cliente, "100,00")
        operacao = OperacaoRecebimentoCliente.objects.get()
        session = self.client.session
        session.pop("receber_cliente_feedback", None)
        session.pop("receber_cliente_comprovantes", None)
        session.save()

        primeira = self.client.get(self._url_recebimento_confirmado(cliente, operacao), secure=True)
        segunda = self.client.get(self._url_recebimento_confirmado(cliente, operacao), secure=True)

        self.assertEqual(resposta_post.status_code, 302)
        self.assertEqual(primeira.status_code, 200)
        self.assertEqual(segunda.status_code, 200)
        self.assertContains(segunda, "Cliente Reload Confirmado")
        self.assertContains(segunda, "R$ 100.00")
        self.assertContains(segunda, "Nao ficou nenhuma conta em aberto apos este pagamento.")

    def test_receber_cliente_confirmado_operacao_de_outro_cliente_retorna_404(self):
        cliente_a = Cliente.objects.create(nome="Cliente Confirmado A", ativo=True)
        cliente_b = Cliente.objects.create(nome="Cliente Confirmado B", ativo=True)
        operacao = self._criar_operacao_recebimento_cliente(cliente_a)

        resposta = self.client.get(self._url_recebimento_confirmado(cliente_b, operacao), secure=True)

        self.assertEqual(resposta.status_code, 404)

    def test_receber_cliente_confirmado_operacao_inexistente_retorna_404(self):
        cliente = Cliente.objects.create(nome="Cliente Confirmado Inexistente", ativo=True)

        resposta = self.client.get(
            reverse(
                "estoque:receber_cliente_confirmado",
                kwargs={"cliente_id": cliente.id, "operacao_id": 999999},
            ),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 404)

    def test_receber_cliente_confirmado_status_enviado_mostra_estado_confirmado(self):
        cliente = Cliente.objects.create(nome="Cliente Confirmado Enviado", ativo=True)
        operacao = self._criar_operacao_recebimento_cliente(
            cliente,
            status=OperacaoRecebimentoCliente.STATUS_RECIBO_ENVIADO,
        )

        resposta = self.client.get(self._url_recebimento_confirmado(cliente, operacao), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Recibo enviado")
        self.assertContains(resposta, 'class="rcp-recibo-pill" id="confirmar-recibo-status"')
        self.assertNotContains(resposta, 'id="btn-confirmar-recibo-enviado"')
        self.assertNotContains(resposta, "data-confirmar-recibo-url")

    def test_receber_cliente_confirmado_status_dispensado_nao_mostra_botao_confirmar(self):
        cliente = Cliente.objects.create(nome="Cliente Confirmado Dispensado", ativo=True)
        operacao = self._criar_operacao_recebimento_cliente(
            cliente,
            status=OperacaoRecebimentoCliente.STATUS_RECIBO_DISPENSADO,
        )

        resposta = self.client.get(self._url_recebimento_confirmado(cliente, operacao), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Recibo dispensado")
        self.assertNotContains(resposta, 'id="btn-confirmar-recibo-enviado"')
        self.assertNotContains(resposta, "data-confirmar-recibo-url")

    def test_receber_cliente_sem_feedback_nao_exige_operacao_id_no_template(self):
        cliente = Cliente.objects.create(nome="Cliente Sem Feedback Operacao", ativo=True)
        self._criar_conta_receber_pix(cliente, "100.00")

        resposta = self.client.get(
            reverse("estoque:receber_cliente", kwargs={"cliente_id": cliente.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertNotContains(resposta, "data-operacao-id")
        self.assertNotContains(resposta, "Recebimento confirmado com sucesso.")
        self.assertNotContains(resposta, "Confirmar recibo enviado")
        self.assertNotContains(resposta, "None")

    def test_visualizar_comprovante_nao_altera_status_recibo(self):
        cliente = Cliente.objects.create(nome="Cliente Status Recibo", ativo=True)
        self._criar_conta_receber_pix(cliente, "100.00")

        resposta = self._post_receber_cliente(cliente, "100,00")

        self.assertEqual(resposta.status_code, 302)
        operacao = OperacaoRecebimentoCliente.objects.get()
        resposta_comprovante = self.client.get(
            reverse(
                "estoque:receber_cliente_operacao_comprovante_imagem",
                kwargs={"cliente_id": cliente.id, "operacao_id": operacao.id},
            ),
            secure=True,
        )
        self.assertEqual(resposta_comprovante.status_code, 200)
        self.assertEqual(resposta_comprovante["Content-Type"], "image/png")
        operacao.refresh_from_db()
        self.assertEqual(operacao.status_recibo, OperacaoRecebimentoCliente.STATUS_RECIBO_PENDENTE)
        self.assertIsNone(operacao.recibo_confirmado_em)
        self.assertIsNone(operacao.recibo_confirmado_por)

    def test_comprovante_recebimento_imagem_compacta_mantem_dados_essenciais(self):
        cliente = Cliente.objects.create(nome="Cliente Imagem Compacta", ativo=True)
        self._criar_conta_receber_pix(cliente, "100.00")
        self._criar_conta_receber_pix(cliente, "80.00")
        self._criar_conta_receber_pix(cliente, "70.00")

        resposta = self._post_receber_cliente(cliente, "100,00")

        self.assertEqual(resposta.status_code, 302)
        operacao = OperacaoRecebimentoCliente.objects.get()
        dados = operacao.comprovante_dados
        self.assertEqual(dados["cliente_nome"], "Cliente Imagem Compacta")
        self.assertEqual(dados["valor_pago"], "100.00")
        self.assertEqual(dados["saldo_atual"], "150.00")
        self.assertEqual(len(dados["contas"]), 1)
        self.assertEqual(len(dados["contas_abertas"]), 2)
        resposta_comprovante = self.client.get(
            reverse(
                "estoque:receber_cliente_operacao_comprovante_imagem",
                kwargs={"cliente_id": cliente.id, "operacao_id": operacao.id},
            ),
            secure=True,
        )
        from PIL import Image

        imagem = Image.open(io.BytesIO(resposta_comprovante.content))
        self.assertEqual(imagem.width, 800)
        self.assertGreater(imagem.height, imagem.width)
        self.assertLess(imagem.height, 1200)

    def test_recibo_whatsapp_card_retorna_png_vertical_sem_alterar_status(self):
        cliente = Cliente.objects.create(nome="Cliente Card WhatsApp", ativo=True)
        self._criar_conta_receber_pix(cliente, "100.00")
        self._criar_conta_receber_pix(cliente, "80.00")

        resposta = self._post_receber_cliente(cliente, "100,00")

        self.assertEqual(resposta.status_code, 302)
        operacao = OperacaoRecebimentoCliente.objects.get()
        resposta_card = self.client.get(
            reverse(
                "estoque:receber_cliente_operacao_recibo_card_imagem",
                kwargs={"cliente_id": cliente.id, "operacao_id": operacao.id},
            ),
            secure=True,
        )
        from PIL import Image

        self.assertEqual(resposta_card.status_code, 200)
        self.assertEqual(resposta_card["Content-Type"], "image/png")
        self.assertIn("recibo-whatsapp", resposta_card["Content-Disposition"])
        imagem = Image.open(io.BytesIO(resposta_card.content))
        self.assertEqual(imagem.width, 800)
        self.assertGreater(imagem.height, imagem.width)
        operacao.refresh_from_db()
        self.assertEqual(operacao.status_recibo, OperacaoRecebimentoCliente.STATUS_RECIBO_PENDENTE)

    def test_receber_cliente_sair_da_tela_sem_confirmar_mantem_recibo_pendente(self):
        cliente = Cliente.objects.create(nome="Cliente Recibo Saiu", ativo=True)
        self._criar_conta_receber_pix(cliente, "100.00")

        resposta = self._post_receber_cliente(cliente, "100,00")
        self.assertEqual(resposta.status_code, 302)
        operacao = OperacaoRecebimentoCliente.objects.get()
        resposta_home = self.client.get(reverse("estoque:home"), secure=True)

        self.assertEqual(resposta_home.status_code, 200)
        operacao.refresh_from_db()
        self.assertEqual(operacao.status_recibo, OperacaoRecebimentoCliente.STATUS_RECIBO_PENDENTE)
        self.assertIsNone(operacao.recibo_confirmado_em)
        self.assertIsNone(operacao.recibo_confirmado_por)

    def test_recebimento_conta_receber_direto_continua_sem_operacao(self):
        cliente = Cliente.objects.create(nome="Cliente Historico Direto", ativo=True)
        conta = self._criar_conta_receber_pix(cliente, "100.00")

        recebimento = RecebimentoContaReceber.objects.create(
            conta=conta,
            data_recebimento=timezone.localdate(),
            valor=Decimal("10.00"),
            forma_pagamento="Dinheiro",
        )

        self.assertIsNone(recebimento.operacao)
        self.assertEqual(OperacaoRecebimentoCliente.objects.count(), 0)

    def test_aplicar_recebimento_conta_sem_operacao_continua_compativel(self):
        cliente = Cliente.objects.create(nome="Cliente Funcao Sem Operacao", ativo=True)
        conta = self._criar_conta_receber_pix(cliente, "100.00")

        with transaction.atomic():
            conta_bloqueada = ContaReceber.objects.select_for_update().get(pk=conta.pk)
            resultado = views._aplicar_recebimento_conta(
                conta_bloqueada,
                timezone.localdate(),
                Decimal("30.00"),
                "Dinheiro",
                "Recebimento direto em teste.",
                "troco",
            )

        self.assertEqual(resultado["valor_aplicado"], Decimal("30.00"))
        self.assertIsNone(resultado["recebimento"].operacao)
        self.assertEqual(OperacaoRecebimentoCliente.objects.count(), 0)

    def test_receber_cliente_grava_rota_snapshot_quando_informada(self):
        cliente = Cliente.objects.create(nome="Cliente Rota Snapshot", bairro="Centro", ativo=True)
        self._criar_conta_receber_pix(cliente, "100.00")

        resposta = self._post_receber_cliente(cliente, "40,00", rota="Centro")

        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(OperacaoRecebimentoCliente.objects.get().rota_snapshot, "Centro")

    def test_receber_cliente_grava_rota_snapshot_recuperada_do_next(self):
        cliente = Cliente.objects.create(nome="Cliente Rota Next Snapshot", bairro="Jardim", ativo=True)
        self._criar_conta_receber_pix(cliente, "100.00")
        next_url = f"{reverse('estoque:receber_cliente_escolher')}?{urlencode({'rota': 'Jardim'})}"

        resposta = self._post_receber_cliente(cliente, "40,00", next_url=next_url)

        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(OperacaoRecebimentoCliente.objects.get().rota_snapshot, "Jardim")

    def test_receber_cliente_sem_rota_snapshot_fica_vazio(self):
        cliente = Cliente.objects.create(nome="Cliente Sem Rota Snapshot", ativo=True)
        self._criar_conta_receber_pix(cliente, "100.00")

        resposta = self._post_receber_cliente(cliente, "40,00")

        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(OperacaoRecebimentoCliente.objects.get().rota_snapshot, "")

    def test_receber_cliente_mostra_credito_disponivel_com_origem_e_saldo_resultante(self):
        cliente = Cliente.objects.create(nome="Cliente Com Credito", ativo=True)
        conta = self._criar_conta_receber_pix(cliente, "100.00")
        CreditoCliente.objects.create(
            cliente=cliente,
            valor=Decimal("35.00"),
            origem_conta_receber=conta,
            observacao="Ajuste por item nao entregue.",
        )
        CreditoCliente.objects.create(
            cliente=cliente,
            valor=Decimal("-10.00"),
            origem_conta_receber=conta,
            observacao="Credito usado em recebimento posterior.",
        )

        resposta = self.client.get(
            reverse("estoque:receber_cliente", kwargs={"cliente_id": cliente.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Créditos disponíveis do cliente")
        self.assertContains(resposta, "Total: R$ 25.00")
        self.assertContains(resposta, f"Venda #{conta.venda_id}")
        self.assertContains(resposta, f"Conta #{conta.id}")
        self.assertContains(resposta, "Motivo: Ajuste por item nao entregue.")
        self.assertContains(resposta, "Saldo resultante se o crédito for considerado")
        self.assertContains(resposta, "R$ 75.00")
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)

    def test_receber_cliente_sem_contas_mostra_mensagem_de_credito_disponivel(self):
        cliente = Cliente.objects.create(nome="Cliente Somente Credito", ativo=True)
        CreditoCliente.objects.create(
            cliente=cliente,
            valor=Decimal("42.50"),
            observacao="Credito manual de teste.",
        )

        resposta = self.client.get(
            reverse("estoque:receber_cliente", kwargs={"cliente_id": cliente.id}),
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Cliente não possui contas em aberto. Crédito disponível: R$ 42.50")
        self.assertContains(resposta, "Motivo: Credito manual de teste.")
        self.assertNotContains(resposta, "Usar credito")
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)

    def test_clientes_autocomplete_por_id_retorna_financeiro_com_credito_atualizado(self):
        cliente = Cliente.objects.create(nome="Lisandra Credito", ativo=True)
        CreditoCliente.objects.create(
            cliente=cliente,
            valor=Decimal("67.00"),
            observacao="Credito disponivel para restauracao em vendas.",
        )

        resposta = self.client.get(
            reverse("estoque:clientes_autocomplete"),
            {"cliente_id": cliente.id, "q": "texto que nao precisa bater"},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertEqual(len(dados["clientes"]), 1)
        self.assertEqual(dados["clientes"][0]["id"], cliente.id)
        self.assertEqual(dados["clientes"][0]["nome"], "Lisandra Credito")
        self.assertEqual(dados["clientes"][0]["financeiro"]["credito_disponivel"], "67.00")

    def test_baixa_com_pix_atual_bloqueia_quando_parecido_ja_foi_baixado(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Duplicado", ativo=True)
        self._criar_conta_receber_pix(cliente, "96.30")
        pix_baixado = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="ABELARDO ROBSON V PIEDADE",
            valor="96.30",
            status=PixRecebido.STATUS_BAIXADO,
        )
        pix_atual = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="ABELARDO ROBSON V PIEDADE",
            valor="96.30",
            status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
            pix_original=pix_baixado,
        )

        resposta = self.client.post(
            f"{reverse('estoque:receber_cliente', kwargs={'cliente_id': cliente.id})}?pix_recebido={pix_atual.id}",
            {
                "pix_recebido": pix_atual.id,
                "data_recebimento": timezone.localdate().isoformat(),
                "valor": "96,30",
                "forma_pagamento": "PIX",
                "destino_diferenca": "troco",
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
        pix_atual.refresh_from_db()
        self.assertEqual(pix_atual.status, PixRecebido.STATUS_POSSIVEL_DUPLICADO)

    def test_central_pix_manual_identifica_duplicado_ja_baixado_e_bloqueia_botao(self):
        cliente = Cliente.objects.create(nome="Ivanildo Ferraz Patricio Junior", ativo=True)
        pix_baixado = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Ivanildo Ferraz Patricio Junior",
            valor="500.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_BAIXADO,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix"),
            {
                "cliente": cliente.id,
                "nome_pagador": "Ivanildo Ferraz Patricio Jr",
                "valor": "500.00",
                "data_pagamento": "2026-05-18T17:45",
                "instituicao_pix": "Mercado Pago",
                "observacao": "",
                "status": PixRecebido.STATUS_PENDENTE,
            },
            secure=True,
            follow=True,
        )

        novo_pix = PixRecebido.objects.order_by("-id").first()
        self.assertEqual(novo_pix.status, PixRecebido.STATUS_POSSIVEL_DUPLICADO)
        self.assertEqual(novo_pix.pix_original, pix_baixado)
        self.assertContains(resposta, "ja foi usado em baixa")
        self.assertContains(resposta, "Use apenas para conferencia")
        self.assertContains(resposta, "Ignorar Pix sem baixa")
        self.assertNotContains(resposta, "este Pix ainda precisa ser usado na baixa")
        self.assertNotContains(resposta, "Usar este Pix na baixa")
        self.assertNotContains(resposta, "Abrir Pix parecido")
        self.assertNotContains(resposta, "Abrir imagem do Pix parecido")
        self.assertEqual(resposta.content.count(b">Ignorar Pix sem baixa</button>"), 1)

    def test_central_pix_prioriza_duplicado_baixado_sobre_pendente(self):
        cliente = Cliente.objects.create(nome="Ivanildo Ferraz Patricio Junior", ativo=True)
        pix_baixado = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Ivanildo Ferraz Patricio Junior",
            valor="500.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_BAIXADO,
        )
        PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Ivanildo Ferraz Patricio Junior",
            valor="500.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix"),
            {
                "cliente": cliente.id,
                "nome_pagador": "Ivanildo Ferraz Patricio Junior",
                "valor": "500.00",
                "data_pagamento": "2026-05-18T17:45",
                "instituicao_pix": "Mercado Pago",
                "observacao": "",
                "status": PixRecebido.STATUS_PENDENTE,
            },
            secure=True,
            follow=True,
        )

        pix_novo = PixRecebido.objects.order_by("-id").first()
        self.assertEqual(pix_novo.pix_original, pix_baixado)
        self.assertEqual(pix_novo.status, PixRecebido.STATUS_POSSIVEL_DUPLICADO)
        self.assertContains(resposta, "ja foi usado em baixa")
        self.assertNotContains(resposta, "Usar este Pix na baixa")

    def test_reler_ocr_no_detalhe_nao_envia_next_para_central(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pix OCR detalhe",
            valor="10.00",
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
            comprovante=SimpleUploadedFile("ocr.txt", b"Comprovante Pix", content_type="text/plain"),
        )

        resposta = self.client.get(
            f"{reverse('estoque:central_pix_detalhe', kwargs={'pix_id': pix.id})}?next={reverse('estoque:central_pix')}",
            secure=True,
        )

        action = reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id})
        self.assertContains(resposta, f'action="{action}"')
        self.assertNotContains(resposta, f'action="{action}?next=')

        with patch("estoque.views.analisar_comprovante_pix", return_value={
            "ok": True,
            "pagador": "Pix OCR detalhe",
            "valor": "10.00",
            "data_pagamento": "2026-05-18T17:45",
            "instituicao_pix": "Mercado Pago",
            "texto_ocr_bruto": "Comprovante Pix",
        }):
            resposta_ocr = self.client.post(action, secure=True, follow=True)

        detalhe_url = reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id})
        self.assertContains(resposta_ocr, "Comprovante original")
        self.assertContains(resposta_ocr, f'href="{detalhe_url}"')

    def test_reler_ocr_preserva_bloqueio_quando_duplicado_ja_baixado(self):
        cliente = Cliente.objects.create(nome="Cliente OCR Duplicado", ativo=True)
        pix_baixado = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Ivanildo Ferraz Patricio Junior",
            valor="500.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_BAIXADO,
        )
        pix = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Ivanildo Ferraz Patricio Junior",
            valor="500.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
            pix_original=pix_baixado,
            comprovante=SimpleUploadedFile("ocr.txt", b"Comprovante Pix", content_type="text/plain"),
        )
        action = reverse("estoque:central_pix_processar_ocr", kwargs={"pix_id": pix.id})

        with patch("estoque.views.analisar_comprovante_pix", return_value={
            "ok": False,
            "pagador": "",
            "valor": "",
            "data_pagamento": "",
            "instituicao_pix": "",
            "texto_ocr_bruto": "Comprovante Pix sem leitura completa",
        }):
            resposta = self.client.post(action, secure=True, follow=True)

        pix.refresh_from_db()
        self.assertEqual(pix.pix_original, pix_baixado)
        self.assertEqual(pix.status, PixRecebido.STATUS_POSSIVEL_DUPLICADO)
        self.assertContains(resposta, "ja foi usado em baixa")
        self.assertContains(resposta, "Ignorar Pix sem baixa")
        self.assertContains(resposta, "Comprovante original")
        self.assertNotContains(resposta, "Usar este Pix na baixa")
        self.assertNotContains(resposta, "este Pix ainda precisa ser usado na baixa")

    def test_detalhe_pix_sem_valor_nao_mostra_acao_de_baixa(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Sem Valor", ativo=True)
        pix = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Cliente Pix Sem Valor",
            valor="0.00",
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
        )

        resposta = self.client.get(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            secure=True,
        )

        self.assertContains(resposta, "Confira/digite o valor do Pix")
        self.assertContains(resposta, "Usar este Pix na baixa")

    def test_detalhe_pix_usar_na_baixa_salva_dados_e_abre_recebimento_com_data_do_pix(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Conferido", ativo=True)
        self._criar_conta_receber_pix(cliente, "172.00")
        data_pix = timezone.make_aware(timezone.datetime(2026, 4, 21, 13, 5))
        pix = PixRecebido.objects.create(
            nome_pagador="Nome OCR antigo",
            valor="0.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 25, 17, 10)),
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            {
                "acao": "usar_baixa",
                "cliente": cliente.id,
                "nome_pagador": "Pagador conferido",
                "valor": "172,00",
                "data_pagamento": "2026-04-21T13:05",
                "instituicao_pix": "Nubank",
            },
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        pix.refresh_from_db()
        self.assertEqual(pix.cliente, cliente)
        self.assertEqual(pix.nome_pagador, "Pagador conferido")
        self.assertEqual(str(pix.valor), "172.00")
        self.assertEqual(timezone.localtime(pix.data_pagamento).strftime("%Y-%m-%dT%H:%M"), "2026-04-21T13:05")
        self.assertEqual(pix.instituicao_pix, "Nubank")
        self.assertContains(resposta, 'name="valor" type="text" inputmode="decimal" autocomplete="off" value="172,00"')
        self.assertContains(resposta, 'name="data_recebimento" type="date" value="2026-04-21"')
        self.assertContains(resposta, '<option value="PIX" selected>PIX</option>', html=True)
        self.assertContains(resposta, "nenhuma baixa foi feita ainda")
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
        pix.refresh_from_db()
        self.assertNotEqual(pix.status, PixRecebido.STATUS_BAIXADO)
        self.assertEqual(timezone.localtime(pix.data_pagamento), data_pix)

    def test_detalhe_pix_usar_na_baixa_sem_cliente_nao_avanca(self):
        pix = PixRecebido.objects.create(
            nome_pagador="Pagador sem cliente",
            valor="172.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 4, 21, 13, 5)),
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            {
                "acao": "usar_baixa",
                "cliente": "",
                "nome_pagador": "Pagador sem cliente",
                "valor": "172,00",
                "data_pagamento": "2026-04-21T13:05",
                "instituicao_pix": "Nubank",
            },
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Confirme o cliente antes de usar este Pix na baixa.")
        self.assertContains(resposta, "baixaSemClienteConfirmado")
        self.assertContains(resposta, "event.preventDefault();")
        self.assertContains(resposta, "pix-client-attention")
        self.assertNotContains(resposta, "Confirmar recebimento do cliente")
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
        pix.refresh_from_db()
        self.assertIsNone(pix.cliente)
        self.assertEqual(pix.status, PixRecebido.STATUS_NAO_IDENTIFICADO)

    def test_detalhe_pix_usar_na_baixa_sem_valor_nao_avanca(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Sem Valor Baixa", ativo=True)
        pix = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Pagador sem valor",
            valor="0.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 4, 21, 13, 5)),
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            {
                "acao": "usar_baixa",
                "cliente": cliente.id,
                "nome_pagador": "Pagador sem valor",
                "valor": "",
                "data_pagamento": "2026-04-21T13:05",
                "instituicao_pix": "Nubank",
            },
            secure=True,
            follow=True,
        )

        self.assertContains(resposta, "Informe o valor do Pix antes de usar na baixa.")
        self.assertNotContains(resposta, "Confirmar recebimento do cliente")
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)

    def test_detalhe_pix_usar_na_baixa_sem_data_nao_avanca(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Sem Data Baixa", ativo=True)
        pix = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Pagador sem data",
            valor="172.00",
            status=PixRecebido.STATUS_NAO_IDENTIFICADO,
        )

        resposta = self.client.post(
            reverse("estoque:central_pix_detalhe", kwargs={"pix_id": pix.id}),
            {
                "acao": "usar_baixa",
                "cliente": cliente.id,
                "nome_pagador": "Pagador sem data",
                "valor": "172,00",
                "data_pagamento": "",
                "instituicao_pix": "Nubank",
            },
            secure=True,
            follow=True,
        )

        self.assertContains(resposta, "Informe a data do pagamento do Pix antes de usar na baixa.")
        self.assertNotContains(resposta, "Confirmar recebimento do cliente")
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)

    def test_baixa_bloqueia_pix_igual_a_baixado_mesmo_sem_vinculo_original(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Sem Vinculo", ativo=True)
        conta = self._criar_conta_receber_pix(cliente, "96.30")
        PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="ABELARDO ROBSON V PIEDADE",
            valor="96.30",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_BAIXADO,
        )
        pix_atual = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="ABELARDO ROBSON V PIEDADE",
            valor="96.30",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.post(
            reverse("estoque:conta_receber_receber", kwargs={"pk": conta.id}),
            {
                "pix_recebido": pix_atual.id,
                "data_recebimento": timezone.localdate().isoformat(),
                "valor": "96,30",
                "forma_pagamento": "PIX",
                "observacao": "",
                "destino_diferenca": "troco",
                "usar_credito": "0",
                "credito_utilizado": "0,00",
            },
            secure=True,
            follow=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Ja existe Pix igual baixado")
        self.assertEqual(RecebimentoContaReceber.objects.count(), 0)
        pix_atual.refresh_from_db()
        conta.refresh_from_db()
        self.assertEqual(pix_atual.status, PixRecebido.STATUS_PENDENTE)
        self.assertEqual(conta.status, ContaReceber.STATUS_ABERTA)

    def test_baixa_marca_pix_baixado_quando_id_vem_na_url(self):
        cliente = Cliente.objects.create(nome="Ivanildo Patricio Jr", ativo=True)
        conta = self._criar_conta_receber_pix(cliente, "500.00")
        pix = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="Ivanildo Ferraz Patricio Junior",
            valor="500.00",
            data_pagamento=timezone.make_aware(timezone.datetime(2026, 5, 18, 17, 45)),
            instituicao_pix="Mercado Pago",
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.post(
            f"{reverse('estoque:receber_cliente', kwargs={'cliente_id': cliente.id})}?pix_recebido={pix.id}",
            {
                "data_recebimento": timezone.localdate().isoformat(),
                "valor": "500,00",
                "forma_pagamento": "PIX",
                "destino_diferenca": "troco",
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        pix.refresh_from_db()
        conta.refresh_from_db()
        self.assertEqual(pix.status, PixRecebido.STATUS_BAIXADO)
        self.assertIn(f"conta(s): {conta.id}", pix.observacao)
        self.assertIn("valor: R$ 500,00", pix.observacao)
        self.assertEqual(RecebimentoContaReceber.objects.count(), 1)

    def test_baixa_com_pix_atual_marca_parecido_pendente_como_duplicado(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Atual", ativo=True)
        self._criar_conta_receber_pix(cliente, "96.30")
        pix_parecido = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="ABELARDO ROBSON V PIEDADE",
            valor="96.30",
            status=PixRecebido.STATUS_PENDENTE,
        )
        pix_atual = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="ABELARDO ROBSON V PIEDADE",
            valor="96.30",
            status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
            pix_original=pix_parecido,
        )

        resposta = self.client.post(
            reverse("estoque:receber_cliente", kwargs={"cliente_id": cliente.id}),
            {
                "pix_recebido": pix_atual.id,
                "data_recebimento": timezone.localdate().isoformat(),
                "valor": "96,30",
                "forma_pagamento": "PIX",
                "destino_diferenca": "troco",
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        pix_atual.refresh_from_db()
        pix_parecido.refresh_from_db()
        self.assertEqual(pix_atual.status, PixRecebido.STATUS_BAIXADO)
        self.assertEqual(pix_parecido.status, PixRecebido.STATUS_DUPLICADO)
        self.assertEqual(pix_parecido.pix_original, pix_atual)
        self.assertEqual(RecebimentoContaReceber.objects.count(), 1)

    def test_baixa_com_pix_parecido_marca_pix_atual_como_duplicado(self):
        cliente = Cliente.objects.create(nome="Cliente Pix Parecido", ativo=True)
        self._criar_conta_receber_pix(cliente, "96.30")
        pix_parecido = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="ABELARDO ROBSON V PIEDADE",
            valor="96.30",
            status=PixRecebido.STATUS_PENDENTE,
        )
        pix_atual = PixRecebido.objects.create(
            cliente=cliente,
            nome_pagador="ABELARDO ROBSON V PIEDADE",
            valor="96.30",
            status=PixRecebido.STATUS_POSSIVEL_DUPLICADO,
            pix_original=pix_parecido,
        )

        resposta = self.client.post(
            reverse("estoque:receber_cliente", kwargs={"cliente_id": cliente.id}),
            {
                "pix_recebido": pix_parecido.id,
                "data_recebimento": timezone.localdate().isoformat(),
                "valor": "96,30",
                "forma_pagamento": "PIX",
                "destino_diferenca": "troco",
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 302)
        pix_atual.refresh_from_db()
        pix_parecido.refresh_from_db()
        self.assertEqual(pix_parecido.status, PixRecebido.STATUS_BAIXADO)
        self.assertEqual(pix_atual.status, PixRecebido.STATUS_DUPLICADO)
        self.assertEqual(pix_atual.pix_original, pix_parecido)
        self.assertEqual(RecebimentoContaReceber.objects.count(), 1)

    def test_analisar_comprovante_pix_nubank_usa_nome_da_origem(self):
        conteudo = (
            "Comprovante Pix\n"
            "Destino\n"
            "Nome: Lincoln Albuquerque Neiva\n"
            "Origem\n"
            "Nome: Joelson Ferreira dos Santos\n"
            "Valor R$ 156,50\n"
            "16 MAI 2026 - 17:51:46\n"
            "Nu Pagamentos S.A.\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Joelson Ferreira dos Santos")
        self.assertEqual(dados["valor"], "156.50")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T17:51")
        self.assertEqual(dados["instituicao_pix"], "Nubank")
        self.assertIsNone(dados["cliente_sugerido_id"])
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_nubank_origem_nome_nao_usa_titulo_data(self):
        conteudo = (
            "Comprovante de transferência\n"
            "04 MAI 2026 - 10:49:17\n"
            "Valor R$ 400,00\n"
            "Tipo de transferência Pix\n"
            "Destino\n"
            "Nome\n"
            "Lincoln Albuquerque Neiva\n"
            "Instituição\n"
            "NU PAGAMENTOS - IP\n"
            "Origem\n"
            "Nome\n"
            "José Carlos da Silva Nascimento\n"
            "Instituição\n"
            "NU PAGAMENTOS - IP\n"
            "CPF\n"
            "***.915.482-**\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "José Carlos da Silva Nascimento")
        self.assertNotIn("transferência", dados["pagador"].lower())
        self.assertEqual(dados["valor"], "400.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-04T10:49")
        self.assertRegex(dados["instituicao_pix"], r"Nubank|NU PAGAMENTOS")

    def test_analisar_comprovante_pix_ton_stone_usa_dados_de_origem(self):
        conteudo = (
            "Comprovante de transfer\u00eancia\n"
            "14/05/2026 09:15\n"
            "Valor R$ 548,30\n"
            "Tipo Pix | Transfer\u00eancia\n"
            "\n"
            "DADOS DE DESTINO\n"
            "Nome\n"
            "Lincoln Albuquerque Neiva\n"
            "CPF ***.319.532-**\n"
            "Institui\u00e7\u00e3o\n"
            "NU PAGAMENTOS S.A. - INSTITUI\u00c7\u00c3O DE PAGAMENTO\n"
            "\n"
            "DADOS DE ORIGEM\n"
            "Nome\n"
            "Lisandra De Oliveira Da Silva\n"
            "CPF ***.157.092-**\n"
            "Institui\u00e7\u00e3o\n"
            "STONE INSTITUI\u00c7\u00c3O DE PAGAMENTO S.A.\n"
            "Ag\u00eancia 0001\n"
            "Conta 24279683-7\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("ton-stone.txt", conteudo, content_type="text/plain")

        dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "548.30")
        self.assertEqual(dados["data_pagamento"], "2026-05-14T09:15")
        self.assertEqual(dados["pagador"], "Lisandra De Oliveira Da Silva")
        self.assertEqual(dados["instituicao_pix"], "Stone")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")
        self.assertNotIn("DESTINO", dados["pagador"].upper())

    def test_analisar_comprovante_pix_caixa_tem_quem_vai_enviar_nao_usa_data(self):
        conteudo = (
            "Pix Pagamento\n"
            "17 de maio de 2026 \u00e0s 08:19:21\n"
            "Valor\n"
            "R$ 826,62\n"
            "Quem vai receber\n"
            "Nome\n"
            "Lincoln Albuquerque Neiva\n"
            "CPF/CNPJ\n"
            "***.319.532-**\n"
            "Banco\n"
            "NU PAGAMENTOS S.A.\n"
            "Quem vai enviar\n"
            "Nome\n"
            "ELIANA NAZARE DA SILVA FERREIRA\n"
            "CPF/CNPJ\n"
            "***.020.762-**\n"
            "Banco\n"
            "Caixa Econ\u00f4mica Federal\n"
            "Dados da transa\u00e7\u00e3o\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        dados = analisar_comprovante_pix(arquivo)

        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "ELIANA NAZARE DA SILVA FERREIRA")
        self.assertNotIn("maio de 2026", dados["pagador"].lower())
        self.assertEqual(dados["valor"], "826.62")
        self.assertEqual(dados["data_pagamento"], "2026-05-17T08:19")
        self.assertRegex(dados["instituicao_pix"], r"Caixa|Caixa Econ")

    def test_analisar_comprovante_pix_nubank_monta_pagador_origem_nome_quebrado(self):
        conteudo = (
            "Destino\n"
            "\n"
            "Nome Lincoln Albuquerque Neiva\n"
            "\n"
            "Instituigaéo = NU PAGAMENTOS - IP\n"
            "\n"
            "Chave Pix +5591984111011\n"
            "\n"
            "Origem\n"
            "\n"
            "Maria Antonia Alves de\n"
            "\n"
            "Nome\n"
            "Paiva\n"
            "\n"
            "Instituigdo = NU PAGAMENTOS - IP\n"
            "\n"
            "CPF s+1.252.002-++\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        dados = analisar_comprovante_pix(arquivo)

        self.assertEqual(dados["pagador"], "Maria Antonia Alves de Paiva")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")

    def test_analisar_comprovante_pix_nubank_com_destino_banco_inter_nao_usa_regra_inter(self):
        conteudo = (
            "nu,\n"
            "\n"
            "Comprovante de transferencia\n"
            "16 MAI 2026 - 23:43:13\n"
            "\n"
            "Valor R$ 5,00\n"
            "Tipo de transferencia Pix\n"
            "\n"
            "Destino\n"
            "Ronise do Socorro dos\n"
            "Nome\n"
            "Santos Ferreira\n"
            "CPF ***.000.000-**\n"
            "Instituigdo BANCO INTER\n"
            "\n"
            "Origem\n"
            "Lincoln Albuquerque\n"
            "Nome\n"
            "Neiva\n"
            "\n"
            "NU PAGAMENTOS - IP\n"
            "Nu Pagamentos S.A.\n"
            "nubank.com.br\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")
        saida = io.StringIO()

        with redirect_stdout(saida):
            resposta = self.client.post(
                reverse("estoque:central_pix_analisar_comprovante"),
                {"comprovante": arquivo},
                secure=True,
            )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T23:43")
        self.assertEqual(dados["pagador"], "Ronise do Socorro dos Santos Ferreira")
        self.assertNotEqual(dados["pagador"], "Neiva")
        self.assertNotIn("[PIX OCR][Banco Inter][BRUTO]", saida.getvalue())
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_mercado_pago_usa_de_como_pagador(self):
        conteudo = (
            "Comprovante de Pix\n"
            "16/maio/2026 \u00e0s 16:33:29\n"
            "R$ 600\n"
            "@ De\n"
            "Ivanildo Ferraz Patr\u00edcio Junior\n"
            "CPF: ***.188.882-**\n"
            "Mercado Pago\n"
            "@ Para\n"
            "Lincoln Albuquerque Neiva\n"
            "CPF: ***.319.532-**\n"
            "Nu Pagamentos S.A.\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Ivanildo Ferraz Patr\u00edcio Junior")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")
        self.assertEqual(dados["valor"], "600.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T16:33")
        self.assertEqual(dados["instituicao_pix"], "Mercado Pago")

    def test_analisar_comprovante_pix_pagador_por_contexto_sem_usar_recebedor(self):
        casos = [
            (
                "mercado-pago-de.txt",
                "Mercado Pago\nComprovante de Pix\n17/05/2026 11:09\nR$ 100\nDe Ivanildo Ferraz Patricio Junior\nCPF ***.188.882-**\nPara Lincoln Albuquerque Neiva\n",
                "Ivanildo Ferraz Patricio Junior",
            ),
            (
                "quem-pagou.txt",
                "Comprovante Pix\nQuem pagou\nNome RONISE DO SOCORRO DOS SANTOS FERREIRA\nQuem recebeu Lincoln Albuquerque Neiva\nValor R$ 76,00\n27 ABR 2026 - 08:04:01\n",
                "RONISE DO SOCORRO DOS SANTOS FERREIRA",
            ),
        ]
        url = reverse("estoque:central_pix_analisar_comprovante")

        for nome_arquivo, conteudo, pagador_esperado in casos:
            with self.subTest(nome=nome_arquivo):
                arquivo = SimpleUploadedFile(nome_arquivo, conteudo.encode("utf-8"), content_type="text/plain")
                resposta = self.client.post(url, {"comprovante": arquivo}, secure=True)

                self.assertEqual(resposta.status_code, 200)
                dados = resposta.json()
                self.assertTrue(dados["ok"])
                self.assertEqual(dados["pagador"], pagador_esperado)
                self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")

    def test_analisar_comprovante_pix_extrai_pagador_de_em_comprovantes_reais(self):
        casos = [
            (
                "pagbank-roceli.txt",
                (
                    "Comprovante de envio de Pix\n"
                    "18/05/2026 \u00e0s 07:08:52\n"
                    "Valor da transferencia R$ 5,00\n"
                    "De\n"
                    "ROSELI DA COSTA GAMA\n"
                    "CPF ***.115.912-**\n"
                    "Instituicao\n"
                    "PagBank (PagSeguro Internet Instituicao de Pagamento S.A.)\n"
                    "Para\n"
                    "Lincoln Albuquerque Neiva\n"
                ),
                "ROSELI DA COSTA GAMA",
                "5.00",
                "2026-05-18T07:08",
                "PagBank",
            ),
            (
                "mercado-pago-joao.txt",
                (
                    "Comprovante de Pix\n"
                    "23/maio/2026 \u00e0s 18:55:32\n"
                    "R$ 645\n"
                    "De\n"
                    "Joao de Almeida E Silva\n"
                    "CPF: ***.105.902-**\n"
                    "Mercado Pago\n"
                    "Para\n"
                    "Lincoln Albuquerque Neiva\n"
                ),
                "Joao de Almeida E Silva",
                "645.00",
                "2026-05-23T18:55",
                "Mercado Pago",
            ),
            (
                "itau-devolucao-euclides.txt",
                (
                    "Comprovante de devolucao de Pix\n"
                    "R$ 5,00\n"
                    "Realizado em 19/05/2026 \u00e0s 17:18:36\n"
                    "De\n"
                    "EUCLIDES CARNEIRO NEIVA NETO\n"
                    "CPF/CNPJ: 787.484.883-72\n"
                    "Instituicao: ITAU UNIBANCO S.A\n"
                    "Para\n"
                    "Lincoln Albuquerque Neiva\n"
                ),
                "EUCLIDES CARNEIRO NEIVA NETO",
                "5.00",
                "2026-05-19T17:18",
                "Ita\u00fa Unibanco",
            ),
            (
                "mercado-pago-ivanildo.txt",
                (
                    "Comprovante de Pix\n"
                    "18/maio/2026 \u00e0s 17:45:00\n"
                    "R$ 500\n"
                    "De\n"
                    "Ivanildo Ferraz Patricio Junior\n"
                    "CPF: ***.188.882-**\n"
                    "Mercado Pago\n"
                    "Para\n"
                    "Lincoln Albuquerque Neiva\n"
                ),
                "Ivanildo Ferraz Patricio Junior",
                "500.00",
                "2026-05-18T17:45",
                "Mercado Pago",
            ),
        ]
        url = reverse("estoque:central_pix_analisar_comprovante")

        for nome_arquivo, conteudo, pagador, valor, data_pagamento, instituicao in casos:
            with self.subTest(nome=nome_arquivo):
                arquivo = SimpleUploadedFile(nome_arquivo, conteudo.encode("utf-8"), content_type="text/plain")
                resposta = self.client.post(url, {"comprovante": arquivo}, secure=True)

                self.assertEqual(resposta.status_code, 200)
                dados = resposta.json()
                self.assertTrue(dados["ok"])
                self.assertEqual(dados["pagador"], pagador)
                self.assertEqual(dados["valor"], valor)
                self.assertEqual(dados["data_pagamento"], data_pagamento)
                self.assertEqual(dados["instituicao_pix"], instituicao)
                self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")

    def test_analisar_comprovante_pix_retorna_nome_arquivo_e_nao_reaproveita_upload_anterior(self):
        primeiro = SimpleUploadedFile(
            "mercado_pago_100.txt",
            (
                "Comprovante de Pix\n"
                "17/05/2026 11:09\n"
                "Valor R$ 100\n"
                "@ De\n"
                "Cliente Mercado Pago\n"
            ).encode("utf-8"),
            content_type="text/plain",
        )
        segundo = SimpleUploadedFile(
            "comprovante_picpay_50.txt",
            (
                "PicPay\n"
                "Comprovante Pix\n"
                "21/set/2025 - 13:20:41\n"
                "Valor\n"
                "R$ 50,00\n"
                "Pagador: ISA ALVES DE SOUZA\n"
                "Instituicao: PicPay\n"
            ).encode("utf-8"),
            content_type="text/plain",
        )
        url = reverse("estoque:central_pix_analisar_comprovante")

        resposta_primeira = self.client.post(url, {"comprovante": primeiro}, secure=True)
        self.assertEqual(resposta_primeira.status_code, 200)
        dados_primeiro = resposta_primeira.json()
        self.assertEqual(dados_primeiro["valor"], "100.00")
        self.assertEqual(dados_primeiro["data_pagamento"], "2026-05-17T11:09")


class PedidoTests(TestCase):
    def setUp(self):
        """Criar dados de teste para pedidos"""
        self.cliente = Cliente.objects.create(
            nome="Cliente Teste",
            cpf_cnpj="12345678901234",
            ativo=True,
        )
        self.produto = Produto.objects.create(
            nome="Produto Teste",
            preco_compra=50.00,
            preco_venda=100.00,
            preco_vista=100.00,
            preco_prazo=110.00,
            quantidade=50,
        )

    def _criar_pedido_com_item(self, quantidade=Decimal("2.000"), total=Decimal("200.00")):
        from .models import Pedido, ItemPedido

        pedido = Pedido.objects.create(
            cliente=self.cliente,
            data_pedido=timezone.now().date(),
            operador="Operador Pedido",
            total=total,
        )
        ItemPedido.objects.create(
            pedido=pedido,
            produto=self.produto,
            quantidade=quantidade,
            unidade="Un",
            preco_unitario=Decimal("100.00"),
            valor_total=total,
            estoque_no_momento=self.produto.quantidade,
        )
        return pedido

    def _post_gravar_venda_com_itens(self, pedido_id=None, itens=None):
        if itens is None:
            itens = [
                {
                    "produto_nome": self.produto.nome,
                    "quantidade": "2.000",
                    "unidade": "Un",
                    "preco_unitario": "100.00",
                    "subtotal": "200.00",
                }
            ]
        dados = {
            "cliente_id": self.cliente.id,
            "data_venda": timezone.localdate().isoformat(),
            "data_vencimento": "",
            "tipo_pagamento": "A vista",
            "operador": "Operador Teste",
            "total": "200.00",
            "itens": itens,
        }
        if pedido_id is not None:
            dados["pedido_id"] = pedido_id
        return self.client.post(
            reverse("estoque:gravar_venda"),
            data=json.dumps(dados),
            content_type="application/json",
            secure=True,
        )

    def _post_gravar_venda_com_item(self, pedido_id=None, quantidade="2.000"):
        return self._post_gravar_venda_com_itens(
            pedido_id=pedido_id,
            itens=[
                {
                    "produto_nome": self.produto.nome,
                    "quantidade": quantidade,
                    "unidade": "Un",
                    "preco_unitario": "100.00",
                    "subtotal": "200.00",
                }
            ],
        )

    def _post_editar_pedido(self, pedido, itens, cliente=None):
        return self.client.post(
            reverse("estoque:pedido_editar", args=[pedido.id]),
            data={
                "data_pedido": pedido.data_pedido.isoformat(),
                "cliente_id": (cliente or pedido.cliente or self.cliente).id,
                "data_prevista_entrega": pedido.data_prevista_entrega.isoformat() if pedido.data_prevista_entrega else "",
                "operador": "Operador Editado",
                "observacao": "Observacao editada",
                "itens_json": json.dumps(itens),
            },
            secure=True,
        )

    def _post_cancelar_pedido(self, pedido):
        return self.client.post(
            reverse("estoque:pedido_cancelar", args=[pedido.id]),
            secure=True,
        )

    def _post_criar_pedido(self, proxima_acao="", operador="Operador Pedido"):
        itens = [
            {
                "produto_id": self.produto.id,
                "produto_nome": self.produto.nome,
                "quantidade": "2.000",
                "unidade": "Un",
                "preco_unitario": "100.00",
                "valor_total": "200.00",
                "estoque_no_momento": str(self.produto.quantidade),
                "observacao": "",
            }
        ]
        return self.client.post(
            reverse("estoque:pedido_criar"),
            data={
                "data_pedido": timezone.localdate().isoformat(),
                "cliente_id": self.cliente.id,
                "data_prevista_entrega": "",
                "operador": operador,
                "observacao": "",
                "itens_json": json.dumps(itens),
                "proxima_acao": proxima_acao,
            },
            secure=True,
        )

    def test_pedido_exibe_apenas_funcionarios_operadores_no_campo_operador(self):
        operador = Funcionario.objects.create(
            nome="Livia Operadora",
            pode_operar_sistema=True,
        )
        Funcionario.objects.create(
            nome="Marcos Sem Operador",
            pode_operar_sistema=False,
        )
        Funcionario.objects.create(
            nome="Operador Inativo",
            ativo=False,
            pode_operar_sistema=True,
        )

        resposta = self.client.get(reverse("estoque:pedido_criar"), secure=True)

        self.assertContains(resposta, '<option value="">Sem operador</option>', html=True)
        self.assertContains(resposta, f'<option value="{operador.id}">Livia Operadora</option>', html=True)
        self.assertNotContains(resposta, "Marcos Sem Operador")
        self.assertNotContains(resposta, "Operador Inativo")

    def test_pedido_enter_do_cabecalho_passa_por_operador_sem_observacao(self):
        resposta = self.client.get(reverse("estoque:pedido_criar"), secure=True)
        conteudo = resposta.content.decode("utf-8")

        self.assertIn('const operadorPedido = document.getElementById("operador");', conteudo)
        self.assertIn('<textarea id="observacao" name="observacao"', conteudo)
        self.assertIn("avancarComEnter(dataPrevistaEntrega, operadorPedido);", conteudo)
        self.assertIn("avancarComEnter(operadorPedido, produtoBusca);", conteudo)
        self.assertIn("if (!operadorPedidoConfirmado) return;", conteudo)
        self.assertNotIn("avancarComEnter(operadorPedido, observacaoPedido);", conteudo)
        self.assertNotIn("avancarComEnter(observacaoPedido, produtoBusca);", conteudo)

    def test_pedido_criar_tem_protecao_para_acoes_perigosas(self):
        resposta = self.client.get(reverse("estoque:pedido_criar"), secure=True)
        conteudo = resposta.content.decode("utf-8")

        self.assertContains(resposta, 'id="btn-cancelar-form"')
        self.assertIn("Sair sem salvar o pedido?", conteudo)
        self.assertIn("Deseja limpar as sugestoes carregadas?", conteudo)
        self.assertIn("Deseja ocultar as sugestoes deste pedido?", conteudo)
        self.assertIn("Salvar este pedido e abrir o envio para venda?", conteudo)
        self.assertIn("salvamentoEmAndamento", conteudo)
        self.assertIn('index === remocaoPendente ? "Confirmar" : "Remover"', conteudo)

    def test_funcionario_marcado_na_tela_aparece_como_operador_no_pedido(self):
        resposta_funcionario = self.client.post(
            reverse("estoque:funcionarios"),
            data={
                "nome": "Rita Operadora",
                "telefone_whatsapp": "",
                "pode_operar_sistema": "on",
                "ativo": "on",
            },
            secure=True,
        )
        self.assertEqual(resposta_funcionario.status_code, 302)
        operador = Funcionario.objects.get(nome="Rita Operadora")
        self.assertTrue(operador.pode_operar_sistema)

        resposta_pedido = self.client.get(reverse("estoque:pedido_criar"), secure=True)

        self.assertContains(resposta_pedido, f'<option value="{operador.id}">Rita Operadora</option>', html=True)

    def test_salvar_pedido_com_operador_funcionario_grava_e_exibe_nome(self):
        from .models import Pedido

        operador = Funcionario.objects.create(
            nome="Paula Operadora",
            pode_operar_sistema=True,
        )

        resposta = self._post_criar_pedido(operador=str(operador.id))

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["sucesso"])
        pedido = Pedido.objects.get(pk=dados["pedido_id"])
        self.assertEqual(pedido.operador, "Paula Operadora")

        resposta_detalhe = self.client.get(reverse("estoque:pedido_detalhe", args=[pedido.id]), secure=True)
        self.assertContains(resposta_detalhe, "Paula Operadora")

        resposta_lista = self.client.get(reverse("estoque:pedidos"), secure=True)
        self.assertContains(resposta_lista, "Paula Operadora")

    def test_pedido_antigo_sem_operador_continua_exibindo_sem_operador(self):
        resposta = self._post_criar_pedido(operador="")

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["sucesso"])

        resposta_detalhe = self.client.get(reverse("estoque:pedido_detalhe", args=[dados["pedido_id"]]), secure=True)
        self.assertContains(resposta_detalhe, "Sem operador")

    def test_criar_pedido_com_cliente_e_itens_salva(self):
        """Criar pedido com cliente e itens deve salvar Pedido e ItemPedido"""
        from .models import Pedido, ItemPedido

        pedido = Pedido.objects.create(
            cliente=self.cliente,
            data_pedido=timezone.now().date(),
            operador="Operador Teste",
            total=100.00,
        )

        item = ItemPedido.objects.create(
            pedido=pedido,
            produto=self.produto,
            quantidade=1,
            unidade="Un",
            preco_unitario=100.00,
            valor_total=100.00,
            estoque_no_momento=50,
        )

        self.assertEqual(Pedido.objects.count(), 1)
        self.assertEqual(ItemPedido.objects.count(), 1)
        self.assertEqual(item.pedido.id, pedido.id)

    def test_salvar_pedido_normal_continua_redirecionando_para_detalhe(self):
        from .models import Pedido

        estoque_antes = self.produto.quantidade
        contas_antes = ContaReceber.objects.count()

        resposta = self._post_criar_pedido()

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["sucesso"])
        pedido = Pedido.objects.get(pk=dados["pedido_id"])
        self.assertEqual(dados["redirect_url"], reverse("estoque:pedido_detalhe", args=[pedido.id]))
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, estoque_antes)
        self.assertEqual(ContaReceber.objects.count(), contas_antes)
        self.assertEqual(Venda.objects.count(), 0)

    def test_salvar_pedido_e_enviar_para_venda_redireciona_sem_gravar_venda(self):
        from .models import Pedido

        estoque_antes = self.produto.quantidade
        contas_antes = ContaReceber.objects.count()

        resposta = self._post_criar_pedido(proxima_acao="enviar_venda")

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["sucesso"])
        pedido = Pedido.objects.get(pk=dados["pedido_id"])
        self.assertEqual(dados["redirect_url"], f"{reverse('estoque:vendas')}?pedido_id={pedido.id}")
        self.assertEqual(pedido.status, Pedido.STATUS_ABERTO)
        self.assertEqual(pedido.itens.count(), 1)
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, estoque_antes)
        self.assertEqual(ContaReceber.objects.count(), contas_antes)
        self.assertEqual(Venda.objects.count(), 0)

        resposta_vendas = self.client.get(reverse("estoque:vendas"), {"pedido_id": pedido.id}, secure=True)
        self.assertEqual(resposta_vendas.status_code, 200)
        self.assertContains(resposta_vendas, f"Venda preparada a partir do Pedido #{pedido.id}")
        self.assertContains(resposta_vendas, "Produto Teste")

    def test_gravar_pedido_nao_altera_quantidade_produto(self):
        """Gravar pedido não deve alterar Produto.quantidade"""
        from .models import Pedido, ItemPedido

        quantidade_inicial = self.produto.quantidade

        pedido = Pedido.objects.create(
            cliente=self.cliente,
            data_pedido=timezone.now().date(),
            total=100.00,
        )

        ItemPedido.objects.create(
            pedido=pedido,
            produto=self.produto,
            quantidade=10,
            unidade="Un",
            preco_unitario=100.00,
            valor_total=1000.00,
            estoque_no_momento=quantidade_inicial,
        )

        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, quantidade_inicial)

    def test_pedido_nao_cria_conta_receber(self):
        """Pedido não deve criar ContaReceber"""
        from .models import Pedido, ItemPedido

        conta_receber_inicial = ContaReceber.objects.count()

        pedido = Pedido.objects.create(
            cliente=self.cliente,
            data_pedido=timezone.now().date(),
            total=100.00,
        )

        ItemPedido.objects.create(
            pedido=pedido,
            produto=self.produto,
            quantidade=1,
            unidade="Un",
            preco_unitario=100.00,
            valor_total=100.00,
            estoque_no_momento=50,
        )

        self.assertEqual(ContaReceber.objects.count(), conta_receber_inicial)

    def test_item_pedido_salva_estoque_no_momento(self):
        """ItemPedido deve salvar estoque_no_momento corretamente"""
        from .models import Pedido, ItemPedido

        estoque_no_momento = self.produto.quantidade

        pedido = Pedido.objects.create(
            cliente=self.cliente,
            data_pedido=timezone.now().date(),
            total=100.00,
        )

        item = ItemPedido.objects.create(
            pedido=pedido,
            produto=self.produto,
            quantidade=1,
            unidade="Un",
            preco_unitario=100.00,
            valor_total=100.00,
            estoque_no_momento=estoque_no_momento,
        )

        self.assertEqual(item.estoque_no_momento, estoque_no_momento)

    def test_detalhe_pedido_aberto_mostra_enviar_para_venda(self):
        pedido = self._criar_pedido_com_item()

        resposta = self.client.get(reverse("estoque:pedido_detalhe", args=[pedido.id]), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Gerar Venda")
        self.assertContains(resposta, f'{reverse("estoque:vendas")}?pedido_id={pedido.id}')
        self.assertNotContains(resposta, "Gerar Venda da Pend?ncia")
        self.assertContains(resposta, "Itens do Pedido")
        self.assertContains(resposta, "Total do Pedido")
        self.assertNotContains(resposta, "Itens pendentes")
        self.assertContains(resposta, "Editar Pedido")
        self.assertContains(resposta, "Gerar venda a partir deste pedido? Confira os dados antes de continuar.")
        self.assertContains(resposta, "Tem certeza que deseja cancelar este pedido? O historico sera preservado, mas o pedido deixara de ficar ativo.")

    def test_editar_pedido_aberto_atualiza_itens_sem_baixar_estoque_ou_criar_financeiro(self):
        from .models import ItemPedido

        produto_novo = Produto.objects.create(
            nome="Produto Novo Pedido",
            preco_compra=Decimal("20.00"),
            preco_venda=Decimal("40.00"),
            preco_vista=Decimal("40.00"),
            preco_prazo=Decimal("45.00"),
            quantidade=7,
        )
        pedido = self._criar_pedido_com_item()
        item_original = pedido.itens.get()
        estoque_original = self.produto.quantidade
        estoque_novo = produto_novo.quantidade
        contas_antes = ContaReceber.objects.count()

        resposta_get = self.client.get(reverse("estoque:pedido_editar", args=[pedido.id]), secure=True)
        self.assertEqual(resposta_get.status_code, 200)
        self.assertContains(resposta_get, "Editar Pedido")
        self.assertContains(resposta_get, "Produto Teste")

        resposta = self._post_editar_pedido(
            pedido,
            [
                {
                    "item_id": item_original.id,
                    "produto_id": self.produto.id,
                    "produto_nome": self.produto.nome,
                    "quantidade": "3.000",
                    "unidade": "Cx",
                    "preco_unitario": "95.50",
                    "valor_total": "286.50",
                    "observacao": "Qtd e preco alterados",
                },
                {
                    "produto_id": produto_novo.id,
                    "produto_nome": produto_novo.nome,
                    "quantidade": "2.000",
                    "unidade": "Un",
                    "preco_unitario": "40.00",
                    "valor_total": "80.00",
                    "observacao": "Produto adicionado",
                },
            ],
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["sucesso"])
        self.assertIn("pedido_editado=1", dados["redirect_url"])
        pedido.refresh_from_db()
        self.produto.refresh_from_db()
        produto_novo.refresh_from_db()
        self.assertEqual(pedido.total, Decimal("366.50"))
        self.assertEqual(pedido.observacao, "Observacao editada")
        self.assertEqual(self.produto.quantidade, estoque_original)
        self.assertEqual(produto_novo.quantidade, estoque_novo)
        self.assertEqual(ContaReceber.objects.count(), contas_antes)
        self.assertEqual(Venda.objects.count(), 0)
        self.assertEqual(ItemPedido.objects.filter(pedido=pedido).count(), 2)
        item_editado = pedido.itens.get(produto=self.produto)
        self.assertEqual(item_editado.quantidade, Decimal("3.000"))
        self.assertEqual(item_editado.preco_unitario, Decimal("95.50"))
        self.assertEqual(item_editado.unidade, "Cx")
        self.assertEqual(item_editado.observacao, "Qtd e preco alterados")
        self.assertTrue(pedido.itens.filter(produto=produto_novo, quantidade=Decimal("2.000")).exists())

    def test_editar_pedido_existente_substitui_quantidade_corrigida(self):
        pedido = self._criar_pedido_com_item(quantidade=Decimal("5000.003"), total=Decimal("500000.30"))
        item = pedido.itens.get()

        resposta = self._post_editar_pedido(
            pedido,
            [
                {
                    "item_id": item.id,
                    "produto_id": self.produto.id,
                    "produto_nome": self.produto.nome,
                    "quantidade": "5",
                    "unidade": "Un",
                    "preco_unitario": "100.00",
                    "valor_total": "500.00",
                    "observacao": "Quantidade corrigida",
                },
            ],
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json()["sucesso"])
        pedido.refresh_from_db()
        item_corrigido = pedido.itens.get(produto=self.produto)
        self.assertEqual(item_corrigido.quantidade, Decimal("5.000"))
        self.assertEqual(item_corrigido.valor_total, Decimal("500.00"))
        self.assertEqual(pedido.total, Decimal("500.00"))

        resposta_decimal = self._post_editar_pedido(
            pedido,
            [
                {
                    "item_id": item_corrigido.id,
                    "produto_id": self.produto.id,
                    "produto_nome": self.produto.nome,
                    "quantidade": "2,5",
                    "unidade": "Un",
                    "preco_unitario": "100.00",
                    "valor_total": "250.00",
                    "observacao": "Quantidade decimal corrigida",
                },
            ],
        )

        self.assertEqual(resposta_decimal.status_code, 200)
        self.assertTrue(resposta_decimal.json()["sucesso"])
        pedido.refresh_from_db()
        item_decimal = pedido.itens.get(produto=self.produto)
        self.assertEqual(item_decimal.quantidade, Decimal("2.500"))
        self.assertEqual(item_decimal.valor_total, Decimal("250.00"))
        self.assertEqual(pedido.total, Decimal("250.00"))

    def test_editar_pedido_convertido_total_bloqueia(self):
        from .models import Pedido

        pedido = self._criar_pedido_com_item()
        pedido.status = Pedido.STATUS_CONVERTIDO_EM_VENDA
        pedido.save(update_fields=["status", "atualizado_em"])
        item_original = pedido.itens.get()

        resposta_get = self.client.get(reverse("estoque:pedido_editar", args=[pedido.id]), secure=True)
        self.assertEqual(resposta_get.status_code, 302)
        self.assertEqual(resposta_get.url, reverse("estoque:pedido_detalhe", args=[pedido.id]))

        resposta_post = self._post_editar_pedido(
            pedido,
            [
                {
                    "item_id": item_original.id,
                    "produto_id": self.produto.id,
                    "produto_nome": self.produto.nome,
                    "quantidade": "5.000",
                    "unidade": "Un",
                    "preco_unitario": "10.00",
                    "valor_total": "50.00",
                    "observacao": "",
                }
            ],
        )

        self.assertEqual(resposta_post.status_code, 400)
        self.assertFalse(resposta_post.json()["sucesso"])
        pedido.refresh_from_db()
        item_original.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_CONVERTIDO_EM_VENDA)
        self.assertEqual(pedido.total, Decimal("200.00"))
        self.assertEqual(item_original.quantidade, Decimal("2.000"))

    def test_editar_pedido_parcial_edita_pendente_sem_mudar_item_ja_vendido(self):
        from .models import Pedido

        self.produto.quantidade = 4
        self.produto.save(update_fields=["quantidade"])
        pedido = self._criar_pedido_com_item(quantidade=Decimal("5.000"), total=Decimal("500.00"))
        resposta_venda = self._post_gravar_venda_com_item(pedido_id=pedido.id, quantidade="5.000")
        self.assertEqual(resposta_venda.status_code, 200)
        pedido.refresh_from_db()
        self.produto.refresh_from_db()
        item_pendente = pedido.itens.get(produto=self.produto)
        self.assertEqual(pedido.status, Pedido.STATUS_PARCIAL)
        self.assertEqual(item_pendente.quantidade, Decimal("1.000"))
        estoque_apos_venda = self.produto.quantidade
        vendas_antes = Venda.objects.count()
        itens_venda_antes = ItemVenda.objects.count()
        contas_antes = ContaReceber.objects.count()

        resposta = self._post_editar_pedido(
            pedido,
            [
                {
                    "item_id": item_pendente.id,
                    "produto_id": self.produto.id,
                    "produto_nome": self.produto.nome,
                    "quantidade": "2.000",
                    "unidade": "Un",
                    "preco_unitario": "120.00",
                    "valor_total": "240.00",
                    "observacao": "Saldo renegociado",
                }
            ],
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json()["sucesso"])
        pedido.refresh_from_db()
        item_pendente.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_PARCIAL)
        self.assertEqual(pedido.total, Decimal("240.00"))
        self.assertEqual(item_pendente.quantidade, Decimal("2.000"))
        self.assertEqual(item_pendente.preco_unitario, Decimal("120.00"))
        self.assertEqual(item_pendente.valor_total, Decimal("240.00"))
        self.assertEqual(self.produto.quantidade, estoque_apos_venda)
        self.assertEqual(Venda.objects.count(), vendas_antes)
        self.assertEqual(ItemVenda.objects.count(), itens_venda_antes)
        self.assertEqual(ContaReceber.objects.count(), contas_antes)

    def test_cancelar_pedido_aberto_marca_cancelado_sem_apagar(self):
        from .models import ItemPedido, Pedido

        pedido = self._criar_pedido_com_item()
        item_id = pedido.itens.get().id

        resposta = self._post_cancelar_pedido(pedido)

        self.assertEqual(resposta.status_code, 302)
        pedido.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_CANCELADO)
        self.assertTrue(ItemPedido.objects.filter(pk=item_id, pedido=pedido).exists())

        resposta_detalhe = self.client.get(reverse("estoque:pedido_detalhe", args=[pedido.id]), secure=True)
        self.assertEqual(resposta_detalhe.status_code, 200)
        self.assertContains(resposta_detalhe, "Cancelado")
        self.assertNotContains(resposta_detalhe, "Cancelar Pedido")
        self.assertNotContains(resposta_detalhe, "Editar Pedido")

    def test_cancelar_pedido_cancelado_nao_mexe_no_estoque_nem_financeiro(self):
        from .models import Pedido

        pedido = self._criar_pedido_com_item()
        estoque_antes = self.produto.quantidade
        contas_antes = ContaReceber.objects.count()
        vendas_antes = Venda.objects.count()

        resposta = self._post_cancelar_pedido(pedido)

        self.assertEqual(resposta.status_code, 302)
        pedido.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_CANCELADO)
        self.assertEqual(self.produto.quantidade, estoque_antes)
        self.assertEqual(ContaReceber.objects.count(), contas_antes)
        self.assertEqual(Venda.objects.count(), vendas_antes)

    def test_cancelar_pedido_convertido_total_bloqueia(self):
        from .models import Pedido

        pedido = self._criar_pedido_com_item()
        pedido.status = Pedido.STATUS_CONVERTIDO_EM_VENDA
        pedido.save(update_fields=["status", "atualizado_em"])
        item_id = pedido.itens.get().id

        resposta = self._post_cancelar_pedido(pedido)

        self.assertEqual(resposta.status_code, 302)
        pedido.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_CONVERTIDO_EM_VENDA)
        self.assertTrue(pedido.itens.filter(pk=item_id).exists())

    def test_cancelar_pedido_parcial_nao_cancela_venda_ja_gerada(self):
        from .models import Pedido

        self.produto.quantidade = 4
        self.produto.save(update_fields=["quantidade"])
        pedido = self._criar_pedido_com_item(quantidade=Decimal("5.000"), total=Decimal("500.00"))
        resposta_venda = self._post_gravar_venda_com_item(pedido_id=pedido.id, quantidade="5.000")
        self.assertEqual(resposta_venda.status_code, 200)
        pedido.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_PARCIAL)
        venda = Venda.objects.get()
        estoque_apos_venda = self.produto.quantidade
        vendas_antes = Venda.objects.count()
        itens_venda_antes = ItemVenda.objects.count()
        contas_antes = ContaReceber.objects.count()

        resposta = self._post_cancelar_pedido(pedido)

        self.assertEqual(resposta.status_code, 302)
        pedido.refresh_from_db()
        venda.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_CANCELADO)
        self.assertFalse(venda.cancelada)
        self.assertEqual(Venda.objects.count(), vendas_antes)
        self.assertEqual(ItemVenda.objects.count(), itens_venda_antes)
        self.assertEqual(ContaReceber.objects.count(), contas_antes)
        self.assertEqual(self.produto.quantidade, estoque_apos_venda)

    def test_importar_pedido_para_vendas_prepara_tela_sem_gravar(self):
        from .models import Pedido

        quantidade_inicial = self.produto.quantidade
        pedido = self._criar_pedido_com_item()

        resposta = self.client.get(
            reverse("estoque:vendas"),
            {"pedido_id": pedido.id},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, f"Venda preparada a partir do Pedido #{pedido.id}")
        self.assertContains(resposta, "pedidoImportadoVenda")
        self.assertContains(resposta, "Produto Teste")
        self.assertContains(resposta, f'"produto_id": {self.produto.id}')
        self.assertContains(resposta, f'"detalhe_url": "{reverse("estoque:pedido_detalhe", args=[pedido.id])}"')
        self.assertContains(resposta, f"Voltar ao Pedido #{pedido.id}")
        conteudo = resposta.content.decode("utf-8")
        self.assertLess(
            conteudo.index("let linhaSelecionada = null;"),
            conteudo.rindex("prepararVendaComPedidoImportado();"),
        )
        self.assertContains(resposta, 'produtoBusca.focus({ preventScroll: true });')
        self.assertContains(resposta, 'produtoBusca.scrollIntoView({ behavior: "smooth", block: "center" });')
        self.assertContains(resposta, 'window.setTimeout(() => produtoBusca.focus({ preventScroll: true }), 180);')
        self.assertContains(resposta, 'let pedidoImportadoVenda = JSON.parse')
        self.assertContains(resposta, 'pedido_id: pedidoImportadoVenda?.id || null')
        self.assertContains(resposta, 'function limparPedidoImportadoVenda()')
        self.assertContains(resposta, 'document.querySelector(".pedido-importado-aviso")?.remove();')
        self.assertContains(resposta, 'function limparVendaAposGravacao()')
        self.assertContains(resposta, 'limparVendaAposGravacao();')

        pedido.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_ABERTO)
        self.assertEqual(self.produto.quantidade, quantidade_inicial)
        self.assertEqual(Venda.objects.count(), 0)
        self.assertEqual(ItemVenda.objects.count(), 0)
        self.assertEqual(ContaReceber.objects.count(), 0)

    def test_gravar_venda_a_partir_de_pedido_converte_pedido_apos_sucesso(self):
        from .models import Pedido

        pedido = self._criar_pedido_com_item()

        resposta = self._post_gravar_venda_com_item(pedido_id=pedido.id)

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["sucesso"])
        self.assertIn("venda_id", dados)
        self.assertIn("visualizar_url", dados)
        pedido.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_CONVERTIDO_EM_VENDA)
        self.assertEqual(Venda.objects.count(), 1)
        self.assertEqual(ItemVenda.objects.count(), 1)
        self.assertEqual(self.produto.quantidade, 48)

        resposta_detalhe = self.client.get(reverse("estoque:pedido_detalhe", args=[pedido.id]), secure=True)
        self.assertContains(resposta_detalhe, "Convertido em venda")
        self.assertContains(resposta_detalhe, "Pedido convertido em venda.")
        self.assertNotContains(resposta_detalhe, "Enviar para Venda")
        self.assertNotContains(resposta_detalhe, "Enviar pend")
        self.assertContains(resposta_detalhe, "Ir para Vendas")
        self.assertContains(resposta_detalhe, "Itens do Pedido")
        self.assertNotContains(resposta_detalhe, "Itens pendentes")

        resposta_lista = self.client.get(reverse("estoque:pedidos"), secure=True)
        self.assertContains(resposta_lista, reverse("estoque:pedido_detalhe", args=[pedido.id]))
        self.assertContains(resposta_lista, "Convertido em venda")

        resposta_abertos = self.client.get(reverse("estoque:pedidos"), {"status": Pedido.STATUS_ABERTO}, secure=True)
        self.assertNotContains(resposta_abertos, reverse("estoque:pedido_detalhe", args=[pedido.id]))

    def test_gravar_venda_de_pedido_com_item_zerado_grava_disponiveis_e_deixa_pendente(self):
        from .models import Pedido, ItemPedido

        produto_zerado = Produto.objects.create(
            nome="Produto Sem Estoque",
            preco_compra=Decimal("10.00"),
            preco_venda=Decimal("50.00"),
            preco_vista=Decimal("50.00"),
            preco_prazo=Decimal("60.00"),
            quantidade=0,
        )
        pedido = self._criar_pedido_com_item()
        ItemPedido.objects.create(
            pedido=pedido,
            produto=produto_zerado,
            quantidade=Decimal("1.000"),
            unidade="Un",
            preco_unitario=Decimal("50.00"),
            valor_total=Decimal("50.00"),
            estoque_no_momento=0,
        )

        resposta = self._post_gravar_venda_com_itens(
            pedido_id=pedido.id,
            itens=[
                {
                    "produto_nome": self.produto.nome,
                    "quantidade": "2.000",
                    "unidade": "Un",
                    "preco_unitario": "100.00",
                    "subtotal": "200.00",
                },
                {
                    "produto_nome": produto_zerado.nome,
                    "quantidade": "1.000",
                    "unidade": "Un",
                    "preco_unitario": "50.00",
                    "subtotal": "50.00",
                },
            ],
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["sucesso"])
        self.assertIn("itens disponiveis", dados["mensagem"])
        self.assertIn("Produto Sem Estoque", dados["mensagem"])
        pedido.refresh_from_db()
        self.produto.refresh_from_db()
        produto_zerado.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_PARCIAL)
        self.assertEqual(pedido.total, Decimal("50.00"))
        self.assertEqual(Venda.objects.count(), 1)
        venda = Venda.objects.get()
        self.assertEqual(venda.total, Decimal("200.00"))
        self.assertEqual(ItemVenda.objects.count(), 1)
        self.assertEqual(ItemVenda.objects.get().produto, self.produto)
        self.assertEqual(self.produto.quantidade, 48)
        self.assertEqual(produto_zerado.quantidade, 0)
        item_vendido = pedido.itens.get(produto=self.produto)
        item_pendente = pedido.itens.get(produto=produto_zerado)
        self.assertEqual(item_vendido.quantidade, Decimal("0.000"))
        self.assertEqual(item_vendido.valor_total, Decimal("0.00"))
        self.assertEqual(item_pendente.quantidade, Decimal("1.000"))
        self.assertEqual(item_pendente.valor_total, Decimal("50.00"))

        resposta_detalhe = self.client.get(reverse("estoque:pedido_detalhe", args=[pedido.id]), secure=True)
        self.assertContains(resposta_detalhe, "Parcial")
        self.assertContains(resposta_detalhe, "Pedido parcialmente atendido.")
        self.assertContains(resposta_detalhe, "Itens pendentes do pedido")
        self.assertContains(resposta_detalhe, "Produto Sem Estoque")
        self.assertContains(resposta_detalhe, "Total pendente")
        self.assertContains(resposta_detalhe, "R$ 50.00")
        self.assertNotContains(resposta_detalhe, "Produto Teste")

    def test_gravar_venda_de_pedido_com_estoque_parcial_vende_disponivel_e_deixa_restante(self):
        from .models import Pedido

        self.produto.quantidade = 4
        self.produto.save(update_fields=["quantidade"])
        pedido = self._criar_pedido_com_item(quantidade=Decimal("5.000"), total=Decimal("500.00"))

        resposta = self._post_gravar_venda_com_item(pedido_id=pedido.id, quantidade="5.000")

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["sucesso"])
        self.assertIn("Produto Teste: 1 Un", dados["mensagem"])
        pedido.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_PARCIAL)
        self.assertEqual(pedido.total, Decimal("100.00"))
        self.assertEqual(Venda.objects.count(), 1)
        venda = Venda.objects.get()
        self.assertEqual(venda.total, Decimal("400.00"))
        item = ItemVenda.objects.get()
        self.assertEqual(item.quantidade, Decimal("4.000"))
        self.assertEqual(item.valor_total, Decimal("400.00"))
        self.assertEqual(self.produto.quantidade, 0)
        item_pedido = pedido.itens.get(produto=self.produto)
        self.assertEqual(item_pedido.quantidade, Decimal("1.000"))
        self.assertEqual(item_pedido.valor_total, Decimal("100.00"))

        resposta_detalhe = self.client.get(reverse("estoque:pedido_detalhe", args=[pedido.id]), secure=True)
        self.assertContains(resposta_detalhe, "Itens pendentes do pedido")
        self.assertContains(resposta_detalhe, "Produto Teste")
        self.assertContains(resposta_detalhe, 'data-label="Quantidade">1</td>')
        self.assertNotContains(resposta_detalhe, "1.000")
        self.assertContains(resposta_detalhe, "R$ 100.00")

    def test_venda_de_pedido_parcial_exibe_aviso_na_nota_e_whatsapp(self):
        from .models import Pedido

        self.cliente.whatsapp = "11999999999"
        self.cliente.save(update_fields=["whatsapp"])
        self.produto.quantidade = 4
        self.produto.save(update_fields=["quantidade"])
        pedido = self._criar_pedido_com_item(quantidade=Decimal("5.000"), total=Decimal("500.00"))

        resposta = self._post_gravar_venda_com_item(pedido_id=pedido.id, quantidade="5.000")

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["sucesso"])
        pedido.refresh_from_db()
        self.produto.refresh_from_db()
        venda = Venda.objects.get()
        item_pendente = pedido.itens.get(produto=self.produto)
        estoque_apos_venda = self.produto.quantidade
        contas_apos_venda = ContaReceber.objects.count()
        self.assertEqual(pedido.status, Pedido.STATUS_PARCIAL)
        self.assertEqual(item_pendente.quantidade, Decimal("1.000"))
        self.assertEqual(item_pendente.valor_total, Decimal("100.00"))
        self.assertTrue(EventoVenda.objects.filter(venda=venda, tipo_evento="pedido_parcial").exists())

        resposta_detalhe = self.client.get(reverse("estoque:venda_detalhe", args=[venda.id]), secure=True)

        self.assertEqual(resposta_detalhe.status_code, 200)
        self.assertContains(resposta_detalhe, f"Venda parcial gerada a partir do Pedido #{pedido.id}")
        self.assertContains(resposta_detalhe, "Esta nota cont")
        self.assertContains(resposta_detalhe, "Itens pendentes:")
        self.assertContains(resposta_detalhe, "Produto Teste: 1 Un")
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.quantidade, estoque_apos_venda)
        self.assertEqual(ContaReceber.objects.count(), contas_apos_venda)
        self.assertEqual(Venda.objects.count(), 1)

        whatsapp_url = views.montar_link_whatsapp_venda(venda)
        mensagem_whatsapp = parse_qs(urlsplit(whatsapp_url).query)["text"][0]
        self.assertIn(f"Pedido #{pedido.id}", mensagem_whatsapp)
        self.assertIn("Itens pendentes:", mensagem_whatsapp)
        self.assertIn("Produto Teste: 1 Un", mensagem_whatsapp)

    def test_venda_parcial_antiga_sem_evento_inferida_pelo_pedido(self):
        from .models import ItemPedido, Pedido

        produto_vendido = Produto.objects.create(
            nome="Produto Vendido Legado",
            preco_compra=Decimal("5.00"),
            preco_venda=Decimal("20.00"),
            preco_vista=Decimal("20.00"),
            preco_prazo=Decimal("22.00"),
            quantidade=0,
        )
        produto_pendente = Produto.objects.create(
            nome="Produto Pendente Legado",
            preco_compra=Decimal("2.00"),
            preco_venda=Decimal("6.80"),
            preco_vista=Decimal("6.80"),
            preco_prazo=Decimal("7.50"),
            quantidade=0,
        )
        pedido = Pedido.objects.create(
            cliente=self.cliente,
            data_pedido=timezone.localdate(),
            status=Pedido.STATUS_PARCIAL,
            total=Decimal("6.80"),
        )
        ItemPedido.objects.create(
            pedido=pedido,
            produto=produto_vendido,
            quantidade=Decimal("0.000"),
            unidade="Un",
            preco_unitario=Decimal("20.00"),
            valor_total=Decimal("0.00"),
            estoque_no_momento=2,
        )
        ItemPedido.objects.create(
            pedido=pedido,
            produto=produto_pendente,
            quantidade=Decimal("1.000"),
            unidade="Un",
            preco_unitario=Decimal("6.80"),
            valor_total=Decimal("6.80"),
            estoque_no_momento=0,
        )
        venda = Venda.objects.create(
            cliente=self.cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            operador="Operador Teste",
            total=Decimal("40.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_vendido,
            quantidade=Decimal("2.000"),
            unidade="Un",
            preco_unitario=Decimal("20.00"),
            valor_total=Decimal("40.00"),
        )
        EventoVenda.objects.create(
            venda=venda,
            tipo_evento="venda_gravada",
            descricao="Venda gravada com sucesso. Estoque baixado para os itens vendidos.",
            canal="sistema",
        )

        resposta = self.client.get(reverse("estoque:venda_detalhe", args=[venda.id]), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, f"Venda parcial gerada a partir do Pedido #{pedido.id}")
        self.assertContains(resposta, "Produto Pendente Legado - 1 Un")
        self.assertFalse(EventoVenda.objects.filter(venda=venda, tipo_evento="pedido_parcial").exists())

    def test_venda_normal_sem_pedido_parcial_nao_exibe_aviso(self):
        resposta = self._post_gravar_venda_com_item()

        self.assertEqual(resposta.status_code, 200)
        venda = Venda.objects.get()

        resposta_detalhe = self.client.get(reverse("estoque:venda_detalhe", args=[venda.id]), secure=True)

        self.assertEqual(resposta_detalhe.status_code, 200)
        self.assertNotContains(resposta_detalhe, "Venda parcial gerada a partir do Pedido")
        self.assertNotContains(resposta_detalhe, "Esta nota contém os itens disponíveis agora")
        self.assertFalse(EventoVenda.objects.filter(venda=venda, tipo_evento="pedido_parcial").exists())

    def test_detalhe_pedido_parcial_antigo_calcula_saldo_pendente_pela_venda_compativel(self):
        from .models import Pedido, ItemPedido

        produto_vendido = Produto.objects.create(
            nome="Produto Vendido Pedido Parcial",
            preco_compra=Decimal("10.00"),
            preco_venda=Decimal("48.00"),
            preco_vista=Decimal("48.00"),
            preco_prazo=Decimal("48.00"),
            quantidade=0,
        )
        produto_pendente = Produto.objects.create(
            nome="Produto Pendente Pedido Parcial",
            preco_compra=Decimal("5.00"),
            preco_venda=Decimal("10.50"),
            preco_vista=Decimal("10.50"),
            preco_prazo=Decimal("10.50"),
            quantidade=0,
        )
        pedido = Pedido.objects.create(
            cliente=self.cliente,
            data_pedido=timezone.now().date(),
            total=Decimal("127.50"),
            status=Pedido.STATUS_ABERTO,
        )
        ItemPedido.objects.create(
            pedido=pedido,
            produto=produto_vendido,
            quantidade=Decimal("2.000"),
            unidade="Un",
            preco_unitario=Decimal("48.00"),
            valor_total=Decimal("96.00"),
            estoque_no_momento=2,
        )
        ItemPedido.objects.create(
            pedido=pedido,
            produto=produto_pendente,
            quantidade=Decimal("3.000"),
            unidade="Un",
            preco_unitario=Decimal("10.50"),
            valor_total=Decimal("31.50"),
            estoque_no_momento=0,
        )
        venda = Venda.objects.create(
            cliente=self.cliente,
            data_venda=timezone.localdate(),
            tipo_pagamento="A vista",
            operador="Operador Teste",
            total=Decimal("96.00"),
        )
        ItemVenda.objects.create(
            venda=venda,
            produto=produto_vendido,
            quantidade=Decimal("2.000"),
            unidade="Un",
            preco_unitario=Decimal("48.00"),
            valor_total=Decimal("96.00"),
        )
        pedido.status = Pedido.STATUS_PARCIAL
        pedido.save(update_fields=["status", "atualizado_em"])

        resposta = self.client.get(reverse("estoque:pedido_detalhe", args=[pedido.id]), secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Itens pendentes do pedido")
        self.assertContains(resposta, "Produto Pendente Pedido Parcial")
        self.assertContains(resposta, 'data-label="Quantidade">3</td>')
        self.assertNotContains(resposta, "3.000")
        self.assertContains(resposta, "R$ 31.50")
        self.assertNotContains(resposta, "Produto Vendido Pedido Parcial")
        self.assertContains(resposta, "Gerar Venda da Pend&ecirc;ncia")
        self.assertContains(resposta, f'{reverse("estoque:vendas")}?pedido_id={pedido.id}')
        self.assertNotContains(resposta, ">Ir para Venda<")

        resposta_vendas = self.client.get(reverse("estoque:vendas"), {"pedido_id": pedido.id}, secure=True)
        self.assertEqual(resposta_vendas.status_code, 200)
        conteudo_vendas = resposta_vendas.content.decode("utf-8")
        self.assertIn("Venda preparada a partir do Pedido", conteudo_vendas)
        self.assertIn('"produto_nome": "Produto Pendente Pedido Parcial"', conteudo_vendas)
        self.assertIn('"quantidade": "3.000"', conteudo_vendas)
        self.assertIn('"valor_total": "31.50"', conteudo_vendas)
        self.assertNotIn('"produto_nome": "Produto Vendido Pedido Parcial"', conteudo_vendas)

    def test_gravar_venda_de_pedido_sem_estoque_nao_grava_e_mantem_aberto(self):
        from .models import Pedido

        self.produto.quantidade = 0
        self.produto.save(update_fields=["quantidade"])
        pedido = self._criar_pedido_com_item()

        resposta = self._post_gravar_venda_com_item(pedido_id=pedido.id)

        self.assertEqual(resposta.status_code, 400)
        dados = resposta.json()
        self.assertFalse(dados["sucesso"])
        self.assertEqual(
            dados["mensagem"],
            f"Nenhum item do Pedido #{pedido.id} possui estoque disponivel para gerar venda. "
            "Os itens continuam pendentes no pedido.",
        )
        self.assertEqual(dados["toast_duracao_ms"], 12000)
        pedido.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_ABERTO)
        self.assertEqual(Venda.objects.count(), 0)
        self.assertEqual(ItemVenda.objects.count(), 0)
        self.assertEqual(self.produto.quantidade, 0)

    def test_gravar_venda_sem_pedido_id_nao_altera_pedidos_abertos(self):
        from .models import Pedido

        pedido = self._criar_pedido_com_item()

        resposta = self._post_gravar_venda_com_item()

        self.assertEqual(resposta.status_code, 200)
        pedido.refresh_from_db()
        self.assertEqual(pedido.status, Pedido.STATUS_ABERTO)
        self.assertEqual(Venda.objects.count(), 1)

    def test_lista_de_pedidos_carrega(self):
        """Lista de pedidos deve carregar corretamente"""
        from .models import Pedido

        pedido = Pedido.objects.create(
            cliente=self.cliente,
            data_pedido=timezone.now().date(),
            total=100.00,
        )

        url = reverse("estoque:pedidos")
        resposta = self.client.get(url, secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("pedidos", resposta.context)
        self.assertIn("localidades", resposta.context)

    def test_lista_de_pedidos_filtra_por_bairro_ou_cidade_do_cliente(self):
        from .models import Pedido

        self.cliente.bairro = "Centro"
        self.cliente.cidade = "Fortaleza"
        self.cliente.save(update_fields=["bairro", "cidade"])
        cliente_outro = Cliente.objects.create(
            nome="Cliente Outra Localidade",
            bairro="Aldeota",
            cidade="Caucaia",
            ativo=True,
        )
        pedido_centro = Pedido.objects.create(
            cliente=self.cliente,
            data_pedido=timezone.now().date(),
            total=Decimal("100.00"),
        )
        pedido_outro = Pedido.objects.create(
            cliente=cliente_outro,
            data_pedido=timezone.now().date(),
            total=Decimal("80.00"),
        )

        resposta_bairro = self.client.get(reverse("estoque:pedidos"), {"localidade": "Centro"}, secure=True)
        self.assertEqual(resposta_bairro.status_code, 200)
        self.assertContains(resposta_bairro, reverse("estoque:pedido_detalhe", args=[pedido_centro.id]))
        self.assertNotContains(resposta_bairro, reverse("estoque:pedido_detalhe", args=[pedido_outro.id]))
        self.assertContains(resposta_bairro, "Centro")

        resposta_cidade = self.client.get(reverse("estoque:pedidos"), {"localidade": "Caucaia"}, secure=True)
        self.assertEqual(resposta_cidade.status_code, 200)
        self.assertContains(resposta_cidade, reverse("estoque:pedido_detalhe", args=[pedido_outro.id]))
        self.assertNotContains(resposta_cidade, reverse("estoque:pedido_detalhe", args=[pedido_centro.id]))

    def test_lista_de_pedidos_mantem_filtros_atuais_com_localidade(self):
        from .models import Pedido

        hoje = timezone.now().date()
        self.cliente.bairro = "Messejana"
        self.cliente.cidade = "Fortaleza"
        self.cliente.save(update_fields=["bairro", "cidade"])
        pedido = Pedido.objects.create(
            cliente=self.cliente,
            data_pedido=hoje,
            status=Pedido.STATUS_CANCELADO,
            total=Decimal("55.00"),
        )

        resposta = self.client.get(
            reverse("estoque:pedidos"),
            {
                "status": Pedido.STATUS_CANCELADO,
                "cliente_id": self.cliente.id,
                "localidade": "Messejana",
                "data_inicio": hoje.isoformat(),
                "data_fim": hoje.isoformat(),
            },
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, f"#{pedido.id}")
        self.assertContains(resposta, "Cancelado")
        self.assertContains(resposta, "Messejana")

    def test_criar_pedido_retorna_sugestoes_por_vendas_ativas_do_cliente(self):
        """Sugestoes de pedido devem usar ultimas vendas ativas do cliente"""
        hoje = timezone.now().date()
        venda_antiga = Venda.objects.create(
            cliente=self.cliente,
            data_venda=hoje - timedelta(days=2),
            total=100,
            cancelada=False,
        )
        venda_recente = Venda.objects.create(
            cliente=self.cliente,
            data_venda=hoje,
            total=200,
            cancelada=False,
        )
        venda_cancelada = Venda.objects.create(
            cliente=self.cliente,
            data_venda=hoje,
            total=300,
            cancelada=True,
        )
        produto_cancelado = Produto.objects.create(
            nome="Produto Cancelado",
            preco_compra=10,
            preco_venda=20,
            preco_vista=20,
            preco_prazo=25,
            quantidade=5,
        )

        ItemVenda.objects.create(
            venda=venda_antiga,
            produto=self.produto,
            quantidade=1,
            unidade="Un",
            preco_unitario=100,
            valor_total=100,
        )
        ItemVenda.objects.create(
            venda=venda_recente,
            produto=self.produto,
            quantidade=2,
            unidade="Un",
            preco_unitario=90,
            valor_total=180,
        )
        ItemVenda.objects.create(
            venda=venda_cancelada,
            produto=produto_cancelado,
            quantidade=3,
            unidade="Un",
            preco_unitario=20,
            valor_total=60,
        )

        resposta = self.client.get(
            reverse("estoque:pedido_criar"),
            {"sugestoes_cliente_id": self.cliente.id},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        sugestoes = resposta.json()["sugestoes"]
        self.assertEqual(len(sugestoes), 1)
        self.assertEqual(sugestoes[0]["produto"], self.produto.nome)
        self.assertEqual(sugestoes[0]["quantidade"], "2")
        self.assertEqual(sugestoes[0]["preco"], "R$ 90,00")
        self.assertEqual(sugestoes[0]["preco_valor"], "90.00")
        self.assertEqual(sugestoes[0]["produto_id"], self.produto.id)
        self.assertEqual(sugestoes[0]["unidade"], "Un")
        self.assertEqual(sugestoes[0]["frequencia"], 2)

    def test_detalhe_de_pedido_carrega(self):
        """Detalhe do pedido deve carregar corretamente"""
        from .models import Pedido, ItemPedido

        pedido = Pedido.objects.create(
            cliente=self.cliente,
            data_pedido=timezone.now().date(),
            total=100.00,
        )

        ItemPedido.objects.create(
            pedido=pedido,
            produto=self.produto,
            quantidade=1,
            unidade="Un",
            preco_unitario=100.00,
            valor_total=100.00,
            estoque_no_momento=50,
        )
        produto_decimal = Produto.objects.create(
            nome="Produto Quantidade Decimal Pedido",
            preco_compra=Decimal("5.00"),
            preco_venda=Decimal("12.00"),
            preco_vista=Decimal("12.00"),
            preco_prazo=Decimal("12.00"),
            quantidade=5,
        )
        ItemPedido.objects.create(
            pedido=pedido,
            produto=produto_decimal,
            quantidade=Decimal("2.500"),
            unidade="Un",
            preco_unitario=Decimal("12.00"),
            valor_total=Decimal("30.00"),
            estoque_no_momento=5,
        )

        url = reverse("estoque:pedido_detalhe", args=[pedido.id])
        resposta = self.client.get(url, secure=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("pedido", resposta.context)
        self.assertContains(resposta, 'data-label="Quantidade">1</td>')
        self.assertContains(resposta, 'data-label="Quantidade">2,5</td>')

    def test_pedido_com_produto_sem_estoque_pode_ser_gravado(self):
        """Pedido com produto sem estoque suficiente deve poder ser gravado (apenas aviso)"""
        from .models import Pedido, ItemPedido

        self.produto.quantidade = 0
        self.produto.save()

        pedido = Pedido.objects.create(
            cliente=self.cliente,
            data_pedido=timezone.now().date(),
            total=100.00,
        )

        item = ItemPedido.objects.create(
            pedido=pedido,
            produto=self.produto,
            quantidade=10,
            unidade="Un",
            preco_unitario=100.00,
            valor_total=1000.00,
            estoque_no_momento=0,
        )

        self.assertEqual(item.quantidade, 10)
        self.assertEqual(item.estoque_no_momento, 0)

    def test_analisar_comprovante_pix_picpay_data_mes_abreviado_e_instituicao(self):
        datas_ocr = [
            "21/set/2025 - 13:20:41",
            "21 / set / 2025 - 13:20:41",
            "21/set/2025 13:20",
            "21/set/2025 - 13:20",
            "21/set/2025 - 13.20.41",
            "21/set/2025 13h20",
            "21/set/2025 às 13h20",
            "21/set/2025 as 13h20",
            "21/set./2025 - 13:20:41",
            "21 / set. / 2025 - 13:20:41",
            "21/5et/2O25 - l3:2O:41",
            "21/sct/2025 - 13.2O.41",
        ]
        url = reverse("estoque:central_pix_analisar_comprovante")

        for indice, data_ocr in enumerate(datas_ocr):
            with self.subTest(data_ocr=data_ocr):
                conteudo = (
                    "PicPay\n"
                    "Comprovante de Pix\n"
                    f"{data_ocr}\n"
                    "Valor\n"
                    "R$ 50,00\n"
                    "De\n"
                    "ISA ALVES DE SOUZA\n"
                    "PIC PAY\n"
                ).encode("utf-8")
                arquivo = SimpleUploadedFile(f"picpay-{indice}.txt", conteudo, content_type="text/plain")

                resposta = self.client.post(url, {"comprovante": arquivo}, secure=True)

                self.assertEqual(resposta.status_code, 200)
                dados = resposta.json()
                self.assertTrue(dados["ok"])
                self.assertEqual(dados["valor"], "50.00")
                self.assertEqual(dados["data_pagamento"], "2025-09-21T13:20")
                self.assertEqual(dados["pagador"], "ISA ALVES DE SOUZA")
                self.assertEqual(dados["instituicao_pix"], "PicPay")
                self.assertIn("Data enviada ao frontend: 2025-09-21T13:20", dados["debug_data_pagamento"])

    def test_analisar_comprovante_pix_lida_com_datas_e_valores_comuns_sem_fallback_atual(self):
        casos = [
            (
                "mercado_maio_as.txt",
                "Mercado Pago\nComprovante de Pix\n2/maio/2026 as 8h42\nValor\nR$ 50,00\nPagador: Maria Silva\n",
                "50.00",
                "2026-05-02T08:42",
                "Mercado Pago",
            ),
            (
                "mercado_maio_acento.txt",
                "Mercado Pago\nComprovante de Pix\n2/maio/2026 às 8h42\nR$ 100\nPagador: Jose Silva\n",
                "100.00",
                "2026-05-02T08:42",
                "Mercado Pago",
            ),
            (
                "nubank_mai.txt",
                "Nu Pagamentos\nComprovante Pix\n05 MAI 2026 - 17:36\nValor R$ 50,00\nOrigem\nNome: Ana Souza\n",
                "50.00",
                "2026-05-05T17:36",
                "Nubank",
            ),
        ]
        url = reverse("estoque:central_pix_analisar_comprovante")

        for nome, conteudo, valor_esperado, data_esperada, instituicao_esperada in casos:
            with self.subTest(nome=nome):
                arquivo = SimpleUploadedFile(nome, conteudo.encode("utf-8"), content_type="text/plain")
                resposta = self.client.post(url, {"comprovante": arquivo}, secure=True)
                self.assertEqual(resposta.status_code, 200)
                dados = resposta.json()
                self.assertTrue(dados["ok"])
                self.assertEqual(dados["nome_arquivo"], nome)
                self.assertEqual(dados["valor"], valor_esperado)
                self.assertEqual(dados["data_pagamento"], data_esperada)
                self.assertEqual(dados["instituicao_pix"], instituicao_esperada)

    def test_analisar_comprovante_pix_banco_inter_prioriza_pagador(self):
        conteudo = (
            "Banco Inter\n"
            "Comprovante Pix\n"
            "Dados do recebedor\n"
            "Nome: Lincoln Albuquerque Neiva\n"
            "Dados do pagador\n"
            "Nome: Ronise Ferreira\n"
            "CPF: ***.000.000-**\n"
            "Valor R$ 75,00\n"
            "Data 16/05/2026 17:30\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Ronise Ferreira")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")
        self.assertEqual(dados["valor"], "75.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T17:30")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_extrair_valor_mercado_pago_sem_separador_decimal_11150_vira_111_50(self):
        """Problema: OCR Mercado Pago retorna 'R$ 11150' em vez de 'R$ 111,50'.
        Esperado: valor deve ser interpretado como 111,50 (últimos 2 dígitos são centavos).
        """
        conteudo = (
            "Comprovante de Pix\n"
            "23/maio/2026 as 18:55:32\n"
            "R$ 11150\n"
            "De\n"
            "Joao de Almeida E Silva\n"
            "Para\n"
            "Lincoln Albuquerque Neiva\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("mercado-pago-sem-separador.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "111.50", f"Erro: OCR retornou '{dados['valor']}' em vez de '111.50'")
        self.assertEqual(dados["data_pagamento"], "2026-05-23T18:55")

    def test_extrair_valor_com_virgula_345_00_continua_funcionando(self):
        """Garantir que valores com vírgula como 'R$ 345,00' continuam sendo extraídos corretamente."""
        conteudo = (
            "Comprovante de Pix\n"
            "Valor R$ 345,00\n"
            "Data 10/06/2026 14:30\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante-virgula.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "345.00")

    def test_extrair_valor_com_ponto_e_virgula_1449_08_continua_funcionando(self):
        """Garantir que valores com ponto e virgula como 'R$ 1.449,08' continuam sendo extraídos corretamente."""
        conteudo = (
            "Comprovante de Pix\n"
            "Valor R$ 1.449,08\n"
            "Data 10/06/2026 14:30\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante-ponto-virgula.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "1449.08")

    def test_analisar_comprovante_pix_banco_inter_nao_usa_recebedor_como_pagador(self):
        conteudo = (
            "Banco Inter\n"
            "Comprovante Pix\n"
            "Dados do recebedor\n"
            "Nome: Lincoln Albuquerque Neiva\n"
            "Valor R$ 75,00\n"
            "Data 16/05/2026 17:30\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "")
        self.assertEqual(dados["valor"], "75.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T17:30")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_normaliza_horario_com_o_no_lugar_de_zero(self):
        conteudo = (
            "Domingo, 17/05/2026\n"
            "O8h09\n"
            "Valor R$ 5,00\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-17T08:09")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_inter_whatsapp_ocr_real_extrai_pagador(self):
        conteudo = (
            "Sobre a transagao\n"
            "Data da transacao\n"
            "Horario\n"
            "\n"
            "Identificador\n"
            "\n"
            "ID da transacao\n"
            "\n"
            "sinter\n"
            "\n"
            "Pix enviado\n"
            "R$ 5,00\n"
            "\n"
            "Domingo, 17/05/2026\n"
            "\n"
            "O8h09\n"
            "\n"
            "\u00a300416968202605171109huk671IHk1t\n"
            "\n"
            "Quem recebeu\n"
            "\n"
            "Nome\n"
            "\n"
            "CPF/CNPJ\n"
            "\n"
            "Instituicao\n"
            "\n"
            "Chave Pix\n"
            "\n"
            "Quem pagou\n"
            "Nome\n"
            "CPF/CNPJ\n"
            "\n"
            "Instituicao\n"
            "\n"
            "Lincoin Albuquerque Neiva\n"
            "\n"
            "*#*,319.532-\u00ab*\n"
            "\n"
            "NU PAGAMENTOS - IP\n"
            "\n"
            "+5591984111011\n"
            "\n"
            "FRANCISCO NETO DA SILVA MIRANDA\n"
            "\n"
            "***,467.952-**\n"
            "\n"
            "BANCO INTER\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "FRANCISCO NETO DA SILVA MIRANDA")
        self.assertNotEqual(dados["pagador"], "Lincoin Albuquerque Neiva")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")
        self.assertNotEqual(dados["pagador"], "NU PAGAMENTOS - IP")
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-17T08:09")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_banpara_prioriza_instituicao_do_cabecalho(self):
        conteudo = (
            "BANCO DO ESTADO DO PAR\u00c1 S.A. - BANPAR\u00c1\n"
            "COMPROVANTE DE PIX\n"
            "Data da Opera\u00e7\u00e3o: 03/02/2026 17:59:09\n"
            "Dados de Origem\n"
            "Titular: RUBEM ARRUDA DE SOUZA\n"
            "Dados do Recebedor\n"
            "Institui\u00e7\u00e3o: NU PAGAMENTOS - IP\n"
            "Valor: 1.449,08\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante-banpara.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["instituicao_pix"], "Banpar\u00e1")
        self.assertEqual(dados["pagador"], "RUBEM ARRUDA DE SOUZA")
        self.assertEqual(dados["valor"], "1449.08")
        self.assertEqual(dados["data_pagamento"], "2026-02-03T17:59")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_caixa_tem_usa_quem_vai_enviar(self):
        cliente = Cliente.objects.create(nome="ELIANA NAZARE DA SILVA FERREIRA", ativo=True)
        conteudo = (
            "CAIXA Tem\n"
            "Pix Pagamento\n"
            "17 de maio de 2026 \u00e0s 08:19:21\n"
            "\n"
            "Valor\n"
            "R$ 826,62\n"
            "\n"
            "Quem vai receber\n"
            "Nome\n"
            "Lincoln Albuquerque\n"
            "Neiva\n"
            "CPF/CNPJ\n"
            "***.319.532-**\n"
            "Banco\n"
            "NU PAGAMENTOS S.A.\n"
            "\n"
            "Quem vai enviar\n"
            "Nome\n"
            "ELIANA NAZARE DA\n"
            "SILVA FERREIRA\n"
            "CPF/CNPJ\n"
            "***.020.762-**\n"
            "Banco\n"
            "Caixa Econ\u00f4mica Federal\n"
            "\n"
            "Dados da transa\u00e7\u00e3o\n"
            "ID\n"
            "E003603052026051711183ffbb3dd95\n"
            "NSU\n"
            "64892124094\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante-caixa-tem.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["valor"], "826.62")
        self.assertEqual(dados["pagador"], "ELIANA NAZARE DA SILVA FERREIRA")
        self.assertEqual(dados["instituicao_pix"], "Caixa Econ\u00f4mica Federal")
        self.assertNotEqual(dados["instituicao_pix"], "Nubank")
        self.assertEqual(dados["data_pagamento"], "2026-05-17T08:19")
        self.assertEqual(dados["cliente_sugerido_id"], cliente.id)
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_banpara_usa_titular_da_origem(self):
        conteudo = (
            "BANCO DO ESTADO DO PARA S.A. - BANPARA\n"
            "COMPROVANTE DE PIX\n"
            "\n"
            "Data da Operacao: 17/05/2026 01:54:04\n"
            "Codigo da Sessao: 123456\n"
            "\n"
            "Dados de Origem\n"
            "Titular: LINCOLN ALBUQUERQUE NEIVA\n"
            "Agencia: 0057\n"
            "Conta: 000341187-7\n"
            "Tipo de Conta: CC\n"
            "\n"
            "Dados do Recebedor\n"
            "Instituicao: NU PAGAMENTOS - IP\n"
            "Titular: RECEBEDOR ERRADO\n"
            "CPF: ***.319.532-**\n"
            "Agencia: 0001\n"
            "Conta: 123\n"
            "Tipo de Conta: Conta de Pagamento\n"
            "\n"
            "ID Transacao: E123\n"
            "Tipo de Pagamento: Chave\n"
            "Finalidade: Compra/Transferencia\n"
            "Valor: 4,40\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "LINCOLN ALBUQUERQUE NEIVA")
        self.assertNotEqual(dados["pagador"], "RECEBEDOR ERRADO")
        self.assertEqual(dados["valor"], "4.40")
        self.assertEqual(dados["data_pagamento"], "2026-05-17T01:54")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_banpara_prioriza_origem_e_data_operacao(self):
        conteudo = (
            "BANCO DO ESTADO DO PARA S.A. - BANPARA\n"
            "COMPROVANTE DE PIX\n"
            "\n"
            "Data da Operacao:                  05/05/2026 16:45:50\n"
            "Codigo da Sessao:                  APP00570000023744PP639135962448580000\n"
            "\n"
            "Dados de Origem\n"
            "Titular:                           RUBEM ARRUDA DE SOUZA\n"
            "Agencia:                           0057\n"
            "Conta:                             000002374-4\n"
            "Tipo de Conta:                     PP\n"
            "\n"
            "Dados do Recebedor\n"
            "Instituicao:                       NU PAGAMENTOS - IP\n"
            "Titular:                           Lincoln Albuquerque Neiva\n"
            "CPF:                               ***.319.532-**\n"
            "Agencia:                           0001\n"
            "Conta:                             088228354-6\n"
            "Tipo de Conta:                     Conta de Pagamento\n"
            "\n"
            "ID Transacao:                      E04913711202605051945RF704X8VJHS\n"
            "Tipo de Pagamento:                 Chave\n"
            "Finalidade:                        Compra/Transferencia\n"
            "Valor:                             847,70\n"
            "Autenticacao:                      209002000000000002633000000.16\n"
            "                                   00530000087366805058084946550.44\n"
            "17:36\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "RUBEM ARRUDA DE SOUZA")
        self.assertNotEqual(dados["pagador"], "5")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")
        self.assertEqual(dados["valor"], "847.70")
        self.assertEqual(dados["data_pagamento"], "2026-05-05T16:45")
        self.assertNotEqual(dados["data_pagamento"], "2026-05-05T17:36")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_banpara_data_operacao_quebrada(self):
        conteudo = (
            "BANPARA\n"
            "COMPROVANTE DE PIX\n"
            "Data da Operacao:\n"
            "05/05/2026\n"
            "16:45:50\n"
            "Codigo da Sessao: APP00570000023744PP639135962448580000\n"
            "Dados de Origem\n"
            "Titular:\n"
            "RUBEM ARRUDA DE SOUZA\n"
            "Agencia: 0057\n"
            "Conta: 000002374-4\n"
            "Tipo de Conta: PP\n"
            "Dados do Recebedor\n"
            "Titular: Lincoln Albuquerque Neiva\n"
            "Valor: 847,70\n"
            "17:36\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertEqual(dados["pagador"], "RUBEM ARRUDA DE SOUZA")
        self.assertEqual(dados["valor"], "847.70")
        self.assertEqual(dados["data_pagamento"], "2026-05-05T16:45")
        self.assertNotEqual(dados["pagador"], "5")
        self.assertNotEqual(dados["data_pagamento"], "2026-05-05T17:36")

    def test_analisar_comprovante_pix_banco_inter_caso_real_recebido(self):
        conteudo = (
            "Inter\n"
            "Comprovante de Pix recebido\n"
            "Valor R$ 5,00\n"
            "16/05/2026 23:43\n"
            "Beneficiario\n"
            "Lincoln Albuquerque Neiva\n"
            "Chave Pix do recebedor\n"
            "Instituicao do recebedor\n"
            "Banco Inter\n"
            "De\n"
            "Ronise Ferreira\n"
            "CPF: ***.000.000-**\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Ronise Ferreira")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T23:43")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_banco_inter_bloqueia_nome_recebedor_conhecido(self):
        conteudo = (
            "Banco Inter\n"
            "Comprovante Pix\n"
            "Valor R$ 5,00\n"
            "16/05/2026 23:43\n"
            "De\n"
            "Lincoln Albuquerque\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "")
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T23:43")

    def test_analisar_comprovante_pix_banco_inter_reconstroi_destino_nome_quebrado(self):
        conteudo = (
            "Comprovante de transferencia\n"
            "16 MAI 2026 - 23:43:13\n"
            "Valor R$ 5,00\n"
            "Tipo de transferencia Pix\n"
            "Destino\n"
            "Ronise do Socorro dos\n"
            "Nome\n"
            "Santos Ferreira\n"
            "CPF ***.000.000-**\n"
            "Instituicao BANCO INTER\n"
            "Origem\n"
            "Lincoln Albuquerque\n"
            "Nome\n"
            "Neiva\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "Ronise do Socorro dos Santos Ferreira")
        self.assertNotEqual(dados["pagador"], "Neiva")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque")
        self.assertNotEqual(dados["pagador"], "Lincoln Albuquerque Neiva")
        self.assertEqual(dados["valor"], "5.00")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T23:43")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_sugere_cliente_com_jr_e_nome_intermediario(self):
        cliente = Cliente.objects.create(nome="Ivanildo Patricio Jr", ativo=True)
        conteudo = (
            "Comprovante de Pix\n"
            "16/maio/2026 \u00e0s 16:33:29\n"
            "R$ 600\n"
            "@ De\n"
            "Ivanildo Ferraz Patr\u00edcio Junior\n"
            "CPF: ***.188.882-**\n"
            "Mercado Pago\n"
            "@ Para\n"
            "Lincoln Albuquerque Neiva\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertEqual(dados["pagador"], "Ivanildo Ferraz Patr\u00edcio Junior")
        self.assertEqual(dados["cliente_sugerido_id"], cliente.id)
        self.assertEqual(dados["cliente_sugerido_nome"], "Ivanildo Patricio Jr")
        self.assertEqual(dados["confianca_cliente"], "alta")
        self.assertEqual(PixRecebido.objects.count(), 0)

    def test_analisar_comprovante_pix_nome_generico_nao_sugere_cliente(self):
        Cliente.objects.create(nome="Maria Silva", ativo=True)
        conteudo = (
            "Comprovante Pix\n"
            "Origem\n"
            "Nome: Maria\n"
            "Valor R$ 20,00\n"
            "Data 16/05/2026 17:30\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertEqual(dados["pagador"], "Maria")
        self.assertIsNone(dados["cliente_sugerido_id"])
        self.assertEqual(dados["confianca_cliente"], "baixa")

    def test_analisar_comprovante_pix_nao_sugere_cliente_quando_ambiguo(self):
        Cliente.objects.create(nome="Joelson Ferreira dos Santos", ativo=True)
        Cliente.objects.create(nome="Joelson Ferreira dos Santos", ativo=True)
        conteudo = (
            "Comprovante Pix\n"
            "Origem\n"
            "Nome: Joelson Ferreira dos Santos\n"
            "Valor R$ 156,50\n"
            "16 MAI 2026 - 17:51:46\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertIsNone(dados["cliente_sugerido_id"])
        self.assertEqual(dados["confianca_cliente"], "ambigua")
        self.assertIn("mais de um cliente parecido", dados["mensagem_cliente"])

    def test_analisar_comprovante_pix_nao_usa_instituicao_como_pagador(self):
        conteudo = (
            "Comprovante Pix\n"
            "Origem\n"
            "Nome\n"
            "Instituição\n"
            "NU PAGAMENTOS - IP\n"
            "Valor R$ 156,50\n"
            "16/05/2026 17:51:46\n"
        ).encode("utf-8")
        arquivo = SimpleUploadedFile("comprovante.txt", conteudo, content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados["ok"])
        self.assertEqual(dados["pagador"], "")
        self.assertEqual(dados["valor"], "156.50")
        self.assertEqual(dados["data_pagamento"], "2026-05-16T17:51")

    def test_analisar_comprovante_pix_falha_sem_bloquear_preenchimento_manual(self):
        arquivo = SimpleUploadedFile("comprovante.txt", b"texto sem dados de pix", content_type="text/plain")

        resposta = self.client.post(
            reverse("estoque:central_pix_analisar_comprovante"),
            {"comprovante": arquivo},
            secure=True,
        )

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertFalse(dados["ok"])
        self.assertIn("Preencha manualmente", dados["mensagem"])
        self.assertIsNone(dados["cliente_sugerido_id"])

    def test_central_pix_bloqueia_duplicado_pendente_evidente(self):
        cliente = Cliente.objects.create(nome="Joelson Ferreira dos Santos", ativo=True)
        data_pagamento = timezone.make_aware(timezone.datetime(2026, 5, 16, 17, 51))
        pix_parecido = PixRecebido.objects.create(
            nome_pagador="Joelson Ferreira dos Santos",
            valor="156.50",
            data_pagamento=data_pagamento,
            status=PixRecebido.STATUS_PENDENTE,
        )

        resposta = self.client.post(reverse("estoque:central_pix"), data={
            "cliente": cliente.id,
            "nome_pagador": "  joelson ferreira dos santos ",
            "valor": "156.50",
            "data_pagamento": "2026-05-16T17:52",
            "observacao": "Tentativa duplicada",
            "status": PixRecebido.STATUS_PENDENTE,
        }, secure=True, follow=True)

        self.assertEqual(resposta.status_code, 200)
        novo_pix = PixRecebido.objects.order_by("-id").first()
        self.assertEqual(PixRecebido.objects.count(), 2)
        self.assertEqual(novo_pix.status, PixRecebido.STATUS_POSSIVEL_DUPLICADO)
        self.assertEqual(novo_pix.pix_original, pix_parecido)
        detalhe_url = reverse("estoque:central_pix_detalhe", kwargs={"pix_id": novo_pix.id})
        self.assertTrue(any(url.startswith(detalhe_url) for url, _status in resposta.redirect_chain))
        mensagem = f"Pix salvo como possivel duplicado. Confira a comparacao com o Pix #{pix_parecido.id}."
        self.assertContains(resposta, mensagem, count=1)
        self.assertContains(resposta, f"Pix parecido #{pix_parecido.id}")
