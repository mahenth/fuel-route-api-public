from django.urls import path
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from planner import views

urlpatterns = [
    path('', views.index, name='home'),
    path('maps/<uuid:trip_id>/', views.index, name='map'),
    path('api/health/', views.health),
    path('api/locations/', views.locations),
    path('api/routes/', views.create_route),
    path('api/routes/<uuid:trip_id>/', views.route_detail),
    path('api/schema/', views.schema),
]
urlpatterns += staticfiles_urlpatterns()
