from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password
from .models import *


# ══════════════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════════════

class EmailNormaliseField(serializers.EmailField):
    """Email sans espaces et en minuscules : « Jean@Mail.com » = « jean@mail.com »."""
    def to_internal_value(self, data):
        return super().to_internal_value(str(data).strip()).lower()


class InscriptionSerializer(serializers.ModelSerializer):
    email     = EmailNormaliseField()
    password  = serializers.CharField(write_only=True, validators=[validate_password])
    password2 = serializers.CharField(write_only=True)

    class Meta:
        model  = Utilisateur
        fields = ['email', 'nom', 'prenom', 'telephone', 'password', 'password2']

    def validate_email(self, value):
        if Utilisateur.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError(
                "Un compte existe déjà avec cet email. Connectez-vous : si le compte "
                "n'est pas encore vérifié, un nouveau code vous sera envoyé."
            )
        return value

    def validate(self, data):
        if data['password'] != data['password2']:
            raise serializers.ValidationError({"password": "Les mots de passe ne correspondent pas."})
        return data

    def create(self, validated_data):
        validated_data.pop('password2')
        return Utilisateur.objects.create_user(**validated_data)


class ConnexionSerializer(serializers.Serializer):
    email    = EmailNormaliseField()
    password = serializers.CharField(write_only=True)


class VerificationCodeSerializer(serializers.Serializer):
    email = EmailNormaliseField()
    code  = serializers.CharField(max_length=6)
    type  = serializers.ChoiceField(choices=['email', 'reset'])


class MotDePasseOublieSerializer(serializers.Serializer):
    email = EmailNormaliseField()


class ReinitialisationMotDePasseSerializer(serializers.Serializer):
    email     = EmailNormaliseField()
    code      = serializers.CharField(max_length=6)
    password  = serializers.CharField(write_only=True, validators=[validate_password])
    password2 = serializers.CharField(write_only=True)

    def validate(self, data):
        if data['password'] != data['password2']:
            raise serializers.ValidationError({"password": "Les mots de passe ne correspondent pas."})
        return data


class UtilisateurSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Utilisateur
        fields = ['id', 'email', 'nom', 'prenom', 'telephone', 'is_verified', 'date_inscription']
        read_only_fields = ['id', 'is_verified', 'date_inscription']


class RechercheUtilisateurSerializer(serializers.ModelSerializer):
    """Utilisé pour la recherche lors de l'ajout d'un membre boutique."""
    class Meta:
        model  = Utilisateur
        fields = ['id', 'email', 'telephone', 'nom', 'prenom']


# ══════════════════════════════════════════════════════════════════════
# PACK & ABONNEMENT
# ══════════════════════════════════════════════════════════════════════

class PackSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Pack
        fields = '__all__'


class AbonnementSerializer(serializers.ModelSerializer):
    pack_detail = PackSerializer(source='pack', read_only=True)
    est_actif   = serializers.SerializerMethodField()
    est_illimite = serializers.SerializerMethodField()

    class Meta:
        model  = Abonnement
        fields = [
            'id', 'pack', 'pack_detail', 'statut',
            'debut', 'fin', 'fin_periode_gratuite',
            'prochain_prelevement', 'est_actif', 'est_illimite', 'cree_le',
        ]
        read_only_fields = ['id', 'utilisateur', 'cree_le']

    def get_est_actif(self, obj):
        return obj.est_actif()

    def get_est_illimite(self, obj):
        return obj.pack.cle == 'basic'


class SouscrireAbonnementSerializer(serializers.Serializer):
    pack_cle = serializers.ChoiceField(choices=['basic', 'pro', 'premium'])


# ══════════════════════════════════════════════════════════════════════
# BOUTIQUE
# ══════════════════════════════════════════════════════════════════════

class BoutiqueSerializer(serializers.ModelSerializer):
    proprietaire_nom = serializers.SerializerMethodField()

    class Meta:
        model  = Boutique
        fields = [
            'id', 'nom', 'couleur', 'devise', 'adresse',
            'latitude', 'longitude', 'active', 'seuil_alerte_solde',
            'proprietaire', 'proprietaire_nom', 'cree_le',
        ]
        read_only_fields = ['id', 'proprietaire', 'cree_le']
        # 0 désactive l'alerte « solde bas »
        extra_kwargs = {'seuil_alerte_solde': {'min_value': 0}}

    def get_proprietaire_nom(self, obj):
        return f"{obj.proprietaire.prenom} {obj.proprietaire.nom}"


class MembreBoutiqueSerializer(serializers.ModelSerializer):
    utilisateur_detail = RechercheUtilisateurSerializer(source='utilisateur', read_only=True)

    class Meta:
        model  = MembreBoutique
        fields = ['id', 'utilisateur', 'utilisateur_detail', 'role', 'actif', 'ajoute_le']
        read_only_fields = ['id', 'ajoute_le']


class AjouterMembreSerializer(serializers.Serializer):
    email = serializers.EmailField(required=False)
    telephone = serializers.CharField(required=False, allow_blank=True)
    identifiant = serializers.CharField(required=False, allow_blank=True)
    role  = serializers.ChoiceField(choices=['gerant', 'admin'])

    def validate(self, data):
        contact = (
            data.get('identifiant')
            or data.get('email')
            or data.get('telephone')
        )
        if not contact:
            raise serializers.ValidationError({
                'identifiant': "Veuillez fournir un email ou un numéro de téléphone."
            })
        data['contact'] = contact.strip()
        return data


