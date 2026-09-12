"""
Client FedaPay minimal (API REST directe, sans SDK non officiel).
Documentation : https://docs.fedapay.com/api-reference
"""
import requests
from django.conf import settings


class FedaPayError(Exception):
    def __init__(self, message, status_code=None, response_body=None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


def _headers():
    return {
        'Authorization': f'Bearer {settings.FEDAPAY_SECRET_KEY}',
        'Content-Type': 'application/json',
    }


def _unwrap(data, key):
    """FedaPay enveloppe certaines ressources sous une clé du type 'v1/transaction'."""
    if isinstance(data, dict) and key in data:
        return data[key]
    return data


def creer_transaction(*, description, montant, email, prenom, nom, callback_url):
    """POST /transactions → crée une transaction en attente de paiement."""
    resp = requests.post(
        f'{settings.FEDAPAY_BASE_URL}/transactions',
        json={
            'description': description,
            'amount': int(montant),
            'currency': {'iso': 'XOF'},
            'callback_url': callback_url,
            'customer': {'email': email, 'firstname': prenom, 'lastname': nom},
        },
        headers=_headers(),
        timeout=15,
    )
    if resp.status_code not in (200, 201):
        raise FedaPayError("Échec de création de la transaction FedaPay.", resp.status_code, resp.text)
    return _unwrap(resp.json(), 'v1/transaction')


def generer_lien_paiement(transaction_id):
    """POST /transactions/{id}/token → génère le lien de paiement hébergé."""
    resp = requests.post(
        f'{settings.FEDAPAY_BASE_URL}/transactions/{transaction_id}/token',
        headers=_headers(),
        timeout=15,
    )
    if resp.status_code != 200:
        raise FedaPayError("Échec de génération du lien de paiement.", resp.status_code, resp.text)
    return resp.json()


def recuperer_transaction(transaction_id):
    """GET /transactions/{id} → statut à jour d'une transaction."""
    resp = requests.get(
        f'{settings.FEDAPAY_BASE_URL}/transactions/{transaction_id}',
        headers=_headers(),
        timeout=15,
    )
    if resp.status_code != 200:
        raise FedaPayError("Transaction FedaPay introuvable.", resp.status_code, resp.text)
    return _unwrap(resp.json(), 'v1/transaction')
