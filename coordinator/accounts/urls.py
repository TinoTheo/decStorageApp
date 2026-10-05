from django.urls import path

from . import views

urlpatterns = [
    path("prelogin", views.PreloginView.as_view(), name="auth-prelogin"),
    path("register", views.RegisterView.as_view(), name="auth-register"),
    path("login", views.LoginView.as_view(), name="auth-login"),
    path("recovery-bundle", views.RecoveryBundleView.as_view(), name="auth-recovery-bundle"),
    path("recover", views.RecoverView.as_view(), name="auth-recover"),
    path("logout", views.LogoutView.as_view(), name="auth-logout"),
    path("me", views.MeView.as_view(), name="auth-me"),
]
