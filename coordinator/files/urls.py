from django.urls import path

from . import views

urlpatterns = [
    path("", views.FileListCreateView.as_view(), name="file-list"),
    path("<uuid:file_id>", views.FileDetailView.as_view(), name="file-detail"),
    path("<uuid:file_id>/complete", views.FileCompleteView.as_view(), name="file-complete"),
    path("<uuid:file_id>/segments/<int:index>", views.SegmentView.as_view(), name="file-segment"),
]
