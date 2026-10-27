import logging
import random
import string
from datetime import date, datetime, time, timedelta

import fedapay as fedapay_sdk
from django.conf import settings
from django.contrib.auth import authenticate
from django.core.mail import send_mail
from django.urls import reverse
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.throttling import AnonRateThrottle
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from . import fedapay_client
from .models import *
from .serializers import *
from .permissions import (
    EstVerifie, EstMembreBoutique, EstProprietaireBoutique,
    AAbonnementActif, APackAvance, get_abonnement_actif,
    boutiques_autorisees_ids,
)
from django.db import transaction as db_transaction
from django.db.models import Avg, Sum, Q

logger = logging.getLogger(__name__)


class CodesAuthThrottle(AnonRateThrottle):
    # Limite par IP les endpoints publics liés aux codes envoyés par email.
    # Le taux est fixé ici (et non dans settings.REST_FRAMEWORK) pour ne pas
    # dépendre de la configuration du serveur.
    scope = 'codes_auth'
    rate = '30/hour'

    def allow_request(self, request, view):
        # Un problème de cache ne doit jamais bloquer l'inscription ou la
        # vérification : les limites par compte (en base) restent actives.
        try:
            return super().allow_request(request, view)
        except Exception:
            logger.exception("[THROTTLE] Limitation par IP indisponible")
            return True


def _verifier_acces_boutique(boutique, utilisateur):
    """
    Vérifie que l'utilisateur est membre actif de la boutique ET que celle-ci
    reste dans la limite actuelle de l'abonnement de son propriétaire (les
    boutiques les plus anciennes restent accessibles en priorité si
    l'abonnement a expiré sans être renouvelé).
    """
    if not MembreBoutique.objects.filter(boutique=boutique, utilisateur=utilisateur, actif=True).exists():
        return False, "Accès refusé."
    if boutique.id not in boutiques_autorisees_ids(boutique.proprietaire):
        return False, (
            "Cette boutique n'est plus accessible : l'abonnement de son "
            "propriétaire ne couvre plus ce nombre de boutiques. "
            "Renouvelez l'abonnement pour la réactiver."
        )
    return True, None


def _date_rapport(request, parameter='date'):
    """Retourne une date ISO valide, jamais postérieure au jour de Porto-Novo."""
    raw_value = request.query_params.get(parameter)
    today_benin = timezone.localdate()
    if not raw_value:
        return today_benin, None
    try:
        selected_date = date.fromisoformat(raw_value)
    except (TypeError, ValueError):
        return None, Response(
            {"error": "La date doit être au format AAAA-MM-JJ."}, status=400
        )
    if selected_date > today_benin:
        return None, Response(
            {"error": "La date du rapport ne peut pas être postérieure à aujourd’hui."}, status=400
        )
    return selected_date, None


def _transactions_du_jour(boutique, selected_date):
    """Filtre une journée civile complète dans le fuseau Africa/Porto-Novo."""
    benin_tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(selected_date, time.min), benin_tz)
    end = start + timedelta(days=1)
    return Transaction.objects.filter(
        boutique=boutique,
        archivee=False,
        date__gte=start,
        date__lt=end,
    )


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


# Limites anti-abus sur les codes envoyés par email
CODE_DELAI_RENVOI   = timedelta(seconds=60)   # un code au plus par minute
CODE_MAX_PAR_HEURE  = 5                       # et 5 codes par heure
CODE_MAX_TENTATIVES = 5                       # saisies erronées avant invalidation


def envoi_code_autorise(utilisateur, type_code):
    """Empêche d'inonder une boîte mail de codes (bots, saisies répétées)."""
    maintenant = timezone.now()
    recents = CodeVerification.objects.filter(
        utilisateur=utilisateur, type=type_code, cree_le__gte=maintenant - timedelta(hours=1),
    )
    if recents.count() >= CODE_MAX_PAR_HEURE:
        return False
    return not recents.filter(cree_le__gte=maintenant - CODE_DELAI_RENVOI).exists()


