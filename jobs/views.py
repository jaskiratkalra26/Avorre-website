from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, FileResponse, Http404, HttpResponse
from django.conf import settings
from .models import Job, Application, Payment, PaymentWebhookEvent
from django.core.mail import send_mail
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt
from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction, IntegrityError
from django.db.models import Q
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from functools import wraps
import razorpay  # type: ignore
import logging
import os
import re
import json
import hmac
import hashlib
import time
from uuid import uuid4
try:
    import magic  # type: ignore[import-not-found]  # python-magic (pip install python-magic or python-magic-bin on Windows)
except ImportError:
    raise ImportError("python-magic is required. Install with 'pip install python-magic' or 'pip install python-magic-bin' on Windows.")

logger = logging.getLogger(__name__)

MAX_RESUME_SIZE_BYTES = 5 * 1024 * 1024
ALLOWED_RESUME_EXTENSIONS = {'.pdf', '.doc', '.docx'}
ALLOWED_RESUME_MIME_TYPES = {
    'application/pdf',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
}
RAZORPAY_ORDER_ID_RE = re.compile(r'^order_[A-Za-z0-9]+$')
RAZORPAY_PAYMENT_ID_RE = re.compile(r'^pay_[A-Za-z0-9]+$')
RAZORPAY_SIGNATURE_RE = re.compile(r'^[A-Fa-f0-9]{64}$')


def _is_valid_email(value):
    try:
        validate_email(value)
        return True
    except ValidationError:
        return False


def _auth_required_for_payments():
    return getattr(settings, 'PAYMENTS_REQUIRE_AUTHENTICATION', False)


def _rate_limited(request, key_prefix, limit, window_seconds):
    if getattr(settings, 'TRUSTED_PROXY', False):
        ip = (request.META.get('HTTP_X_FORWARDED_FOR', '') or request.META.get('REMOTE_ADDR', 'unknown')).split(',')[0].strip()
    else:
        ip = request.META.get('REMOTE_ADDR', 'unknown')
    cache_key = f'rl:{key_prefix}:{ip}'
    current = cache.get(cache_key, 0)
    if current >= limit:
        return True
    cache.set(cache_key, current + 1, timeout=window_seconds)
    return False


def rate_limit(key_prefix, limit=20, window_seconds=60):
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if _rate_limited(request, key_prefix, limit, window_seconds):
                return JsonResponse({'error': 'Too many requests. Please try again shortly.'}, status=429)
            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator


def _validate_payment_tokens(order_id, payment_id, signature):
    if not RAZORPAY_ORDER_ID_RE.match(order_id or ''):
        return False
    if not RAZORPAY_PAYMENT_ID_RE.match(payment_id or ''):
        return False
    if not RAZORPAY_SIGNATURE_RE.match(signature or ''):
        return False
    return True


def _verify_checkout_signature(order_id, payment_id, signature):
    message = f'{order_id}|{payment_id}'.encode('utf-8')
    expected = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode('utf-8'),
        message,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _verify_webhook_signature(raw_body, signature):
    webhook_secret = getattr(settings, 'RAZORPAY_WEBHOOK_SECRET', '')
    if not webhook_secret:
        return False
    expected = hmac.new(
        webhook_secret.encode('utf-8'),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature or '')


def build_apply_context(job, error=None):
    context = {
        'job': job,
        'company_name': settings.COMPANY_NAME,
        'company_phone': settings.COMPANY_PHONE,
        'company_address': settings.COMPANY_ADDRESS,
        'company_website': settings.COMPANY_WEBSITE,
        'application_fee': job.application_fee,
    }
    if error:
        context['error'] = error
    return context

def home(request):
    """View to handle the homepage"""
    
    # Handle search from frontend (Bonus 7)
    query = request.GET.get('search', '')
    if query:
        jobs = Job.objects.filter(
            Q(title__icontains=query) |
            Q(location__icontains=query) |
            Q(description__icontains=query)
        ).order_by('-created_at')
    else:
        jobs = Job.objects.all().order_by('-created_at')

    recent_jobs = Job.objects.order_by('-created_at')[:5]

    context = {
        'jobs': jobs,
        'recent_jobs': recent_jobs,
        'company_name': settings.COMPANY_NAME,
        'company_phone': settings.COMPANY_PHONE,
        'company_address': settings.COMPANY_ADDRESS,
        'company_website': settings.COMPANY_WEBSITE,
        'search_query': query,
        'no_jobs_message': settings.MESSAGE_NO_JOBS,
        'current_year': settings.CURRENT_YEAR,
    }
    return render(request, 'index.html', context)

