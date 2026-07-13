from django.db import migrations


def normalizar_numero(valor):
    return "".join(caractere for caractere in str(valor or "") if caractere.isdigit())


def migrar_telefones_contatos(apps, schema_editor):
    FornecedorContato = apps.get_model("estoque", "FornecedorContato")
    FornecedorContatoTelefone = apps.get_model("estoque", "FornecedorContatoTelefone")

    for contato in FornecedorContato.objects.all().iterator():
        numero = normalizar_numero(contato.telefone_whatsapp_normalizado) or normalizar_numero(contato.telefone_whatsapp)
        if not numero:
            continue

        FornecedorContatoTelefone.objects.get_or_create(
            contato_id=contato.pk,
            numero=numero,
            defaults={
                "tipo": "celular",
                "whatsapp": True,
                "principal": True,
                "ativo": True,
                "ordem": 1,
            },
        )


def desfazer_migracao_telefones_contatos(apps, schema_editor):
    FornecedorContatoTelefone = apps.get_model("estoque", "FornecedorContatoTelefone")
    FornecedorContatoTelefone.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("estoque", "0074_fornecedorcontatotelefone"),
    ]

    operations = [
        migrations.RunPython(migrar_telefones_contatos, desfazer_migracao_telefones_contatos),
    ]
