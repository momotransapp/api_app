from datetime import datetime, timedelta, time
from decimal import Decimal

from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import (
    Utilisateur, Boutique, MembreBoutique, SoldeOperateur, Transaction, Notification, Pack, Abonnement, Sim,
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
        # Une SIM par opérateur : les opérations envoyées avec le seul
        # opérateur (anciennes versions de l'app) utilisent cette SIM.
        for op, montant in [('MTN', 50000), ('Moov', 0), ('Celtiis', 0)]:
            Sim.objects.create(boutique=self.boutique, operateur=op, nom=f'SIM {op}', solde=montant)
        SoldeOperateur.objects.create(boutique=self.boutique, operateur='Caisse', montant=20000)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = f'/api/boutiques/{self.boutique.id}/transactions/'

    def solde(self, op):
        if op == 'Caisse':
            return SoldeOperateur.objects.get(boutique=self.boutique, operateur=op).montant
        return sum(s.solde for s in Sim.objects.filter(boutique=self.boutique, operateur=op, active=True))

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

    def test_limite_des_dernieres_operations(self):
        for i in range(4):
            self.post(type='Depot', operateur='MTN', montant=100 + i, numero_client='97000000')
        r = self.client.get(self.url, {'limite': 2})
        self.assertEqual([Decimal(t['montant']) for t in r.data], [Decimal('103'), Decimal('102')])
        self.assertEqual(self.client.get(self.url, {'limite': 'abc'}).status_code, 400)
        self.assertEqual(len(self.client.get(self.url).data), 4)

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


class SimsTests(TestCase):
    """Plusieurs SIM par opérateur, chacune avec son solde."""

    def setUp(self):
        self.user = Utilisateur.objects.create_user(
            'sims@example.com', 'motdepasse123', nom='Doe', prenom='Sam', is_verified=True,
        )
        pack = Pack.objects.create(cle='basic', nom='Basic', max_boutiques=1, max_utilisateurs=2)
        Abonnement.objects.create(
            utilisateur=self.user, pack=pack, statut='actif',
            debut=timezone.localdate(), fin=timezone.localdate() + timedelta(days=365),
        )
        self.boutique = Boutique.objects.create(proprietaire=self.user, nom='Shop', seuil_alerte_solde=10000)
        MembreBoutique.objects.create(boutique=self.boutique, utilisateur=self.user, role='proprietaire')
        SoldeOperateur.objects.create(boutique=self.boutique, operateur='Caisse', montant=100000)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.base = f'/api/boutiques/{self.boutique.id}'

    def ajouter_sim(self, **data):
        data = {'operateur': 'MTN', 'nom': 'SIM dépôt', 'numero': '+2290197426830', 'solde': 0, **data}
        return self.client.post(f'{self.base}/sims/', data, format='json')

    def operation(self, **data):
        data = {'numero_client': '97000000', **data}
        return self.client.post(f'{self.base}/transactions/', data, format='json')

    def test_ajout_et_liste_des_sims(self):
        r = self.ajouter_sim(solde=30000)
        self.assertEqual(r.status_code, 201, r.data)
        self.ajouter_sim(nom='SIM retrait', solde=20000)
        self.ajouter_sim(operateur='Moov', nom='SIM unique', solde=5000)
        r = self.client.get(f'{self.base}/sims/')
        self.assertEqual(len(r.data), 3)
        # Soldes agrégés par opérateur (SIM confondues)
        soldes = {s['operateur']: Decimal(str(s['montant'])) for s in self.client.get(f'{self.base}/soldes/').data}
        self.assertEqual(soldes, {
            'MTN': Decimal('50000'), 'Moov': Decimal('5000'), 'Celtiis': Decimal('0'), 'Caisse': Decimal('100000'),
        })

    def test_validation_sim(self):
        self.assertEqual(self.ajouter_sim(numero='').status_code, 400)
        self.assertEqual(self.ajouter_sim(nom='  ').status_code, 400)
        self.assertEqual(self.ajouter_sim(solde=-5).status_code, 400)
        self.assertEqual(self.ajouter_sim(operateur='Caisse').status_code, 400)

    def test_operation_sur_la_sim_choisie(self):
        depot = self.ajouter_sim(solde=30000).data
        retrait = self.ajouter_sim(nom='SIM retrait', solde=0).data
        r = self.operation(type='Depot', sim=depot['id'], montant=10000)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['operateur'], 'MTN')
        self.assertEqual(r.data['sim_nom'], 'SIM dépôt')
        r = self.operation(type='Retrait', sim=retrait['id'], montant=4000)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Sim.objects.get(id=depot['id']).solde, Decimal('20000'))
        self.assertEqual(Sim.objects.get(id=retrait['id']).solde, Decimal('4000'))
        self.assertEqual(SoldeOperateur.objects.get(boutique=self.boutique).montant, Decimal('106000'))
        # Historique filtrable par SIM
        r = self.client.get(f'{self.base}/transactions/', {'sim': retrait['id']})
        self.assertEqual([t['type'] for t in r.data], ['Retrait'])

    def test_solde_insuffisant_sur_une_sim(self):
        sim = self.ajouter_sim(solde=1000).data
        self.ajouter_sim(nom='SIM retrait', solde=500000)
        r = self.operation(type='Depot', sim=sim['id'], montant=2000)
        self.assertEqual(r.status_code, 400)
        self.assertIn('SIM dépôt', r.data['error'])

    def test_plusieurs_sims_sans_choix_refuse(self):
        self.ajouter_sim(solde=30000)
        self.ajouter_sim(nom='SIM retrait', solde=30000)
        r = self.operation(type='Depot', operateur='MTN', montant=1000)
        self.assertEqual(r.status_code, 400)
        self.assertIn('plusieurs SIM', r.data['error'])

    def test_sim_d_une_autre_boutique_refusee(self):
        autre = Boutique.objects.create(proprietaire=self.user, nom='Autre')
        sim_autre = Sim.objects.create(boutique=autre, operateur='MTN', nom='X', solde=50000)
        r = self.operation(type='Depot', sim=str(sim_autre.id), montant=1000)
        self.assertEqual(r.status_code, 400)

    def test_depense_depuis_une_sim(self):
        sim = self.ajouter_sim(operateur='Celtiis', nom='SIM Celtiis', solde=8000).data
        r = self.operation(type='Depense', sim=sim['id'], montant=3000, motif='Transport', numero_client='')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Sim.objects.get(id=sim['id']).solde, Decimal('5000'))

    def test_changer_de_sim_a_la_modification(self):
        a = self.ajouter_sim(solde=30000).data
        b = self.ajouter_sim(nom='SIM 2', solde=30000).data
        tx = self.operation(type='Depot', sim=a['id'], montant=10000).data
        r = self.client.patch(f"{self.base}/transactions/{tx['id']}/", {'sim': b['id']}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Sim.objects.get(id=a['id']).solde, Decimal('30000'))
        self.assertEqual(Sim.objects.get(id=b['id']).solde, Decimal('20000'))

    def test_alerte_solde_bas_par_sim(self):
        sim = self.ajouter_sim(solde=15000).data
        self.operation(type='Depot', sim=sim['id'], montant=6000)
        alerte = Notification.objects.get(type='alerte')
        self.assertIn('MTN — SIM dépôt', alerte.titre)

    def test_mise_a_jour_et_retrait_de_sim(self):
        sim = self.ajouter_sim(solde=30000).data
        url = f"{self.base}/sims/{sim['id']}/"
        self.assertEqual(self.client.patch(url, {'solde': 45000}, format='json').data['solde'], '45000.00')
        # Retrait impossible tant que le solde n'est pas à 0
        self.assertEqual(self.client.delete(url).status_code, 400)
        self.client.patch(url, {'solde': 0}, format='json')
        self.assertEqual(self.client.delete(url).status_code, 200)
        self.assertEqual(self.client.get(f'{self.base}/sims/').data, [])

    def test_gerant_ne_peut_pas_ajouter_de_sim(self):
        gerant = Utilisateur.objects.create_user('g@example.com', 'x', nom='G', prenom='G', is_verified=True)
        MembreBoutique.objects.create(boutique=self.boutique, utilisateur=gerant, role='gerant')
        sim = self.ajouter_sim(solde=1000).data
        self.client.force_authenticate(gerant)
        self.assertEqual(self.ajouter_sim(nom='SIM 2').status_code, 403)
        # … mais peut mettre à jour le solde
        r = self.client.patch(f"{self.base}/sims/{sim['id']}/", {'solde': 2000}, format='json')
        self.assertEqual(r.status_code, 200)

    def test_ancienne_app_mise_a_jour_solde_operateur(self):
        url = f'{self.base}/soldes/mise-a-jour/'
        # Aucune SIM MTN : une SIM est créée avec ce solde
        self.assertEqual(self.client.patch(url, {'MTN': 7000, 'Caisse': 1}, format='json').status_code, 200)
        self.assertEqual(Sim.objects.get(boutique=self.boutique).solde, Decimal('7000'))
        self.ajouter_sim(nom='SIM 2')
        self.assertEqual(self.client.patch(url, {'MTN': 1}, format='json').status_code, 400)


