import random
import string
from datetime import date, timedelta

from django.contrib.auth import authenticate
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from .models import *
from .serializers import *
from .permissions import (
    EstVerifie, EstMembreBoutique, EstProprietaireBoutique,
    AAbonnementActif, APackAvance, get_abonnement_actif,
)
from django.db.models import Sum, Q

def creer_notification(utilisateur, type_notif: str, titre: str, message: str = '', boutique=None):
    """
    Crée une notification pour un utilisateur.
    À appeler après chaque action importante (POST transaction, DELETE, PATCH…).
 
    Exemple d'utilisation dans views.py :
        creer_notification(
            utilisateur=request.user,
            type_notif='suppression',
            titre="Suppression d'une opération de dépôt",
            boutique=boutique,
        )
    """
    from .models import Notification
    Notification.objects.create(
        utilisateur=utilisateur,
        boutique=boutique,
        type=type_notif,
        titre=titre,
        message=message,
    )
 
 
# ── Utilitaire : génération de code à 6 chiffres ──────────────────────────
def generer_code():
    return ''.join(random.choices(string.digits, k=6))


def creer_code(utilisateur, type_code):
    CodeVerification.objects.filter(utilisateur=utilisateur, type=type_code, utilise=False).delete()
    return CodeVerification.objects.create(
        utilisateur=utilisateur,
        code=generer_code(),
        type=type_code,
        expire_le=timezone.now() + timedelta(minutes=15),
    )


def tokens_pour(utilisateur):
    refresh = RefreshToken.for_user(utilisateur)
    return {
        'refresh': str(refresh),
        'access':  str(refresh.access_token),
    }


# ══════════════════════════════════════════════════════════════════════════
#  AUTH — INSCRIPTION
# ══════════════════════════════════════════════════════════════════════════

@api_view(['POST'])
@permission_classes([AllowAny])
def inscription(request):
    """
    POST /auth/inscription/
    Crée un compte et envoie un code de vérification email (6 chiffres).
    """
    serializer = InscriptionSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)

    utilisateur = serializer.save()

    # Générer et (simuler) envoyer le code
    code_obj = creer_code(utilisateur, 'email')
    # TODO: envoyer par email → send_mail(...)
    print(f"[DEV] Code vérification email : {code_obj.code}")

    # Abonnement Basic gratuit automatique
    pack_basic = Pack.objects.get(cle='basic')
    Abonnement.objects.create(
        utilisateur=utilisateur,
        pack=pack_basic,
        statut='actif',
        debut=date.today(),
        fin=date(9999, 12, 31),
    )

    return Response({
        "message": "Compte créé. Veuillez vérifier votre email.",
        "email": utilisateur.email,
    }, status=201)


# ══════════════════════════════════════════════════════════════════════════
#  AUTH — VÉRIFICATION CODE
# ══════════════════════════════════════════════════════════════════════════

@api_view(['POST'])
@permission_classes([AllowAny])
def verifier_code(request):
    """
    POST /auth/verifier-code/
    Vérifie le code à 6 chiffres (email ou reset).
    """
    serializer = VerificationCodeSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)

    try:
        utilisateur = Utilisateur.objects.get(email=serializer.validated_data['email'])
    except Utilisateur.DoesNotExist:
        return Response({"error": "Utilisateur introuvable."}, status=404)

    code_obj = (
        CodeVerification.objects
        .filter(
            utilisateur=utilisateur,
            code=serializer.validated_data['code'],
            type=serializer.validated_data['type'],
            utilise=False,
        )
        .order_by('-cree_le')
        .first()
    )

    if not code_obj or not code_obj.est_valide():
        return Response({"error": "Code invalide ou expiré."}, status=400)

    code_obj.utilise = True
    code_obj.save()

    if serializer.validated_data['type'] == 'email':
        utilisateur.is_verified = True
        utilisateur.save()
        return Response({"message": "Email vérifié avec succès.", **tokens_pour(utilisateur)})

    return Response({"message": "Code valide. Vous pouvez réinitialiser votre mot de passe."})


# ══════════════════════════════════════════════════════════════════════════
#  AUTH — CONNEXION
# ══════════════════════════════════════════════════════════════════════════

