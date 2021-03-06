from django.db import models
from django.conf import settings
from django.contrib.auth.models import User  # noqa
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db.models.signals import post_save
from django.dispatch import receiver

from recurrence.fields import RecurrenceField
from rest_framework.authtoken.models import Token

import extract_targets


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_auth_token(sender, instance=None, created=False, **kwargs):
    """Automatically generate an API key when a user is created, then create Agent."""

    if created:
        # Generate API token for user.
        api_token = Token.objects.create(user=instance)

        # Only create agent using username and API token for non-admin users.
        if instance.is_superuser is False:
            Agent.objects.create(scan_agent=instance, api_token=api_token)


class Agent(models.Model):
    """Model for an Agent"""

    id = models.AutoField(primary_key=True, verbose_name="Agent ID")
    scan_agent = models.CharField(
        unique=True,
        max_length=255,
        validators=[
            RegexValidator(
                regex="^[a-zA-Z0-9/()_\- ]*$",  # Must escape -
                message="Agent name can only contain alphanumeric characters, /, (), -, _, or spaces",
            )
        ],
        verbose_name="Agent Name",
    )
    description = models.CharField(unique=False, max_length=255, blank=True, verbose_name="Agent Description")
    api_token = models.CharField(unique=True, max_length=40, blank=False, verbose_name="API Key")
    last_checkin = models.DateTimeField(blank=True, null=True, verbose_name="Last Agent Check In")

    def __str__(self):
        return str(self.scan_agent)

    class Meta:
        verbose_name_plural = "Agents"


class ScanCommand(models.Model):
    """Model for a scan command"""

    # fmt: off
    SCAN_BINARY = (
        ("masscan", "masscan"),
        ("nmap", "nmap"),
    )
    # fmt: on

    id = models.AutoField(primary_key=True, verbose_name="scan command ID")
    scan_binary = models.CharField(max_length=7, choices=SCAN_BINARY, default="nmap", verbose_name="Scan binary")
    scan_command_name = models.CharField(unique=True, max_length=255, verbose_name="Scan command name")
    scan_command = models.TextField(unique=False, verbose_name="Scan command")

    def __str__(self):
        return f"{self.scan_binary}||{self.scan_command_name}"

    class Meta:
        verbose_name_plural = "Scan Commands"


class Site(models.Model):
    """Model for a Site.  Must be defined prior to Scan model."""

    id = models.AutoField(primary_key=True, verbose_name="Site ID")
    site_name = models.CharField(
        unique=True,
        max_length=255,
        validators=[
            RegexValidator(
                regex="^[a-zA-Z0-9/()_\- ]*$",  # Must escape -
                message="Site name can only contain alphanumeric characters, /, (), -, _, or spaces",
            )
        ],
        verbose_name="Site Name",
    )
    description = models.CharField(unique=False, max_length=255, blank=True, verbose_name="Description")
    targets = models.CharField(
        unique=False,
        max_length=1_048_576,  # 2^20 = 1048576
        validators=[
            RegexValidator(
                regex="^[a-zA-Z0-9/\.\: ]*$",  # Characters to support IPv4, IPv6, and FQDNs only.  Space delimited.
                message="Targets can only contain alphanumeric characters, /, ., :, and spaces",
            )
        ],
        verbose_name="Targets",
    )
    excluded_targets = models.CharField(
        unique=False,
        blank=True,
        max_length=1_048_576,  # 2^20 = 1048576
        validators=[
            RegexValidator(
                regex="^[a-zA-Z0-9/\.\: ]*$",  # Characters to support IPv4, IPv6, and FQDNs only.  Space delimited.
                message="Excluded targets can only contain alphanumeric characters, /, ., :, and spaces",
            )
        ],
        verbose_name="Excluded targets",
    )
    scan_command = models.ForeignKey(ScanCommand, on_delete=models.CASCADE, verbose_name="Scan binary and name")
    scan_agent = models.ForeignKey(Agent, on_delete=models.CASCADE, verbose_name="Scan Agent")

    def clean(self):
        """Checks for any invalid IPs, IP subnets, or FQDNs in the targets and excluded_targets fields."""

        # Targets
        target_extractor = extract_targets.TargetExtractor(
            targets_string=self.targets, private_ips_allowed=True, sort_targets=True
        )
        targets_dict = target_extractor.targets_dict

        if targets_dict["invalid_targets"]:
            invalid_targets = ",".join(target_extractor.targets_dict["invalid_targets"])
            raise ValidationError(f"Invalid targets provided: {invalid_targets}")

        self.targets = targets_dict["as_nmap"]

        # Excluded targets
        target_extractor = extract_targets.TargetExtractor(
            targets_string=self.excluded_targets, private_ips_allowed=True, sort_targets=True
        )
        targets_dict = target_extractor.targets_dict

        if targets_dict["invalid_targets"]:
            invalid_targets = ",".join(target_extractor.targets_dict["invalid_targets"])
            raise ValidationError(f"Invalid excluded targets provided: {invalid_targets}")

        self.excluded_targets = targets_dict["as_nmap"]

    def __str__(self):
        return str(self.site_name)

    class Meta:
        verbose_name_plural = "Sites"


