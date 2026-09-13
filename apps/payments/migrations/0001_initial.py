# Generated for the Fox Pay initial scaffold.

import django.core.validators
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import apps.payments.models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Merchant",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=160)),
                ("slug", models.SlugField(unique=True)),
                ("website_url", models.URLField(blank=True)),
                ("support_email", models.EmailField(blank=True, max_length=254)),
                ("default_currency", models.CharField(default="USD", max_length=3)),
                ("card_provider", models.CharField(default="mock", max_length=40)),
                ("crypto_provider", models.CharField(default="manual", max_length=40)),
                ("crypto_addresses", models.JSONField(blank=True, default=dict, help_text='Example: {"BTC": "bc1...", "ETH": "0x..."}')),
                ("is_active", models.BooleanField(default=True)),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="merchants", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="PaymentIntent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("public_id", models.CharField(default=apps.payments.models.payment_intent_id, max_length=64, unique=True)),
                ("client_secret", models.CharField(default=apps.payments.models.client_secret, max_length=80, unique=True)),
                ("amount", models.PositiveIntegerField(help_text="Amount in the smallest currency unit, such as cents.", validators=[django.core.validators.MinValueValidator(1)])),
                ("currency", models.CharField(max_length=3)),
                ("description", models.CharField(blank=True, max_length=240)),
                ("status", models.CharField(choices=[("requires_payment_method", "Requires payment method"), ("processing", "Processing"), ("succeeded", "Succeeded"), ("failed", "Failed"), ("cancelled", "Cancelled"), ("expired", "Expired")], default="requires_payment_method", max_length=32)),
                ("success_url", models.URLField(blank=True)),
                ("cancel_url", models.URLField(blank=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("merchant", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="payment_intents", to="payments.merchant")),
            ],
        ),
        migrations.CreateModel(
            name="APIKey",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=120)),
                ("prefix", models.CharField(db_index=True, max_length=32)),
                ("key_hash", models.CharField(max_length=256)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("is_active", models.BooleanField(default=True)),
                ("merchant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="api_keys", to="payments.merchant")),
            ],
            options={
                "verbose_name": "API key",
                "verbose_name_plural": "API keys",
            },
        ),
        migrations.CreateModel(
            name="PaymentAttempt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("method", models.CharField(choices=[("card", "Card"), ("crypto", "Crypto")], max_length=16)),
                ("provider", models.CharField(max_length=40)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("action_required", "Action required"), ("succeeded", "Succeeded"), ("failed", "Failed")], default="pending", max_length=24)),
                ("provider_reference", models.CharField(blank=True, max_length=160)),
                ("checkout_url", models.URLField(blank=True)),
                ("instructions", models.TextField(blank=True)),
                ("intent", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="attempts", to="payments.paymentintent")),
            ],
        ),
        migrations.CreateModel(
            name="WebhookDelivery",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("provider", models.CharField(max_length=40)),
                ("event_type", models.CharField(max_length=120)),
                ("provider_event_id", models.CharField(blank=True, max_length=160)),
                ("payload", models.JSONField(default=dict)),
                ("processed", models.BooleanField(default=False)),
                ("error", models.TextField(blank=True)),
            ],
            options={
                "verbose_name_plural": "Webhook deliveries",
            },
        ),
        migrations.CreateModel(
            name="CryptoInvoice",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("asset", models.CharField(default="BTC", max_length=12)),
                ("network", models.CharField(default="bitcoin", max_length=24)),
                ("address", models.CharField(blank=True, max_length=160)),
                ("amount_decimal", models.DecimalField(blank=True, decimal_places=12, max_digits=28, null=True)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("confirmations_required", models.PositiveSmallIntegerField(default=1)),
                ("confirmations_seen", models.PositiveSmallIntegerField(default=0)),
                ("transaction_id", models.CharField(blank=True, max_length=160)),
                ("attempt", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="crypto_invoice", to="payments.paymentattempt")),
            ],
        ),
        migrations.AddIndex(
            model_name="paymentintent",
            index=models.Index(fields=["merchant", "status"], name="payments_pa_merchan_992868_idx"),
        ),
        migrations.AddIndex(
            model_name="paymentintent",
            index=models.Index(fields=["public_id"], name="payments_pa_public__f6c24d_idx"),
        ),
    ]
