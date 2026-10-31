from django.urls import path

from agent import views

app_name = 'agent'

urlpatterns = [
    path('', views.chat_page, name='chat'),
    path('send/', views.chat_send, name='chat_send'),
    path('verify/', views.chat_verify, name='chat_verify'),
    path('dismiss/', views.chat_dismiss, name='chat_dismiss'),
]