def controler_code(utilisateur, type_code, code_saisi, consommer):
    """
    Vérifie le dernier code envoyé. Chaque erreur est comptée et le code est
    invalidé après CODE_MAX_TENTATIVES essais (protection contre la force brute).
    Retourne un message d'erreur, ou None si le code est bon.
    """
    code_obj = (
        CodeVerification.objects
        .filter(utilisateur=utilisateur, type=type_code, utilise=False)
        .order_by('-cree_le')
        .first()
    )
    if not code_obj or not code_obj.est_valide():
        return "Code invalide ou expiré."
    if code_obj.code != code_saisi:
        code_obj.tentatives += 1
        if code_obj.tentatives >= CODE_MAX_TENTATIVES:
            code_obj.utilise = True
            code_obj.save(update_fields=['tentatives', 'utilise'])
            return "Trop d'essais incorrects. Demandez un nouveau code."
        code_obj.save(update_fields=['tentatives'])
        return "Code invalide ou expiré."
    if consommer:
        code_obj.utilise = True
        code_obj.save(update_fields=['utilise'])
    return None


def ip_client(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return forwarded.split(',')[0].strip() if forwarded else request.META.get('REMOTE_ADDR', '')


def creer_code(utilisateur, type_code):
    # Les anciens codes sont invalidés (et non supprimés) pour garder
    # l'historique servant aux limites d'envoi.
    CodeVerification.objects.filter(utilisateur=utilisateur, type=type_code, utilise=False).update(utilise=True)
    return CodeVerification.objects.create(
        utilisateur=utilisateur,
        code=generer_code(),
        type=type_code,
        expire_le=timezone.now() + timedelta(minutes=15),
    )


def envoyer_code_email(utilisateur, code_obj):
    """Envoie le code de vérification/réinitialisation par email (best-effort)."""
    if code_obj.type == 'email':
        sujet   = "Vérifiez votre compte MomoTrans"
        message = (
            f"Bonjour {utilisateur.prenom},\n\n"
            f"Voici votre code de vérification MomoTrans : {code_obj.code}\n"
            f"Ce code expire dans 15 minutes.\n\n"
            f"Si vous n'êtes pas à l'origine de cette demande, ignorez cet email."
        )
    else:
        sujet   = "Réinitialisation de votre mot de passe MomoTrans"
        message = (
            f"Bonjour {utilisateur.prenom},\n\n"
            f"Voici votre code de réinitialisation de mot de passe MomoTrans : {code_obj.code}\n"
            f"Ce code expire dans 15 minutes.\n\n"
            f"Si vous n'êtes pas à l'origine de cette demande, ignorez cet email."
        )
    try:
        send_mail(sujet, message, settings.DEFAULT_FROM_EMAIL, [utilisateur.email], fail_silently=False)
    except Exception as e:
        print(f"[EMAIL] Échec d'envoi à {utilisateur.email} : {e}")
        print(f"[DEV] Code {code_obj.type} pour {utilisateur.email} : {code_obj.code}")


def trouver_utilisateur(email):
    """Recherche insensible à la casse (comptes créés avant la normalisation)."""
    return Utilisateur.objects.filter(email__iexact=email).first()


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

    with db_transaction.atomic():
        utilisateur = serializer.save()

        # Abonnement Basic gratuit automatique
        pack_basic = Pack.objects.get(cle='basic')
        Abonnement.objects.create(
            utilisateur=utilisateur,
            pack=pack_basic,
            statut='actif',
            debut=date.today(),
            fin=date(9999, 12, 31),
        )
        code_obj = creer_code(utilisateur, 'email')

    # Envoi après la création : un échec d'email n'annule pas le compte,
    # l'utilisateur peut redemander un code depuis l'écran de vérification.
    envoyer_code_email(utilisateur, code_obj)

    return Response({
        "message": "Compte créé. Veuillez vérifier votre email.",
        "email": utilisateur.email,
    }, status=201)


# ══════════════════════════════════════════════════════════════════════════
#  AUTH — VÉRIFICATION CODE
# ══════════════════════════════════════════════════════════════════════════

@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([CodesAuthThrottle])
def verifier_code(request):
    """
    POST /auth/verifier-code/
    Vérifie le code à 6 chiffres (email ou reset).
    """
    serializer = VerificationCodeSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)

    utilisateur = trouver_utilisateur(serializer.validated_data['email'])
    if not utilisateur:
        return Response({"error": "Utilisateur introuvable."}, status=404)

    type_code = serializer.validated_data['type']
    erreur = controler_code(
        utilisateur, type_code, serializer.validated_data['code'],
        # Le code « reset » sera de nouveau exigé par reinitialiser_mot_de_passe
        consommer=(type_code == 'email'),
    )
    if erreur:
        return Response({"error": erreur}, status=400)

    if type_code == 'email':
        utilisateur.is_verified = True
        utilisateur.save(update_fields=['is_verified'])
        return Response({
            "message": "Email vérifié avec succès.",
            "utilisateur": UtilisateurSerializer(utilisateur).data,
            **tokens_pour(utilisateur),
        })

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

    compte = trouver_utilisateur(serializer.validated_data['email'])
    utilisateur = compte and authenticate(
        request,
        username=compte.email,
        password=serializer.validated_data['password'],
    )
    if not utilisateur:
        return Response({"error": "Email ou mot de passe incorrect."}, status=401)

    if not utilisateur.is_verified:
        # Renvoyer un code (dans la limite anti-abus) et envoyer l'app
        # vers l'écran de vérification.
        if envoi_code_autorise(utilisateur, 'email'):
            envoyer_code_email(utilisateur, creer_code(utilisateur, 'email'))
        return Response({
            "error": "Compte non vérifié. Entrez le code envoyé à votre adresse email.",
            "code": "compte_non_verifie",
            "user_email": utilisateur.email,
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
@throttle_classes([CodesAuthThrottle])
def renvoyer_code_verification(request):
    """
    POST /auth/renvoyer-code/  {"email": "..."}
    Renvoie un code de vérification d'email (compte pas encore vérifié).
    """
    serializer = MotDePasseOublieSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)

    reponse = Response({"message": "Si ce compte attend une vérification, un code a été envoyé."})
    utilisateur = trouver_utilisateur(serializer.validated_data['email'])
    if not utilisateur or utilisateur.is_verified or not envoi_code_autorise(utilisateur, 'email'):
        return reponse

    envoyer_code_email(utilisateur, creer_code(utilisateur, 'email'))
    return reponse


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([CodesAuthThrottle])
def mot_de_passe_oublie(request):
    """
    POST /auth/mot-de-passe-oublie/
    Envoie un code de réinitialisation par email.
    """
    serializer = MotDePasseOublieSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)

    utilisateur = trouver_utilisateur(serializer.validated_data['email'])
    if not utilisateur:
        # Sécurité : ne pas révéler si l'email existe
        return Response({"message": "Si cet email existe, un code a été envoyé."})

    # Trace pour identifier l'origine des demandes non sollicitées
    logger.warning("[RESET] Demande de code pour %s depuis l'IP %s", utilisateur.email, ip_client(request))

    if not envoi_code_autorise(utilisateur, 'reset'):
        # Même réponse, sans nouvel email : on ne révèle pas la limite
        return Response({"message": "Si cet email existe, un code a été envoyé."})

    code_obj = creer_code(utilisateur, 'reset')
    envoyer_code_email(utilisateur, code_obj)

    return Response({"message": "Si cet email existe, un code a été envoyé."})


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([CodesAuthThrottle])
def reinitialiser_mot_de_passe(request):
    """
    POST /auth/reinitialiser-mot-de-passe/
    Réinitialise le mot de passe après vérification du code reset.
    """
    serializer = ReinitialisationMotDePasseSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)

    utilisateur = trouver_utilisateur(serializer.validated_data['email'])
    if not utilisateur:
        return Response({"error": "Utilisateur introuvable."}, status=404)

    erreur = controler_code(utilisateur, 'reset', serializer.validated_data['code'], consommer=True)
    if erreur:
        return Response({"error": erreur}, status=400)

    utilisateur.set_password(serializer.validated_data['password'])
    utilisateur.save()

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
            utilisateur = Utilisateur.objects.get(email__iexact=value)
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


