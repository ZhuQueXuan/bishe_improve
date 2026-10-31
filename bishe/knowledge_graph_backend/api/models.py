from django.db import models
from django.contrib.auth.models import User


class Conversation(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='conversations')
    title = models.CharField(max_length=120, default='新对话')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    memory_after_id = models.PositiveBigIntegerField(default=0)
    memory_version = models.PositiveIntegerField(default=0)
    context_notes = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['user', '-updated_at'], name='conversation_user_updated_idx'),
        ]

    def __str__(self):
        return f'{self.user.username}: {self.title}'


class ChatMessage(models.Model):
    ROLE_CHOICES = [
        ('user', '用户'),
        ('assistant', '助手'),
    ]

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name='messages',
    )
    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    content = models.TextField()
    has_context = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']
        indexes = [
            models.Index(fields=['conversation', 'created_at'], name='message_conversation_time_idx'),
        ]

    def __str__(self):
        return f'{self.role}: {self.content[:30]}'


class MemorySettings(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='memory_settings')
    enabled = models.BooleanField(default=True)
    auto_enabled = models.BooleanField(default=True)
    health_collect_enabled = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=0)


class UserMemory(models.Model):
    """Explicit finite presentation choices, never inferred medical history."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='user_memories')
    kind = models.CharField(max_length=32)
    value = models.CharField(max_length=32, blank=True)
    status = models.CharField(max_length=16, default='active')
    version = models.PositiveIntegerField(default=1)
    source = models.CharField(max_length=32, default='manual_selection')
    source_text = models.CharField(max_length=160, blank=True)
    source_message_ref = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user','kind'], name='memory_user_kind_unique')]


class MemoryInputEvent(models.Model):
    user = models.ForeignKey(User,on_delete=models.CASCADE)
    event_id = models.CharField(max_length=64)
    input_hash = models.CharField(max_length=64)
    source_message = models.ForeignKey(ChatMessage, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['user','event_id'],name='memory_input_user_event_unique')]


class HealthMemory(models.Model):
    """Confirmed, finite self-reports. Never a diagnosis or a knowledge source."""
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    report = models.JSONField(default=dict)
    status = models.CharField(max_length=16, default='active')
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class HealthMemoryProposal(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    request_key = models.CharField(max_length=64)
    input_hash = models.CharField(max_length=64)
    source_message = models.ForeignKey(ChatMessage, null=True, on_delete=models.SET_NULL)
    profile_version = models.PositiveIntegerField()
    conversation_version = models.PositiveIntegerField()
    status = models.CharField(max_length=16, default='pending')
    result_id = models.PositiveBigIntegerField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user','request_key'], name='health_proposal_user_key_unique')]


class MemorySourceExclusion(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    message_id = models.PositiveBigIntegerField()
    reason = models.CharField(max_length=32, default='user_forgot')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user','message_id'],name='memory_exclusion_user_message_unique')]


class MemoryMaintenanceTask(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    request_key = models.CharField(max_length=64)
    profile_version = models.PositiveIntegerField()
    status = models.CharField(max_length=16, default='queued')
    attempts = models.PositiveIntegerField(default=0)
    lease_token = models.CharField(max_length=64, blank=True)
    lease_until = models.DateTimeField(null=True)
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user','request_key'],name='memory_task_user_request_unique')]


class PersonalMemoryPolicy(models.Model):
    """P02 opt-in policy; never inferred from legacy auto_enabled."""
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    version = models.PositiveIntegerField(default=0)
    collect_types = models.JSONField(default=list)
    history_conversation_ids = models.JSONField(default=list)
    use_enabled = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)


class PersonalMemoryEntry(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    memory_key = models.CharField(max_length=32)
    version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=16, default='active', choices=[
        ('active','有效'), ('disputed','待核对'), ('paused','停用'), ('forgotten','已遗忘')])
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user','memory_key'],name='personal_entry_owner_key')]


class PersonalMemoryRevision(models.Model):
    entry = models.ForeignKey(PersonalMemoryEntry, on_delete=models.CASCADE, related_name='revisions')
    version = models.PositiveIntegerField()
    previous_version = models.PositiveIntegerField(null=True)
    candidate = models.JSONField(default=dict)
    source_message = models.ForeignKey(ChatMessage, null=True, on_delete=models.SET_NULL)
    source_hash = models.CharField(max_length=64, blank=True)
    conversation_version = models.PositiveIntegerField(default=0)
    source_time = models.DateTimeField(null=True)
    origin = models.CharField(max_length=24, default='automatic_candidate')
    user_confirmed = models.BooleanField(default=False)
    status = models.CharField(max_length=16, default='active')
    index_status = models.CharField(max_length=24, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['entry','version'],name='personal_revision_unique')]


class PersonalMemoryReceipt(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    request_key = models.CharField(max_length=64)
    input_hash = models.CharField(max_length=64)
    result = models.JSONField(default=dict)
    entry = models.ForeignKey(PersonalMemoryEntry, null=True, on_delete=models.SET_NULL)
    source_message = models.ForeignKey(ChatMessage, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user','request_key'],name='personal_receipt_owner_key')]


class PersonalMemoryIndexTask(models.Model):
    entry = models.ForeignKey(PersonalMemoryEntry, on_delete=models.CASCADE)
    revision = models.ForeignKey(PersonalMemoryRevision, on_delete=models.CASCADE)
    operation = models.CharField(max_length=16)
    policy_version = models.PositiveIntegerField()
    content_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=16, default='queued')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['entry','revision','operation'],name='personal_index_revision_op')]


# Registration only. No query, feature enablement or production route on import.
from .natural_memory_models import NaturalMemoryLedger, NaturalMemoryJob, NaturalMemoryOperation  # noqa: E402,F401
from .natural_memory_models import NaturalMemoryAnswerGrant  # noqa: E402,F401
from .natural_memory_models import NaturalMemoryDerived  # noqa: E402,F401
from .natural_memory_models import NaturalMemoryBackgroundTask, NaturalMemoryInbox  # noqa: E402,F401
from .natural_memory_models import NaturalMemoryDispatchBudget, NaturalMemoryDispatchAttempt  # noqa: E402,F401
