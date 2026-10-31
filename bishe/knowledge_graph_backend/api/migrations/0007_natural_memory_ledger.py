"""R4 proposed migration. Applied only to synthetic test databases this stage."""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('api', '0006_personal_memory_storage'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name='NaturalMemoryLedger', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('revision', models.PositiveIntegerField(default=0)),
            ('epoch', models.PositiveIntegerField(default=0)),
            ('lock_version', models.PositiveIntegerField(default=0)),
            ('pending_epoch', models.PositiveIntegerField(null=True)),
            ('policy', models.JSONField(default=dict)), ('permissions', models.JSONField(default=dict)),
            ('state', models.JSONField(default=dict)),
            ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
        ]),
        migrations.CreateModel(name='NaturalMemoryJob', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('job_key', models.CharField(max_length=32, unique=True)),
            ('epoch', models.PositiveIntegerField()), ('revision', models.PositiveIntegerField()),
            ('status', models.CharField(default='awaiting_extraction', max_length=24)),
            ('mode', models.CharField(default='new_message', max_length=24)),
            ('snapshot', models.JSONField(default=dict)), ('extraction_reply', models.TextField(blank=True)),
            ('extraction_hash', models.CharField(blank=True, max_length=64)),
            ('review_hash', models.CharField(blank=True, max_length=64)),
            ('reason', models.CharField(blank=True, max_length=80)),
            ('audit', models.JSONField(default=dict)), ('deadline_at', models.DateTimeField()),
            ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ('source_message', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to='api.chatmessage')),
        ]),
        migrations.CreateModel(name='NaturalMemoryOperation', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('request_key', models.CharField(max_length=100)), ('input_hash', models.CharField(max_length=64)),
            ('receipt', models.JSONField(default=dict)),
            ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
        ], options={'constraints': [models.UniqueConstraint(fields=('user', 'request_key'), name='natural_operation_owner_key')]}),
    ]
