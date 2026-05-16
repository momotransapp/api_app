from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password
from .models import (
    Utilisateur, CodeVerification, Pack, Abonnement,
    Boutique, MembreBoutique, SoldeOperateur, Transaction, RapportJournalier,
)


# ══════════════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════════════

class InscriptionSerializer(serializers.ModelSerializer):
    password  = serializers.CharField(write_only=True, validators=[validate_password])
    password2 = serializers.CharField(write_only=True)

    class Meta:
        model  = Utilisateur
        fields = ['email', 'nom', 'prenom', 'telephone', 'password', 'password2']

    def validate(self, data):
        if data['password'] != data['password2']:
            raise serializers.ValidationError({"password": "Les mots de passe ne correspondent pas."})
        return data

    def create(self, validated_data):
        validated_data.pop('password2')
        return Utilisateur.objects.create_user(**validated_data)


class ConnexionSerializer(serializers.Serializer):
    email    = serializers.EmailField()
    password = serializers.CharField(write_only=True)


class VerificationCodeSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code  = serializers.CharField(max_length=6)
    type  = serializers.ChoiceField(choices=['email', 'reset'])


class MotDePasseOublieSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ReinitialisationMotDePasseSerializer(serializers.Serializer):
    email     = serializers.EmailField()
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
        fields = ['id', 'email', 'nom', 'prenom']


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

    class Meta:
        model  = Abonnement
        fields = [
            'id', 'pack', 'pack_detail', 'statut',
            'debut', 'fin', 'fin_periode_gratuite',
            'prochain_prelevement', 'est_actif', 'cree_le',
        ]
        read_only_fields = ['id', 'utilisateur', 'cree_le']

    def get_est_actif(self, obj):
        return obj.est_actif()


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
            'latitude', 'longitude', 'active',
            'proprietaire', 'proprietaire_nom', 'cree_le',
        ]
        read_only_fields = ['id', 'proprietaire', 'cree_le']

    def get_proprietaire_nom(self, obj):
        return f"{obj.proprietaire.prenom} {obj.proprietaire.nom}"


class MembreBoutiqueSerializer(serializers.ModelSerializer):
    utilisateur_detail = RechercheUtilisateurSerializer(source='utilisateur', read_only=True)

    class Meta:
        model  = MembreBoutique
        fields = ['id', 'utilisateur', 'utilisateur_detail', 'role', 'actif', 'ajoute_le']
        read_only_fields = ['id', 'ajoute_le']


class AjouterMembreSerializer(serializers.Serializer):
    email = serializers.EmailField()
    role  = serializers.ChoiceField(choices=['gerant', 'admin'])


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
    MTN     = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    Moov    = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    Celtiis = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    Caisse  = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)


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
            'type_credit', 'date',
            'effectuee_par', 'effectuee_par_nom',
            'boutique', 'cree_le',
        ]
        read_only_fields = ['id', 'boutique', 'effectuee_par', 'cree_le']

    def get_effectuee_par_nom(self, obj):
        if obj.effectuee_par:
            return f"{obj.effectuee_par.prenom} {obj.effectuee_par.nom}"
        return None

    def validate(self, data):
        if data.get('type') == 'Credit' and not data.get('type_credit'):
            raise serializers.ValidationError({"type_credit": "Requis pour un achat de crédit/forfait."})
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