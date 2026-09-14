import secrets
import uuid

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from .security import decrypt_secret, encrypt_secret


def generate_id(prefix):
    return f"{prefix}_{secrets.token_urlsafe(18)}"


def payment_intent_id():
    return generate_id("fp_pi")


def client_secret():
    return generate_id("fp_secret")


def refund_id():
    return generate_id("fp_re")


def payment_link_token():
    return generate_id("fp_link")


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Merchant(TimeStampedModel):
    STATUS_PENDING = "pending"
    STATUS_ACTIVE = "active"
    STATUS_RESTRICTED = "restricted"
    STATUS_DISABLED = "disabled"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending review"),
        (STATUS_ACTIVE, "Active"),
        (STATUS_RESTRICTED, "Restricted"),
        (STATUS_DISABLED, "Disabled"),
    ]

    uuid = models.UUIDField(default=uuid.uuid4, db_index=True, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="merchants")
    name = models.CharField(max_length=160)
    slug = models.SlugField(unique=True)
    legal_name = models.CharField(max_length=240, blank=True)
    website_url = models.URLField(blank=True)
    support_email = models.EmailField(blank=True)
    country = models.CharField(max_length=2, blank=True)
    business_category = models.CharField(max_length=120, blank=True)
    business_description = models.TextField(blank=True)
    default_currency = models.CharField(max_length=3, default="USD")
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_PENDING)
    live_payments_enabled = models.BooleanField(default=False)
    allow_legacy_provider_configs = models.BooleanField(default=False)
    approved_at = models.DateTimeField(blank=True, null=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name="approved_merchants", blank=True, null=True)
    reviewed_at = models.DateTimeField(blank=True, null=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name="reviewed_merchants", blank=True, null=True)
    suspended_at = models.DateTimeField(blank=True, null=True)
    suspension_reason = models.CharField(max_length=500, blank=True)
    risk_level = models.CharField(max_length=24, default="unreviewed")
    card_provider = models.CharField(max_length=40, default="mock")
    crypto_provider = models.CharField(max_length=40, default="manual")
    crypto_addresses = models.JSONField(default=dict, blank=True, help_text="Example: {\"BTC\": \"bc1...\", \"ETH\": \"0x...\"}")
    settings = models.JSONField(default=dict, blank=True)
    environment_settings = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class MerchantMembership(TimeStampedModel):
    ROLE_OWNER = "owner"
    ROLE_ADMIN = "administrator"
    ROLE_DEVELOPER = "developer"
    ROLE_FINANCE = "finance"
    ROLE_SUPPORT = "support"
    ROLE_VIEWER = "viewer"

    ROLE_CHOICES = [
        (ROLE_OWNER, "Owner"),
        (ROLE_ADMIN, "Administrator"),
        (ROLE_DEVELOPER, "Developer"),
        (ROLE_FINANCE, "Finance"),
        (ROLE_SUPPORT, "Support"),
        (ROLE_VIEWER, "Viewer"),
    ]

    uuid = models.UUIDField(default=uuid.uuid4, db_index=True, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="merchant_memberships", blank=True, null=True)
    tg11_user_uuid = models.UUIDField(blank=True, null=True, db_index=True)
    role = models.CharField(max_length=24, choices=ROLE_CHOICES, default=ROLE_VIEWER)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["merchant", "user"], name="unique_merchant_user_membership"),
            models.UniqueConstraint(fields=["merchant", "tg11_user_uuid"], name="unique_merchant_tg11_membership"),
        ]

    def __str__(self):
        identity = self.tg11_user_uuid or self.user_id
        return f"{self.merchant} {identity} {self.role}"


class MerchantAgreementAcceptance(models.Model):
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name="agreement_acceptances")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="merchant_agreement_acceptances")
    agreement_version = models.CharField(max_length=40)
    agreement_hash = models.CharField(max_length=64)
    acceptable_use_version = models.CharField(max_length=40)
    acceptable_use_hash = models.CharField(max_length=64)
    accepted_at = models.DateTimeField(default=timezone.now, editable=False)
    request_ip = models.GenericIPAddressField(blank=True, null=True)
    user_agent = models.CharField(max_length=500, blank=True)
    supersedes = models.ForeignKey("self", on_delete=models.PROTECT, blank=True, null=True, related_name="superseded_by")

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("Agreement acceptances are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Agreement acceptances are append-only.")


