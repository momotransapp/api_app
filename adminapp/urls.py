from django.urls import path
from . import views

app_name = 'dashboard'

urlpatterns = [
    path('login/',  views.login_view,  name='login'),
    path('logout/', views.logout_view, name='logout'),

    path('',                                        views.accueil,              name='accueil'),
    path('utilisateurs/',                           views.utilisateurs,         name='utilisateurs'),
    path('utilisateurs/<uuid:user_id>/',            views.utilisateur_detail,   name='utilisateur_detail'),
    path('boutiques/',                              views.boutiques,            name='boutiques'),
    path('boutiques/<uuid:boutique_id>/',           views.boutique_detail,      name='boutique_detail'),
    path('transactions/',                           views.transactions_view,    name='transactions'),
    path('abonnements/',                            views.abonnements_view,     name='abonnements'),
    path('avis/',                                   views.avis_view,            name='avis'),
    path('packs/',                                  views.packs_view,           name='packs'),
]