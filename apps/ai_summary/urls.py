"""URL routes for apps.ai_summary."""

from django.urls import path

from apps.ai_summary import views

app_name = "ai_summary"

urlpatterns = [
    path("", views.AISummaryPageView.as_view(), name="summary"),
    path("generate/", views.GenerateAISummaryView.as_view(), name="generate"),
]