class MigrationSimsTests(TransactionTestCase):
    """0012 : les anciens soldes par opérateur deviennent une SIM chacun."""

    avant = [('momoapi', '0011_sims')]
    apres = [('momoapi', '0012_migrer_soldes_vers_sims')]

    def test_soldes_et_transactions_migres(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        executor.migrate(self.avant)
        apps = executor.loader.project_state(self.avant).apps
        U = apps.get_model('momoapi', 'Utilisateur')
        B = apps.get_model('momoapi', 'Boutique')
        S = apps.get_model('momoapi', 'SoldeOperateur')
        T = apps.get_model('momoapi', 'Transaction')

        user = U.objects.create(email='m@example.com', nom='M', prenom='M')
        boutique = B.objects.create(proprietaire=user, nom='Shop')
        for op, montant in [('MTN', 40000), ('Moov', 0), ('Caisse', 9000)]:
            S.objects.create(boutique=boutique, operateur=op, montant=montant)
        T.objects.create(boutique=boutique, type='Depot', operateur='MTN', montant=1000, numero_client='1')

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(self.apres)
        apps = executor.loader.project_state(self.apres).apps
        Sim = apps.get_model('momoapi', 'Sim')
        S = apps.get_model('momoapi', 'SoldeOperateur')
        T = apps.get_model('momoapi', 'Transaction')

        sims = {s.operateur: s for s in Sim.objects.filter(boutique_id=boutique.id)}
        self.assertEqual(set(sims), {'MTN', 'Moov'})  # pas de Celtiis : ni solde ni opération
        self.assertEqual(sims['MTN'].solde, Decimal('40000'))
        self.assertEqual(sims['MTN'].nom, 'SIM MTN')
        self.assertEqual(list(S.objects.filter(boutique_id=boutique.id).values_list('operateur', 'montant')),
                         [('Caisse', Decimal('9000'))])
        self.assertEqual(T.objects.get().sim_id, sims['MTN'].id)

        # Migration inverse : les soldes par opérateur sont reconstitués
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(self.avant)
        apps = executor.loader.project_state(self.avant).apps
        S = apps.get_model('momoapi', 'SoldeOperateur')
        self.assertEqual(S.objects.get(boutique_id=boutique.id, operateur='MTN').montant, Decimal('40000'))

        # Remettre la base au dernier état pour les autres tests
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())