class MerchantProviderConnection(TimeStampedModel):
    STATUS_PENDING = "pending"
    STATUS_ACTIVE = "active"
    STATUS_RESTRICTED = "restricted"
    STATUS_REVOKED = "revoked"
    STATUS_ERROR = "error"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_ACTIVE, "Active"),
        (STATUS_RESTRICTED, "Restricted"),
        (STATUS_REVOKED, "Revoked"),
        (STATUS_ERROR, "Error"),
    ]
    PROVIDER_CHOICES = [(name, name.title()) for name in ("stripe", "square", "paypal", "nowpayments")]

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="provider_connections")
    provider = models.CharField(max_length=24, choices=PROVIDER_CHOICES)
    environment = models.CharField(max_length=12, default="test")
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_PENDING)
    authorization_method = models.CharField(max_length=32)
    external_account_id = models.CharField(max_length=180, blank=True)
    granted_scopes = models.JSONField(default=list, blank=True)
    capabilities = models.JSONField(default=list, blank=True)
    encrypted_access_token = models.TextField(blank=True)
    encrypted_refresh_token = models.TextField(blank=True)
    token_expires_at = models.DateTimeField(blank=True, null=True)
    connected_at = models.DateTimeField(blank=True, null=True)
    revoked_at = models.DateTimeField(blank=True, null=True)
    last_verified_at = models.DateTimeField(blank=True, null=True)
    last_error_at = models.DateTimeField(blank=True, null=True)
    last_error_code = models.CharField(max_length=80, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["merchant", "provider", "environment", "external_account_id"],
                condition=~models.Q(external_account_id=""),
                name="unique_merchant_provider_account",
            ),
        ]

    def set_access_token(self, value):
        self.encrypted_access_token = encrypt_secret(value)

    def access_token(self):
        return decrypt_secret(self.encrypted_access_token)

    def set_refresh_token(self, value):
        self.encrypted_refresh_token = encrypt_secret(value)

    def refresh_token(self):
        return decrypt_secret(self.encrypted_refresh_token)

    def clear_tokens(self):
        self.encrypted_access_token = ""
        self.encrypted_refresh_token = ""
        self.token_expires_at = None

    def __str__(self):
        return f"{self.merchant} {self.provider} {self.environment} ({self.status})"