@api_view(['POST'])
@permission_classes([AllowAny])
def connexion(request):
    """
    POST /auth/connexion/
    Retourne les tokens JWT si les identifiants sont corrects.
    """
    serializer = ConnexionSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)

    utilisateur = authenticate(
        request,
        username=serializer.validated_data['email'],
        password=serializer.validated_data['password'],
    )
    if not utilisateur:
        return Response({"error": "Email ou mot de passe incorrect."}, status=401)

    if not utilisateur.is_active:
        # Renvoyer un nouveau code
        code_obj = creer_code(utilisateur, 'email')
        print(f"[DEV] Code vérification renvoyé : {code_obj.code}")
        return Response({
            "error": "Compte non activé.",
            "message": "Un nouveau code a été envoyé à votre adresse email.",
            'user_email': utilisateur.email,
        }, status=403)

    return Response({
        "message": "Connexion réussie.",
        "utilisateur": UtilisateurSerializer(utilisateur).data,
        **tokens_pour(utilisateur),
    })


# ══════════════════════════════════════════════════════════════════════════
#  AUTH — MOT DE PASSE OUBLIÉ
# ══════════════════════════════════════════════════════════════════════════

@api_view(['POST'])
@permission_classes([AllowAny])
def mot_de_passe_oublie(request):
    """
    POST /auth/mot-de-passe-oublie/
    Envoie un code de réinitialisation par email.
    """
    serializer = MotDePasseOublieSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)

    try:
        utilisateur = Utilisateur.objects.get(email=serializer.validated_data['email'])
    except Utilisateur.DoesNotExist:
        # Sécurité : ne pas révéler si l'email existe
        return Response({"message": "Si cet email existe, un code a été envoyé."})

    code_obj = creer_code(utilisateur, 'reset')
    print(f"[DEV] Code reset : {code_obj.code}")
    # TODO: envoyer par email

    return Response({"message": "Si cet email existe, un code a été envoyé."})


@api_view(['POST'])
@permission_classes([AllowAny])
def reinitialiser_mot_de_passe(request):
    """
    POST /auth/reinitialiser-mot-de-passe/
    Réinitialise le mot de passe après vérification du code reset.
    """
    serializer = ReinitialisationMotDePasseSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)

    try:
        utilisateur = Utilisateur.objects.get(email=serializer.validated_data['email'])
    except Utilisateur.DoesNotExist:
        return Response({"error": "Utilisateur introuvable."}, status=404)

    code_obj = (
        CodeVerification.objects
        .filter(utilisateur=utilisateur, type='reset', utilise=False)
        .order_by('-cree_le')
        .first()
    )

    if not code_obj or code_obj.code != serializer.validated_data['code'] or not code_obj.est_valide():
        return Response({"error": "Code invalide ou expiré."}, status=400)

    utilisateur.set_password(serializer.validated_data['password'])
    utilisateur.save()
    code_obj.utilise = True
    code_obj.save()

    return Response({"message": "Mot de passe réinitialisé avec succès."})


# ══════════════════════════════════════════════════════════════════════════
#  AUTH — PROFIL
# ══════════════════════════════════════════════════════════════════════════

@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def profil(request):
    """
    GET  /auth/profil/  → voir son profil
    PATCH /auth/profil/ → modifier nom, prenom, telephone
    """
    if request.method == 'GET':
        return Response(UtilisateurSerializer(request.user).data)

    serializer = UtilisateurSerializer(request.user, data=request.data, partial=True)
    if serializer.is_valid():
        serializer.save()
        return Response(serializer.data)
    return Response(serializer.errors, status=400)


@api_view(['POST'])
@permission_classes([AllowAny])
def rechercher_utilisateur(request):
    """
    POST /auth/rechercher/  {"email": "..."} ou {"telephone": "..."} ou {"contact": "..."}
    Utilisé pour chercher un utilisateur avant de l'ajouter à une boutique.
    """
    email = request.data.get('email', '').strip()
    telephone = request.data.get('telephone', '').strip()
    contact = request.data.get('contact', '').strip()

    value = email or telephone or contact
    if not value:
        return Response({"error": "Email ou numéro de téléphone requis."}, status=400)

    try:
        if email or ('@' in value):
            utilisateur = Utilisateur.objects.get(email=value)
        else:
            utilisateur = Utilisateur.objects.get(telephone=value)
        return Response(RechercheUtilisateurSerializer(utilisateur).data)
    except Utilisateur.DoesNotExist:
        return Response({"found": False, "message": "Aucun compte trouvé."}, status=404)


# ══════════════════════════════════════════════════════════════════════════
#  PACKS
# ══════════════════════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([AllowAny])
def liste_packs(request):
    """GET /packs/  → liste des packs disponibles"""
    packs = Pack.objects.filter(actif=True)
    return Response(PackSerializer(packs, many=True).data)


