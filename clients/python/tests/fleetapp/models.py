"""Django models for the fleet-telemetry tables used by the integration tests."""

from django.db import models


class Organization(models.Model):
    id = models.UUIDField(primary_key=True)
    name = models.TextField()
    region = models.TextField()

    class Meta:
        app_label = "fleetapp"
        managed = False
        db_table = "organizations"


class Device(models.Model):
    STATUS = [("active", "Active"), ("inactive", "Inactive"), ("retired", "Retired")]

    id = models.UUIDField(primary_key=True)
    org = models.ForeignKey(Organization, on_delete=models.DO_NOTHING, db_column="org_id")
    serial = models.TextField()
    status = models.CharField(max_length=16, choices=STATUS)
    model = models.TextField()
    installed_at = models.DateTimeField()

    class Meta:
        app_label = "fleetapp"
        managed = False
        db_table = "devices"


class Trip(models.Model):
    id = models.BigAutoField(primary_key=True)
    org = models.ForeignKey(Organization, on_delete=models.DO_NOTHING, db_column="org_id")
    device = models.ForeignKey(Device, on_delete=models.DO_NOTHING)
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField()
    distance_m = models.FloatField(null=True, help_text="Metres travelled")
    is_assisted = models.BooleanField(default=False)
    note = models.TextField(null=True)
    service_date = models.DateField(null=True)
    cost = models.DecimalField(max_digits=10, decimal_places=2, null=True)
    payload = models.JSONField(null=True)
    duration = models.DurationField(null=True)
    tags = models.ManyToManyField("Tag")

    class Meta:
        app_label = "fleetapp"
        managed = False
        db_table = "trips"


class Tag(models.Model):
    label = models.SlugField()

    class Meta:
        app_label = "fleetapp"
        db_table = "tags"


class Patient(models.Model):
    email = models.EmailField(null=True)
    ward = models.TextField(null=True)
    organization = models.OneToOneField(Organization, on_delete=models.DO_NOTHING, null=True)

    class Meta:
        app_label = "fleetapp"
        managed = False
        db_table = "patients"


class PatientProxy(Patient):
    class Meta:
        app_label = "fleetapp"
        proxy = True