class ParcoursAppMobileTests(TestCase):
    """Rejoue les requêtes exactes envoyées par l'app (add.tsx, SimSoldeModal, DetailsScreen)."""

    def setUp(self):
        self.user = Utilisateur.objects.create_user(
            'app@example.com', 'motdepasse123', nom='Doe', prenom='Ana', is_verified=True,
        )
        pack = Pack.objects.create(cle='basic', nom='Basic', max_boutiques=1, max_utilisateurs=1)
        Abonnement.objects.create(
            utilisateur=self.user, pack=pack, statut='actif',
            debut=timezone.localdate(), fin=timezone.localdate() + timedelta(days=365),
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_parcours_complet(self):
        # Création de boutique depuis l'app : seule la Caisse existe
        boutique = self.client.post('/api/boutiques/', {'nom': 'Ets Test', 'couleur': '#3B82F6', 'devise': 'XOF', 'adresse': ''}, format='json').data
        base = f"/api/boutiques/{boutique['id']}"
        self.assertEqual(self.client.get(f'{base}/sims/').data, [])
        soldes = {s['operateur']: Decimal(str(s['montant'])) for s in self.client.get(f'{base}/soldes/').data}
        self.assertEqual(soldes['Caisse'], 0)

        # AjoutSimScreen
        r = self.client.post(f'{base}/sims/', {'operateur': 'MTN', 'nom': 'SIM dépôt', 'numero': '+2290197426830', 'solde': 50000}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        sim = r.data
        self.client.post(f'{base}/sims/', {'operateur': 'MTN', 'nom': 'SIM retrait', 'numero': '+2290197426831', 'solde': 0}, format='json')

        # SimSoldeModal : Caisse puis SIM
        self.assertEqual(self.client.patch(f'{base}/soldes/mise-a-jour/', {'Caisse': 20000}, format='json').status_code, 200)

        # add.tsx — Dépôt (opérateur + SIM + montant remis / monnaie)
        depot = self.client.post(f'{base}/transactions/', {
            'type': 'Depot', 'operateur': 'MTN', 'sim': sim['id'], 'montant': 4500,
            'numero_client': '0197000000', 'nom_client': 'Kevin', 'reference': 'REF1', 'remarque': '',
            'montant_remis': 5000, 'monnaie_rendue': 500,
        }, format='json')
        self.assertEqual(depot.status_code, 201, depot.data)
        self.assertEqual(depot.data['sim_nom'], 'SIM dépôt')

        # add.tsx — Dépense payée en espèces (sim: null)
        depense = self.client.post(f'{base}/transactions/', {
            'type': 'Depense', 'operateur': 'Caisse', 'sim': None, 'montant': 1000, 'motif': 'Eau', 'remarque': '',
        }, format='json')
        self.assertEqual(depense.status_code, 201, depense.data)

        # add.tsx — Crédit sans montant remis (champs vides envoyés à null)
        credit = self.client.post(f'{base}/transactions/', {
            'type': 'Credit', 'operateur': 'MTN', 'sim': sim['id'], 'montant': 500, 'type_credit': 'Forfait',
            'numero_client': '0197000001', 'nom_client': '', 'reference': '', 'remarque': '',
            'montant_remis': None, 'monnaie_rendue': None,
        }, format='json')
        self.assertEqual(credit.status_code, 201, credit.data)

        # Soldes attendus : SIM 50 000 - 4 500 - 500 ; Caisse 20 000 + 4 500 - 1 000 + 500
        self.assertEqual(Sim.objects.get(id=sim['id']).solde, Decimal('45000'))
        soldes = {s['operateur']: Decimal(str(s['montant'])) for s in self.client.get(f'{base}/soldes/').data}
        self.assertEqual(soldes, {'MTN': Decimal('45000'), 'Moov': 0, 'Celtiis': 0, 'Caisse': Decimal('24000')})

        # DetailsScreen — modification (le corps n'envoie pas la SIM : elle est conservée)
        r = self.client.patch(f"{base}/transactions/{depot.data['id']}/", {
            'montant': 5500, 'commission': 0, 'numero_client': '0197000000', 'nom_client': 'Kevin',
            'reference': 'REF1', 'remarque': '', 'montant_remis': 6000, 'monnaie_rendue': 500,
        }, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(str(r.data['sim']), str(sim['id']))
        self.assertEqual(Sim.objects.get(id=sim['id']).solde, Decimal('44000'))

        # DetailsScreen — archivage puis restauration
        url = f"{base}/transactions/{credit.data['id']}/"
        self.assertEqual(self.client.patch(url, {'archivee': True}, format='json').status_code, 200)
        self.assertEqual(Sim.objects.get(id=sim['id']).solde, Decimal('44500'))
        self.assertEqual(self.client.patch(url, {'archivee': False}, format='json').status_code, 200)
        self.assertEqual(Sim.objects.get(id=sim['id']).solde, Decimal('44000'))

        # Historique : la SIM est renvoyée pour l'affichage
        liste = self.client.get(f'{base}/transactions/').data
        self.assertEqual({t['sim_nom'] for t in liste}, {'SIM dépôt', None})

        # Rapports (écran stats) : solde restant MTN = somme des SIM
        rapport = self.client.get(f'{base}/rapports/solde-global/').data
        self.assertEqual(len(rapport['sims']), 2)
