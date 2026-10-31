"""R6 proposed derivative store, never applied to the formal DB this stage."""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('api', '0008_natural_memory_answer_grant'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [migrations.CreateModel(name='NaturalMemoryDerived', fields=[
        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
        ('artifact_key', models.CharField(max_length=32, unique=True)),
        ('kind', models.CharField(max_length=20)), ('status', models.CharField(default='ready', max_length=16)),
        ('binding', models.JSONField(default=dict)), ('payload', models.JSONField(default=dict)),
        ('payload_hash', models.CharField(max_length=64)), ('seal', models.CharField(max_length=64)),
        ('expires_at', models.DateTimeField(null=True)), ('reason', models.CharField(blank=True, max_length=64)),
        ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
    ])]