class ProviderOnboardingSession(models.Model):
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="onboarding_sessions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="provider_onboarding_sessions")
    provider = models.CharField(max_length=24)
    environment = models.CharField(max_length=12)
    state_hash = models.CharField(max_length=64, unique=True)
    requested_scopes = models.JSONField(default=list, blank=True)
    encrypted_pkce_verifier = models.TextField(blank=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(blank=True, null=True)
    return_path = models.CharField(max_length=500, default="/seller/")
    created_at = models.DateTimeField(auto_now_add=True)

    def set_pkce_verifier(self, value):
        self.encrypted_pkce_verifier = encrypt_secret(value)

    def pkce_verifier(self):
        return decrypt_secret(self.encrypted_pkce_verifier)


class ProviderConfig(TimeStampedModel):
    KIND_CARD = "card"
    KIND_CRYPTO = "crypto"

    KIND_CHOICES = [
        (KIND_CARD, "Card"),
        (KIND_CRYPTO, "Crypto"),
    ]

    uuid = models.UUIDField(default=uuid.uuid4, db_index=True, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="provider_configs")
    connection = models.ForeignKey(MerchantProviderConnection, on_delete=models.PROTECT, related_name="provider_configs", blank=True, null=True)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    provider = models.CharField(max_length=40, help_text="Merchant-facing provider code, such as acquirer-us or btc-wallet-primary.")
    adapter = models.CharField(max_length=40, blank=True, help_text="Fox Pay adapter type, such as hosted, mock, or manual. Defaults to provider.")
    display_name = models.CharField(max_length=120, blank=True)
    environment = models.CharField(max_length=12, default="test")
    capabilities = models.JSONField(default=list, blank=True)
    priority = models.PositiveSmallIntegerField(default=100)
    is_active = models.BooleanField(default=True)
    settings = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["priority", "created_at"]
        constraints = [
            models.UniqueConstraint(fields=["merchant", "environment", "kind", "provider"], name="unique_merchant_env_provider_kind"),
        ]

    def __str__(self):
        return self.display_name or f"{self.merchant} {self.kind} {self.provider}"

    @property
    def adapter_name(self):
        return self.adapter or self.provider


class ProviderCredential(TimeStampedModel):
    uuid = models.UUIDField(default=uuid.uuid4, db_index=True, editable=False)
    provider_config = models.ForeignKey(ProviderConfig, on_delete=models.CASCADE, related_name="credentials")
    name = models.CharField(max_length=120)
    encrypted_value = models.TextField()
    last_rotated_at = models.DateTimeField(default=timezone.now)
    revoked_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"{self.provider_config.provider} {self.name}"

    def set_secret(self, value):
        self.encrypted_value = encrypt_secret(value)
        self.last_rotated_at = timezone.now()

    def reveal_secret(self):
        return decrypt_secret(self.encrypted_value)

    @property
    def is_active(self):
        return self.revoked_at is None


class APIKey(TimeStampedModel):
    PREFIX_LENGTH = 24
    TYPE_SECRET = "secret"
    TYPE_PUBLISHABLE = "publishable"

    TYPE_CHOICES = [
        (TYPE_SECRET, "Secret"),
        (TYPE_PUBLISHABLE, "Publishable"),
    ]

    uuid = models.UUIDField(default=uuid.uuid4, db_index=True, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="api_keys")
    name = models.CharField(max_length=120)
    prefix = models.CharField(max_length=32, db_index=True)
    key_hash = models.CharField(max_length=256)
    key_type = models.CharField(max_length=16, choices=TYPE_CHOICES, default=TYPE_SECRET)
    environment = models.CharField(max_length=12, default="test")
    scopes = models.JSONField(default=list, blank=True)
    expires_at = models.DateTimeField(blank=True, null=True)
    revoked_at = models.DateTimeField(blank=True, null=True)
    last_used_at = models.DateTimeField(blank=True, null=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "API key"
        verbose_name_plural = "API keys"

    def __str__(self):
        return f"{self.merchant} - {self.name}"

    @classmethod
    def issue(cls, merchant, name="Default API key", key_type=TYPE_SECRET, scopes=None, environment=None):
        mode = environment or getattr(settings, "FOXPAY_ENV", "test")
        if key_type == cls.TYPE_PUBLISHABLE:
            raw_key = f"foxpay_pk_{mode}_{secrets.token_urlsafe(32)}"
        else:
            raw_key = f"foxpay_{mode}_{secrets.token_urlsafe(32)}"
        key = cls.objects.create(
            merchant=merchant,
            name=name,
            prefix=raw_key[: cls.PREFIX_LENGTH],
            key_hash=make_password(raw_key),
            key_type=key_type,
            environment=mode,
            scopes=scopes or ["payments:read", "payments:write", "refunds:write"],
        )
        return key, raw_key

    def has_scope(self, scope):
        return "*" in self.scopes or scope in self.scopes

    def matches(self, raw_key):
        return check_password(raw_key, self.key_hash)

    @property
    def usable(self):
        if not self.is_active or self.revoked_at:
            return False
        if self.expires_at and self.expires_at <= timezone.now():
            return False
        return True

    def mark_used(self):
        self.last_used_at = timezone.now()
        self.save(update_fields=["last_used_at", "updated_at"])


class Customer(TimeStampedModel):
    uuid = models.UUIDField(default=uuid.uuid4, db_index=True, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="customers")
    external_id = models.CharField(max_length=160, blank=True)
    tg11_user_uuid = models.UUIDField(blank=True, null=True, db_index=True)
    email = models.EmailField(blank=True)
    name = models.CharField(max_length=180, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["merchant", "external_id"], condition=~models.Q(external_id=""), name="unique_merchant_customer_external_id"),
            models.UniqueConstraint(fields=["merchant", "tg11_user_uuid"], name="unique_merchant_customer_tg11_user"),
        ]

    def __str__(self):
        return self.name or self.email or str(self.uuid)


class PaymentMethodReference(TimeStampedModel):
    uuid = models.UUIDField(default=uuid.uuid4, db_index=True, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="payment_method_references")
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="payment_methods", blank=True, null=True)
    provider_config = models.ForeignKey(ProviderConfig, on_delete=models.PROTECT, related_name="payment_method_references", blank=True, null=True)
    provider = models.CharField(max_length=40)
    provider_reference = models.CharField(max_length=180)
    type = models.CharField(max_length=32)
    display_metadata = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField(blank=True, null=True)
    fingerprint = models.CharField(max_length=120, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["provider_config", "provider_reference"], name="unique_provider_payment_method_reference"),
        ]

    def __str__(self):
        return f"{self.type} {self.provider}"