# ══════════════════════════════════════════════════════════════════════════
#  ABONNEMENTS
# ══════════════════════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def mon_abonnement(request):
    """GET /abonnements/mon-abonnement/  → abonnement actif de l'utilisateur"""
    abo = get_abonnement_actif(request.user)
    if not abo:
        return Response({"message": "Aucun abonnement actif."}, status=404)
    return Response(AbonnementSerializer(abo).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated, EstVerifie])
def souscrire_abonnement(request):
    """
    POST /abonnements/souscrire/
    {"pack_cle": "pro"}
    Souscrit ou change de pack.
    """
    serializer = SouscrireAbonnementSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)

    pack_cle = serializer.validated_data['pack_cle']

    try:
        pack = Pack.objects.get(cle=pack_cle, actif=True)
    except Pack.DoesNotExist:
        return Response({"error": "Pack introuvable."}, status=404)

    # Annuler l'abonnement actif actuel
    Abonnement.objects.filter(
        utilisateur=request.user, statut='actif'
    ).update(statut='annule')

    today = date.today()
    fin_periode_gratuite = None
    prochain_prelevement = None

    if pack.premier_mois_offert:
        fin_periode_gratuite = today + timedelta(days=30)
        prochain_prelevement = today + timedelta(days=60)
        fin = today + timedelta(days=365)
    else:
        fin = date(9999, 12, 31) if pack.cle == 'basic' else today + timedelta(days=30)

    abo = Abonnement.objects.create(
        utilisateur=request.user,
        pack=pack,
        statut='actif',
        debut=today,
        fin=fin,
        fin_periode_gratuite=fin_periode_gratuite,
        prochain_prelevement=prochain_prelevement,
    )

    return Response(AbonnementSerializer(abo).data, status=201)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def historique_abonnements(request):
    """GET /abonnements/historique/"""
    abonnements = Abonnement.objects.filter(utilisateur=request.user)
    return Response(AbonnementSerializer(abonnements, many=True).data)


# ══════════════════════════════════════════════════════════════════════════
#  BOUTIQUES
# ══════════════════════════════════════════════════════════════════════════

def _verifier_limite_boutiques(utilisateur):
    """Vérifie si l'utilisateur peut créer une boutique supplémentaire."""
    abo = get_abonnement_actif(utilisateur)
    if not abo:
        return False, "Aucun abonnement actif."

    max_b = abo.pack.max_boutiques
    if max_b == -1:
        return True, None

    nb_boutiques = Boutique.objects.filter(proprietaire=utilisateur, active=True).count()
    if nb_boutiques >= max_b:
        return False, f"Votre pack {abo.pack.nom} permet au maximum {max_b} boutique(s)."
    return True, None


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated, EstVerifie])
def mes_boutiques(request):
    """
    GET  /boutiques/
         → Retourne toutes les boutiques dont l'utilisateur est membre actif.
           Cela inclut les boutiques dont il est propriétaire ET celles
           où il a été ajouté comme gérant/admin.
 
    POST /boutiques/
         → Crée une nouvelle boutique (le créateur devient propriétaire + membre).
    """
    if request.method == 'GET':
        # Récupérer les IDs de boutiques où l'user est membre actif
        boutique_ids = MembreBoutique.objects.filter(
            utilisateur=request.user,
            actif=True,
        ).values_list('boutique_id', flat=True)
 
        # Récupérer les boutiques actives correspondantes
        boutiques = Boutique.objects.filter(id__in=boutique_ids, active=True)
        return Response(BoutiqueSerializer(boutiques, many=True).data)
 
    # ── POST : créer une boutique ─────────────────────────────────────────
    ok, msg = _verifier_limite_boutiques(request.user)
    if not ok:
        return Response({"error": msg}, status=403)
 
    serializer = BoutiqueSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)
 
    boutique = serializer.save(proprietaire=request.user)
 
    # Créer les soldes par défaut (MTN, Moov, Celtiis, Caisse)
    for op in ['MTN', 'Moov', 'Celtiis', 'Caisse']:
        SoldeOperateur.objects.get_or_create(boutique=boutique, operateur=op)
 
    # Ajouter le propriétaire comme membre (rôle propriétaire, ne peut pas être retiré)
    MembreBoutique.objects.create(
        boutique=boutique,
        utilisateur=request.user,
        role='proprietaire',
    )
 
    return Response(BoutiqueSerializer(boutique).data, status=201)
 
 

