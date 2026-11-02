"""
Filtres d'affichage des opérations dans le dashboard admin.
Les règles d'impact reprennent celles de l'API (momoapi.views._impact_soldes) :
Dépôt/Crédit : SIM -montant, Caisse +montant ; Retrait : l'inverse ;
Dépense : la source (SIM ou Caisse) -montant.
"""
from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()

EMOJI = {'MTN': '🟡', 'Moov': '🟢', 'Celtiis': '🔵', 'Caisse': '💼'}

BADGES = {
    'Depot':   ('badge-green',  '↑ Dépôt'),
    'Retrait': ('badge-red',    '↓ Retrait'),
    'Credit':  ('badge-blue',   '◈ Crédit'),
    'Depense': ('badge-amber',  '− Dépense'),
}


def _delta(tx, compte):
    """Variation (signée) du compte « sim » ou « caisse » pour l'opération."""
    depuis_caisse = tx.type == 'Depense' and tx.operateur == 'Caisse'
    if tx.type in ('Depot', 'Credit'):
        return -tx.montant if compte == 'sim' else tx.montant
    if tx.type == 'Retrait':
        return tx.montant if compte == 'sim' else -tx.montant
    if tx.type == 'Depense':
        if (compte == 'caisse') == depuis_caisse:
            return -tx.montant
    return None


@register.filter
def type_badge(tx):
    classe, libelle = BADGES.get(tx.type, ('badge-gray', tx.type))
    return format_html('<span class="badge {}">{}</span>', classe, libelle)


@register.filter
def source_badge(tx):
    """« 🟡 MTN · SIM dépôt » ou « 💼 Caisse »."""
    libelle = tx.operateur
    if getattr(tx, 'sim', None):
        libelle = f"{tx.operateur} · {tx.sim.nom}"
    return format_html('<span class="badge badge-gray">{} {}</span>', EMOJI.get(tx.operateur, ''), libelle)


@register.filter
def impact(tx, compte):
    """Montant signé coloré, ou « — » si le compte n'est pas touché."""
    delta = _delta(tx, compte)
    if delta is None:
        return mark_safe('<span style="color:var(--text3)">—</span>')
    couleur, signe = ('var(--green)', '↑ +') if delta > 0 else ('var(--red)', '↓ -')
    return format_html(
        '<span style="color:{};font-weight:600">{}{}</span>', couleur, signe, f"{abs(delta):,.0f}".replace(',', ' '),
    )


@register.filter
def emoji_operateur(operateur):
    return EMOJI.get(operateur, '💼')
