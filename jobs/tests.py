from unittest.mock import MagicMock, patch
import shutil
import tempfile
import hashlib
import hmac

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Application, Job, Payment
from django.conf import settings

TEST_MEDIA_ROOT = tempfile.mkdtemp()


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class JobPortalViewTests(TestCase):
	def setUp(self):
		self.job = Job.objects.create(
			title='Security Guard',
			salary='15000',
			application_fee=500,
			location='Jammu',
			description='Test job description',
			category='Security',
		)

	@override_settings(RAZORPAY_KEY_ID='test_key', RAZORPAY_KEY_SECRET='test_secret')
	@patch('jobs.views.razorpay.Client')
	def test_create_order_requires_email(self, mock_client):
		response = self.client.post(reverse('create_order', args=[self.job.id]))
		self.assertEqual(response.status_code, 400)
		self.assertIn('Email is required', response.json().get('error', ''))
		mock_client.assert_not_called()

	@override_settings(RAZORPAY_KEY_ID='test_key', RAZORPAY_KEY_SECRET='test_secret')
	@patch('jobs.views.razorpay.Client')
	def test_create_order_prevents_duplicate_application(self, mock_client):
		resume = SimpleUploadedFile('resume.pdf', b'test', content_type='application/pdf')
		Application.objects.create(
			job=self.job,
			name='Test User',
			email='test@example.com',
			phone='9999999999',
			resume=resume,
			experience='Two years'
		)

		response = self.client.post(
			reverse('create_order', args=[self.job.id]),
			data={'email': 'test@example.com'}
		)
		self.assertEqual(response.status_code, 400)
		self.assertIn('already applied', response.json().get('error', '').lower())
		mock_client.assert_not_called()

	def test_verify_payment_requires_resume(self):
		response = self.client.post(
			reverse('verify_payment', args=[self.job.id]),
			data={
				'name': 'Test User',
				'email': 'test@example.com',
				'phone': '9999999999',
				'experience': 'Two years',
			}
		)
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Resume is required.')

	@override_settings(RAZORPAY_KEY_ID='test_key', RAZORPAY_KEY_SECRET='test_secret')
	@patch('jobs.views.magic.from_buffer', return_value='application/pdf')
	@patch('jobs.views.razorpay.Client')
	def test_verify_payment_success_creates_records(self, mock_client_class, _mock_mime):
		mock_client = MagicMock()
		mock_client.payment.fetch.return_value = {
			'order_id': 'order_1',
			'amount': self.job.application_fee * 100,
			'currency': settings.CURRENCY,
			'status': 'captured'
		}
		mock_client.order.fetch.return_value = {
			'amount': self.job.application_fee * 100,
			'currency': settings.CURRENCY,
			'notes': {
				'job_id': str(self.job.id),
				'applicant_email': 'test@example.com',
			},
		}
		mock_client_class.return_value = mock_client

		Payment.objects.create(
			job=self.job,
			user_name='test@example.com',
			email='test@example.com',
			amount=self.job.application_fee,
			amount_paise=self.job.application_fee * 100,
			currency=settings.CURRENCY,
			razorpay_order_id='order_1',
			status=Payment.Status.INITIATED,
			gateway_status='created',
		)

		signature = hmac.new(
			b'test_secret',
			b'order_1|pay_1',
			hashlib.sha256,
		).hexdigest()

		resume = SimpleUploadedFile('resume.pdf', b'%PDF-1.4 test', content_type='application/pdf')
		response = self.client.post(
			reverse('verify_payment', args=[self.job.id]),
			data={
				'name': 'Test User',
				'email': 'test@example.com',
				'phone': '9999999999',
				'experience': 'Two years',
				'resume': resume,
				'razorpay_payment_id': 'pay_1',
				'razorpay_order_id': 'order_1',
				'razorpay_signature': signature,
			}
		)

		self.assertEqual(response.status_code, 302)
		self.assertRedirects(response, reverse('success'))
		self.assertEqual(Application.objects.count(), 1)
		self.assertEqual(Payment.objects.count(), 1)
		self.assertEqual(Payment.objects.first().status, 'Success')

	def test_download_resume_requires_staff(self):
		resume = SimpleUploadedFile('resume.pdf', b'test', content_type='application/pdf')
		application = Application.objects.create(
			job=self.job,
			name='Test User',
			email='test@example.com',
			phone='9999999999',
			resume=resume,
			experience='Two years'
		)

		response = self.client.get(reverse('download_resume', args=[application.id]))
		self.assertEqual(response.status_code, 302)

		User = get_user_model()
		staff_user = User.objects.create_user(
			username='staff',
			password='password123',
			is_staff=True
		)
		self.client.login(username='staff', password='password123')
		response = self.client.get(reverse('download_resume', args=[application.id]))
		self.assertEqual(response.status_code, 200)

	@classmethod
	def tearDownClass(cls):
		super().tearDownClass()
		shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)
