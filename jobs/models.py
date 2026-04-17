from django.conf import settings
from django.db import models

class Job(models.Model):
    title = models.CharField(max_length=255)
    salary = models.CharField(max_length=100)
    application_fee = models.IntegerField(default=500, help_text="Application fee in INR for this specific job")
    location = models.CharField(max_length=255)
    description = models.TextField()
    category = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title

class Application(models.Model):
    STATUS_CHOICES = (
        ('Pending', 'Pending'),
        ('Reviewed', 'Reviewed'),
        ('Selected', 'Selected'),
        ('Rejected', 'Rejected'),
    )

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='applications')
    name = models.CharField(max_length=255)
    email = models.EmailField()
    phone = models.CharField(max_length=20)
    resume = models.FileField(upload_to='resumes/')
    experience = models.TextField()
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Pending')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['job', 'email'], name='uniq_application_per_job_email'),
        ]

    def __str__(self):
        return f"{self.name} - {self.job.title}"

class Payment(models.Model):
    class Status(models.TextChoices):
        INITIATED = 'Initiated', 'Initiated'
        SUCCESS = 'Success', 'Success'
        FAILED = 'Failed', 'Failed'
        DUPLICATE = 'Duplicate', 'Duplicate'
        CANCELLED = 'Cancelled', 'Cancelled'
        NEEDS_REVIEW = 'NeedsReview', 'NeedsReview'

    job = models.ForeignKey(Job, on_delete=models.SET_NULL, null=True, blank=True, related_name='payments')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='job_payments')
    user_name = models.CharField(max_length=255, blank=True, default='')
    email = models.EmailField(blank=True, default='')
    application = models.OneToOneField(Application, on_delete=models.SET_NULL, null=True, blank=True, related_name='payment')
    amount = models.PositiveIntegerField(help_text="Amount in INR")
    amount_paise = models.PositiveIntegerField(default=0, help_text="Amount in paise")
    currency = models.CharField(max_length=10, default='INR')
    razorpay_order_id = models.CharField(max_length=255, unique=True, db_index=True)
    razorpay_payment_id = models.CharField(max_length=255, blank=True, null=True, unique=True, db_index=True)
    razorpay_signature = models.CharField(max_length=255, blank=True, null=True)
    gateway_status = models.CharField(max_length=50, blank=True, default='')
    failure_reason = models.CharField(max_length=255, blank=True, default='')
    status = models.CharField(max_length=50, choices=Status.choices, default=Status.INITIATED, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['email', 'created_at']),
            models.Index(fields=['status', 'created_at']),
        ]

    def __str__(self):
        return f"{self.user_name} - {self.amount} INR - {self.status}"


class PaymentWebhookEvent(models.Model):
    class Status(models.TextChoices):
        PROCESSED = 'Processed', 'Processed'
        FAILED = 'Failed', 'Failed'

    event_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=100)
    payload = models.JSONField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PROCESSED)
    error_message = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.event_type} ({self.event_id})"
