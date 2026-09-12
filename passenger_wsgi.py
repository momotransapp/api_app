"""
Point d'entrée attendu par Phusion Passenger (hébergement cPanel/LWS).
Délègue simplement au WSGI standard de Django.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'Momo_trans_api.settings')

from django.core.wsgi import get_wsgi_application

application = get_wsgi_application()