class ProviderCustomerReference(TimeStampedModel):
    provider_config = models.ForeignKey(ProviderConfig, on_delete=models.PROTECT, related_name="customer_references")
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="provider_references")
    provider_reference = models.CharField(max_length=180)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["provider_config", "customer"], name="unique_provider_customer"),
            models.UniqueConstraint(fields=["provider_config", "provider_reference"], name="unique_provider_customer_reference"),
        ]


class SubscriptionReference(TimeStampedModel):
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="subscription_references")
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="subscription_references")
    provider = models.CharField(max_length=40)
    provider_reference = models.CharField(max_length=180)
    plan_name = models.CharField(max_length=160)
    status = models.CharField(max_length=40)
    amount = models.PositiveIntegerField(blank=True, null=True)
    currency = models.CharField(max_length=3, blank=True)
    current_period_end = models.DateTimeField(blank=True, null=True)
    cancel_at_period_end = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["merchant", "provider", "provider_reference"],
                name="unique_merchant_subscription_reference",
            ),
        ]

    def __str__(self):
        return f"{self.merchant} {self.plan_name}"


class PaymentIntent(TimeStampedModel):
    STATUS_CREATED = "created"
    STATUS_AWAITING_PAYMENT_METHOD = "awaiting_payment_method"
    STATUS_REQUIRES_PAYMENT = "requires_payment_method"
    STATUS_PENDING = "pending"
    STATUS_PROCESSING = "processing"
    STATUS_REQUIRES_ACTION = "requires_action"
    STATUS_AUTHORIZED = "authorized"
    STATUS_CAPTURED = "captured"
    STATUS_PARTIALLY_REFUNDED = "partially_refunded"
    STATUS_REFUNDED = "refunded"
    STATUS_SUCCEEDED = "succeeded"
    STATUS_FAILED = "failed"
    STATUS_CANCELED = "canceled"
    STATUS_CANCELLED = "cancelled"
    STATUS_EXPIRED = "expired"

    CAPTURE_AUTOMATIC = "automatic"
    CAPTURE_MANUAL = "manual"

    STATUS_CHOICES = [
        (STATUS_CREATED, "Created"),
        (STATUS_AWAITING_PAYMENT_METHOD, "Awaiting payment method"),
        (STATUS_REQUIRES_PAYMENT, "Requires payment method"),
        (STATUS_PENDING, "Pending"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_REQUIRES_ACTION, "Requires action"),
        (STATUS_AUTHORIZED, "Authorized"),
        (STATUS_CAPTURED, "Captured"),
        (STATUS_PARTIALLY_REFUNDED, "Partially refunded"),
        (STATUS_REFUNDED, "Refunded"),
        (STATUS_SUCCEEDED, "Succeeded"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELED, "Canceled"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_EXPIRED, "Expired"),
    ]

    uuid = models.UUIDField(default=uuid.uuid4, db_index=True, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name="payment_intents")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="payment_intents", blank=True, null=True)
    public_id = models.CharField(max_length=64, unique=True, default=payment_intent_id)
    client_secret = models.CharField(max_length=80, unique=True, default=client_secret)
    amount = models.PositiveIntegerField(validators=[MinValueValidator(1)], help_text="Amount in the smallest currency unit, such as cents.")
    currency = models.CharField(max_length=3)
    description = models.CharField(max_length=240, blank=True)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_REQUIRES_PAYMENT)
    requested_payment_methods = models.JSONField(default=list, blank=True)
    selected_provider_config = models.ForeignKey(ProviderConfig, on_delete=models.SET_NULL, related_name="selected_payment_intents", blank=True, null=True)
    environment = models.CharField(max_length=12, default="test")
    capture_strategy = models.CharField(max_length=16, default=CAPTURE_AUTOMATIC)
    statement_descriptor = models.CharField(max_length=120, blank=True)
    reference = models.CharField(max_length=160, blank=True)
    expires_at = models.DateTimeField(blank=True, null=True)
    success_url = models.URLField(max_length=1000, blank=True)
    cancel_url = models.URLField(max_length=1000, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["merchant", "status"]),
            models.Index(fields=["public_id"]),
        ]

    def __str__(self):
        return f"{self.public_id} {self.currency} {self.amount}"

    def mark_succeeded(self):
        self.status = self.STATUS_SUCCEEDED
        self.save(update_fields=["status", "updated_at"])


