from django.db import models


class AIConversation(models.Model):
    """One ongoing agent session on a channel.

    Ownership binds to a verified email only after the user completes OTP
    verification; until then the conversation is anonymous and the agent
    must not touch escrow records. ``channel_identity`` is the channel's
    own session marker (Django session key for web, sender id for
    WhatsApp) — never proof of identity by itself.
    """

    CHANNEL_CHOICES = [
        ('web', 'Website chat'),
        ('whatsapp', 'WhatsApp'),
    ]

    channel = models.CharField(max_length=15, choices=CHANNEL_CHOICES)
    channel_identity = models.CharField(max_length=255, db_index=True)
    owner_email = models.EmailField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-id']

    def __str__(self):
        return f"{self.channel}:{self.channel_identity}"


class ConversationMessage(models.Model):
    """A single persisted turn. Tool calls and results are stored as their
    own rows so history can be replayed without re-executing anything."""

    ROLE_CHOICES = [
        ('user', 'User'),
        ('assistant', 'Assistant'),
        ('tool', 'Tool result'),
    ]

    conversation = models.ForeignKey(
        AIConversation, on_delete=models.CASCADE, related_name='messages'
    )
    role = models.CharField(max_length=15, choices=ROLE_CHOICES)
    content = models.TextField(blank=True)
    tool_name = models.CharField(max_length=100, null=True, blank=True)
    tool_arguments = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f"{self.role} message {self.id}"