def _map_statut_fedapay(statut_fedapay):
    if statut_fedapay == 'approved':
        return 'actif'
    if statut_fedapay in ('declined', 'canceled'):
        return 'annule'
    return 'en_attente'


def _activer_abonnement_paye(abo):
    """Active un abonnement 'en_attente' suite à un paiement FedaPay confirmé."""
    if abo.statut == 'actif':
        return
    Abonnement.objects.filter(
        utilisateur=abo.utilisateur, statut='actif'
    ).exclude(id=abo.id).update(statut='annule')
    abo.statut = 'actif'
    abo.debut  = date.today()
    abo.save(update_fields=['statut', 'debut'])


@api_view(['POST'])
@permission_classes([IsAuthenticated, EstVerifie])
def souscrire_abonnement(request):
    """
    POST /abonnements/souscrire/
    {"pack_cle": "pro"}
    Souscrit ou change de pack. Les packs payants déclenchent une transaction
    FedaPay : l'abonnement reste "en_attente" jusqu'à confirmation du paiement
    (webhook ou vérification via /abonnements/paiement/<id>/statut/).
    """
    serializer = SouscrireAbonnementSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)

    pack_cle = serializer.validated_data['pack_cle']

    try:
        pack = Pack.objects.get(cle=pack_cle, actif=True)
    except Pack.DoesNotExist:
        return Response({"error": "Pack introuvable."}, status=404)

    today = date.today()
    fin_periode_gratuite = None
    prochain_prelevement = None

    if pack.premier_mois_offert:
        fin_periode_gratuite = today + timedelta(days=30)
        prochain_prelevement = today + timedelta(days=60)
        fin = today + timedelta(days=365)
    else:
        fin = date(9999, 12, 31) if pack.cle == 'basic' else today + timedelta(days=30)

    # ── Pack gratuit : activation immédiate, aucun paiement requis ──
    if pack.prix_mensuel <= 0:
        Abonnement.objects.filter(
            utilisateur=request.user, statut='actif'
        ).update(statut='annule')

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

    # ── Pack payant : créer la transaction FedaPay et renvoyer le lien de paiement ──
    abo = Abonnement.objects.create(
        utilisateur=request.user,
        pack=pack,
        statut='en_attente',
        debut=today,
        fin=fin,
        fin_periode_gratuite=fin_periode_gratuite,
        prochain_prelevement=prochain_prelevement,
    )

    try:
        transaction = fedapay_client.creer_transaction(
            description=f"Abonnement MomoTrans — {pack.nom}",
            montant=pack.prix_mensuel,
            email=request.user.email,
            prenom=request.user.prenom,
            nom=request.user.nom,
            callback_url=request.build_absolute_uri(reverse('paiement_retour')),
        )
        lien = fedapay_client.generer_lien_paiement(transaction['id'])
    except fedapay_client.FedaPayError:
        abo.delete()
        return Response(
            {"error": "Impossible d'initier le paiement. Veuillez réessayer."}, status=502
        )

    abo.fedapay_transaction_id = str(transaction['id'])
    abo.save(update_fields=['fedapay_transaction_id'])

    return Response({
        "abonnement": AbonnementSerializer(abo).data,
        "paiement": {
            "transaction_id": transaction['id'],
            "montant": int(pack.prix_mensuel),
            "devise": "XOF",
            "payment_url": lien.get('url'),
        },
    }, status=201)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def statut_paiement(request, transaction_id):
    """
    GET /abonnements/paiement/<transaction_id>/statut/
    Filet de sécurité pour l'app mobile : si le webhook n'est pas (encore)
    configuré ou n'est pas encore arrivé, on interroge directement FedaPay.
    """
    abo = Abonnement.objects.filter(
        utilisateur=request.user, fedapay_transaction_id=str(transaction_id)
    ).first()
    if not abo:
        return Response({"error": "Paiement introuvable."}, status=404)

    if abo.statut == 'en_attente':
        try:
            transaction = fedapay_client.recuperer_transaction(transaction_id)
        except fedapay_client.FedaPayError:
            transaction = None

        if transaction:
            nouveau_statut = _map_statut_fedapay(transaction.get('status', ''))
            if nouveau_statut == 'actif':
                _activer_abonnement_paye(abo)
            elif nouveau_statut == 'annule':
                abo.statut = 'annule'
                abo.save(update_fields=['statut'])

    return Response(AbonnementSerializer(abo).data)


