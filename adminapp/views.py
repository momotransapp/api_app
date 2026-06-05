from datetime import date, timedelta
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Sum, Count, Q
from django.utils import timezone
from django.http import JsonResponse
from django.contrib import messages

from momoapi.models import *


def staff_required(view_func):
    return user_passes_test(lambda u: u.is_authenticated and u.is_staff, login_url='/dashboard/login/')(view_func)


# ══════════════════════════════════════════════════════════════════════════
#  AUTH
# ══════════════════════════════════════════════════════════════════════════

def login_view(request):
    if request.user.is_authenticated and request.user.is_staff:
        return redirect('dashboard:accueil')

    error = None
    if request.method == 'POST':
        email    = request.POST.get('email', '').strip()
        password = request.POST.get('password', '')
        user = authenticate(request, username=email, password=password)
        if user and user.is_staff:
            login(request, user)
            return redirect(request.GET.get('next', 'dashboard:accueil'))
        error = "Identifiants incorrects ou accès non autorisé."

    return render(request, 'dashboard/login.html', {'error': error})


def logout_view(request):
    logout(request)
    return redirect('dashboard:login')


# ══════════════════════════════════════════════════════════════════════════
#  ACCUEIL — VUE D'ENSEMBLE
# ══════════════════════════════════════════════════════════════════════════

@staff_required
def accueil(request):
    today      = date.today()
    hier       = today - timedelta(days=1)
    ce_mois    = today.replace(day=1)
    mois_passe = (ce_mois - timedelta(days=1)).replace(day=1)

    # ── KPIs principaux ──
    kpis = {
        'total_utilisateurs':   Utilisateur.objects.count(),
        'nouveaux_ce_mois':     Utilisateur.objects.filter(date_inscription__date__gte=ce_mois).count(),
        'total_boutiques':      Boutique.objects.filter(active=True).count(),
        'total_transactions':   Transaction.objects.count(),
        'transactions_auj':     Transaction.objects.filter(date__date=today).count(),
        'total_avis':           Avis.objects.count(),
        'avis_non_lus':         Avis.objects.filter(lu=False).count(),
        'abonnements_actifs':   Abonnement.objects.filter(statut='actif', fin__gte=today).count(),
    }

    # ── Volume transactions 7 derniers jours ──
    tx_7j = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        cnt = Transaction.objects.filter(date__date=d).count()
        tx_7j.append({'date': d.strftime('%d/%m'), 'count': cnt})

    # ── Répartition par type ──
    tx_types = Transaction.objects.values('type').annotate(total=Count('id'))
    type_data = {t['type']: t['total'] for t in tx_types}

    # ── Top boutiques par transactions ──
    top_boutiques = (
        Boutique.objects.filter(active=True)
        .annotate(nb_tx=Count('transactions'))
        .order_by('-nb_tx')[:5]
    )

    # ── Dernières transactions ──
    dernieres_tx = Transaction.objects.select_related('boutique', 'effectuee_par').order_by('-date')[:8]

    # ── Répartition abonnements par pack ──
    abo_par_pack = (
        Abonnement.objects.filter(statut='actif', fin__gte=today)
        .values('pack__nom', 'pack__cle')
        .annotate(total=Count('id'))
    )

    # ── Nouveaux utilisateurs 30j ──
    users_30j = []
    for i in range(29, -1, -1):
        d = today - timedelta(days=i)
        cnt = Utilisateur.objects.filter(date_inscription__date=d).count()
        users_30j.append({'date': d.strftime('%d/%m'), 'count': cnt})

    context = {
        'page': 'accueil',
        'kpis': kpis,
        'tx_7j': tx_7j,
        'type_data': type_data,
        'top_boutiques': top_boutiques,
        'dernieres_tx': dernieres_tx,
        'abo_par_pack': list(abo_par_pack),
        'users_30j': users_30j,
    }
    return render(request, 'dashboard/accueil.html', context)


# ══════════════════════════════════════════════════════════════════════════
#  UTILISATEURS
# ══════════════════════════════════════════════════════════════════════════

@staff_required
def utilisateurs(request):
    q        = request.GET.get('q', '')
    filtre   = request.GET.get('filtre', 'tous')  # tous | verifie | non_verifie | staff

    qs = Utilisateur.objects.prefetch_related('abonnements').order_by('-date_inscription')

    if q:
        qs = qs.filter(
            Q(email__icontains=q) |
            Q(nom__icontains=q) |
            Q(prenom__icontains=q)
        )
    if filtre == 'verifie':
        qs = qs.filter(is_verified=True)
    elif filtre == 'non_verifie':
        qs = qs.filter(is_verified=False)
    elif filtre == 'staff':
        qs = qs.filter(is_staff=True)

    return render(request, 'dashboard/utilisateurs.html', {
        'page': 'utilisateurs',
        'users': qs,
        'q': q,
        'filtre': filtre,
        'total': qs.count(),
    })


