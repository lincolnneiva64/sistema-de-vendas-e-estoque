"""Copy real route data read-only, then audit Chrome on an isolated test database."""
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'sistema.settings')


def main():
    import django
    django.setup()
    from django.core import serializers
    from estoque.models import EntregaRota, EntregaChecklistItem
    from locacoes.models import TarefaOperacionalLocacao
    route = EntregaRota.objects.get(pk=134)
    pickup = TarefaOperacionalLocacao.objects.get(pk=108, tipo='recolhimento')
    if not pickup:
        raise RuntimeError('No active real pickup available for the audit.')
    records = {}

    def add(obj):
        key = (obj._meta.label, obj.pk)
        if key in records:
            return
        for field in obj._meta.fields:
            if field.many_to_one and getattr(obj, field.attname) is not None:
                add(getattr(obj, field.name))
        records[key] = obj

    add(route); add(pickup)
    for obj in route.itens.all():
        add(obj)
        for item in obj.venda.itens.all():
            add(item)
    for obj in EntregaChecklistItem.objects.filter(rota_item__rota=route):
        add(obj)
    with TemporaryDirectory(prefix='offline-real-fixture-') as folder:
        fixture = Path(folder) / 'real-routes.json'
        fixture.write_text(serializers.serialize('json', records.values()), encoding='utf-8')
        env = {**os.environ, 'OFFLINE_REAL_ROUTE_FIXTURE':str(fixture), 'OFFLINE_REAL_PICKUP':str(pickup.pk)}
        env.pop('DJANGO_SETTINGS_MODULE', None)
        result = subprocess.run([sys.executable, 'manage.py', 'test',
            'offline.tests_browser.OfflineBrowserTests.test_real_delivery_and_pickup_routes',
            '--settings=offline.test_settings', '--verbosity=1'], env=env)
        return result.returncode


if __name__ == '__main__':
    sys.exit(main())
