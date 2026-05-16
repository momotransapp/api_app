from rest_framework.permissions import BasePermission
from .models import MembreBoutique, Abonnement, Pack
from datetime import date


def get_abonnement_actif(utilisateur):
    """Retourne l'abonnement actif de l'utilisateur ou None."""
    return (
        Abonnement.objects
        .filter(utilisateur=utilisateur, statut='actif', fin__gte=date.today())
        .select_related('pack')
        .first()
    )


class EstVerifie(BasePermission):
    """L'utilisateur doit avoir vérifié son email."""
    message = "Veuillez vérifier votre adresse email."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_verified)


class EstMembreBoutique(BasePermission):
    """L'utilisateur doit être membre actif de la boutique (passée en kwarg 'boutique_id')."""
    message = "Vous n'avez pas accès à cette boutique."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        boutique_id = view.kwargs.get('boutique_id')
        if not boutique_id:
            return False
        return MembreBoutique.objects.filter(
            boutique_id=boutique_id,
            utilisateur=request.user,
            actif=True,
        ).exists()


class EstProprietaireBoutique(BasePermission):
    """Seul le propriétaire peut effectuer cette action."""
    message = "Seul le propriétaire peut effectuer cette action."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        boutique_id = view.kwargs.get('boutique_id')
        if not boutique_id:
            return False
        from .models import Boutique
        return Boutique.objects.filter(
            id=boutique_id, proprietaire=request.user
        ).exists()


class AAbonnementActif(BasePermission):
    """L'utilisateur doit avoir un abonnement actif (autre que basic pour les features avancées)."""
    message = "Abonnement actif requis."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return get_abonnement_actif(request.user) is not None


class APackAvance(BasePermission):
    """L'utilisateur doit avoir le Pack Pro ou Premium (rapports avancés)."""
    message = "Cette fonctionnalité nécessite le Pack Pro ou Premium."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        abo = get_abonnement_actif(request.user)
        return abo is not None and abo.pack.rapports_avances