def job_list(request):
    """View to display all jobs"""
    query = request.GET.get('search', '')
    if query:
        jobs = Job.objects.filter(
            Q(title__icontains=query) |
            Q(location__icontains=query) |
            Q(description__icontains=query)
        ).order_by('-created_at')
    else:
        jobs = Job.objects.all().order_by('-created_at')
    recent_jobs = Job.objects.order_by('-created_at')[:5]

    return render(request, 'jobs.html', {
        'jobs': jobs,
        'recent_jobs': recent_jobs,
        'company_name': settings.COMPANY_NAME,
        'search_query': query,
        'no_jobs_message': settings.MESSAGE_NO_JOBS,
    })

def apply_job(request, job_id):
    """View to handle job applications"""
    job = get_object_or_404(Job, id=job_id)
    return render(request, 'apply.html', build_apply_context(job))

@require_POST
@rate_limit('create-order', limit=10, window_seconds=60)
def create_order(request, job_id):
    """Creates a Razorpay order and returns JSON format order details"""
    job = get_object_or_404(Job, id=job_id)
    email = request.POST.get('email', '').strip()

    if _auth_required_for_payments() and not request.user.is_authenticated:
        return JsonResponse({'error': 'Authentication is required to initiate payment.'}, status=401)

    if not email:
        return JsonResponse({'error': 'Email is required to start payment.'}, status=400)

    if not _is_valid_email(email):
        return JsonResponse({'error': 'A valid email is required.'}, status=400)

    if Application.objects.filter(job=job, email__iexact=email).exists():
        return JsonResponse({'error': 'You have already applied for this job.'}, status=400)

    if Payment.objects.filter(
        job=job,
        email__iexact=email,
        status__in=[Payment.Status.INITIATED, Payment.Status.SUCCESS],
    ).exists():
        return JsonResponse({'error': 'A payment for this job is already in progress.'}, status=400)

    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        return JsonResponse({'error': 'Payment configuration is missing.'}, status=500)

    amount_in_inr = round(job.application_fee)
    if amount_in_inr <= 0:
        return JsonResponse({'error': 'Invalid application fee.'}, status=400)

    amount_in_paise = amount_in_inr * 100

    try:
        client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
        
        # FIX: explicitly pass currency as string to prevent type issues
        order_data = {
            'amount': int(amount_in_paise),
            'currency': str(settings.CURRENCY),
            'receipt': f'job-{job.id}-{uuid4().hex[:16]}',
            'payment_capture': 1,
            'notes': {
                'job_id': str(job.id),
                'applicant_email': email,
            },
        }
        razorpay_order = client.order.create(order_data)

        Payment.objects.create(
            job=job,
            user=request.user if request.user.is_authenticated else None,
            user_name=email,
            email=email,
            amount=amount_in_inr,
            amount_paise=amount_in_paise,
            currency=settings.CURRENCY,
            razorpay_order_id=razorpay_order['id'],
            status=Payment.Status.INITIATED,
            gateway_status='created',
        )

        return JsonResponse({
            'order_id': razorpay_order['id'],
            'amount': amount_in_paise,
            'currency': settings.CURRENCY,
            'key_id': settings.RAZORPAY_KEY_ID
        })
    except Exception:
        logger.exception("Razorpay order creation failed")
        return JsonResponse({'error': 'Payment service is currently unavailable.'}, status=502)

