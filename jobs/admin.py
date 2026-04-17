from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from .models import Job, Application, Payment, PaymentWebhookEvent

@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ('title', 'salary', 'application_fee', 'location', 'created_at')
    search_fields = ('title', 'location')
    list_filter = ('category', 'created_at')
    list_editable = ('salary', 'application_fee', 'location')

class PaymentInline(admin.StackedInline):
    model = Payment
    extra = 0
    readonly_fields = (
        'razorpay_order_id',
        'razorpay_payment_id',
        'amount',
        'amount_paise',
        'currency',
        'status',
        'gateway_status',
        'failure_reason',
        'created_at',
        'updated_at',
    )
    can_delete = False

@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'phone', 'job', 'status', 'created_at', 'resume_link', 'payment_status')
    search_fields = ('name', 'email')
    list_filter = ('status', 'job', 'created_at')
    list_editable = ('status',)
    inlines = [PaymentInline]

    # Added custom dashboard override specifically tied to Applications for basic analytics (Req 6)
    def changelist_view(self, request, extra_context=None):
        response = super().changelist_view(
            request,
            extra_context=extra_context
        )
        try:
            qs = response.context_data['cl'].queryset
        except (AttributeError, KeyError):
            return response
        
        # Calculate Basic Analytics
        metrics = {
            'total_jobs': Job.objects.count(),
            'total_applications': qs.count(),
            'successful_payments': Payment.objects.filter(status='Success').count()
        }
        response.context_data['metrics'] = metrics
        return response

    def resume_link(self, obj):
        if obj.resume:
            url = reverse('download_resume', args=[obj.id])
            return format_html('<a href="{}" target="_blank">Download Resume</a>', url)
        return "No Resume"
    resume_link.short_description = 'Resume'

    def payment_status(self, obj):
        if hasattr(obj, 'payment') and obj.payment:
            status = obj.payment.status
            color = 'green' if status == 'Success' else 'red'
            return format_html('<span style="color: {}; font-weight: bold;">{}</span>', color, status)
        return "Pending/Failed"
    payment_status.short_description = 'Payment Status'

@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('user_name', 'email', 'amount', 'currency', 'status', 'gateway_status', 'created_at')
    search_fields = ('user_name', 'email', 'razorpay_order_id', 'razorpay_payment_id')
    list_filter = ('status', 'created_at')
    readonly_fields = (
        'razorpay_order_id',
        'razorpay_payment_id',
        'razorpay_signature',
        'amount',
        'amount_paise',
        'currency',
        'gateway_status',
        'failure_reason',
        'created_at',
        'updated_at',
    )


@admin.register(PaymentWebhookEvent)
class PaymentWebhookEventAdmin(admin.ModelAdmin):
    list_display = ('event_id', 'event_type', 'status', 'created_at')
    search_fields = ('event_id', 'event_type')
    list_filter = ('status', 'event_type', 'created_at')
    readonly_fields = ('event_id', 'event_type', 'payload', 'status', 'error_message', 'created_at')