class IdempotencyRecord(TimeStampedModel):
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="idempotency_records")
    key = models.CharField(max_length=160)
    payment_intent = models.ForeignKey(PaymentIntent, on_delete=models.CASCADE, related_name="idempotency_records")
    response_object_type = models.CharField(max_length=80, default="payment_intent")
    response_object_id = models.CharField(max_length=120, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["merchant", "key"], name="unique_merchant_idempotency_key"),
        ]

    def __str__(self):
        return f"{self.merchant} {self.key}"


class PaymentAttempt(TimeStampedModel):
    METHOD_CARD = "card"
    METHOD_CRYPTO = "crypto"
    STATUS_PENDING = "pending"
    STATUS_ACTION_REQUIRED = "action_required"
    STATUS_SUCCEEDED = "succeeded"
    STATUS_FAILED = "failed"

    uuid = models.UUIDField(default=uuid.uuid4, db_index=True, editable=False)
    METHOD_CHOICES = [
        (METHOD_CARD, "Card"),
        (METHOD_CRYPTO, "Crypto"),
    ]
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_ACTION_REQUIRED, "Action required"),
        (STATUS_SUCCEEDED, "Succeeded"),
        (STATUS_FAILED, "Failed"),
    ]

    intent = models.ForeignKey(PaymentIntent, on_delete=models.CASCADE, related_name="attempts")
    provider_config = models.ForeignKey(ProviderConfig, on_delete=models.SET_NULL, related_name="payment_attempts", blank=True, null=True)
    method = models.CharField(max_length=16, choices=METHOD_CHOICES)
    rail = models.CharField(max_length=32, blank=True)
    provider = models.CharField(max_length=40)
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_PENDING)
    amount = models.PositiveIntegerField(default=0)
    currency = models.CharField(max_length=3, blank=True)
    provider_reference = models.CharField(max_length=160, blank=True)
    provider_status = models.CharField(max_length=80, blank=True)
    provider_response_metadata = models.JSONField(default=dict, blank=True)
    failure_code = models.CharField(max_length=80, blank=True)
    failure_category = models.CharField(max_length=80, blank=True)
    checkout_url = models.URLField(max_length=1000, blank=True)
    instructions = models.TextField(blank=True)

    def __str__(self):
        return f"{self.intent.public_id} {self.method}"


