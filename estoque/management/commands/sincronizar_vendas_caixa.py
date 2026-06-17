from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from estoque.models import MovimentoFinanceiro, Venda
from estoque.views import (
    _conta_financeira_venda_a_vista,
    _contexto_venda_quitada,
    _financeiro_moeda_br,
    _registrar_movimento_venda_a_vista,
    _venda_pagamento_imediato,
)


def _movimento_venda_existente(venda):
    prefixos = [
        f"Venda a vista #{venda.id}",
        f"Venda \u00e0 vista #{venda.id}",
    ]
    consulta = MovimentoFinanceiro.objects.filter(
        tipo=MovimentoFinanceiro.TIPO_ENTRADA,
        origem="venda",
    )
    for prefixo in prefixos:
        movimento = consulta.filter(descricao__startswith=prefixo).first()
        if movimento:
            return movimento
    return None


class Command(BaseCommand):
    help = "Sincroniza vendas antigas a vista/Pix/Banco quitadas com o Caixa/Banco."

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirmar",
            action="store_true",
            help="Cria os movimentos financeiros. Sem este parametro, apenas lista o que seria feito.",
        )
        parser.add_argument(
            "--venda-id",
            type=int,
            help="Sincroniza apenas uma venda especifica.",
        )

    def handle(self, *args, **options):
        confirmar = options["confirmar"]
        venda_id = options.get("venda_id")

        vendas = Venda.objects.select_related("cliente").filter(cancelada=False).order_by("id")
        if venda_id:
            vendas = vendas.filter(pk=venda_id)
            if not vendas.exists():
                raise CommandError(f"Venda #{venda_id} nao encontrada ou cancelada.")

        candidatos = []
        ignoradas = 0
        ja_sincronizadas = 0

        for venda in vendas:
            if not _venda_pagamento_imediato(venda.tipo_pagamento):
                ignoradas += 1
                continue

            contexto = _contexto_venda_quitada(venda)
            if not contexto.get("quitada"):
                ignoradas += 1
                continue

            movimento_existente = _movimento_venda_existente(venda)
            if movimento_existente:
                ja_sincronizadas += 1
                continue

            valor = (venda.total or Decimal("0.00")).quantize(Decimal("0.01"))
            if valor <= Decimal("0.00"):
                ignoradas += 1
                continue

            conta = _conta_financeira_venda_a_vista(venda.tipo_pagamento)
            if not conta:
                ignoradas += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"Venda #{venda.id}: conta financeira nao encontrada para {venda.tipo_pagamento!r}."
                    )
                )
                continue

            candidatos.append((venda, conta, valor))

        if not candidatos:
            self.stdout.write("Nenhuma venda pendente para sincronizar.")
            self.stdout.write(f"Ignoradas: {ignoradas}. Ja sincronizadas: {ja_sincronizadas}.")
            return

        self.stdout.write("")
        self.stdout.write("VENDAS PENDENTES DE SINCRONIZACAO")
        for venda, conta, valor in candidatos:
            cliente_nome = venda.cliente.nome if venda.cliente else "Cliente nao informado"
            self.stdout.write(
                f"- Venda #{venda.id} | Cliente: {cliente_nome} | "
                f"Pagamento: {venda.tipo_pagamento or '-'} | "
                f"Total: {_financeiro_moeda_br(valor)} | Conta destino: {conta.nome}"
            )

        if not confirmar:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    "Simulacao concluida. Rode novamente com --confirmar para criar os movimentos."
                )
            )
            self.stdout.write(
                f"Seriam criados: {len(candidatos)} movimento(s). "
                f"Ignoradas: {ignoradas}. Ja sincronizadas: {ja_sincronizadas}."
            )
            return

        criados = []
        with transaction.atomic():
            for venda, _conta, _valor in candidatos:
                movimento = _registrar_movimento_venda_a_vista(venda)
                if movimento and movimento.id:
                    criados.append(movimento)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Movimentos criados: {len(criados)}."))
        for movimento in criados:
            self.stdout.write(
                f"- Movimento #{movimento.id} | {movimento.conta.nome} | "
                f"{_financeiro_moeda_br(movimento.valor)} | {movimento.descricao}"
            )
        self.stdout.write(f"Ignoradas: {ignoradas}. Ja sincronizadas: {ja_sincronizadas}.")