@api_view(['POST'])
@permission_classes([AllowAny])
def fedapay_webhook(request):
    """
    POST /abonnements/paiement/webhook/
    Endpoint appelé par FedaPay (à configurer dans Workbench → Webhooks avec
    l'URL publique de cet endpoint). Vérifie la signature avant tout traitement.
    """
    signature = request.headers.get('X-FEDAPAY-SIGNATURE', '')
    try:
        event = fedapay_sdk.Webhook.construct_event(
            request.body, signature, settings.FEDAPAY_WEBHOOK_SECRET,
        )
    except Exception:
        return Response({"error": "Signature invalide."}, status=400)

    entite = event.get('entity') or {}
    transaction_id = str(entite.get('id', ''))
    statut_fedapay = entite.get('status', '')

    abo = Abonnement.objects.filter(fedapay_transaction_id=transaction_id).first()
    if abo:
        nouveau_statut = _map_statut_fedapay(statut_fedapay)
        if nouveau_statut == 'actif':
            _activer_abonnement_paye(abo)
        elif nouveau_statut == 'annule' and abo.statut != 'annule':
            abo.statut = 'annule'
            abo.save(update_fields=['statut'])

    return Response({"received": True})


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

    ok, msg = _verifier_acces_boutique(boutique, request.user)
    if not ok:
        return Response({"error": msg}, status=403)

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
    # Basic est l'offre gratuite permanente : elle reste valable même si une
    # ancienne ligne d'abonnement est absente ou expirée.
    pack = abo.pack if abo else Pack.objects.filter(cle='basic', actif=True).first()
    if not pack:
        return False, "Aucune offre disponible pour le propriétaire."

    max_u = pack.max_utilisateurs
    if max_u == -1:
        return True, None

    # La limite du pack concerne les collaborateurs ajoutés. Le propriétaire,
    # créé automatiquement avec sa boutique, ne consomme pas cette place.
    nb = MembreBoutique.objects.filter(
        boutique=boutique, actif=True,
    ).exclude(role='proprietaire').count()
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

    ok, msg = _verifier_acces_boutique(boutique, request.user)
    if not ok:
        return Response({"error": msg}, status=403)

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
            nouveau = Utilisateur.objects.get(email__iexact=contact)
        else:
            nouveau = Utilisateur.objects.get(telephone=contact)
    except Utilisateur.DoesNotExist:
        return Response({"error": "Aucun compte trouvé pour ce contact."}, status=404)

    existant = MembreBoutique.objects.filter(boutique=boutique, utilisateur=nouveau).first()
    if existant:
        if existant.actif:
            return Response({"error": "Cet utilisateur est déjà membre de la boutique."}, status=409)
        # Ancien membre retiré : on réactive son adhésion plutôt que d'en créer
        # une nouvelle (contrainte unique_together sur boutique/utilisateur).
        existant.actif = True
        existant.role = serializer.validated_data['role']
        existant.save()
        return Response(MembreBoutiqueSerializer(existant).data, status=201)

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

    ok, msg = _verifier_acces_boutique(boutique, request.user)
    if not ok:
        return Response({"error": msg}, status=403)

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

    ok, msg = _verifier_acces_boutique(boutique, request.user)
    if not ok:
        return Response({"error": msg}, status=403)

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

