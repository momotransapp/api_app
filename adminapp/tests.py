from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from momoapi.models import Boutique, SoldeOperateur, Sim, Transaction, Utilisateur


class DashboardSimsTests(TestCase):
    """Les pages du dashboard affichent les SIM et l'impact des opérations."""

    def setUp(self):
        self.admin = Utilisateur.objects.create_superuser('admin@example.com', 'x', nom='A', prenom='A')
        self.client.force_login(self.admin)
        self.boutique = Boutique.objects.create(proprietaire=self.admin, nom='Ets Test')
        SoldeOperateur.objects.create(boutique=self.boutique, operateur='Caisse', montant=5000)
        self.sim = Sim.objects.create(
            boutique=self.boutique, operateur='MTN', nom='SIM dépôt', numero='+2290197426830', solde=33000,
        )
        self.depot = Transaction.objects.create(
            boutique=self.boutique, type='Depot', operateur='MTN', sim=self.sim,
            montant=Decimal('1500'), numero_client='97000000',
        )
        self.depense = Transaction.objects.create(
            boutique=self.boutique, type='Depense', operateur='Caisse', montant=Decimal('700'), motif='Eau',
        )

    def test_pages_principales(self):
        for nom in ('dashboard:accueil', 'dashboard:transactions', 'dashboard:boutiques', 'dashboard:utilisateurs'):
            self.assertEqual(self.client.get(reverse(nom)).status_code, 200, nom)

    def test_detail_boutique_affiche_les_sims(self):
        r = self.client.get(reverse('dashboard:boutique_detail', args=[self.boutique.id]))
        self.assertContains(r, 'SIM dépôt')
        self.assertContains(r, '+2290197426830')
        self.assertContains(r, 'Soldes par SIM')
        self.assertContains(r, '38000')  # total SIM + Caisse

    def test_detail_transaction_impacts(self):
        r = self.client.get(reverse('dashboard:transaction_detail', args=[self.depot.id]))
        self.assertContains(r, 'SIM dépôt')
        self.assertContains(r, '↓ -1 500')   # SIM débitée
        self.assertContains(r, '↑ +1 500')   # Caisse créditée
        r = self.client.get(reverse('dashboard:transaction_detail', args=[self.depense.id]))
        self.assertContains(r, '− Dépense')
        self.assertContains(r, '↓ -700')

    def test_liste_filtres(self):
        r = self.client.get(reverse('dashboard:transactions'), {'q': 'SIM dépôt'})
        self.assertEqual(r.context['total'], 1)
        r = self.client.get(reverse('dashboard:transactions'), {'type': 'Depense'})
        self.assertEqual(r.context['total'], 1)
        r = self.client.get(reverse('dashboard:transactions'), {'date_debut': 'pas-une-date'})
        self.assertEqual(r.status_code, 200)
