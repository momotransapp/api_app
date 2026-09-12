from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from . import views

urlpatterns = [

    # ── Auth ──────────────────────────────────────────────────────────────
    path('auth/inscription/',                  views.inscription,                    name='inscription'),
    path('auth/connexion/',                    views.connexion,                      name='connexion'),
    path('auth/token/refresh/',                TokenRefreshView.as_view(),           name='token_refresh'),
    path('auth/verifier-code/',                views.verifier_code,                  name='verifier_code'),
    path('auth/mot-de-passe-oublie/',          views.mot_de_passe_oublie,            name='mot_de_passe_oublie'),
    path('auth/reinitialiser-mot-de-passe/',   views.reinitialiser_mot_de_passe,     name='reinitialiser_mot_de_passe'),
    path('auth/profil/',                       views.profil,                         name='profil'),
    path('auth/rechercher/',                   views.rechercher_utilisateur,         name='rechercher_utilisateur'),

    # ── Packs ─────────────────────────────────────────────────────────────
    path('packs/',                             views.liste_packs,                    name='liste_packs'),

    # ── Abonnements ───────────────────────────────────────────────────────
    path('abonnements/mon-abonnement/',        views.mon_abonnement,                 name='mon_abonnement'),
    path('abonnements/souscrire/',             views.souscrire_abonnement,           name='souscrire_abonnement'),
    path('abonnements/historique/',            views.historique_abonnements,         name='historique_abonnements'),

    # ── Paiement FedaPay ──────────────────────────────────────────────────
    path('abonnements/paiement/webhook/',
         views.fedapay_webhook,                                                      name='fedapay_webhook'),
    path('abonnements/paiement/<int:transaction_id>/statut/',
         views.statut_paiement,                                                      name='statut_paiement'),

    # ── Boutiques ─────────────────────────────────────────────────────────
    path('boutiques/',                         views.mes_boutiques,                  name='mes_boutiques'),
    path('boutiques/<uuid:boutique_id>/',      views.detail_boutique,                name='detail_boutique'),

    # Membres
    path('boutiques/<uuid:boutique_id>/membres/',
         views.membres_boutique,                                                     name='membres_boutique'),
    path('boutiques/<uuid:boutique_id>/membres/<int:membre_id>/',
         views.retirer_membre,                                                       name='retirer_membre'),

    # Soldes
    path('boutiques/<uuid:boutique_id>/soldes/',
         views.soldes_boutique,                                                      name='soldes_boutique'),
    path('boutiques/<uuid:boutique_id>/soldes/mise-a-jour/',
         views.mettre_a_jour_soldes,                                                 name='mettre_a_jour_soldes'),

    # Transactions
    path('boutiques/<uuid:boutique_id>/transactions/',
         views.transactions_boutique,                                                name='transactions_boutique'),
    path('boutiques/<uuid:boutique_id>/transactions/<uuid:transaction_id>/',
         views.detail_transaction,                                                   name='detail_transaction'),

    # Rapports
    path('boutiques/<uuid:boutique_id>/rapports/journalier/',
         views.rapport_journalier,                                                   name='rapport_journalier'),
    path('boutiques/<uuid:boutique_id>/rapports/operateurs/',
         views.rapport_par_operateur,                                                name='rapport_par_operateur'),
    path('boutiques/<uuid:boutique_id>/rapports/commissions/',
         views.rapport_commissions,                                                  name='rapport_commissions'),
    path('boutiques/<uuid:boutique_id>/rapports/solde-global/',
         views.solde_global,                                                         name='solde_global'),
    
    path('avis/',                    views.soumettre_avis,   name='soumettre_avis'),
    path('avis/admin/',              views.liste_avis,        name='liste_avis'),
    path('avis/<uuid:avis_id>/lu/',  views.marquer_avis_lu,  name='marquer_avis_lu'),
    
       # Notifications
    path('notifications/',                              views.mes_notifications,              name='mes_notifications'),
    path('notifications/compteur/',                     views.compteur_non_lues,              name='compteur_non_lues'),
    path('notifications/tout-marquer-lu/',              views.tout_marquer_lu,                name='tout_marquer_lu'),
    path('notifications/tout-supprimer/',               views.supprimer_toutes_notifications, name='supprimer_toutes_notifications'),
    path('notifications/<uuid:notif_id>/lue/',          views.marquer_notification_lue,       name='marquer_notification_lue'),
    path('notifications/<uuid:notif_id>/',              views.supprimer_notification,         name='supprimer_notification'),
]