def _impact_soldes(type_tx, operateur, montant, sens=1):
    """
    Retourne l'effet d'une transaction sur les soldes : {operateur: delta}.
    Dépôt/Crédit : opérateur -montant, caisse +montant
    Retrait      : caisse -montant, opérateur +montant
    Dépense      : source (opérateur ou caisse) -montant
    sens=-1 donne l'effet inverse (annulation).
    """
    m = sens * montant
    if type_tx in ('Depot', 'Credit'):
        return {operateur: -m, 'Caisse': m}
    if type_tx == 'Retrait':
        return {'Caisse': -m, operateur: m}
    if type_tx == 'Depense':
        return {operateur: -m}
    return {}


def _cumuler_impacts(*impacts):
    total = {}
    for impact in impacts:
        for op, delta in impact.items():
            total[op] = total.get(op, 0) + delta
    return total


def _fcfa(montant):
    return f"{montant:,.0f}".replace(',', ' ') + " FCFA"


def _appliquer_impact(boutique, impact):
    """
    Vérifie puis applique un impact sur les soldes (à appeler dans un bloc
    atomique). Aucun solde ne peut devenir négatif : si c'est le cas, rien
    n'est modifié et on retourne (message_erreur, None). Sinon on retourne
    (None, soldes_avant) où soldes_avant = {operateur: montant} sert aux alertes.
    """
    soldes = {
        s.operateur: s
        for s in SoldeOperateur.objects.select_for_update().filter(
            boutique=boutique, operateur__in=list(impact.keys()),
        )
    }
    for op, delta in impact.items():
        actuel = soldes[op].montant if op in soldes else 0
        if delta < 0 and actuel + delta < 0:
            return f"Solde {op} insuffisant (disponible : {_fcfa(actuel)}).", None

    avant = {}
    for op, delta in impact.items():
        if not delta:
            continue
        solde = soldes.get(op) or SoldeOperateur.objects.create(boutique=boutique, operateur=op)
        avant[op] = solde.montant
        solde.montant += delta
        solde.save()
    return None, avant