class CryptoInvoice(TimeStampedModel):
    SETTLEMENT_PENDING = "pending"
    SETTLEMENT_DETECTED = "detected"
    SETTLEMENT_CONFIRMED = "confirmed"
    SETTLEMENT_UNDERPAID = "underpaid"
    SETTLEMENT_OVERPAID = "overpaid"
    SETTLEMENT_EXPIRED = "expired"

    uuid = models.UUIDField(default=uuid.uuid4, db_index=True, editable=False)
    payment_intent = models.ForeignKey(PaymentIntent, on_delete=models.CASCADE, related_name="crypto_invoices", blank=True, null=True)
    attempt = models.OneToOneField(PaymentAttempt, on_delete=models.CASCADE, related_name="crypto_invoice")
    asset = models.CharField(max_length=12, default="BTC")
    network = models.CharField(max_length=24, default="bitcoin")
    address = models.CharField(max_length=160, blank=True)
    expected_amount_decimal = models.DecimalField(max_digits=28, decimal_places=12, blank=True, null=True)
    received_amount_decimal = models.DecimalField(max_digits=28, decimal_places=12, blank=True, null=True)
    exchange_rate_snapshot = models.JSONField(default=dict, blank=True)
    amount_decimal = models.DecimalField(max_digits=28, decimal_places=12, blank=True, null=True)
    expires_at = models.DateTimeField(blank=True, null=True)
    confirmations_required = models.PositiveSmallIntegerField(default=1)
    confirmations_seen = models.PositiveSmallIntegerField(default=0)
    transaction_id = models.CharField(max_length=160, blank=True)
    settlement_state = models.CharField(max_length=24, default=SETTLEMENT_PENDING)

    def __str__(self):
        return f"{self.asset} invoice for {self.attempt.intent.public_id}"


class WebhookDelivery(TimeStampedModel):
    uuid = models.UUIDField(default=uuid.uuid4, db_index=True, editable=False)
    provider = models.CharField(max_length=40)
    event_type = models.CharField(max_length=120)
    provider_event_id = models.CharField(max_length=160, blank=True)
    payload = models.JSONField(default=dict)
    processed = models.BooleanField(default=False)
    error = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "Webhook deliveries"

    def __str__(self):
        return f"{self.provider} {self.event_type}"


class ProviderEvent(TimeStampedModel):
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="provider_events", blank=True, null=True)
    provider = models.CharField(max_length=40)
    provider_event_id = models.CharField(max_length=160)
    event_type = models.CharField(max_length=120)
    normalized_event_type = models.CharField(max_length=120)
    payment_intent = models.ForeignKey(PaymentIntent, on_delete=models.SET_NULL, related_name="provider_events", blank=True, null=True)
    payload = models.JSONField(default=dict)
    processed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["provider", "provider_event_id"], name="unique_provider_event"),
        ]


class Refund(TimeStampedModel):
    STATUS_CREATED = "created"
    STATUS_PENDING = "pending"
    STATUS_SUCCEEDED = "succeeded"
    STATUS_FAILED = "failed"
    STATUS_CANCELED = "canceled"

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    public_id = models.CharField(max_length=64, unique=True, default=refund_id)
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name="refunds")
    payment_intent = models.ForeignKey(PaymentIntent, on_delete=models.PROTECT, related_name="refunds")
    payment_attempt = models.ForeignKey(PaymentAttempt, on_delete=models.PROTECT, related_name="refunds", blank=True, null=True)
    provider_config = models.ForeignKey(ProviderConfig, on_delete=models.SET_NULL, related_name="refunds", blank=True, null=True)
    amount = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=24, default=STATUS_CREATED)
    provider = models.CharField(max_length=40, blank=True)
    provider_refund_id = models.CharField(max_length=160, blank=True)
    reason = models.CharField(max_length=240, blank=True)
    metadata = models.JSONField(default=dict, blank=True)


class Dispute(TimeStampedModel):
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name="disputes")
    payment_intent = models.ForeignKey(PaymentIntent, on_delete=models.PROTECT, related_name="disputes")
    provider = models.CharField(max_length=40)
    provider_dispute_id = models.CharField(max_length=160, blank=True)
    reason = models.CharField(max_length=120, blank=True)
    amount = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=40, default="needs_response")
    evidence_due_at = models.DateTimeField(blank=True, null=True)


