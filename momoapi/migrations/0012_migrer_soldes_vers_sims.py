"""
Passage au modèle « plusieurs SIM par opérateur ».

Pour chaque boutique, l'ancien solde MTN / Moov / Celtiis devient une SIM
(« SIM MTN », …) portant ce solde, et les transactions existantes de cet
opérateur y sont rattachées. Seul le solde « Caisse » reste dans
SoldeOperateur. La migration inverse refait les soldes par opérateur à partir
des SIM.
"""
from django.db import migrations
from django.db.models import Sum

OPERATEURS = ('MTN', 'Moov', 'Celtiis')


def vers_sims(apps, schema_editor):
    Boutique = apps.get_model('momoapi', 'Boutique')
    SoldeOperateur = apps.get_model('momoapi', 'SoldeOperateur')
    Sim = apps.get_model('momoapi', 'Sim')
    Transaction = apps.get_model('momoapi', 'Transaction')

    for boutique in Boutique.objects.all():
        for op in OPERATEURS:
            ancien = SoldeOperateur.objects.filter(boutique=boutique, operateur=op).first()
            a_des_operations = Transaction.objects.filter(boutique=boutique, operateur=op).exists()
            if not ancien and not a_des_operations:
                continue
            sim = Sim.objects.create(
                boutique=boutique,
                operateur=op,
                nom=f"SIM {op}",
                solde=ancien.montant if ancien else 0,
            )
            Transaction.objects.filter(boutique=boutique, operateur=op).update(sim=sim)
        SoldeOperateur.objects.filter(boutique=boutique, operateur__in=OPERATEURS).delete()
        SoldeOperateur.objects.get_or_create(boutique=boutique, operateur='Caisse')


def vers_soldes_operateur(apps, schema_editor):
    Boutique = apps.get_model('momoapi', 'Boutique')
    SoldeOperateur = apps.get_model('momoapi', 'SoldeOperateur')
    Sim = apps.get_model('momoapi', 'Sim')

    for boutique in Boutique.objects.all():
        for op in OPERATEURS:
            total = Sim.objects.filter(boutique=boutique, operateur=op).aggregate(t=Sum('solde'))['t'] or 0
            SoldeOperateur.objects.update_or_create(
                boutique=boutique, operateur=op, defaults={'montant': total},
            )


class Migration(migrations.Migration):

    dependencies = [
        ('momoapi', '0011_sims'),
    ]

    operations = [
        migrations.RunPython(vers_sims, vers_soldes_operateur),
    ]
