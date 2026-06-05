import uuid
from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.utils import timezone


# ══════════════════════════════════════════════════════════════════════
# MANAGER UTILISATEUR
# ══════════════════════════════════════════════════════════════════════

class UtilisateurManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("L'email est obligatoire")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_verified', True)
        return self.create_user(email, password, **extra_fields)


# ══════════════════════════════════════════════════════════════════════
# UTILISATEUR PERSONNALISÉ
# ══════════════════════════════════════════════════════════════════════

class Utilisateur(AbstractBaseUser, PermissionsMixin):
    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email       = models.EmailField(unique=True)
    nom         = models.CharField(max_length=100)
    prenom      = models.CharField(max_length=100)
    telephone   = models.CharField(max_length=20, blank=True)
    is_active   = models.BooleanField(default=True)
    is_staff    = models.BooleanField(default=False)
    is_verified = models.BooleanField(default=False)  # email vérifié
    date_inscription = models.DateTimeField(auto_now_add=True)

    USERNAME_FIELD  = 'email'
    REQUIRED_FIELDS = ['nom', 'prenom']

    objects = UtilisateurManager()

    class Meta:
        verbose_name = "Utilisateur"

    def __str__(self):
        return f"{self.prenom} {self.nom} ({self.email})"


# ══════════════════════════════════════════════════════════════════════
# CODE DE VÉRIFICATION (email + reset mot de passe)
# ══════════════════════════════════════════════════════════════════════

class CodeVerification(models.Model):
    TYPE_CHOICES = [
        ('email',  'Vérification email'),
        ('reset',  'Réinitialisation mot de passe'),
    ]
    utilisateur = models.ForeignKey(Utilisateur, on_delete=models.CASCADE, related_name='codes')
    code        = models.CharField(max_length=6)
    type        = models.CharField(max_length=10, choices=TYPE_CHOICES)
    expire_le   = models.DateTimeField()
    utilise     = models.BooleanField(default=False)
    cree_le     = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-cree_le']

    def est_valide(self):
        return not self.utilise and self.expire_le > timezone.now()

    def __str__(self):
        return f"Code {self.type} pour {self.utilisateur.email}"


# ══════════════════════════════════════════════════════════════════════
# ABONNEMENT / PACK
# ══════════════════════════════════════════════════════════════════════

class Pack(models.Model):
    CLE_CHOICES = [
        ('basic',   'Basic'),
        ('pro',     'Pro'),
        ('premium', 'Premium'),
    ]
    cle              = models.CharField(max_length=20, choices=CLE_CHOICES, unique=True)
    nom              = models.CharField(max_length=100)
    prix_mensuel     = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    max_boutiques    = models.IntegerField(default=1)   # -1 = illimité
    max_utilisateurs = models.IntegerField(default=1)   # -1 = illimité
    rapports_avances = models.BooleanField(default=False)
    premier_mois_offert = models.BooleanField(default=False)
    description      = models.TextField(blank=True)
    actif            = models.BooleanField(default=True)

    def __str__(self):
        return self.nom


class Abonnement(models.Model):
    STATUT_CHOICES = [
        ('actif',     'Actif'),
        ('expire',    'Expiré'),
        ('annule',    'Annulé'),
        ('en_attente','En attente de paiement'),
    ]
    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    utilisateur  = models.ForeignKey(Utilisateur, on_delete=models.CASCADE, related_name='abonnements')
    pack         = models.ForeignKey(Pack, on_delete=models.PROTECT)
    statut       = models.CharField(max_length=20, choices=STATUT_CHOICES, default='en_attente')
    debut        = models.DateField()
    fin          = models.DateField()
    fin_periode_gratuite = models.DateField(null=True, blank=True)
    prochain_prelevement = models.DateField(null=True, blank=True)
    cree_le      = models.DateTimeField(auto_now_add=True)
    mis_a_jour   = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-cree_le']

    def est_actif(self):
        from datetime import date
        return self.statut == 'actif' and self.fin >= date.today()

    def __str__(self):
        return f"{self.utilisateur.email} — {self.pack.nom} ({self.statut})"


# ══════════════════════════════════════════════════════════════════════
# BOUTIQUE
# ══════════════════════════════════════════════════════════════════════

class Boutique(models.Model):
    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    proprietaire = models.ForeignKey(Utilisateur, on_delete=models.CASCADE, related_name='boutiques')
    nom          = models.CharField(max_length=200)
    couleur      = models.CharField(max_length=30, default='#3B82F6')
    devise       = models.CharField(max_length=10, default='XOF')
    adresse      = models.CharField(max_length=300, blank=True)
    latitude     = models.FloatField(null=True, blank=True)
    longitude    = models.FloatField(null=True, blank=True)
    active       = models.BooleanField(default=True)
    cree_le      = models.DateTimeField(auto_now_add=True)
    mis_a_jour   = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.nom} — {self.proprietaire.email}"


class MembreBoutique(models.Model):
    ROLE_CHOICES = [
        ('proprietaire', 'Propriétaire'),
        ('gerant',       'Gérant'),
        ('admin',        'Administrateur'),
    ]
    boutique    = models.ForeignKey(Boutique, on_delete=models.CASCADE, related_name='membres')
    utilisateur = models.ForeignKey(Utilisateur, on_delete=models.CASCADE, related_name='memberships')
    role        = models.CharField(max_length=20, choices=ROLE_CHOICES, default='gerant')
    actif       = models.BooleanField(default=True)
    ajoute_le   = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('boutique', 'utilisateur')

    def __str__(self):
        return f"{self.utilisateur.email} → {self.boutique.nom} ({self.role})"


