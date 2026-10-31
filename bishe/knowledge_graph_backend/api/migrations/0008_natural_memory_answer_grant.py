"""R5 proposed grant table, synthetic databases only at this stage."""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('api', '0007_natural_memory_ledger'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [migrations.CreateModel(name='NaturalMemoryAnswerGrant', fields=[
        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
        ('version', models.PositiveIntegerField(default=1)),
        ('cloud_routing', models.BooleanField(default=False)),
        ('cloud_answer_personal_context', models.BooleanField(default=False)),
        ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
    ])]
