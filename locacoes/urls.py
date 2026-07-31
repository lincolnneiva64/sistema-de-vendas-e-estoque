from django.urls import path

from . import views


app_name = "locacoes"

urlpatterns = [
    path("", views.lista, name="lista"),
    path("nova/", views.nova, name="nova"),
    path("configuracoes/", views.configuracoes, name="configuracoes"),
    path("disponibilidade/", views.disponibilidade_dinamica, name="disponibilidade_dinamica"),
    path("resumo-valores/", views.resumo_valores_dinamico, name="resumo_valores_dinamico"),
    path("checklist-operacional/", views.checklist_operacional, name="checklist_operacional"),
    path(
        "tarefas-operacionais/<int:pk>/conferencia-entrega/",
        views.conferencia_entrega,
        name="conferencia_entrega",
    ),
    path(
        "tarefas-operacionais/<int:pk>/conferencia-recolhimento/",
        views.conferencia_recolhimento,
        name="conferencia_recolhimento",
    ),
    path("tarefas-operacionais/<int:pk>/confirmar/", views.confirmar_tarefa_operacional, name="confirmar_tarefa_operacional"),
    path("tarefas-operacionais/<int:pk>/nao-possivel/", views.tarefa_operacional_nao_possivel, name="tarefa_operacional_nao_possivel"),
    path("<int:pk>/", views.detalhe, name="detalhe"),
    path("<int:pk>/tarefa-operacional/<str:tipo>/", views.abrir_tarefa_operacional_locacao, name="abrir_tarefa_operacional"),
    path("<int:pk>/cancelar/", views.cancelar, name="cancelar"),
    path("<int:pk>/saiu-para-entrega/", views.marcar_saiu_para_entrega, name="marcar_saiu_para_entrega"),
    path("<int:pk>/confirmar-entrega/", views.confirmar_entrega, name="confirmar_entrega"),
    path("<int:pk>/devolucao/", views.registrar_devolucao, name="registrar_devolucao"),
    path("<int:pk>/pagamento/", views.registrar_pagamento, name="registrar_pagamento"),
    path("<int:pk>/vencimento-saldo/", views.alterar_vencimento_saldo, name="alterar_vencimento_saldo"),
    path("<int:pk>/termo/", views.termo, name="termo"),
    path("recibos-pendentes/", views.recibos_pendentes, name="recibos_pendentes"),
    path("pagamentos/<int:pk>/recibo/", views.recibo_pagamento, name="recibo_pagamento"),
    path("pagamentos/<int:pk>/recibo/confirmar/", views.confirmar_recibo_enviado, name="confirmar_recibo_enviado"),
    path("pagamentos/<int:pk>/recibo/dispensar/", views.dispensar_recibo, name="dispensar_recibo"),
]
