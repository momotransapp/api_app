from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('momoapi', '0006_demandesuppressioncompte_telephone_and_more')]

    operations = [
        migrations.AddField(model_name='transaction', name='archivee', field=models.BooleanField(default=False)),
        migrations.AddField(model_name='transaction', name='archivee_le', field=models.DateTimeField(blank=True, null=True)),
    ]
