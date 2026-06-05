from django.core.management.base import BaseCommand, CommandError

from estoque.models import Cliente, ItemVenda, Venda
from estoque.views import _sugestoes_ultimas_compras_cliente


class Command(BaseCommand):
    help = "Conferir sugestoes de pedido geradas pelas ultimas compras ativas do cliente."

    def add_arguments(self, parser):
        parser.add_argument("cliente", help="Trecho do nome do cliente, por exemplo: Lincoln")

    def handle(self, *args, **options):
        termo_cliente = options["cliente"].strip()
        if not termo_cliente:
            raise CommandError("Informe um trecho do nome do cliente.")

        clientes = list(Cliente.objects.filter(nome__icontains=termo_cliente).order_by("nome", "id")[:10])
        if not clientes:
            raise CommandError(f"Nenhum cliente encontrado para: {termo_cliente}")
        if len(clientes) > 1:
            self.stdout.write("Mais de um cliente encontrado. Use um nome mais especifico:")
            for cliente in clientes:
                self.stdout.write(f"- ID {cliente.id}: {cliente.nome}")
            return

        cliente = clientes[0]
        vendas = list(
            Venda.objects.filter(cliente=cliente, cancelada=False)
            .order_by("-data_venda", "-id")[:6]
        )
        vendas_ids = [venda.id for venda in vendas]
        itens = list(
            ItemVenda.objects.filter(venda_id__in=vendas_ids)
            .select_related("venda", "produto")
            .order_by("-venda__data_venda", "-venda_id", "id")
        )
        sugestoes = _sugestoes_ultimas_compras_cliente(cliente.id)
        produtos_unicos = {
            item.produto_id
            for item in itens
            if item.produto_id
        }

        self.stdout.write("")
        self.stdout.write("CLIENTE ENCONTRADO")
        self.stdout.write(f"ID: {cliente.id}")
        self.stdout.write(f"Nome: {cliente.nome}")

        self.stdout.write("")
        self.stdout.write("ULTIMAS 6 VENDAS ATIVAS CONSIDERADAS")
        if not vendas:
            self.stdout.write("Nenhuma venda ativa encontrada.")
        for venda in vendas:
            self.stdout.write(
                f"Venda #{venda.id} | Data: {venda.data_venda:%d/%m/%Y} | "
                f"Total: R$ {venda.total:.2f} | Cancelada: {venda.cancelada}"
            )

        self.stdout.write("")
        self.stdout.write("ITENS DESSAS VENDAS")
        if not itens:
            self.stdout.write("Nenhum item encontrado nas vendas consideradas.")
        for item in itens:
            produto_nome = item.produto.nome if item.produto else "Produto nao identificado"
            self.stdout.write(
                f"Venda #{item.venda_id} | Data: {item.venda.data_venda:%d/%m/%Y} | "
                f"Produto: {produto_nome} | Qtd: {item.quantidade} | "
                f"Preco unit.: R$ {item.preco_unitario:.2f} | Total item: R$ {item.valor_total:.2f}"
            )

        self.stdout.write("")
        self.stdout.write("SUGESTOES FINAIS GERADAS")
        if not sugestoes:
            self.stdout.write("Nenhuma sugestao encontrada para este cliente ainda.")
        for indice, sugestao in enumerate(sugestoes, start=1):
            self.stdout.write(
                f"{indice}. {sugestao['produto']} | Ultima qtd: {sugestao['quantidade']} | "
                f"Ultimo preco: {sugestao['preco']} | Ultima compra: {sugestao['data']} | "
                f"Vezes nas ultimas vendas: {sugestao['frequencia']} | Estoque ref.: {sugestao['estoque']}"
            )

        self.stdout.write("")
        self.stdout.write("RESUMO")
        self.stdout.write(f"Total de vendas consideradas: {len(vendas)}")
        self.stdout.write(f"Total de produtos unicos encontrados: {len(produtos_unicos)}")
        self.stdout.write(f"Total de sugestoes geradas: {len(sugestoes)}")
        if sugestoes:
            topo = ", ".join(
                f"{sugestao['produto']} ({sugestao['frequencia']}x)"
                for sugestao in sugestoes[:5]
            )
            self.stdout.write(f"Produtos mais frequentes no topo: {topo}")