def _alerter(boutique, titre, message):
    """Notifie tous les membres actifs de la boutique (propriétaire compris)."""
    destinataires = Utilisateur.objects.filter(
        memberships__boutique=boutique, memberships__actif=True,
    ).distinct()
    for utilisateur in destinataires:
        creer_notification(utilisateur, 'alerte', titre, message, boutique=boutique)


def _verifier_alertes(boutique, soldes_avant, transaction=None):
    """
    Alertes intelligentes :
    - solde bas : un solde vient de passer sous le seuil de la boutique ;
    - opération anormale : montant >= 3x la moyenne des 30 derniers jours
      pour ce type d'opération (au moins 5 opérations de référence).
    """
    seuil = boutique.seuil_alerte_solde
    if seuil and seuil > 0:
        for solde in SoldeOperateur.objects.filter(boutique=boutique, operateur__in=list(soldes_avant.keys())):
            if soldes_avant[solde.operateur] >= seuil > solde.montant:
                _alerter(
                    boutique,
                    f"Solde {solde.operateur} bas",
                    f"Le solde {solde.operateur} de {boutique.nom} est de {_fcfa(solde.montant)}, "
                    f"sous le seuil d'alerte de {_fcfa(seuil)}.",
                )

    if transaction is None:
        return
    reference = Transaction.objects.filter(
        boutique=boutique, type=transaction.type, archivee=False,
        date__gte=timezone.now() - timedelta(days=30),
    ).exclude(id=transaction.id)
    if reference.count() < 5:
        return
    moyenne = reference.aggregate(m=Avg('montant'))['m'] or 0
    if moyenne > 0 and transaction.montant >= 3 * moyenne:
        _alerter(
            boutique,
            "Opération inhabituelle détectée",
            f"{transaction.get_type_display()} {transaction.operateur} de {_fcfa(transaction.montant)}, "
            f"soit {transaction.montant / moyenne:.1f}x la moyenne des 30 derniers jours "
            f"({_fcfa(moyenne)}).",
        )


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

    ok, msg = _verifier_acces_boutique(boutique, request.user)
    if not ok:
        return Response({"error": msg}, status=403)

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
        # Bornes explicites dans le fuseau du Bénin : les lookups `date__date`
        # ne renvoient rien sous MySQL sans tables de fuseaux horaires.
        benin_tz = timezone.get_current_timezone()
        try:
            if date_debut:
                start = timezone.make_aware(datetime.combine(date.fromisoformat(date_debut), time.min), benin_tz)
                qs = qs.filter(date__gte=start)
            if date_fin:
                end = timezone.make_aware(
                    datetime.combine(date.fromisoformat(date_fin) + timedelta(days=1), time.min), benin_tz,
                )
                qs = qs.filter(date__lt=end)
        except ValueError:
            return Response({"error": "Les dates doivent être au format AAAA-MM-JJ."}, status=400)
        if search:
            qs = qs.filter(
                Q(numero_client__icontains=search) |
                Q(nom_client__icontains=search) |
                Q(reference__icontains=search) |
                Q(motif__icontains=search) |
                Q(operateur__icontains=search)
            )

        return Response(TransactionSerializer(qs, many=True).data)

    # POST
    serializer = TransactionSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)

    v = serializer.validated_data
    with db_transaction.atomic():
        erreur, soldes_avant = _appliquer_impact(
            boutique, _impact_soldes(v['type'], v['operateur'], v['montant']),
        )
        if erreur:
            return Response({"error": erreur}, status=400)
        transaction = serializer.save(boutique=boutique, effectuee_par=request.user)

    creer_notification(
        utilisateur=request.user,
        type_notif={'Depot': 'depot', 'Retrait': 'retrait', 'Depense': 'depense'}.get(transaction.type, 'credit'),
        titre=f"Une opération de {transaction.get_type_display().lower()} a été effectuée",
        boutique=boutique,
    )
    _verifier_alertes(boutique, soldes_avant, transaction)

    return Response(TransactionSerializer(transaction).data, status=201)