@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticated, EstVerifie])
def detail_boutique(request, boutique_id):
    """
    GET    /boutiques/<boutique_id>/  → détail
    PATCH  /boutiques/<boutique_id>/  → modifier (propriétaire seulement)
    DELETE /boutiques/<boutique_id>/  → désactiver (propriétaire seulement)
    """
    try:
        boutique = Boutique.objects.get(id=boutique_id)
    except Boutique.DoesNotExist:
        return Response({"error": "Boutique introuvable."}, status=404)

    est_membre = MembreBoutique.objects.filter(
        boutique=boutique, utilisateur=request.user, actif=True
    ).exists()
    if not est_membre:
        return Response({"error": "Accès refusé."}, status=403)

    if request.method == 'GET':
        return Response(BoutiqueSerializer(boutique).data)

    # Seul le propriétaire peut modifier / supprimer
    if boutique.proprietaire != request.user:
        return Response({"error": "Seul le propriétaire peut effectuer cette action."}, status=403)

    if request.method == 'PATCH':
        serializer = BoutiqueSerializer(boutique, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=400)

    if request.method == 'DELETE':
        boutique.active = False
        boutique.save()
        return Response({"message": "Boutique désactivée."})


# ══════════════════════════════════════════════════════════════════════════
#  MEMBRES BOUTIQUE
# ══════════════════════════════════════════════════════════════════════════

def _verifier_limite_membres(boutique):
    """Vérifie si la boutique peut accueillir un membre supplémentaire."""
    proprietaire = boutique.proprietaire
    abo = get_abonnement_actif(proprietaire)
    if not abo:
        return False, "Propriétaire sans abonnement actif."

    max_u = abo.pack.max_utilisateurs
    if max_u == -1:
        return True, None

    nb = MembreBoutique.objects.filter(boutique=boutique, actif=True).count()
    if nb >= max_u:
        return False, f"Votre pack permet au maximum {max_u} utilisateur(s) par boutique."
    return True, None


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated, EstVerifie])
def membres_boutique(request, boutique_id):
    """
    GET  /boutiques/<boutique_id>/membres/  → lister les membres
    POST /boutiques/<boutique_id>/membres/  → ajouter un membre (propriétaire)
    """
    try:
        boutique = Boutique.objects.get(id=boutique_id)
    except Boutique.DoesNotExist:
        return Response({"error": "Boutique introuvable."}, status=404)

    if not MembreBoutique.objects.filter(boutique=boutique, utilisateur=request.user, actif=True).exists():
        return Response({"error": "Accès refusé."}, status=403)

    if request.method == 'GET':
        membres = MembreBoutique.objects.filter(boutique=boutique, actif=True).select_related('utilisateur')
        return Response(MembreBoutiqueSerializer(membres, many=True).data)

    # POST — seul propriétaire
    if boutique.proprietaire != request.user:
        return Response({"error": "Seul le propriétaire peut ajouter des membres."}, status=403)

    ok, msg = _verifier_limite_membres(boutique)
    if not ok:
        return Response({"error": msg}, status=403)

    serializer = AjouterMembreSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)

    contact = serializer.validated_data.get('contact')

    try:
        if '@' in contact:
            nouveau = Utilisateur.objects.get(email=contact)
        else:
            nouveau = Utilisateur.objects.get(telephone=contact)
    except Utilisateur.DoesNotExist:
        return Response({"error": "Aucun compte trouvé pour ce contact."}, status=404)

    if MembreBoutique.objects.filter(boutique=boutique, utilisateur=nouveau).exists():
        return Response({"error": "Cet utilisateur est déjà membre de la boutique."}, status=409)

    membre = MembreBoutique.objects.create(
        boutique=boutique,
        utilisateur=nouveau,
        role=serializer.validated_data['role'],
    )
    return Response(MembreBoutiqueSerializer(membre).data, status=201)



@api_view(['DELETE'])
@permission_classes([IsAuthenticated, EstVerifie])
def retirer_membre(request, boutique_id, membre_id):
    """DELETE /boutiques/<boutique_id>/membres/<membre_id>/"""
    try:
        boutique = Boutique.objects.get(id=boutique_id, proprietaire=request.user)
    except Boutique.DoesNotExist:
        return Response({"error": "Boutique introuvable ou accès refusé."}, status=404)
 
    try:
        membre = MembreBoutique.objects.get(id=membre_id, boutique=boutique)
    except MembreBoutique.DoesNotExist:
        return Response({"error": "Membre introuvable."}, status=404)
 
    # Bloquer la suppression du propriétaire (protection absolue)
    if membre.role == 'proprietaire':
        return Response({"error": "Le propriétaire ne peut pas être retiré de sa boutique."}, status=400)
 
    # Bloquer l'auto-suppression
    if membre.utilisateur == request.user:
        return Response({"error": "Vous ne pouvez pas vous retirer vous-même."}, status=400)
 
    membre.actif = False
    membre.save()
    return Response({"message": "Membre retiré de la boutique."})