@require_POST
def verify_payment(request, job_id):
    """Verifies payment signature from Razorpay. On success saves Application and Payment models"""
    job = get_object_or_404(Job, id=job_id)

    name = request.POST.get('name', '').strip()
    email = request.POST.get('email', '').strip()
    phone = request.POST.get('phone', '').strip()
    experience = request.POST.get('experience', '').strip()
    resume = request.FILES.get('resume')

    if _auth_required_for_payments() and not request.user.is_authenticated:
        return render(request, 'apply.html', build_apply_context(job, 'Please login before continuing.'))

    if not all([name, email, phone, experience]):
        return render(request, 'apply.html', build_apply_context(job, 'All fields are required.'))

    if len(name) > 100:
        return render(request, 'apply.html', build_apply_context(job, 'Name must be 100 characters or less.'))
    if len(phone) > 20:
        return render(request, 'apply.html', build_apply_context(job, 'Phone number must be 20 characters or less.'))
    if len(experience) > 1000:
        return render(request, 'apply.html', build_apply_context(job, 'Experience must be 1000 characters or less.'))

    if not _is_valid_email(email):
        return render(request, 'apply.html', build_apply_context(job, 'Please enter a valid email address.'))

    if len(email) > 254:
        return render(request, 'apply.html', build_apply_context(job, 'Email address is too long.'))

    if not resume:
        return render(request, 'apply.html', build_apply_context(job, 'Resume is required.'))

    resume_ext = os.path.splitext(resume.name)[1].lower()
    if resume_ext not in ALLOWED_RESUME_EXTENSIONS:
        return render(request, 'apply.html', build_apply_context(job, 'Resume must be PDF or DOC/DOCX.'))

    resume_header = resume.read(2048)
    resume.seek(0)
    resume_mime = magic.from_buffer(resume_header, mime=True)
    if resume_mime not in ALLOWED_RESUME_MIME_TYPES:
        return render(request, 'apply.html', build_apply_context(job, 'Resume must be PDF or DOC/DOCX.'))

    if resume.size > MAX_RESUME_SIZE_BYTES:
        return render(request, 'apply.html', build_apply_context(job, 'Resume must be 5MB or smaller.'))

    razorpay_payment_id = request.POST.get('razorpay_payment_id')
    razorpay_order_id = request.POST.get('razorpay_order_id')
    razorpay_signature = request.POST.get('razorpay_signature')

    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        return render(request, 'apply.html', build_apply_context(job, 'Payment configuration is missing.'))

    if not all([razorpay_payment_id, razorpay_order_id, razorpay_signature]):
        return render(request, 'apply.html', build_apply_context(job, 'Payment data is incomplete.'))

    if not _validate_payment_tokens(razorpay_order_id, razorpay_payment_id, razorpay_signature):
        return render(request, 'apply.html', build_apply_context(job, 'Invalid payment reference data.'))

    client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
    amount_in_inr = round(job.application_fee)
    if amount_in_inr <= 0:
        return render(request, 'apply.html', build_apply_context(job, 'Invalid application fee.'))

    amount_in_paise = amount_in_inr * 100

    with transaction.atomic():
        payment_record = Payment.objects.select_for_update().filter(
            razorpay_order_id=razorpay_order_id,
            job=job,
        ).first()

        if not payment_record:
            logger.warning('Unknown order verification attempt for job_id=%s order_id=%s', job.id, razorpay_order_id)
            return render(request, 'apply.html', build_apply_context(job, 'Order validation failed. Please restart payment.'))

        if payment_record.email.lower() != email.lower():
            logger.warning('Order ownership mismatch for order_id=%s', razorpay_order_id)
            payment_record.status = Payment.Status.FAILED
            payment_record.failure_reason = 'Order ownership mismatch'
            payment_record.save(update_fields=['status', 'failure_reason', 'updated_at'])
            return render(request, 'apply.html', build_apply_context(job, 'Order ownership validation failed.'))

        if payment_record.user_id and request.user.is_authenticated and payment_record.user_id != request.user.id:
            logger.warning('Authenticated user mismatch for order_id=%s', razorpay_order_id)
            return render(request, 'apply.html', build_apply_context(job, 'This payment does not belong to your account.'))

        if payment_record.amount_paise != amount_in_paise or payment_record.currency != settings.CURRENCY:
            logger.warning('Amount mismatch against server record for order_id=%s', razorpay_order_id)
            payment_record.status = Payment.Status.FAILED
            payment_record.failure_reason = 'Amount mismatch against server order record'
            payment_record.save(update_fields=['status', 'failure_reason', 'updated_at'])
            return render(request, 'apply.html', build_apply_context(job, 'Payment amount verification failed.'))

    try:
        if not _verify_checkout_signature(razorpay_order_id, razorpay_payment_id, razorpay_signature):
            raise ValueError('Invalid checkout signature')

        fetched_payment = client.payment.fetch(razorpay_payment_id)
        if fetched_payment.get('order_id') != razorpay_order_id:
            raise ValueError('Payment order mismatch')
        if int(fetched_payment.get('amount', 0)) != payment_record.amount_paise:
            raise ValueError('Payment amount mismatch')
        if fetched_payment.get('currency') != payment_record.currency:
            raise ValueError('Payment currency mismatch')

        fetched_order = client.order.fetch(razorpay_order_id)
        if int(fetched_order.get('amount', 0)) != payment_record.amount_paise:
            raise ValueError('Order amount mismatch')
        if fetched_order.get('currency') != payment_record.currency:
            raise ValueError('Order currency mismatch')

        order_notes = fetched_order.get('notes') or {}
        if str(order_notes.get('job_id', '')) != str(job.id):
            raise ValueError('Order job mismatch')
        if str(order_notes.get('applicant_email', '')).lower() != email.lower():
            raise ValueError('Order email mismatch')

        captured_payment = None
        if fetched_payment.get('status') == 'authorized':
            captured_payment = client.payment.capture(razorpay_payment_id, payment_record.amount_paise)

        if captured_payment is not None:
            if captured_payment.get('status') != 'captured':
                raise ValueError('Payment not captured')
        elif fetched_payment.get('status') != 'captured':
            raise ValueError('Payment not captured')

        try:
            with transaction.atomic():
                payment_record = Payment.objects.select_for_update().get(id=payment_record.id)

                if payment_record.status == Payment.Status.SUCCESS and payment_record.application_id:
                    return redirect('success')

                existing_payment = Payment.objects.filter(razorpay_payment_id=razorpay_payment_id).exclude(id=payment_record.id).first()
                if existing_payment:
                    payment_record.status = Payment.Status.DUPLICATE
                    payment_record.failure_reason = 'Duplicate payment id received'
                    payment_record.save(update_fields=['status', 'failure_reason', 'updated_at'])
                    return render(request, 'apply.html', build_apply_context(job, 'Payment already processed. Please contact support.'))

                if Application.objects.filter(job=job, email__iexact=email).exists():
                    payment_record.user_name = name
                    payment_record.razorpay_payment_id = razorpay_payment_id
                    payment_record.razorpay_signature = razorpay_signature
                    payment_record.status = Payment.Status.DUPLICATE
                    payment_record.gateway_status = fetched_payment.get('status', '')
                    payment_record.failure_reason = 'Duplicate application attempt'
                    payment_record.save(update_fields=[
                        'user_name',
                        'razorpay_payment_id',
                        'razorpay_signature',
                        'status',
                        'gateway_status',
                        'failure_reason',
                        'updated_at',
                    ])
                    return render(request, 'apply.html', build_apply_context(job, 'You have already applied for this job.'))

                app_instance = Application.objects.create(
                    job=job,
                    name=name,
                    email=email,
                    phone=phone,
                    resume=resume,
                    experience=experience
                )

                payment_record.user_name = name
                payment_record.email = email
                payment_record.razorpay_payment_id = razorpay_payment_id
                payment_record.razorpay_signature = razorpay_signature
                payment_record.status = Payment.Status.SUCCESS
                payment_record.gateway_status = (captured_payment or fetched_payment).get('status', '')
                payment_record.failure_reason = ''
                payment_record.application = app_instance
                payment_record.save(update_fields=[
                    'user_name',
                    'email',
                    'razorpay_payment_id',
                    'razorpay_signature',
                    'status',
                    'gateway_status',
                    'failure_reason',
                    'application',
                    'updated_at',
                ])
        except IntegrityError:
            payment_record.status = Payment.Status.DUPLICATE
            payment_record.failure_reason = 'Duplicate transaction race'
            payment_record.save(update_fields=['status', 'failure_reason', 'updated_at'])
            return render(request, 'apply.html', build_apply_context(job, 'You have already applied for this job.'))

        try:
            if settings.EMAIL_HOST_USER:
                subject = f"Application Received - {job.title}"
                message = (
                    f"Hi {name},\n\n"
                    f"Your application for the position of {job.title} has been successfully received.\n"
                    "Our team will review your profile and get back to you shortly.\n\n"
                    "Thank you,\nAvorre Group"
                )
                send_mail(
                    subject,
                    message,
                    settings.EMAIL_HOST_USER,
                    [email],
                    fail_silently=True,
                )
        except Exception:
            logger.exception('Application confirmation email failed for order_id=%s', razorpay_order_id)

        request.session['payment_success'] = time.time()
        return redirect('success')

    except Exception:
        logger.exception('Payment verification failed for order_id=%s', razorpay_order_id)
        payment_record.status = Payment.Status.FAILED
        payment_record.user_name = name
        payment_record.failure_reason = 'Verification failed'
        payment_record.save(update_fields=['status', 'user_name', 'failure_reason', 'updated_at'])
        return render(request, 'apply.html', build_apply_context(job, 'Payment could not be verified. Please contact support.'))


