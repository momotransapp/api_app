from django.contrib import admin
from .models import Abonnement, Boutique, DemandeSuppressionCompte, Pack, Sim, Utilisateur

admin.site.register(Pack)
admin.site.register(Utilisateur)
admin.site.register(Boutique)
admin.site.register(Abonnement)
admin.site.register(DemandeSuppressionCompte)
admin.site.register(Sim)
