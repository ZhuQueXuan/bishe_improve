"""R4 versioned aggregate persistence, isolated until a later release gate."""
from django.conf import settings
from django.db import models


class NaturalMemoryLedger(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    revision = models.PositiveIntegerField(default=0)
    epoch = models.PositiveIntegerField(default=0)
    lock_version = models.PositiveIntegerField(default=0)
    pending_epoch = models.PositiveIntegerField(null=True)
    policy = models.JSONField(default=dict)
    permissions = models.JSONField(default=dict)
    state = models.JSONField(default=dict)


class NaturalMemoryJob(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    job_key = models.CharField(max_length=32, unique=True)
    source_message = models.ForeignKey('api.ChatMessage', null=True, on_delete=models.SET_NULL)
    epoch = models.PositiveIntegerField()
    revision = models.PositiveIntegerField()
    status = models.CharField(max_length=24, default='awaiting_extraction')
    mode = models.CharField(max_length=24, default='new_message')
    snapshot = models.JSONField(default=dict)
    extraction_reply = models.TextField(blank=True)
    extraction_hash = models.CharField(max_length=64, blank=True)
    review_hash = models.CharField(max_length=64, blank=True)
    reason = models.CharField(max_length=80, blank=True)
    audit = models.JSONField(default=dict)
    deadline_at = models.DateTimeField()


class NaturalMemoryOperation(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    request_key = models.CharField(max_length=100)
    input_hash = models.CharField(max_length=64)
    receipt = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'request_key'], name='natural_operation_owner_key')]


class NaturalMemoryAnswerGrant(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    version = models.PositiveIntegerField(default=1)
    cloud_routing = models.BooleanField(default=False)
    cloud_answer_personal_context = models.BooleanField(default=False)


class NaturalMemoryDerived(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    artifact_key = models.CharField(max_length=32, unique=True)
    kind = models.CharField(max_length=20)
    status = models.CharField(max_length=16, default='ready')
    binding = models.JSONField(default=dict)
    payload = models.JSONField(default=dict)
    payload_hash = models.CharField(max_length=64)
    seal = models.CharField(max_length=64)
    expires_at = models.DateTimeField(null=True)
    reason = models.CharField(max_length=64, blank=True)


class NaturalMemoryBackgroundTask(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    source_message = models.OneToOneField('api.ChatMessage', on_delete=models.CASCADE)
    task_key = models.CharField(max_length=32, unique=True)
    status = models.CharField(max_length=24, default='queued')
    snapshot = models.JSONField(default=dict)
    permissions = models.JSONField(default=dict)
    extraction_reply = models.TextField(blank=True)
    extraction_calls = models.PositiveSmallIntegerField(default=0)
    review_calls = models.PositiveSmallIntegerField(default=0)
    deadline_at = models.DateTimeField()
    audit = models.JSONField(default=dict)
    reason = models.CharField(max_length=80, blank=True)


class NaturalMemoryInbox(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    task = models.OneToOneField(NaturalMemoryBackgroundTask, on_delete=models.CASCADE)
    item_key = models.CharField(max_length=32, unique=True)
    status = models.CharField(max_length=24, default='pending')
    payload = models.JSONField(default=dict)
    audit = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class NaturalMemoryDispatchBudget(models.Model):
    batch_key = models.CharField(max_length=64, unique=True)
    authorization_hash = models.CharField(max_length=64)
    approved = models.BooleanField(default=False)
    closed = models.BooleanField(default=False)
    reason = models.CharField(max_length=80, blank=True)
    owner_ids = models.JSONField(default=list)
    stages = models.JSONField(default=list)
    maximum_calls = models.PositiveIntegerField()
    budget_usd = models.DecimalField(max_digits=14, decimal_places=8)
    estimated_usd = models.DecimalField(max_digits=14, decimal_places=8, default=0)
    reserved_usd = models.DecimalField(max_digits=14, decimal_places=8, default=0)
    attempts = models.PositiveIntegerField(default=0)
    lock_version = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField()


class NaturalMemoryDispatchAttempt(models.Model):
    budget = models.ForeignKey(NaturalMemoryDispatchBudget, on_delete=models.CASCADE)
    owner_id = models.PositiveBigIntegerField()
    stage = models.CharField(max_length=24)
    request_ref = models.CharField(max_length=80)
    status = models.CharField(max_length=24, default='dispatching')
    request_hash = models.CharField(max_length=64)
    response_hash = models.CharField(max_length=64, blank=True)
    usage = models.JSONField(default=dict)
    estimated_usd = models.DecimalField(max_digits=14, decimal_places=8, default=0)
    reason = models.CharField(max_length=80, blank=True)
    deadline_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['budget', 'stage', 'request_ref'], name='natural_dispatch_once')]