@staff_required
def utilisateur_detail(request, user_id):
    user = get_object_or_404(Utilisateur, id=user_id)
    boutiques = MembreBoutique.objects.filter(utilisateur=user, actif=True).select_related('boutique')
    abonnements = Abonnement.objects.filter(utilisateur=user).select_related('pack').order_by('-cree_le')
    transactions = Transaction.objects.filter(effectuee_par=user).order_by('-date')[:10]
    avis = Avis.objects.filter(utilisateur=user).order_by('-cree_le')

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'toggle_staff':
            user.is_staff = not user.is_staff
            user.save()
            messages.success(request, f"Statut staff modifié pour {user.email}.")
        elif action == 'toggle_active':
            user.is_active = not user.is_active
            user.save()
            messages.success(request, f"Compte {'activé' if user.is_active else 'désactivé'}.")
        elif action == 'toggle_verified':
            user.is_verified = not user.is_verified
            user.save()
            messages.success(request, f"Vérification modifiée.")
        return redirect('dashboard:utilisateur_detail', user_id=user_id)

    return render(request, 'dashboard/utilisateur_detail.html', {
        'page': 'utilisateurs',
        'u': user,
        'boutiques': boutiques,
        'abonnements': abonnements,
        'transactions': transactions,
        'avis': avis,
    })


# ══════════════════════════════════════════════════════════════════════════
#  BOUTIQUES
# ══════════════════════════════════════════════════════════════════════════

@staff_required
def boutiques(request):
    q      = request.GET.get('q', '')
    filtre = request.GET.get('filtre', 'actives')

    qs = Boutique.objects.select_related('proprietaire').annotate(
        nb_membres=Count('membres', filter=Q(membres__actif=True)),
        nb_transactions=Count('transactions'),
    ).order_by('-cree_le')

    if q:
        qs = qs.filter(Q(nom__icontains=q) | Q(proprietaire__email__icontains=q))
    if filtre == 'actives':
        qs = qs.filter(active=True)
    elif filtre == 'inactives':
        qs = qs.filter(active=False)

    return render(request, 'dashboard/boutiques.html', {
        'page': 'boutiques',
        'boutiques': qs,
        'q': q,
        'filtre': filtre,
        'total': qs.count(),
    })


@staff_required
def boutique_detail(request, boutique_id):
    boutique = get_object_or_404(Boutique, id=boutique_id)
    membres  = MembreBoutique.objects.filter(boutique=boutique).select_related('utilisateur')
    soldes   = SoldeOperateur.objects.filter(boutique=boutique)
    transactions = Transaction.objects.filter(boutique=boutique).select_related('effectuee_par').order_by('-date')[:15]

    # Stats transactions
    stats = Transaction.objects.filter(boutique=boutique).aggregate(
        total_depots=Sum('montant', filter=Q(type='Depot')),
        total_retraits=Sum('montant', filter=Q(type='Retrait')),
        total_credits=Sum('montant', filter=Q(type='Credit')),
        total_commissions=Sum('commission'),
    )

    if request.method == 'POST' and request.POST.get('action') == 'toggle_active':
        boutique.active = not boutique.active
        boutique.save()
        messages.success(request, f"Boutique {'activée' if boutique.active else 'désactivée'}.")
        return redirect('dashboard:boutique_detail', boutique_id=boutique_id)

    return render(request, 'dashboard/boutique_detail.html', {
        'page': 'boutiques',
        'boutique': boutique,
        'membres': membres,
        'soldes': soldes,
        'transactions': transactions,
        'stats': stats,
    })


# ══════════════════════════════════════════════════════════════════════════
#  TRANSACTIONS
# ══════════════════════════════════════════════════════════════════════════

