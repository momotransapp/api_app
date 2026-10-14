from datetime import datetime, timedelta, time
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import (
    Utilisateur, Boutique, MembreBoutique, SoldeOperateur, Transaction, Notification, Pack, Abonnement,
)


class TransactionsTests(TestCase):
    def setUp(self):
        pack = Pack.objects.create(cle='basic', nom='Basic', max_boutiques=1, max_utilisateurs=1)
        self.user = Utilisateur.objects.create_user(
            'test@example.com', 'motdepasse123', nom='Doe', prenom='Jane', is_verified=True,
        )
        Abonnement.objects.create(
            utilisateur=self.user, pack=pack, statut='actif',
            debut=timezone.localdate(), fin=timezone.localdate() + timedelta(days=365),
        )
        self.boutique = Boutique.objects.create(proprietaire=self.user, nom='Shop', seuil_alerte_solde=10000)
        MembreBoutique.objects.create(boutique=self.boutique, utilisateur=self.user, role='proprietaire')
        for op, montant in [('MTN', 50000), ('Moov', 0), ('Celtiis', 0), ('Caisse', 20000)]:
            SoldeOperateur.objects.create(boutique=self.boutique, operateur=op, montant=montant)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = f'/api/boutiques/{self.boutique.id}/transactions/'

    def solde(self, op):
        return SoldeOperateur.objects.get(boutique=self.boutique, operateur=op).montant

    def post(self, **data):
        return self.client.post(self.url, data, format='json')

    def test_depense_debite_la_source(self):
        r = self.post(type='Depense', operateur='Caisse', montant=5000, motif='Électricité')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['motif'], 'Électricité')
        self.assertEqual(self.solde('Caisse'), Decimal('15000'))
        self.assertEqual(self.solde('MTN'), Decimal('50000'))

    def test_montant_remis_informatif(self):
        r = self.post(type='Depot', operateur='MTN', montant=4500, numero_client='97000000',
                      montant_remis=5000, monnaie_rendue=500)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(r.data['montant_remis']), Decimal('5000'))
        self.assertEqual(Decimal(r.data['monnaie_rendue']), Decimal('500'))
        # Aucun impact sur les soldes au-delà du montant de l'opération
        self.assertEqual(self.solde('MTN'), Decimal('45500'))
        self.assertEqual(self.solde('Caisse'), Decimal('24500'))
        # Ignorés pour les autres types d'opération
        r = self.post(type='Depense', operateur='Caisse', montant=100, motif='Eau', montant_remis=200)
        self.assertIsNone(r.data['montant_remis'])

    def test_depense_sans_motif_refusee(self):
        r = self.post(type='Depense', operateur='Caisse', montant=5000)
        self.assertEqual(r.status_code, 400)
        self.assertIn('motif', r.data)

    def test_solde_ne_peut_pas_devenir_negatif(self):
        r = self.post(type='Retrait', operateur='MTN', montant=25000, numero_client='97000000')
        self.assertEqual(r.status_code, 400)
        self.assertIn('Caisse', r.data['error'])
        self.assertEqual(self.solde('Caisse'), Decimal('20000'))
        self.assertEqual(Transaction.objects.count(), 0)

        r = self.post(type='Depot', operateur='Moov', montant=1000, numero_client='97000000')
        self.assertEqual(r.status_code, 400)

    def test_mise_a_jour_solde_negatif_refusee(self):
        r = self.client.patch(
            f'/api/boutiques/{self.boutique.id}/soldes/mise-a-jour/', {'MTN': -1}, format='json',
        )
        self.assertEqual(r.status_code, 400)

    def test_filtre_par_date(self):
        self.post(type='Depot', operateur='MTN', montant=1000, numero_client='97000000')
        ancienne = self.post(type='Depot', operateur='MTN', montant=1000, numero_client='97000001')
        hier = timezone.localdate() - timedelta(days=1)
        # 23h30 heure du Bénin = 22h30 UTC : doit rester rattachée à « hier »
        Transaction.objects.filter(id=ancienne.data['id']).update(
            date=timezone.make_aware(datetime.combine(hier, time(23, 30)))
        )
        j = hier.isoformat()
        r = self.client.get(self.url, {'date_debut': j, 'date_fin': j})
        self.assertEqual([t['id'] for t in r.data], [ancienne.data['id']])
        aujourdhui = timezone.localdate().isoformat()
        r = self.client.get(self.url, {'date_debut': aujourdhui, 'date_fin': aujourdhui})
        self.assertEqual(len(r.data), 1)

    def test_alerte_solde_bas(self):
        self.post(type='Depot', operateur='MTN', montant=45000, numero_client='97000000')
        alertes = Notification.objects.filter(type='alerte')
        self.assertEqual(alertes.count(), 1)
        self.assertIn('MTN', alertes.first().titre)
        # Déjà sous le seuil : pas de nouvelle alerte à chaque opération
        self.post(type='Depot', operateur='MTN', montant=1000, numero_client='97000000')
        self.assertEqual(Notification.objects.filter(type='alerte').count(), 1)

    def test_alerte_operation_anormale(self):
        for _ in range(5):
            self.post(type='Depot', operateur='MTN', montant=1000, numero_client='97000000')
        self.post(type='Depot', operateur='MTN', montant=8000, numero_client='97000000')
        self.assertTrue(Notification.objects.filter(type='alerte', titre__icontains='inhabituelle').exists())

    def test_archivage_bloque_si_solde_insuffisant(self):
        r = self.post(type='Retrait', operateur='MTN', montant=20000, numero_client='97000000')
        self.assertEqual(r.status_code, 201)
        # Archiver le retrait reprend 20 000 sur MTN (70 000) : OK
        self.post(type='Depot', operateur='MTN', montant=60000, numero_client='97000000')
        # MTN = 10 000 maintenant, archiver le retrait exigerait 20 000
        r = self.client.patch(f"{self.url}{r.data['id']}/", {'archivee': True}, format='json')
        self.assertEqual(r.status_code, 400)


class CodesTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()  # remet à zéro la limite par IP
        self.user = Utilisateur.objects.create_user(
            'reset@example.com', 'ancienMotDePasse1', nom='Doe', prenom='Jo', is_verified=True,
        )
        self.client = APIClient()

    def demander(self):
        return self.client.post('/api/auth/mot-de-passe-oublie/', {'email': 'reset@example.com'}, format='json')

    def dernier_code(self, type_code='reset'):
        from .models import CodeVerification
        return CodeVerification.objects.filter(utilisateur=self.user, type=type_code).order_by('-cree_le').first()

    def test_flux_reinitialisation_complet(self):
        self.demander()
        code = self.dernier_code().code
        r = self.client.post('/api/auth/verifier-code/',
                             {'email': 'reset@example.com', 'code': code, 'type': 'reset'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        r = self.client.post('/api/auth/reinitialiser-mot-de-passe/', {
            'email': 'reset@example.com', 'code': code,
            'password': 'NouveauMotDePasse9', 'password2': 'NouveauMotDePasse9',
        }, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('NouveauMotDePasse9'))
        # Le code ne peut pas resservir
        r = self.client.post('/api/auth/reinitialiser-mot-de-passe/', {
            'email': 'reset@example.com', 'code': code,
            'password': 'EncoreUnAutre77', 'password2': 'EncoreUnAutre77',
        }, format='json')
        self.assertEqual(r.status_code, 400)

    def test_un_seul_email_par_minute(self):
        from django.core import mail
        self.demander()
        r = self.demander()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)

    def test_code_invalide_apres_5_erreurs(self):
        self.demander()
        bon = self.dernier_code().code
        faux = '000000' if bon != '000000' else '111111'
        for _ in range(5):
            self.client.post('/api/auth/verifier-code/',
                             {'email': 'reset@example.com', 'code': faux, 'type': 'reset'}, format='json')
        r = self.client.post('/api/auth/verifier-code/',
                             {'email': 'reset@example.com', 'code': bon, 'type': 'reset'}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_renvoi_code_verification_email(self):
        from django.core import mail
        self.user.is_verified = False
        self.user.save()
        r = self.client.post('/api/auth/renvoyer-code/', {'email': 'reset@example.com'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.dernier_code('email').type, 'email')
        self.assertIsNone(self.dernier_code('reset'))
        self.assertEqual(len(mail.outbox), 1)


class InscriptionConnexionTests(TestCase):
    """Parcours complet : inscription → connexion bloquée → vérification → connexion."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        Pack.objects.create(cle='basic', nom='Basic', max_boutiques=1, max_utilisateurs=1)
        self.client = APIClient()

    def inscrire(self, email='Jean.Dupont@Mail.com'):
        return self.client.post('/api/auth/inscription/', {
            'email': email, 'nom': 'Dupont', 'prenom': 'Jean', 'telephone': '+22997000000',
            'password': 'MotDePasse!2026', 'password2': 'MotDePasse!2026',
        }, format='json')

    def connecter(self, email='jean.dupont@mail.com'):
        return self.client.post('/api/auth/connexion/',
                                {'email': email, 'password': 'MotDePasse!2026'}, format='json')

    def code_email(self):
        from .models import CodeVerification
        return CodeVerification.objects.filter(type='email', utilise=False).latest('cree_le').code

    def test_parcours_complet(self):
        from django.core import mail
        r = self.inscrire()
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['email'], 'jean.dupont@mail.com')
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(Abonnement.objects.filter(utilisateur__email='jean.dupont@mail.com').exists())

        # Avant vérification : connexion refusée avec redirection possible
        r = self.connecter()
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.data['code'], 'compte_non_verifie')
        self.assertEqual(r.data['user_email'], 'jean.dupont@mail.com')

        # Vérification : renvoie directement session + profil
        r = self.client.post('/api/auth/verifier-code/',
                             {'email': 'jean.dupont@mail.com', 'code': self.code_email(), 'type': 'email'},
                             format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn('access', r.data)
        self.assertTrue(r.data['utilisateur']['is_verified'])

        # Les routes protégées sont accessibles avec ce token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
        self.assertEqual(self.client.get('/api/boutiques/').status_code, 200)
        self.client.credentials()

        # Connexion normale, email insensible à la casse
        r = self.connecter('  JEAN.dupont@MAIL.com ')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['utilisateur']['prenom'], 'Jean')

    def test_email_deja_utilise(self):
        self.inscrire()
        r = self.inscrire('JEAN.DUPONT@mail.com')
        self.assertEqual(r.status_code, 400)
        self.assertIn('email', r.data)
        self.assertEqual(Utilisateur.objects.count(), 1)

    def test_mauvais_mot_de_passe(self):
        self.inscrire()
        r = self.client.post('/api/auth/connexion/',
                             {'email': 'jean.dupont@mail.com', 'password': 'faux'}, format='json')
        self.assertEqual(r.status_code, 401)

    def test_inscription_annulee_si_pack_absent(self):
        Pack.objects.all().delete()
        with self.assertRaises(Pack.DoesNotExist):
            self.inscrire()
        self.assertEqual(Utilisateur.objects.count(), 0)
