"""
URL configuration for Momo_trans_api project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.contrib import admin
from django.urls import path, include
from django.views.static import serve as static_serve
from adminapp.views import (
    home, paiement_retour, conditions_utilisation, politique_confidentialite,
    demande_suppression_compte,
)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('momoapi.urls')),
    path('', home, name='home'),
    path('paiement/retour/', paiement_retour, name='paiement_retour'),
    path('conditions-utilisation/', conditions_utilisation, name='conditions_utilisation'),
    path('politique-confidentialite/', politique_confidentialite, name='politique_confidentialite'),
    path('suppression-compte/', demande_suppression_compte, name='demande_suppression_compte'),
    # Route explicite pour les fichiers statiques : sert directement depuis le
    # disque à chaque requête, sans dépendre de la détection automatique de
    # WhiteNoise (qui s'est montrée peu fiable derrière Passenger sur LWS).
    path('static/<path:path>', static_serve, {'document_root': settings.STATIC_ROOT}),
    path('dashboard/', include('adminapp.urls')),
]