@staff_required
def transactions_view(request):
    q          = request.GET.get('q', '')
    type_filtre = request.GET.get('type', '')
    op_filtre  = request.GET.get('operateur', '')
    date_debut = request.GET.get('date_debut', '')
    date_fin   = request.GET.get('date_fin', '')

    qs = Transaction.objects.select_related('boutique', 'effectuee_par').order_by('-date')

    if q:
        qs = qs.filter(
            Q(numero_client__icontains=q) |
            Q(nom_client__icontains=q) |
            Q(reference__icontains=q) |
            Q(boutique__nom__icontains=q)
        )
    if type_filtre:
        qs = qs.filter(type=type_filtre)
    if op_filtre:
        qs = qs.filter(operateur=op_filtre)
    if date_debut:
        qs = qs.filter(date__date__gte=date_debut)
    if date_fin:
        qs = qs.filter(date__date__lte=date_fin)

    totaux = qs.aggregate(
        total_montant=Sum('montant'),
        total_commissions=Sum('commission'),
    )

    return render(request, 'dashboard/transactions.html', {
        'page': 'transactions',
        'transactions': qs[:100],
        'total': qs.count(),
        'q': q,
        'type_filtre': type_filtre,
        'op_filtre': op_filtre,
        'date_debut': date_debut,
        'date_fin': date_fin,
        'totaux': totaux,
    })


@staff_required
def transaction_detail(request, tx_id):
    """Affiche le détail complet d'une transaction"""
    tx = get_object_or_404(Transaction, id=tx_id)
    
    # Calculer l'impact sur les comptes
    if tx.type == 'Retrait':
        impact_operateur = f"+{tx.montant}"
        impact_caisse = f"-{tx.montant}"
    else:  # Depot ou Credit
        impact_operateur = f"-{tx.montant}"
        impact_caisse = f"+{tx.montant}"
    
    # Récupérer les transactions précédentes pour montrer le solde avant/après
    tx_avant = Transaction.objects.filter(
        boutique=tx.boutique,
        operateur=tx.operateur,
        date__lt=tx.date
    ).order_by('-date').first()
    
    return render(request, 'dashboard/transaction_detail.html', {
        'page': 'transactions',
        'tx': tx,
        'impact_operateur': impact_operateur,
        'impact_caisse': impact_caisse,
    })


# ══════════════════════════════════════════════════════════════════════════
#  ABONNEMENTS
# ══════════════════════════════════════════════════════════════════════════

@staff_required
def abonnements_view(request):
    today  = date.today()
    filtre = request.GET.get('filtre', 'actifs')
    q      = request.GET.get('q', '')

    qs = Abonnement.objects.select_related('utilisateur', 'pack').order_by('-cree_le')

    if filtre == 'actifs':
        qs = qs.filter(statut='actif', fin__gte=today)
    elif filtre == 'expires':
        qs = qs.filter(Q(statut='expire') | Q(fin__lt=today))
    elif filtre == 'annules':
        qs = qs.filter(statut='annule')

    if q:
        qs = qs.filter(utilisateur__email__icontains=q)

    packs = Pack.objects.all()
    abo_par_pack = (
        Abonnement.objects.filter(statut='actif', fin__gte=today)
        .values('pack__nom', 'pack__cle')
        .annotate(total=Count('id'))
    )

    return render(request, 'dashboard/abonnements.html', {
        'page': 'abonnements',
        'abonnements': qs,
        'total': qs.count(),
        'filtre': filtre,
        'q': q,
        'packs': packs,
        'abo_par_pack': list(abo_par_pack),
    })


# ══════════════════════════════════════════════════════════════════════════
#  AVIS
# ══════════════════════════════════════════════════════════════════════════

@staff_required
def avis_view(request):
    filtre = request.GET.get('filtre', 'tous')

    qs = Avis.objects.select_related('utilisateur').order_by('-cree_le')
    if filtre == 'non_lus':
        qs = qs.filter(lu=False)
    elif filtre == 'lus':
        qs = qs.filter(lu=True)

    if request.method == 'POST':
        avis_id = request.POST.get('avis_id')
        action  = request.POST.get('action')
        avis    = get_object_or_404(Avis, id=avis_id)
        if action == 'marquer_lu':
            avis.lu = True
            avis.save()
        elif action == 'supprimer':
            avis.delete()
            messages.success(request, "Avis supprimé.")
        return redirect(f"?filtre={filtre}")

    return render(request, 'dashboard/avis.html', {
        'page': 'avis',
        'avis_list': qs,
        'total': qs.count(),
        'non_lus': Avis.objects.filter(lu=False).count(),
        'filtre': filtre,
    })


# ══════════════════════════════════════════════════════════════════════════
#  PACKS
# ══════════════════════════════════════════════════════════════════════════

@staff_required
def packs_view(request):
    today = date.today()
    packs = Pack.objects.annotate(
        nb_abonnes=Count('abonnement', filter=Q(
            abonnement__statut='actif', abonnement__fin__gte=today
        ))
    ).order_by('prix_mensuel')

    return render(request, 'dashboard/packs.html', {
        'page': 'packs',
        'packs': packs,
    })