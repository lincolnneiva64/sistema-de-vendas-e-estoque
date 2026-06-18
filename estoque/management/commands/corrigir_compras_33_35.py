from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from estoque.models import Compra, ContaFinanceira, MovimentoFinanceiro, Produto


CORRECAO_33 = "CORRECAO COMPRA #33"
ESTORNO_35 = "ESTORNO COMPRA TESTE #35"
ESTORNO_ESTOQUE_35 = "ESTORNO ESTOQUE COMPRA TESTE #35"


def dinheiro(valor):
    return (valor or Decimal("0.00")).quantize(Decimal("0.01"))


def quantidade(valor):
    return (valor or Decimal("0.000")).quantize(Decimal("0.001"))


def moeda(valor):
    return f"R$ {dinheiro(valor):.2f}".replace(".", ",")


def qtd_texto(valor):
    return f"{quantidade(valor):.3f}".replace(".", ",")


def conta_por_alias(aliases, tipo):
    return ContaFinanceira.objects.filter(
        ativo=True,
        tipo=tipo,
        nome__in=aliases,
    ).order_by("id").first()


def contas_padrao():
    contas = {
        "caixa": conta_por_alias(["Caixa em especie", "Caixa em espécie"], ContaFinanceira.TIPO_CAIXA),
        "reserva": conta_por_alias(
            [
                "Sangria / Reserva em maos",
                "Sangria / Reserva em mãos",
                "Reserva em maos",
                "Reserva em mãos",
            ],
            ContaFinanceira.TIPO_CAIXA,
        ),
        "banco": conta_por_alias(["Banco/Pix"], ContaFinanceira.TIPO_BANCO),
    }
    faltando = [nome for nome, conta in contas.items() if not conta]
    if faltando:
        raise CommandError(f"Conta(s) financeira(s) nao encontrada(s): {', '.join(faltando)}.")
    return contas


def movimentos_da_compra(compra):
    consulta = MovimentoFinanceiro.objects.select_related("conta", "conta_destino").filter(
        Q(compra=compra) |
        Q(descricao__icontains=f"Compra #{compra.id}") |
        Q(descricao__icontains=f"Compra {compra.id}")
    )
    return list(consulta.order_by("id"))


def efeito_movimento(movimento):
    valor = dinheiro(movimento.valor)
    if movimento.tipo == MovimentoFinanceiro.TIPO_SAIDA:
        return -valor
    if movimento.tipo in {MovimentoFinanceiro.TIPO_ENTRADA, MovimentoFinanceiro.TIPO_AJUSTE}:
        return valor
    if movimento.tipo == MovimentoFinanceiro.TIPO_TRANSFERENCIA:
        return -valor
    return Decimal("0.00")


def tipo_para_diferenca(diferenca):
    if diferenca > Decimal("0.00"):
        return MovimentoFinanceiro.TIPO_ENTRADA, diferenca
    return MovimentoFinanceiro.TIPO_SAIDA, abs(diferenca)


def adicionar_observacao(compra, texto):
    agora = timezone.localtime().strftime("%d/%m/%Y %H:%M")
    observacao_atual = (compra.observacao or "").strip()
    nova_linha = f"[{agora}] {texto}"
    compra.observacao = f"{observacao_atual}\n{nova_linha}".strip() if observacao_atual else nova_linha


