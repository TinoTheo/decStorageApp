from django.urls import path

from . import views

urlpatterns = [
    path("register", views.RegisterView.as_view(), name="node-register"),
    path("heartbeat", views.HeartbeatView.as_view(), name="node-heartbeat"),
    path("tasks/<int:replica_id>", views.TaskResultView.as_view(), name="node-task-result"),
]
