"""Candidate only; bounded model dispatch is still disabled by default."""
from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):
    dependencies = [('api', '0010_natural_memory_background')]
    operations = [
        migrations.CreateModel(name='NaturalMemoryDispatchBudget', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('batch_key', models.CharField(max_length=64, unique=True)), ('authorization_hash', models.CharField(max_length=64)),
            ('approved', models.BooleanField(default=False)), ('closed', models.BooleanField(default=False)),
            ('reason', models.CharField(blank=True, max_length=80)), ('owner_ids', models.JSONField(default=list)), ('stages', models.JSONField(default=list)),
            ('maximum_calls', models.PositiveIntegerField()), ('budget_usd', models.DecimalField(decimal_places=8, max_digits=14)),
            ('estimated_usd', models.DecimalField(decimal_places=8, default=0, max_digits=14)),
            ('reserved_usd', models.DecimalField(decimal_places=8, default=0, max_digits=14)),
            ('attempts', models.PositiveIntegerField(default=0)), ('lock_version', models.PositiveIntegerField(default=0)), ('expires_at', models.DateTimeField()),
        ]),
        migrations.CreateModel(name='NaturalMemoryDispatchAttempt', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('owner_id', models.PositiveBigIntegerField()), ('stage', models.CharField(max_length=24)), ('request_ref', models.CharField(max_length=80)),
            ('status', models.CharField(default='dispatching', max_length=24)), ('request_hash', models.CharField(max_length=64)),
            ('response_hash', models.CharField(blank=True, max_length=64)), ('usage', models.JSONField(default=dict)),
            ('estimated_usd', models.DecimalField(decimal_places=8, default=0, max_digits=14)), ('reason', models.CharField(blank=True, max_length=80)),
            ('deadline_at', models.DateTimeField()),
            ('budget', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='api.naturalmemorydispatchbudget')),
        ], options={'constraints': [models.UniqueConstraint(fields=('budget', 'stage', 'request_ref'), name='natural_dispatch_once')]}),
    ]
