import uuid
from django.db import models


class Station(models.Model):
    opis_id = models.PositiveIntegerField(primary_key=True)
    name = models.CharField(max_length=250)
    address = models.CharField(max_length=300)
    city = models.CharField(max_length=150)
    state = models.CharField(max_length=2)
    price = models.DecimalField(max_digits=12, decimal_places=8)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    coordinate_source = models.TextField(blank=True)
    coordinate_precision = models.CharField(max_length=30, blank=True)

    class Meta:
        indexes = [models.Index(fields=['latitude', 'longitude'])]


class Dataset(models.Model):
    key = models.CharField(max_length=30, primary_key=True, default='fuel')
    fingerprint = models.CharField(max_length=64)
    summary = models.JSONField(default=dict)
    imported_at = models.DateTimeField(auto_now=True)


class RouteCache(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    payload = models.JSONField()
    expires_at = models.DateTimeField()


class Trip(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=64, unique=True)
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()


class ServiceGate(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    available_at = models.FloatField(default=0)