@csrf_exempt
@require_POST
@rate_limit('razorpay-webhook', limit=120, window_seconds=60)
def razorpay_webhook(request):
    raw_body = request.body
    signature = request.META.get('HTTP_X_RAZORPAY_SIGNATURE', '')

    if not getattr(settings, 'RAZORPAY_WEBHOOK_SECRET', ''):
        logger.error('RAZORPAY_WEBHOOK_SECRET is missing; webhook request rejected.')
        return JsonResponse({'error': 'Webhook configuration is missing.'}, status=500)

    if not _verify_webhook_signature(raw_body, signature):
        logger.warning('Razorpay webhook signature verification failed.')
        return JsonResponse({'error': 'Invalid webhook signature.'}, status=400)

    try:
        payload = json.loads(raw_body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({'error': 'Invalid webhook payload.'}, status=400)

    event_type = payload.get('event', '')
    event_id = request.META.get('HTTP_X_RAZORPAY_EVENT_ID') or payload.get('id')
    if not event_id:
        event_id = hashlib.sha256(raw_body).hexdigest()

    try:
        with transaction.atomic():
            PaymentWebhookEvent.objects.create(
                event_id=event_id,
                event_type=event_type,
                payload=payload,
            )
    except IntegrityError:
        logger.info('Duplicate Razorpay webhook event ignored: %s', event_id)
        return HttpResponse(status=200)

    try:
        payment_entity = payload.get('payload', {}).get('payment', {}).get('entity', {})
        order_entity = payload.get('payload', {}).get('order', {}).get('entity', {})

        order_id = payment_entity.get('order_id') or order_entity.get('id')
        payment_id = payment_entity.get('id')
        payment_status = payment_entity.get('status', '')
        failure_reason = payment_entity.get('error_description', '')

        if order_id:
            with transaction.atomic():
                payment_record = Payment.objects.select_for_update().filter(razorpay_order_id=order_id).first()
                if payment_record:
                    if payment_id and not payment_record.razorpay_payment_id:
                        payment_record.razorpay_payment_id = payment_id
                    payment_record.gateway_status = payment_status

                    if event_type in ('payment.captured', 'order.paid'):
                        if payment_record.application_id:
                            payment_record.status = Payment.Status.SUCCESS
                            payment_record.failure_reason = ''
                        else:
                            logger.warning('Payment captured without application for order_id=%s', order_id)
                            payment_record.status = Payment.Status.NEEDS_REVIEW
                            payment_record.failure_reason = 'Captured without application'
                    elif event_type in ('payment.failed',):
                        payment_record.status = Payment.Status.FAILED
                        payment_record.failure_reason = failure_reason or 'Gateway reported payment failure'

                    payment_record.save(update_fields=[
                        'razorpay_payment_id',
                        'gateway_status',
                        'status',
                        'failure_reason',
                        'updated_at',
                    ])

        return HttpResponse(status=200)
    except Exception as exc:
        logger.exception('Webhook handling failed for event_id=%s', event_id)
        PaymentWebhookEvent.objects.filter(event_id=event_id).update(
            status=PaymentWebhookEvent.Status.FAILED,
            error_message=str(exc)[:255],
        )
        return HttpResponse(status=200)

def success(request):
    """Success page after application submission"""
    ts = request.session.pop('payment_success', None)
    if not ts or (time.time() - ts) > 300:
        return redirect('home')
    return render(request, 'success.html', {
        'company_name': settings.COMPANY_NAME,
        'message': settings.MESSAGE_PAYMENT_SUCCESS,
        'message_desc': settings.MESSAGE_PAYMENT_SUCCESS_DESC,
    })


@staff_member_required
def download_resume(request, application_id):
    application = get_object_or_404(Application, id=application_id)
    if not application.resume:
        raise Http404('Resume not found')
    resume_file = application.resume.open('rb')
    filename = os.path.basename(application.resume.name)
    return FileResponse(resume_file, as_attachment=True, filename=filename, content_type='application/octet-stream')