class LedgerAccount(TimeStampedModel):
    TYPE_ASSET = "asset"
    TYPE_LIABILITY = "liability"
    TYPE_REVENUE = "revenue"
    TYPE_EXPENSE = "expense"

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="ledger_accounts")
    code = models.CharField(max_length=80)
    name = models.CharField(max_length=160)
    account_type = models.CharField(max_length=24)
    currency = models.CharField(max_length=3)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["merchant", "code", "currency"], name="unique_merchant_ledger_account"),
        ]

    def __str__(self):
        return f"{self.code} {self.currency}"


class LedgerTransaction(TimeStampedModel):
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="ledger_transactions")
    transaction_type = models.CharField(max_length=40)
    payment_intent = models.ForeignKey(PaymentIntent, on_delete=models.SET_NULL, related_name="ledger_transactions", blank=True, null=True)
    refund = models.ForeignKey(Refund, on_delete=models.SET_NULL, related_name="ledger_transactions", blank=True, null=True)
    request_id = models.CharField(max_length=80, blank=True)
    metadata = models.JSONField(default=dict, blank=True)


class LedgerEntry(TimeStampedModel):
    DIRECTION_DEBIT = "debit"
    DIRECTION_CREDIT = "credit"

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    transaction = models.ForeignKey(LedgerTransaction, on_delete=models.PROTECT, related_name="entries")
    account = models.ForeignKey(LedgerAccount, on_delete=models.PROTECT, related_name="entries")
    direction = models.CharField(max_length=8, choices=[(DIRECTION_DEBIT, "Debit"), (DIRECTION_CREDIT, "Credit")])
    amount = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    currency = models.CharField(max_length=3)


class MerchantWebhookEndpoint(TimeStampedModel):
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="webhook_endpoints")
    url = models.URLField()
    description = models.CharField(max_length=160, blank=True)
    enabled_events = models.JSONField(default=list, blank=True)
    secret_hash = models.CharField(max_length=256)
    encrypted_secret = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    @classmethod
    def create_with_secret(cls, merchant, url, description="", enabled_events=None):
        secret = f"whsec_{secrets.token_urlsafe(32)}"
        endpoint = cls.objects.create(
            merchant=merchant,
            url=url,
            description=description,
            enabled_events=enabled_events or ["*"],
            secret_hash=make_password(secret),
            encrypted_secret=encrypt_secret(secret),
        )
        return endpoint, secret

    def matches_secret(self, secret):
        return check_password(secret, self.secret_hash)

    def reveal_secret(self):
        return decrypt_secret(self.encrypted_secret)


class MerchantWebhookEvent(TimeStampedModel):
    STATUS_PENDING = "pending"
    STATUS_DELIVERED = "delivered"
    STATUS_FAILED = "failed"

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="webhook_events")
    event_type = models.CharField(max_length=120)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=24, default=STATUS_PENDING)
    idempotency_key = models.CharField(max_length=160, blank=True)


class MerchantWebhookAttempt(TimeStampedModel):
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    event = models.ForeignKey(MerchantWebhookEvent, on_delete=models.CASCADE, related_name="delivery_attempts")
    endpoint = models.ForeignKey(MerchantWebhookEndpoint, on_delete=models.CASCADE, related_name="delivery_attempts")
    status_code = models.PositiveSmallIntegerField(blank=True, null=True)
    response_body = models.TextField(blank=True)
    error = models.TextField(blank=True)
    next_retry_at = models.DateTimeField(blank=True, null=True)


class AuditLog(TimeStampedModel):
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="audit_logs", blank=True, null=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name="audit_logs", blank=True, null=True)
    action = models.CharField(max_length=120)
    object_type = models.CharField(max_length=80, blank=True)
    object_id = models.CharField(max_length=120, blank=True)
    request_id = models.CharField(max_length=80, blank=True)
    metadata = models.JSONField(default=dict, blank=True)


class PaymentLink(TimeStampedModel):
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="payment_links")
    token = models.CharField(max_length=80, unique=True, default=payment_link_token)
    amount = models.PositiveIntegerField(blank=True, null=True)
    currency = models.CharField(max_length=3)
    description = models.CharField(max_length=240, blank=True)
    enabled_payment_methods = models.JSONField(default=list, blank=True)
    allow_customer_amount = models.BooleanField(default=False)
    expires_at = models.DateTimeField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
