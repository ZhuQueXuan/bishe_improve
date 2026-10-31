"""Candidate only: durable background work and independent review inbox."""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):
    dependencies = [('api', '0009_natural_memory_derived'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name='NaturalMemoryBackgroundTask', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('task_key', models.CharField(max_length=32, unique=True)), ('status', models.CharField(default='queued', max_length=24)),
            ('snapshot', models.JSONField(default=dict)), ('permissions', models.JSONField(default=dict)),
            ('extraction_reply', models.TextField(blank=True)), ('extraction_calls', models.PositiveSmallIntegerField(default=0)),
            ('review_calls', models.PositiveSmallIntegerField(default=0)), ('deadline_at', models.DateTimeField()),
            ('audit', models.JSONField(default=dict)), ('reason', models.CharField(blank=True, max_length=80)),
            ('source_message', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, to='api.chatmessage')),
            ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
        ]),
        migrations.CreateModel(name='NaturalMemoryInbox', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('item_key', models.CharField(max_length=32, unique=True)), ('status', models.CharField(default='pending', max_length=24)),
            ('payload', models.JSONField(default=dict)), ('audit', models.JSONField(default=dict)), ('created_at', models.DateTimeField(auto_now_add=True)),
            ('task', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, to='api.naturalmemorybackgroundtask')),
            ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
        ]),
    ]