@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated, EstVerifie])
def detail_transaction(request, boutique_id, transaction_id):
    """
    GET    /boutiques/<boutique_id>/transactions/<transaction_id>/
    PATCH  /boutiques/<boutique_id>/transactions/<transaction_id>/
    PATCH avec {"archivee": true|false} archive ou restaure l'opération.
    """
    try:
        boutique = Boutique.objects.get(id=boutique_id)
        transaction = Transaction.objects.get(id=transaction_id, boutique=boutique)
    except (Boutique.DoesNotExist, Transaction.DoesNotExist):
        return Response({"error": "Introuvable."}, status=404)

    ok, msg = _verifier_acces_boutique(boutique, request.user)
    if not ok:
        return Response({"error": msg}, status=403)

    if request.method == 'GET':
        return Response(TransactionSerializer(transaction).data)

    if request.method == 'PATCH':
        # Une archive reste visible, mais annule son impact sur les soldes.
        # Désarchiver réalise strictement l'opération inverse.
        if 'archivee' in request.data:
            archivee = bool(request.data['archivee'])
            if transaction.archivee != archivee:
                with db_transaction.atomic():
                    erreur, soldes_avant = _appliquer_impact(boutique, _impact_soldes(
                        transaction.type, transaction.operateur, transaction.montant,
                        sens=-1 if archivee else 1,
                    ))
                    if erreur:
                        return Response({"error": erreur}, status=400)
                    transaction.archivee = archivee
                    transaction.archivee_le = timezone.now() if archivee else None
                    transaction.save(update_fields=['archivee', 'archivee_le', 'mis_a_jour'])
                _verifier_alertes(boutique, soldes_avant)
                creer_notification(
                    utilisateur=request.user,
                    type_notif='modification',
                    titre=("Transaction archivée : son effet sur les soldes a été annulé."
                           if archivee else "Transaction désarchivée : son effet sur les soldes a été restauré."),
                    boutique=boutique,
                )
            return Response(TransactionSerializer(transaction).data)

        if transaction.archivee:
            return Response({"error": "Restaurez la transaction avant de la modifier."}, status=400)

        # Capturer l'impact actuel avant modification, pour pouvoir l'annuler
        ancien_type, ancien_operateur, ancien_montant = (
            transaction.type, transaction.operateur, transaction.montant
        )

        serializer = TransactionSerializer(transaction, data=request.data, partial=True)
        if serializer.is_valid():
            v = serializer.validated_data
            # Annuler l'ancien impact puis appliquer le nouveau (gère aussi
            # le cas où le type/l'opérateur/le montant ont changé).
            impact = _cumuler_impacts(
                _impact_soldes(ancien_type, ancien_operateur, ancien_montant, sens=-1),
                _impact_soldes(
                    v.get('type', ancien_type), v.get('operateur', ancien_operateur),
                    v.get('montant', ancien_montant),
                ),
            )
            with db_transaction.atomic():
                erreur, soldes_avant = _appliquer_impact(boutique, impact)
                if erreur:
                    return Response({"error": erreur}, status=400)
                serializer.save()
            _verifier_alertes(boutique, soldes_avant)

            creer_notification(
                utilisateur=request.user,
                type_notif='modification',
                titre=f"Une modification a été effectuée sur une opération d'achat de crédit / Forfait"
                    if transaction.type == 'Credit'
                    else f"Modification d'une opération de {transaction.get_type_display().lower()}",
                boutique=boutique,
            )
            return Response(serializer.data)
        return Response(serializer.errors, status=400)

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

    ok, msg = _verifier_acces_boutique(boutique, request.user)
    if not ok:
        return Response({"error": msg}, status=403)

    selected_date, error_response = _date_rapport(request)
    if error_response:
        return error_response
    qs = _transactions_du_jour(boutique, selected_date)

    totaux = qs.aggregate(
        total_depots=Sum('montant', filter=Q(type='Depot')),
        total_retraits=Sum('montant', filter=Q(type='Retrait')),
        total_credits=Sum('montant', filter=Q(type='Credit')),
        total_depenses=Sum('montant', filter=Q(type='Depense')),
    )

    total_depots     = totaux['total_depots']     or 0
    total_retraits   = totaux['total_retraits']   or 0
    total_credits    = totaux['total_credits']    or 0
    total_depenses   = totaux['total_depenses']   or 0

    return Response({
        "date": selected_date.isoformat(),
        "boutique": str(boutique_id),
        "total_depots": total_depots,
        "total_retraits": total_retraits,
        "total_credits": total_credits,
        "total_depenses": total_depenses,
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

    ok, msg = _verifier_acces_boutique(boutique, request.user)
    if not ok:
        return Response({"error": msg}, status=403)

    ok, msg = _verifier_acces_rapports(request.user, boutique)
    if not ok:
        return Response({"error": msg}, status=403)

    selected_date, error_response = _date_rapport(request)
    if error_response:
        return error_response
    operateurs = ['MTN', 'Moov', 'Celtiis']
    rapport = []

    for op in operateurs:
        qs = _transactions_du_jour(boutique, selected_date).filter(operateur=op)
        totaux = qs.aggregate(
            total_depots=Sum('montant', filter=Q(type='Depot')),
            total_retraits=Sum('montant', filter=Q(type='Retrait')),
            total_credits=Sum('montant', filter=Q(type='Credit')),
            total_commissions=Sum('commission'),
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
            "total_commissions": totaux['total_commissions'] or 0,
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

    ok, msg = _verifier_acces_boutique(boutique, request.user)
    if not ok:
        return Response({"error": msg}, status=403)

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

    ok, msg = _verifier_acces_boutique(boutique, request.user)
    if not ok:
        return Response({"error": msg}, status=403)

    ok, msg = _verifier_acces_rapports(request.user, boutique)
    if not ok:
        return Response({"error": msg}, status=403)

    # Le client mobile envoie une seule date. Les deux paramètres historiques
    # restent acceptés afin de ne pas casser les anciennes versions.
    if request.query_params.get('date'):
        selected_date, error_response = _date_rapport(request)
        if error_response:
            return error_response
        qs = _transactions_du_jour(boutique, selected_date)
    else:
        date_debut, error_response = _date_rapport(request, 'date_debut')
        if error_response:
            return error_response
        date_fin, error_response = _date_rapport(request, 'date_fin')
        if error_response:
            return error_response
        if date_debut > date_fin:
            return Response({"error": "La date de début doit précéder la date de fin."}, status=400)
        benin_tz = timezone.get_current_timezone()
        start = timezone.make_aware(datetime.combine(date_debut, time.min), benin_tz)
        end = timezone.make_aware(datetime.combine(date_fin + timedelta(days=1), time.min), benin_tz)
        qs = Transaction.objects.filter(
            boutique=boutique, archivee=False, date__gte=start, date__lt=end,
        )

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
 
