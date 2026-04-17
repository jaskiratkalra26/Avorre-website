from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('jobs/', views.job_list, name='job_list'),
    path('apply/<int:job_id>/', views.apply_job, name='apply_job'),
    path('create-order/<int:job_id>/', views.create_order, name='create_order'),
    path('verify-payment/<int:job_id>/', views.verify_payment, name='verify_payment'),
    path('payments/webhook/razorpay/', views.razorpay_webhook, name='razorpay_webhook'),
    path('success/', views.success, name='success'),
    path('resumes/<int:application_id>/download/', views.download_resume, name='download_resume'),
]