# ══════════════════════════════════════════════════════════════════════════
#  SOLDES
# ══════════════════════════════════════════════════════════════════════════

@api_view(['GET'])
@permission_classes([IsAuthenticated, EstVerifie])
def soldes_boutique(request, boutique_id):
    """GET /boutiques/<boutique_id>/soldes/"""
    try:
        boutique = Boutique.objects.get(id=boutique_id)
    except Boutique.DoesNotExist:
        return Response({"error": "Boutique introuvable."}, status=404)

    if not MembreBoutique.objects.filter(boutique=boutique, utilisateur=request.user, actif=True).exists():
        return Response({"error": "Accès refusé."}, status=403)

    soldes = SoldeOperateur.objects.filter(boutique=boutique)
    return Response(SoldeOperateurSerializer(soldes, many=True).data)


@api_view(['PATCH'])
@permission_classes([IsAuthenticated, EstVerifie])
def mettre_a_jour_soldes(request, boutique_id):
    """
    PATCH /boutiques/<boutique_id>/soldes/
    {"MTN": 50000, "Moov": 20000, "Caisse": 10000}
    """
    try:
        boutique = Boutique.objects.get(id=boutique_id)
    except Boutique.DoesNotExist:
        return Response({"error": "Boutique introuvable."}, status=404)

    if not MembreBoutique.objects.filter(boutique=boutique, utilisateur=request.user, actif=True).exists():
        return Response({"error": "Accès refusé."}, status=403)

    serializer = MiseAJourSoldesSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)

    updated = []
    for operateur, montant in serializer.validated_data.items():
        solde, _ = SoldeOperateur.objects.get_or_create(boutique=boutique, operateur=operateur)
        solde.montant = montant
        solde.save()
        updated.append(SoldeOperateurSerializer(solde).data)

    return Response(updated)


# ══════════════════════════════════════════════════════════════════════════
#  TRANSACTIONS
# ══════════════════════════════════════════════════════════════════════════

@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated, EstVerifie])
def transactions_boutique(request, boutique_id):
    """
    GET  /boutiques/<boutique_id>/transactions/   → liste avec filtres optionnels
         ?type=Depot&operateur=MTN&date_debut=2025-01-01&date_fin=2025-12-31&search=...
    POST /boutiques/<boutique_id>/transactions/   → créer une transaction
    """
    try:
        boutique = Boutique.objects.get(id=boutique_id)
    except Boutique.DoesNotExist:
        return Response({"error": "Boutique introuvable."}, status=404)

    if not MembreBoutique.objects.filter(boutique=boutique, utilisateur=request.user, actif=True).exists():
        return Response({"error": "Accès refusé."}, status=403)

    if request.method == 'GET':
        qs = Transaction.objects.filter(boutique=boutique)

        # Filtres
        type_tx   = request.query_params.get('type')
        operateur = request.query_params.get('operateur')
        date_debut = request.query_params.get('date_debut')
        date_fin   = request.query_params.get('date_fin')
        search     = request.query_params.get('search')

        if type_tx:
            qs = qs.filter(type=type_tx)
        if operateur:
            qs = qs.filter(operateur=operateur)
        if date_debut:
            qs = qs.filter(date__date__gte=date_debut)
        if date_fin:
            qs = qs.filter(date__date__lte=date_fin)
        if search:
            qs = qs.filter(
                Q(numero_client__icontains=search) |
                Q(nom_client__icontains=search) |
                Q(reference__icontains=search) |
                Q(operateur__icontains=search)
            )

        return Response(TransactionSerializer(qs, many=True).data)

    # POST
    serializer = TransactionSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)

    transaction = serializer.save(boutique=boutique, effectuee_par=request.user)
    creer_notification(
        utilisateur=request.user,
        type_notif='depot' if transaction.type == 'Depot'
                   else 'retrait' if transaction.type == 'Retrait'
                   else 'credit',
        titre=f"Une opération de {transaction.type.lower()} a été effectuée",
        boutique=boutique,
    )
    
    # Mettre à jour les soldes : Opérateur et Caisse
    try:
        # Solde de l'opérateur (MTN, Moov, Celtiis, etc.)
        solde_operateur = SoldeOperateur.objects.get(boutique=boutique, operateur=transaction.operateur)
        # Solde de la Caisse
        solde_caisse = SoldeOperateur.objects.get(boutique=boutique, operateur='Caisse')
        
        if transaction.type == 'Depot':
            # Dépôt : Opérateur diminue (-), Caisse augmente (+)
            solde_operateur.montant -= transaction.montant
            solde_caisse.montant += transaction.montant
        elif transaction.type == 'Retrait':
            # Retrait : Caisse diminue (-), Opérateur augmente (+)
            solde_caisse.montant -= transaction.montant
            solde_operateur.montant += transaction.montant
        elif transaction.type == 'Credit':
            # Crédit/Forfait : Opérateur diminue (-), Caisse augmente (+)
            solde_operateur.montant -= transaction.montant
            solde_caisse.montant += transaction.montant
        
        solde_operateur.save()
        solde_caisse.save()
    except SoldeOperateur.DoesNotExist:
        # Créer les soldes s'ils n'existent pas
        pass

    return Response(TransactionSerializer(transaction).data, status=201)


