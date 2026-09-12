from django.contrib import admin
from .models import Pack, Utilisateur, Boutique, Abonnement, DemandeSuppressionCompte

admin.site.register(Pack)
admin.site.register(Utilisateur)
admin.site.register(Boutique)
admin.site.register(Abonnement)
admin.site.register(DemandeSuppressionCompte)