class Scan(models.Model):
    """Model for a type of Scan"""

    id = models.AutoField(primary_key=True, verbose_name="Scan ID")
    site = models.ForeignKey(Site, on_delete=models.CASCADE)
    scan_name = models.CharField(unique=False, max_length=255, blank=True, verbose_name="Scan Name")
    start_time = models.TimeField(verbose_name="Scan start time")
    recurrences = RecurrenceField(include_dtstart=False, verbose_name="Recurrences")

    def __str__(self):
        return str(self.id)

    # def get_text_rules_inclusion(self):
    #     schedule_scan = ScheduledScan.objects.get(id=self.id)
    #     text_rules_inclusion = []
    #
    #     for rule in schedule_scan.recurrences.rrules:
    #         text_rules_inclusion.append(rule.to_text())
    #
    #     print(text_rules_inclusion)
    #     return text_rules_inclusion

    class Meta:
        verbose_name_plural = "Scans"


class ScheduledScan(models.Model):
    """Model for a list of upcoming scans for a day."""

    SCAN_STATUS_CHOICES = (
        ("pending", "Pending"),
        ("started", "Started"),
        ("completed", "Completed"),
        ("error", "Error"),
    )

    id = models.AutoField(primary_key=True, verbose_name="Scheduled Scan ID")
    site_name = models.CharField(
        unique=False,
        max_length=255,
        validators=[
            RegexValidator(
                regex="^[a-zA-Z0-9/()_\- ]*$",  # Must escape -
                message="Site name can only contain alphanumeric characters, /, (), -, _, or spaces",
            )
        ],
        verbose_name="Site Name",
    )
    site_name_id = models.IntegerField(
        validators=[MinValueValidator(1, message="Site name ID must be greater than 0")], verbose_name="Site name ID"
    )
    scan_id = models.IntegerField(
        validators=[MinValueValidator(1, message="Scan ID must be greater than 0")], verbose_name="Scan ID"
    )
    start_time = models.TimeField(verbose_name="Scan start time")
    scan_agent = models.CharField(
        unique=False,
        max_length=255,
        validators=[
            RegexValidator(
                regex="^[a-zA-Z0-9/()_\- ]*$",  # Must escape -
                message="Agent name can only contain alphanumeric characters, /, (), -, _, or spaces",
            )
        ],
        verbose_name="Agent Name",
    )
    scan_agent_id = models.IntegerField(
        validators=[MinValueValidator(1, message="Scan agent ID must be greater than 0")], verbose_name="Scan agent ID"
    )
    start_datetime = models.DateTimeField(verbose_name="Scheduled scan start date and time")
    scan_binary = models.CharField(max_length=7, default="nmap", verbose_name="Scan binary")
    scan_command = models.CharField(unique=False, max_length=1024, verbose_name="Scan command")
    scan_command_id = models.IntegerField(
        validators=[MinValueValidator(1, message="Scan command ID must be greater than 0")],
        verbose_name="Scan command ID",
    )
    targets = models.CharField(
        unique=False,
        max_length=1_048_576,  # 2^20 = 1048576
        validators=[
            RegexValidator(
                regex="^[a-zA-Z0-9/\.\: ]*$",  # Characters to support IPv4, IPv6, and FQDNs only.  Space delimited.
                message="Targets can only contain alphanumeric characters, /, ., :, and spaces",
            )
        ],
        verbose_name="Targets",
    )
    excluded_targets = models.CharField(
        unique=False,
        blank=True,
        max_length=1_048_576,  # 2^20 = 1048576
        validators=[
            RegexValidator(
                regex="^[a-zA-Z0-9/\.\: ]*$",  # Characters to support IPv4, IPv6, and FQDNs only.  Space delimited.
                message="Excluded targets can only contain alphanumeric characters, /, ., :, and spaces",
            )
        ],
        verbose_name="Excluded targets",
    )
    scan_status = models.CharField(
        max_length=9, choices=SCAN_STATUS_CHOICES, default="pending", verbose_name="Scan status"
    )
    completed_time = models.DateTimeField(null=True, blank=True, verbose_name="Scan completion time")
    result_file_base_name = models.CharField(max_length=255, blank=True, verbose_name="Result file base name")

    def __str__(self):
        return str(self.id)

    class Meta:
        verbose_name_plural = "Scheduled Scans"
