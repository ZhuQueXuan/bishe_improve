from django.db import models
from django.contrib.auth.models import User


class Conversation(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='conversations')
    title = models.CharField(max_length=120, default='新对话')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

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