class Command(BaseCommand):
    help = "Corrige de forma rastreavel os efeitos financeiros/estoque das compras #33 e #35."

    def add_arguments(self, parser):
        parser.add_argument(
            "--aplicar",
            action="store_true",
            help="Cria os movimentos e aplica estorno de estoque. Sem esta flag, apenas simula.",
        )

    def handle(self, *args, **options):
        aplicar = options["aplicar"]
        contas = contas_padrao()
        compra33 = Compra.objects.filter(pk=33).select_related("fornecedor").first()
        compra35 = Compra.objects.filter(pk=35).select_related("fornecedor").first()

        if not compra33:
            raise CommandError("Compra #33 nao encontrada.")
        if not compra35:
            raise CommandError("Compra #35 nao encontrada.")

        plano33 = self.plano_compra_33(compra33, contas)
        plano35_financeiro = self.plano_financeiro_compra_35(compra35)
        plano35_estoque = self.plano_estoque_compra_35(compra35)

        self.imprimir_plano(compra33, compra35, plano33, plano35_financeiro, plano35_estoque)

        if not aplicar:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("Simulacao concluida. Rode novamente com --aplicar para gravar."))
            return

        self.validar_estoque_seguro(plano35_estoque)

        with transaction.atomic():
            compra33 = Compra.objects.select_for_update().get(pk=33)
            compra35 = Compra.objects.select_for_update().get(pk=35)

            criados33 = self.aplicar_movimentos_compra_33(compra33, plano33)
            criados35 = self.aplicar_financeiro_compra_35(compra35, plano35_financeiro)
            estoque_aplicado = self.aplicar_estoque_compra_35(compra35, plano35_estoque)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Correcao aplicada."))
        self.stdout.write(f"Movimentos criados para Compra #33: {criados33}.")
        self.stdout.write(f"Movimentos criados para Compra #35: {criados35}.")
        self.stdout.write(f"Estornos de estoque aplicados na Compra #35: {estoque_aplicado}.")

    def plano_compra_33(self, compra, contas):
        if MovimentoFinanceiro.objects.filter(descricao__icontains=CORRECAO_33).exists():
            return {"ja_corrigida": True, "movimentos": [], "efeitos": {}}

        conta_para_chave = {conta.pk: chave for chave, conta in contas.items()}
        efeitos = {chave: Decimal("0.00") for chave in contas}
        for movimento in movimentos_da_compra(compra):
            chave = conta_para_chave.get(movimento.conta_id)
            if not chave:
                continue
            efeitos[chave] += efeito_movimento(movimento)

        alvo = {
            "caixa": Decimal("0.00"),
            "reserva": Decimal("-1700.00"),
            "banco": Decimal("-134.00"),
        }
        movimentos = []
        for chave in ["caixa", "reserva", "banco"]:
            diferenca = (alvo[chave] - efeitos[chave]).quantize(Decimal("0.01"))
            if diferenca == Decimal("0.00"):
                continue
            tipo, valor = tipo_para_diferenca(diferenca)
            movimentos.append({
                "conta": contas[chave],
                "tipo": tipo,
                "valor": valor,
                "descricao": f"{CORRECAO_33} - ajustar {contas[chave].nome}"[:255],
            })

        return {"ja_corrigida": False, "movimentos": movimentos, "efeitos": efeitos}

    def plano_financeiro_compra_35(self, compra):
        if MovimentoFinanceiro.objects.filter(descricao__icontains=ESTORNO_35).exists():
            return {"ja_estornada": True, "movimentos": [], "originais": []}

        originais = [
            movimento
            for movimento in movimentos_da_compra(compra)
            if ESTORNO_35 not in (movimento.descricao or "")
        ]
        movimentos = []
        for movimento in originais:
            valor = dinheiro(movimento.valor)
            if valor <= Decimal("0.00"):
                continue
            if movimento.tipo == MovimentoFinanceiro.TIPO_SAIDA:
                tipo_estorno = MovimentoFinanceiro.TIPO_ENTRADA
            elif movimento.tipo == MovimentoFinanceiro.TIPO_ENTRADA:
                tipo_estorno = MovimentoFinanceiro.TIPO_SAIDA
            else:
                continue
            movimentos.append({
                "conta": movimento.conta,
                "tipo": tipo_estorno,
                "valor": valor,
                "descricao": f"{ESTORNO_35} - estorno movimento #{movimento.id} - {movimento.conta.nome}"[:255],
            })

        return {"ja_estornada": False, "movimentos": movimentos, "originais": originais}

    def plano_estoque_compra_35(self, compra):
        ja_estornada = ESTORNO_ESTOQUE_35 in (compra.observacao or "")
        itens = []
        if ja_estornada or not compra.estoque_entrada_realizada:
            return {"ja_estornada": ja_estornada, "itens": itens}

        for item in compra.itens.select_related("produto").all():
            if not item.produto:
                itens.append({
                    "item": item,
                    "produto": None,
                    "quantidade": quantidade(item.quantidade),
                    "estoque_atual": Decimal("0.000"),
                    "seguro": False,
                    "motivo": "Produto nao encontrado no item.",
                })
                continue
            estoque_atual = quantidade(item.produto.quantidade)
            qtd = quantidade(item.quantidade)
            itens.append({
                "item": item,
                "produto": item.produto,
                "quantidade": qtd,
                "estoque_atual": estoque_atual,
                "seguro": estoque_atual >= qtd,
                "motivo": "" if estoque_atual >= qtd else "Estoque ficaria negativo.",
            })
        return {"ja_estornada": False, "itens": itens}

    def imprimir_plano(self, compra33, compra35, plano33, plano35_financeiro, plano35_estoque):
        self.stdout.write("")
        self.stdout.write("COMPRA #33")
        self.stdout.write(f"Total registrado: {moeda(compra33.total)}")
        if plano33["ja_corrigida"]:
            self.stdout.write(self.style.WARNING(f"Ja existe movimento com '{CORRECAO_33}'. Nada sera criado."))
        else:
            self.stdout.write("Efeito financeiro atual identificado:")
            for chave, valor in plano33["efeitos"].items():
                self.stdout.write(f"- {chave}: {moeda(valor)}")
            self.stdout.write("Movimentos de correcao planejados:")
            self.imprimir_movimentos(plano33["movimentos"])

        self.stdout.write("")
        self.stdout.write("COMPRA #35")
        self.stdout.write(f"Total registrado: {moeda(compra35.total)}")
        if plano35_financeiro["ja_estornada"]:
            self.stdout.write(self.style.WARNING(f"Ja existe movimento com '{ESTORNO_35}'. Estorno financeiro nao sera repetido."))
        else:
            self.stdout.write("Movimentos financeiros de estorno planejados:")
            self.imprimir_movimentos(plano35_financeiro["movimentos"])

        self.stdout.write("Estoque da Compra #35:")
        if plano35_estoque["ja_estornada"]:
            self.stdout.write(self.style.WARNING(f"Observacao ja contem '{ESTORNO_ESTOQUE_35}'. Estoque nao sera repetido."))
        elif not compra35.estoque_entrada_realizada:
            self.stdout.write("- Compra nao esta marcada com entrada de estoque realizada.")
        elif not plano35_estoque["itens"]:
            self.stdout.write("- Nenhum item com estoque para estornar.")
        else:
            for entrada in plano35_estoque["itens"]:
                produto = entrada["produto"]
                nome = produto.nome if produto else "Produto nao identificado"
                status = "OK" if entrada["seguro"] else f"BLOQUEADO: {entrada['motivo']}"
                self.stdout.write(
                    f"- {nome}: estornar {qtd_texto(entrada['quantidade'])}. "
                    f"Estoque atual: {qtd_texto(entrada['estoque_atual'])}. {status}"
                )

    def imprimir_movimentos(self, movimentos):
        if not movimentos:
            self.stdout.write("- Nenhum movimento necessario.")
            return
        for movimento in movimentos:
            tipo = "Entrada" if movimento["tipo"] == MovimentoFinanceiro.TIPO_ENTRADA else "Saida"
            self.stdout.write(
                f"- {tipo} {moeda(movimento['valor'])} | {movimento['conta'].nome} | {movimento['descricao']}"
            )

    def validar_estoque_seguro(self, plano35_estoque):
        bloqueados = [entrada for entrada in plano35_estoque["itens"] if not entrada["seguro"]]
        if bloqueados:
            nomes = [
                entrada["produto"].nome if entrada["produto"] else "Produto nao identificado"
                for entrada in bloqueados
            ]
            raise CommandError(
                "Estorno de estoque da Compra #35 bloqueado para evitar estoque negativo/sem produto: "
                + ", ".join(nomes)
            )

    def aplicar_movimentos_compra_33(self, compra, plano):
        if plano["ja_corrigida"] or MovimentoFinanceiro.objects.filter(descricao__icontains=CORRECAO_33).exists():
            return 0
        criados = 0
        for movimento in plano["movimentos"]:
            MovimentoFinanceiro.objects.create(
                conta=movimento["conta"],
                tipo=movimento["tipo"],
                valor=movimento["valor"],
                data=timezone.localdate(),
                descricao=movimento["descricao"],
                operador="correcao_sistema",
                origem="correcao_compra_33",
                compra=compra,
            )
            criados += 1
        if criados:
            adicionar_observacao(
                compra,
                f"{CORRECAO_33}: ajuste financeiro rastreavel para Caixa 0, Reserva {moeda(Decimal('1700.00'))}, Banco/Pix {moeda(Decimal('134.00'))}.",
            )
            compra.save(update_fields=["observacao", "atualizado_em"])
        return criados

    def aplicar_financeiro_compra_35(self, compra, plano):
        if plano["ja_estornada"] or MovimentoFinanceiro.objects.filter(descricao__icontains=ESTORNO_35).exists():
            return 0
        criados = 0
        for movimento in plano["movimentos"]:
            MovimentoFinanceiro.objects.create(
                conta=movimento["conta"],
                tipo=movimento["tipo"],
                valor=movimento["valor"],
                data=timezone.localdate(),
                descricao=movimento["descricao"],
                operador="correcao_sistema",
                origem="estorno_compra_teste_35",
                compra=compra,
            )
            criados += 1
        return criados

    def aplicar_estoque_compra_35(self, compra, plano):
        if plano["ja_estornada"] or ESTORNO_ESTOQUE_35 in (compra.observacao or ""):
            return 0

        aplicados = 0
        for entrada in plano["itens"]:
            produto = entrada["produto"]
            if not produto:
                continue
            produto = Produto.objects.select_for_update().get(pk=produto.pk)
            produto.quantidade = quantidade(produto.quantidade) - entrada["quantidade"]
            produto.save(update_fields=["quantidade", "atualizado_em"])
            aplicados += 1

        if aplicados or compra.estoque_entrada_realizada:
            adicionar_observacao(
                compra,
                f"{ESTORNO_ESTOQUE_35}: compra de teste estornada; estoque e financeiro revertidos por comando seguro.",
            )
            compra.status = Compra.STATUS_CANCELADA
            compra.cancelada = True
            compra.cancelada_em = timezone.now()
            compra.motivo_cancelamento = ESTORNO_ESTOQUE_35
            compra.estoque_entrada_realizada = False
            compra.save(update_fields=[
                "observacao",
                "status",
                "cancelada",
                "cancelada_em",
                "motivo_cancelamento",
                "estoque_entrada_realizada",
                "atualizado_em",
            ])
        return aplicados
