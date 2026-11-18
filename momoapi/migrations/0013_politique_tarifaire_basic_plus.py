from django.db import migrations, models


def appliquer_politique_tarifaire(apps, schema_editor):
    Pack = apps.get_model('momoapi', 'Pack')
    packs = {
        'basic': {
            'nom': 'Basic', 'prix_mensuel': 0, 'max_boutiques': 1,
            'max_utilisateurs': -1, 'rapports_avances': False,
            'premier_mois_offert': False,
            'description': 'Une boutique, opérations, soldes, historique et rapports de base.',
            'actif': True,
        },
        'basic_plus': {
            'nom': 'Basic Plus', 'prix_mensuel': 1300, 'max_boutiques': 1,
            'max_utilisateurs': -1, 'rapports_avances': True,
            'premier_mois_offert': False,
            'description': 'Une boutique avec analyses par opérateur et commissions.',
            'actif': True,
        },
        'pro': {
            'nom': 'Pro', 'prix_mensuel': 2000, 'max_boutiques': 3,
            'max_utilisateurs': -1, 'rapports_avances': True,
            'premier_mois_offert': False,
            'description': 'De 2 à 3 boutiques, gestion centralisée et analyses avancées.',
            'actif': True,
        },
        'premium': {
            'nom': 'Premium', 'prix_mensuel': 5000, 'max_boutiques': -1,
            'max_utilisateurs': -1, 'rapports_avances': True,
            'premier_mois_offert': False,
            'description': 'Réseau de boutiques étendu avec suivi avancé.',
            'actif': True,
        },
    }
    for cle, defaults in packs.items():
        # Les offres historiques sont déjà présentes en production. Les mettre
        # à jour évite de dupliquer les données des installations existantes ;
        # seule Basic Plus est une nouvelle offre à créer si nécessaire.
        updated = Pack.objects.filter(cle=cle).update(**defaults)
        if not updated and cle == 'basic_plus':
            Pack.objects.create(cle=cle, **defaults)


def annuler_politique_tarifaire(apps, schema_editor):
    apps.get_model('momoapi', 'Pack').objects.filter(cle='basic_plus').delete()


class Migration(migrations.Migration):
    dependencies = [('momoapi', '0012_migrer_soldes_vers_sims')]

    operations = [
        migrations.AlterField(
            model_name='pack',
            name='cle',
            field=models.CharField(
                choices=[
                    ('basic', 'Basic'),
                    ('basic_plus', 'Basic Plus'),
                    ('pro', 'Pro'),
                    ('premium', 'Premium'),
                ],
                max_length=20,
                unique=True,
            ),
        ),
        migrations.RunPython(appliquer_politique_tarifaire, annuler_politique_tarifaire),
    ]
