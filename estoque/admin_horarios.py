"""Presentation opt-in for selected admins; no form or global date changes."""
from django.contrib import admin
from django.utils.html import format_html
from estoque.templatetags.horarios_documentos import instante_registro


def horario_admin(valor, vazio):
    if valor is None or valor == "":
        return vazio
    return format_html(
        '<span class="admin-horario" style="display:inline-block;background:#f8fafc;color:#1e293b;'
        'padding:2px 4px;border-radius:3px;white-space:normal">{}</span>', instante_registro(valor))


def coluna_horario(campo, rotulo):
    @admin.display(description=rotulo, ordering=campo)
    def apresentar(self, obj):
        return horario_admin(getattr(obj, campo), self.get_empty_value_display())
    return apresentar


class HorariosAdminMixin:
    change_form_template = "admin/horarios/change_form.html"
    object_history_template = "admin/horarios/object_history.html"
    horarios_admin = {}