# ══════════════════════════════════════════════════════════════════════
# SOLDE
# ══════════════════════════════════════════════════════════════════════

class SoldeOperateurSerializer(serializers.ModelSerializer):
    class Meta:
        model  = SoldeOperateur
        fields = ['id', 'operateur', 'montant', 'mis_a_jour']
        read_only_fields = ['id', 'mis_a_jour']


class MiseAJourSoldesSerializer(serializers.Serializer):
    """Payload : {"MTN": 50000, "Moov": 20000, ...}"""
    # Un solde ne peut jamais être négatif
    MTN     = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, min_value=0)
    Moov    = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, min_value=0)
    Celtiis = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, min_value=0)
    Caisse  = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, min_value=0)


# ══════════════════════════════════════════════════════════════════════
# TRANSACTION
# ══════════════════════════════════════════════════════════════════════

class TransactionSerializer(serializers.ModelSerializer):
    effectuee_par_nom = serializers.SerializerMethodField()

    class Meta:
        model  = Transaction
        fields = [
            'id', 'type', 'operateur', 'montant', 'commission',
            'numero_client', 'nom_client', 'reference', 'remarque',
            'motif', 'montant_remis', 'monnaie_rendue', 'type_credit', 'date',
            'archivee', 'archivee_le',
            'effectuee_par', 'effectuee_par_nom',
            'boutique', 'cree_le',
        ]
        read_only_fields = ['id', 'boutique', 'effectuee_par', 'cree_le', 'archivee_le']
        extra_kwargs = {
            'montant_remis':  {'min_value': 0},
            'monnaie_rendue': {'min_value': 0},
        }

    def get_effectuee_par_nom(self, obj):
        if obj.effectuee_par:
            return f"{obj.effectuee_par.prenom} {obj.effectuee_par.nom}"
        return None

    def validate_montant(self, value):
        if value <= 0:
            raise serializers.ValidationError("Le montant doit être supérieur à 0.")
        return value

    def validate(self, data):
        # En PATCH partiel, compléter avec les valeurs actuelles de l'instance
        def champ(nom):
            if nom in data:
                return data[nom]
            return getattr(self.instance, nom, None) if self.instance else None

        type_tx = champ('type')
        if type_tx == 'Credit' and not champ('type_credit'):
            raise serializers.ValidationError({"type_credit": "Requis pour un achat de crédit/forfait."})

        # Montant remis / monnaie rendue : uniquement pour Dépôt et Crédit
        if type_tx not in ('Depot', 'Credit'):
            data['montant_remis'] = None
            data['monnaie_rendue'] = None

        if type_tx == 'Depense':
            if not (champ('motif') or '').strip():
                raise serializers.ValidationError({"motif": "Le motif de la dépense est requis."})
        else:
            if champ('operateur') == 'Caisse':
                raise serializers.ValidationError({"operateur": "Opérateur invalide pour ce type d'opération."})
            if not (champ('numero_client') or '').strip():
                raise serializers.ValidationError({"numero_client": "Le numéro du client est requis."})
        return data


# ══════════════════════════════════════════════════════════════════════
# RAPPORT
# ══════════════════════════════════════════════════════════════════════

class RapportJournalierSerializer(serializers.ModelSerializer):
    class Meta:
        model  = RapportJournalier
        fields = '__all__'
        read_only_fields = ['id', 'boutique', 'cree_le']


class RapportOperateurSerializer(serializers.Serializer):
    """Calculé à la volée depuis les transactions."""
    operateur          = serializers.CharField()
    total_depots       = serializers.DecimalField(max_digits=14, decimal_places=2)
    total_retraits     = serializers.DecimalField(max_digits=14, decimal_places=2)
    total_credits      = serializers.DecimalField(max_digits=14, decimal_places=2)
    total_commissions  = serializers.DecimalField(max_digits=14, decimal_places=2)
    solde_restant      = serializers.DecimalField(max_digits=14, decimal_places=2)


class RapportCommissionsSerializer(serializers.Serializer):
    operateur         = serializers.CharField()
    total_commissions = serializers.DecimalField(max_digits=14, decimal_places=2)


class SoldeGlobalSerializer(serializers.Serializer):
    operateur = serializers.CharField()
    montant   = serializers.DecimalField(max_digits=14, decimal_places=2)

class AvisSerializer(serializers.ModelSerializer):
    utilisateur_email = serializers.SerializerMethodField()

    class Meta:
        model  = Avis
        fields = ['id', 'utilisateur_email', 'message', 'lu', 'cree_le']
        read_only_fields = ['id', 'utilisateur_email', 'lu', 'cree_le']

    def get_utilisateur_email(self, obj):
        return obj.utilisateur.email if obj.utilisateur else None
class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Notification
        fields = ['id', 'type', 'titre', 'message', 'lu', 'cree_le', 'boutique']
        read_only_fields = ['id', 'cree_le']
 
