def dashboard_badges(request):
    """Compteurs affichés dans les badges de la barre latérale du dashboard,
    disponibles sur toutes les pages sans que chaque vue ait à les recalculer."""
    if not (request.user.is_authenticated and request.user.is_staff):
        return {}

    from momoapi.models import Avis, DemandeSuppressionCompte
    return {
        'avis_non_lus': Avis.objects.filter(lu=False).count(),
        'demandes_suppression_en_attente': DemandeSuppressionCompte.objects.filter(statut='en_attente').count(),
    }