@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticated, EstVerifie])
def detail_transaction(request, boutique_id, transaction_id):
    """
    GET    /boutiques/<boutique_id>/transactions/<transaction_id>/
    PATCH  /boutiques/<boutique_id>/transactions/<transaction_id>/
    DELETE /boutiques/<boutique_id>/transactions/<transaction_id>/
    """
    try:
        boutique = Boutique.objects.get(id=boutique_id)
        transaction = Transaction.objects.get(id=transaction_id, boutique=boutique)
    except (Boutique.DoesNotExist, Transaction.DoesNotExist):
        return Response({"error": "Introuvable."}, status=404)

    if not MembreBoutique.objects.filter(boutique=boutique, utilisateur=request.user, actif=True).exists():
        return Response({"error": "Accès refusé."}, status=403)

    if request.method == 'GET':
        return Response(TransactionSerializer(transaction).data)

    if request.method == 'PATCH':
        serializer = TransactionSerializer(transaction, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            creer_notification(
                utilisateur=request.user,
                type_notif='modification',
                titre=f"Une modification a été effectuée sur une opération d'achat de crédit / Forfait"
                    if transaction.type == 'Credit'
                    else f"Modification d'une opération de {transaction.type.lower()}",
                boutique=boutique,
            )
            return Response(serializer.data)
        return Response(serializer.errors, status=400)

    if request.method == 'DELETE':
        # Annuler l'impact sur les soldes (opérateur et caisse)
        try:
            solde_operateur = SoldeOperateur.objects.get(boutique=boutique, operateur=transaction.operateur)
            solde_caisse = SoldeOperateur.objects.get(boutique=boutique, operateur='Caisse')
            
            if transaction.type == 'Depot':
                # Annuler dépôt : Opérateur augmente (+), Caisse diminue (-)
                solde_operateur.montant += transaction.montant
                solde_caisse.montant -= transaction.montant
            elif transaction.type == 'Retrait':
                # Annuler retrait : Caisse augmente (+), Opérateur diminue (-)
                solde_caisse.montant += transaction.montant
                solde_operateur.montant -= transaction.montant
            elif transaction.type == 'Credit':
                # Annuler crédit : Opérateur augmente (+), Caisse diminue (-)
                solde_operateur.montant += transaction.montant
                solde_caisse.montant -= transaction.montant
            
            solde_operateur.save()
            solde_caisse.save()
            
            creer_notification(
                utilisateur=request.user,
                type_notif='suppression',
                titre=f"Suppression d'une opération de {transaction.type.lower()}",
                boutique=boutique,
            )
        except SoldeOperateur.DoesNotExist:
            pass

        transaction.delete()
        return Response({"message": "Transaction supprimée."})


# ══════════════════════════════════════════════════════════════════════════
#  RAPPORTS
# ══════════════════════════════════════════════════════════════════════════

def _verifier_acces_rapports(utilisateur, boutique):
    """Vérifie l'accès aux rapports avancés selon le pack."""
    abo = get_abonnement_actif(utilisateur)
    if not abo:
        return False, "Aucun abonnement actif."
    if not abo.pack.rapports_avances:
        return False, "Les rapports avancés nécessitent le Pack Pro ou Premium."
    return True, None


@api_view(['GET'])
@permission_classes([IsAuthenticated, EstVerifie])
def rapport_journalier(request, boutique_id):
    """
    GET /boutiques/<boutique_id>/rapports/journalier/?date=2025-04-10
    Retourne le rapport calculé depuis les transactions du jour.
    """
    try:
        boutique = Boutique.objects.get(id=boutique_id)
    except Boutique.DoesNotExist:
        return Response({"error": "Boutique introuvable."}, status=404)

    if not MembreBoutique.objects.filter(boutique=boutique, utilisateur=request.user, actif=True).exists():
        return Response({"error": "Accès refusé."}, status=403)

    date_str = request.query_params.get('date', str(date.today()))

    qs = Transaction.objects.filter(boutique=boutique, date__date=date_str)

    totaux = qs.aggregate(
        total_depots=Sum('montant', filter=Q(type='Depot')),
        total_retraits=Sum('montant', filter=Q(type='Retrait')),
        total_credits=Sum('montant', filter=Q(type='Credit')),
     
    )

    total_depots     = totaux['total_depots']     or 0
    total_retraits   = totaux['total_retraits']   or 0
    total_credits    = totaux['total_credits']    or 0

    return Response({
        "date": date_str,
        "boutique": str(boutique_id),
        "total_depots": total_depots,
        "total_retraits": total_retraits,
        "total_credits": total_credits,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated, EstVerifie])
def rapport_par_operateur(request, boutique_id):
    """
    GET /boutiques/<boutique_id>/rapports/operateurs/?date=2025-04-10
    Nécessite Pack Pro ou Premium.
    """
    try:
        boutique = Boutique.objects.get(id=boutique_id)
    except Boutique.DoesNotExist:
        return Response({"error": "Boutique introuvable."}, status=404)

    if not MembreBoutique.objects.filter(boutique=boutique, utilisateur=request.user, actif=True).exists():
        return Response({"error": "Accès refusé."}, status=403)

    ok, msg = _verifier_acces_rapports(request.user, boutique)
    if not ok:
        return Response({"error": msg}, status=403)

    date_str = request.query_params.get('date', str(date.today()))
    operateurs = ['MTN', 'Moov', 'Celtiis']
    rapport = []

    for op in operateurs:
        qs = Transaction.objects.filter(boutique=boutique, operateur=op, date__date=date_str)
        totaux = qs.aggregate(
            total_depots=Sum('montant', filter=Q(type='Depot')),
            total_retraits=Sum('montant', filter=Q(type='Retrait')),
            total_credits=Sum('montant', filter=Q(type='Credit')),
        )
        try:
            solde = SoldeOperateur.objects.get(boutique=boutique, operateur=op).montant
        except SoldeOperateur.DoesNotExist:
            solde = 0

        rapport.append({
            "operateur": op,
            "total_depots":      totaux['total_depots'] or 0,
            "total_retraits":    totaux['total_retraits'] or 0,
            "total_credits":     totaux['total_credits'] or 0,
            "solde_restant":     solde,
        })

    return Response(rapport)




@api_view(['GET'])
@permission_classes([IsAuthenticated, EstVerifie])
def solde_global(request, boutique_id):
    """GET /boutiques/<boutique_id>/rapports/solde-global/"""
    try:
        boutique = Boutique.objects.get(id=boutique_id)
    except Boutique.DoesNotExist:
        return Response({"error": "Boutique introuvable."}, status=404)

    if not MembreBoutique.objects.filter(boutique=boutique, utilisateur=request.user, actif=True).exists():
        return Response({"error": "Accès refusé."}, status=403)

    soldes = SoldeOperateur.objects.filter(boutique=boutique)
    data = [{"operateur": s.operateur, "montant": s.montant} for s in soldes]
    total = sum(s.montant for s in soldes)

    return Response({"soldes": data, "total": total})



@api_view(['GET'])
@permission_classes([IsAuthenticated, EstVerifie])
def rapport_commissions(request, boutique_id):
    """
    GET /boutiques/<boutique_id>/rapports/commissions/?date_debut=...&date_fin=...
    Nécessite Pack Pro ou Premium.
    """
    try:
        boutique = Boutique.objects.get(id=boutique_id)
    except Boutique.DoesNotExist:
        return Response({"error": "Boutique introuvable."}, status=404)

    if not MembreBoutique.objects.filter(boutique=boutique, utilisateur=request.user, actif=True).exists():
        return Response({"error": "Accès refusé."}, status=403)

    ok, msg = _verifier_acces_rapports(request.user, boutique)
    if not ok:
        return Response({"error": msg}, status=403)

    date_debut = request.query_params.get('date_debut', str(date.today()))
    date_fin   = request.query_params.get('date_fin', str(date.today()))

    qs = Transaction.objects.filter(boutique=boutique, date__date__gte=date_debut, date__date__lte=date_fin)

    rapport = []
    for op in ['MTN', 'Moov', 'Celtiis']:
        total = qs.filter(operateur=op).aggregate(total=Sum('commission'))['total'] or 0
        rapport.append({"operateur": op, "total_commissions": total})

    total_global = sum(r['total_commissions'] for r in rapport)
    return Response({"commissions_par_operateur": rapport, "total_global": total_global})


# ══════════════════════════════════════════════════════════════════════════
#  AVIS
# ══════════════════════════════════════════════════════════════════════════

@api_view(['POST'])
@permission_classes([IsAuthenticated, EstVerifie])
def soumettre_avis(request):
    """
    POST /avis/
    {"message": "Super application !"}
    Soumet un avis lié à l'utilisateur connecté.
    """
    message = request.data.get('message', '').strip()

    if not message:
        return Response({"error": "Le message ne peut pas être vide."}, status=400)

    if len(message) > 255:
        return Response({"error": "Le message ne doit pas dépasser 255 caractères."}, status=400)

    avis = Avis.objects.create(
        utilisateur=request.user,
        message=message,
    )

    return Response(AvisSerializer(avis).data, status=201)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def liste_avis(request):
    """
    GET /avis/admin/
    Réservé aux admins Django (is_staff).
    Paramètres optionnels : ?lu=true|false
    """
    if not request.user.is_staff:
        return Response({"error": "Accès réservé aux administrateurs."}, status=403)

    qs = Avis.objects.select_related('utilisateur').all()

    lu_param = request.query_params.get('lu')
    if lu_param is not None:
        qs = qs.filter(lu=lu_param.lower() == 'true')

    return Response(AvisSerializer(qs, many=True).data)


@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
def marquer_avis_lu(request, avis_id):
    """
    PATCH /avis/<avis_id>/lu/
    Réservé aux admins Django. Marque un avis comme lu.
    """
    if not request.user.is_staff:
        return Response({"error": "Accès réservé aux administrateurs."}, status=403)

    try:
        avis = Avis.objects.get(id=avis_id)
    except Avis.DoesNotExist:
        return Response({"error": "Avis introuvable."}, status=404)

    avis.lu = True
    avis.save()
    return Response(AvisSerializer(avis).data)

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def mes_notifications(request):
    """
    GET /notifications/
    Retourne les notifications de l'utilisateur connecté.
    Paramètre optionnel : ?non_lues=true  → seulement les non lues
    """
    qs = Notification.objects.filter(utilisateur=request.user)
 
    non_lues = request.query_params.get('non_lues')
    if non_lues and non_lues.lower() == 'true':
        qs = qs.filter(lu=False)
 
    return Response(NotificationSerializer(qs, many=True).data)
 
 
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def compteur_non_lues(request):
    """
    GET /notifications/compteur/
    Retourne le nombre de notifications non lues.
    """
    count = Notification.objects.filter(utilisateur=request.user, lu=False).count()
    return Response({"non_lues": count})
 
 
@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
def marquer_notification_lue(request, notif_id):
    """
    PATCH /notifications/<notif_id>/lue/
    Marque une notification comme lue.
    """
    try:
        notif = Notification.objects.get(id=notif_id, utilisateur=request.user)
    except Notification.DoesNotExist:
        return Response({"error": "Notification introuvable."}, status=404)
 
    notif.lu = True
    notif.save()
    return Response(NotificationSerializer(notif).data)
 
 
@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
def tout_marquer_lu(request):
    """
    PATCH /notifications/tout-marquer-lu/
    Marque toutes les notifications de l'utilisateur comme lues.
    Paramètre optionnel : {"date": "2026-06-05"}  → seulement ce jour-là
    """
    qs = Notification.objects.filter(utilisateur=request.user, lu=False)
 
    date_param = request.data.get('date')
    if date_param:
        qs = qs.filter(cree_le__date=date_param)
 
    count = qs.update(lu=True)
    return Response({"message": f"{count} notification(s) marquée(s) comme lue(s)."})
 
 
@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def supprimer_notification(request, notif_id):
    """
    DELETE /notifications/<notif_id>/
    Supprime une notification.
    """
    try:
        notif = Notification.objects.get(id=notif_id, utilisateur=request.user)
    except Notification.DoesNotExist:
        return Response({"error": "Notification introuvable."}, status=404)
 
    notif.delete()
    return Response({"message": "Notification supprimée."})
 
 
@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def supprimer_toutes_notifications(request):
    """
    DELETE /notifications/tout-supprimer/
    Supprime toutes les notifications de l'utilisateur.
    """
    Notification.objects.filter(utilisateur=request.user).delete()
    return Response({"message": "Toutes les notifications ont été supprimées."})
 