# ══════════════════════════════════════════════════════════════════════
# SOLDE PAR OPÉRATEUR
# ══════════════════════════════════════════════════════════════════════

class SoldeOperateur(models.Model):
    OPERATEUR_CHOICES = [
        ('MTN',     'MTN'),
        ('Moov',    'Moov'),
        ('Celtiis', 'Celtiis'),
        ('Caisse',  'Caisse'),
    ]
    boutique   = models.ForeignKey(Boutique, on_delete=models.CASCADE, related_name='soldes')
    operateur  = models.CharField(max_length=20, choices=OPERATEUR_CHOICES)
    montant    = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    mis_a_jour = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('boutique', 'operateur')

    def __str__(self):
        return f"{self.boutique.nom} — {self.operateur} : {self.montant}"


# ══════════════════════════════════════════════════════════════════════
# TRANSACTION
# ══════════════════════════════════════════════════════════════════════

class Transaction(models.Model):
    TYPE_CHOICES = [
        ('Depot',   'Dépôt'),
        ('Retrait', 'Retrait'),
        ('Credit',  'Achat de crédit/forfait'),
    ]
    OPERATEUR_CHOICES = [
        ('MTN',     'MTN'),
        ('Moov',    'Moov'),
        ('Celtiis', 'Celtiis'),
    ]
    TYPE_CREDIT_CHOICES = [
        ('Credit',  'Crédit'),
        ('Forfait', 'Forfait'),
    ]

    id             = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    boutique       = models.ForeignKey(Boutique, on_delete=models.CASCADE, related_name='transactions')
    effectuee_par  = models.ForeignKey(Utilisateur, on_delete=models.SET_NULL, null=True, related_name='transactions')
    type           = models.CharField(max_length=10, choices=TYPE_CHOICES)
    operateur      = models.CharField(max_length=20, choices=OPERATEUR_CHOICES)
    montant        = models.DecimalField(max_digits=14, decimal_places=2)
    commission     = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    numero_client  = models.CharField(max_length=30)
    nom_client     = models.CharField(max_length=200, blank=True)
    reference      = models.CharField(max_length=100, blank=True)
    remarque       = models.TextField(blank=True)
    # Champ spécifique Crédit
    type_credit    = models.CharField(max_length=10, choices=TYPE_CREDIT_CHOICES, blank=True)
    date           = models.DateTimeField(default=timezone.now)
    cree_le        = models.DateTimeField(auto_now_add=True)
    mis_a_jour     = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date']

    def __str__(self):
        return f"{self.type} — {self.operateur} — {self.montant} ({self.boutique.nom})"


# ══════════════════════════════════════════════════════════════════════
# RAPPORT (snapshot quotidien sauvegardé)
# ══════════════════════════════════════════════════════════════════════

class RapportJournalier(models.Model):
    id              = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    boutique        = models.ForeignKey(Boutique, on_delete=models.CASCADE, related_name='rapports')
    date            = models.DateField()
    total_depots    = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_retraits  = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_credits   = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_transferts= models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_commissions = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    benefice_net    = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    cree_le         = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('boutique', 'date')
        ordering = ['-date']

    def __str__(self):
        return f"Rapport {self.boutique.nom} — {self.date}"
    
# ══════════════════════════════════════════════════════════════════════
# AVIS UTILISATEUR
# ══════════════════════════════════════════════════════════════════════

class Avis(models.Model):
    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    utilisateur = models.ForeignKey(
        Utilisateur,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='avis',
    )
    message     = models.TextField()
    lu          = models.BooleanField(default=False)   # marqué lu par un admin
    cree_le     = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-cree_le']
        verbose_name = "Avis"
        verbose_name_plural = "Avis"

    def __str__(self):
        auteur = self.utilisateur.email if self.utilisateur else "Anonyme"
        return f"Avis de {auteur} — {self.cree_le:%d/%m/%Y}"
    
# ══════════════════════════════════════════════════════════════════════
# NOTIFICATION
# À ajouter à la fin de models.py
# ══════════════════════════════════════════════════════════════════════

class Notification(models.Model):
    TYPE_CHOICES = [
        ('suppression', 'Suppression'),
        ('modification', 'Modification'),
        ('retrait',      'Retrait effectué'),
        ('depot',        'Dépôt effectué'),
        ('credit',       'Achat de crédit'),
        ('export',       'Export PDF'),
        ('info',         'Information'),
    ]

    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    utilisateur  = models.ForeignKey(
        Utilisateur, on_delete=models.CASCADE, related_name='notifications'
    )
    boutique     = models.ForeignKey(
        Boutique, on_delete=models.CASCADE, related_name='notifications', null=True, blank=True
    )
    type         = models.CharField(max_length=20, choices=TYPE_CHOICES, default='info')
    titre        = models.CharField(max_length=255)
    message      = models.TextField(blank=True)
    lu           = models.BooleanField(default=False)
    cree_le      = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-cree_le']
        verbose_name = "Notification"
        verbose_name_plural = "Notifications"

    def __str__(self):
        return f"[{self.type}] {self.titre} → {self.utilisateur.email}"