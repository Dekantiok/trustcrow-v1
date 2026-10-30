from django.contrib import admin
from django.urls import path
from escrow import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', views.home_view, name='home'),
    path('my-escrow/', views.my_escrow, name='my_escrow'),
    path('my-escrow/verify/', views.my_escrow_verify_otp, name='my_escrow_verify_otp'),
    path('my-escrow/list/', views.my_escrow_list, name='my_escrow_list'),
    path('join/', views.join_escrow_page, name='join_escrow_page'),
    path('create/', views.create_contract, name='create_contract'),
    path('verify/<str:code>/', views.verify_otp_view, name='verify_otp'),
    path('escrow/<str:code>/', views.contract_detail, name='contract_detail'),
    path('escrow/<str:code>/verify/', views.counterparty_verify_otp, name='counterparty_verify_otp'),
    path('escrow/<str:code>/pay/', views.initiate_payment, name='initiate_payment'),
    path('escrow/<str:code>/callback/', views.payment_callback, name='payment_callback'),
    path('webhooks/paystack/', views.paystack_webhook, name='paystack_webhook'),
    path('escrow/<str:code>/dispatch/', views.seller_confirm_dispatch, name='seller_confirm_dispatch'),
    path('escrow/<str:code>/receipt/', views.buyer_confirm_receipt, name='buyer_confirm_receipt'),
    path('escrow/<str:code>/release/', views.buyer_release_funds, name='buyer_release_funds'),
    path('escrow/<str:code>/dispute/', views.buyer_raise_dispute, name='buyer_raise_dispute'),
    path('escrow/<str:code>/seller-dispute/', views.seller_raise_dispute, name='seller_raise_dispute'),
    path('escrow/<str:code>/cancel/', views.cancel_escrow, name='cancel_escrow'),
    path('escrow/<str:code>/vault/', views.seller_add_vault, name='seller_add_vault'),
]