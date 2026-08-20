from django import forms
from .models import Produto
from django.db.models import Q


class ProdutoRevisaoForm(forms.ModelForm):
    """Form para edição em lote de produtos na revisão de importação"""
    
    revisado = forms.BooleanField(
        required=False,
        label="Revisar agora",
        help_text="Marque para indicar que este produto foi revisado"
    )
    
    class Meta:
        model = Produto
        fields = ['nome', 'codigo', 'categoria', 'revisado']
        widgets = {
            'nome': forms.TextInput(attrs={
                'class': 'form-control form-control-sm',
                'placeholder': 'Nome do produto',
            }),
            'codigo': forms.TextInput(attrs={
                'class': 'form-control form-control-sm mono',
                'placeholder': 'Código',
            }),
            'categoria': forms.Select(attrs={
                'class': 'form-select form-select-sm',
            }),
        }
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Carregar categorias únicas existentes
        categorias_existentes = Produto.objects.filter(
            excluido=False
        ).values_list('categoria', flat=True).distinct().order_by('categoria')
        
        categoria_choices = [('', '--- Selecione uma categoria ---')]
        for cat in categorias_existentes:
            if cat:  # Ignore null
                categoria_choices.append((cat, cat))
        
        self.fields['categoria'].choices = categoria_choices


class ProdutoRevisaoFiltrosForm(forms.Form):
    """Form para filtros na tela de revisão"""
    
    FILTRO_CHOICES = [
        ('pendentes', 'Pendentes'),
        ('revisados', 'Revisados'),
        ('todos', 'Todos'),
    ]
    
    filtro = forms.ChoiceField(
        choices=FILTRO_CHOICES,
        initial='pendentes',
        required=False,
        widget=forms.RadioSelect(attrs={
            'class': 'form-check-input',
        })
    )
    
    busca = forms.CharField(
        max_length=200,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Buscar por nome...',
        })
    )
    
    page = forms.IntegerField(
        min_value=1,
        required=False,
        initial=1,
        widget=forms.HiddenInput()
    )
