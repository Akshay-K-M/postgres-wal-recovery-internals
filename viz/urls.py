from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("api/start", views.start),
    path("api/step", views.step),
    path("api/stop", views.stop),
]
