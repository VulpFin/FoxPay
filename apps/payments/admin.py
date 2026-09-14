from django.contrib import admin

from .models import (
    APIKey,
    AuditLog,
    CryptoInvoice,
    Customer,
    Dispute,
    IdempotencyRecord,
    LedgerAccount,
    LedgerEntry,
    LedgerTransaction,
    Merchant,
    MerchantAgreementAcceptance,
    MerchantProviderConnection,
    MerchantMembership,
    MerchantWebhookAttempt,
    MerchantWebhookEndpoint,
    MerchantWebhookEvent,
    PaymentAttempt,
    PaymentIntent,
    PaymentLink,
    PaymentMethodReference,
    ProviderConfig,
    ProviderCredential,
    ProviderCustomerReference,
    ProviderEvent,
    Refund,
    SubscriptionReference,
    WebhookDelivery,
)


class ProviderConfigInline(admin.TabularInline):
    model = ProviderConfig
    extra = 0
    fields = ("kind", "provider", "adapter", "display_name", "priority", "is_active", "settings")


@admin.register(Merchant)
class MerchantAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "owner", "status", "live_payments_enabled", "default_currency", "created_at")
    list_filter = ("status", "live_payments_enabled", "card_provider", "crypto_provider")
    search_fields = ("name", "slug", "support_email")
    readonly_fields = ("status", "live_payments_enabled", "allow_legacy_provider_configs", "approved_at", "approved_by", "reviewed_at", "reviewed_by", "suspended_at", "suspension_reason", "risk_level")
    inlines = [ProviderConfigInline]


@admin.register(MerchantAgreementAcceptance)
class MerchantAgreementAcceptanceAdmin(admin.ModelAdmin):
    list_display = ("merchant", "user", "agreement_version", "accepted_at")
    readonly_fields = ("merchant", "user", "agreement_version", "agreement_hash", "acceptable_use_version", "acceptable_use_hash", "accepted_at", "request_ip", "user_agent", "supersedes")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MerchantProviderConnection)
class MerchantProviderConnectionAdmin(admin.ModelAdmin):
    list_display = ("merchant", "provider", "environment", "status", "authorization_method", "last_verified_at")
    list_filter = ("provider", "environment", "status")
    search_fields = ("merchant__name", "external_account_id")
    exclude = ("encrypted_access_token", "encrypted_refresh_token")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ProviderConfig)
class ProviderConfigAdmin(admin.ModelAdmin):
    list_display = ("merchant", "kind", "provider", "adapter", "display_name", "priority", "is_active", "updated_at")
    list_filter = ("kind", "adapter", "is_active")
    search_fields = ("merchant__name", "provider", "adapter", "display_name")


@admin.register(MerchantMembership)
class MerchantMembershipAdmin(admin.ModelAdmin):
    list_display = ("merchant", "user", "tg11_user_uuid", "role", "created_at")
    list_filter = ("role",)
    search_fields = ("merchant__name", "user__username", "tg11_user_uuid")


@admin.register(ProviderCredential)
class ProviderCredentialAdmin(admin.ModelAdmin):
    list_display = ("provider_config", "name", "last_rotated_at", "revoked_at")
    search_fields = ("provider_config__provider", "name")
    readonly_fields = ("encrypted_value", "last_rotated_at", "revoked_at", "created_at", "updated_at")


@admin.register(APIKey)
class APIKeyAdmin(admin.ModelAdmin):
    list_display = ("name", "merchant", "prefix", "is_active", "last_used_at", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "prefix", "merchant__name")
    readonly_fields = ("prefix", "key_hash", "last_used_at", "created_at", "updated_at")


class PaymentAttemptInline(admin.TabularInline):
    model = PaymentAttempt
    extra = 0
    readonly_fields = ("created_at", "updated_at")


@admin.register(PaymentIntent)
class PaymentIntentAdmin(admin.ModelAdmin):
    list_display = ("public_id", "merchant", "status", "amount", "currency", "environment", "created_at")
    list_filter = ("status", "currency", "environment")
    search_fields = ("public_id", "description", "reference", "merchant__name")
    readonly_fields = ("public_id", "client_secret", "created_at", "updated_at")
    inlines = [PaymentAttemptInline]


@admin.register(PaymentAttempt)
class PaymentAttemptAdmin(admin.ModelAdmin):
    list_display = ("intent", "method", "provider", "status", "created_at")
    list_filter = ("method", "provider", "status")
    search_fields = ("intent__public_id", "provider_reference")


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("merchant", "external_id", "tg11_user_uuid", "email", "name", "created_at")
    search_fields = ("merchant__name", "external_id", "tg11_user_uuid", "email", "name")


@admin.register(PaymentMethodReference)
class PaymentMethodReferenceAdmin(admin.ModelAdmin):
    list_display = ("merchant", "customer", "provider", "type", "created_at")
    search_fields = ("merchant__name", "customer__email", "provider", "provider_reference", "fingerprint")


@admin.register(ProviderCustomerReference)
class ProviderCustomerReferenceAdmin(admin.ModelAdmin):
    list_display = ("provider_config", "customer", "provider_reference", "created_at")
    search_fields = ("provider_config__provider", "customer__email", "provider_reference")


@admin.register(SubscriptionReference)
class SubscriptionReferenceAdmin(admin.ModelAdmin):
    list_display = ("merchant", "customer", "provider", "plan_name", "status", "current_period_end")
    list_filter = ("provider", "status")
    search_fields = ("merchant__name", "customer__email", "provider_reference", "plan_name")


@admin.register(IdempotencyRecord)
class IdempotencyRecordAdmin(admin.ModelAdmin):
    list_display = ("merchant", "key", "payment_intent", "created_at")
    search_fields = ("key", "merchant__name", "payment_intent__public_id")
    readonly_fields = ("created_at", "updated_at")


@admin.register(CryptoInvoice)
class CryptoInvoiceAdmin(admin.ModelAdmin):
    list_display = ("attempt", "asset", "network", "confirmations_seen", "confirmations_required", "transaction_id")
    search_fields = ("attempt__intent__public_id", "address", "transaction_id")


@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    list_display = ("public_id", "payment_intent", "amount", "currency", "status", "provider", "created_at")
    list_filter = ("status", "currency", "provider")
    search_fields = ("public_id", "payment_intent__public_id", "provider_refund_id")


@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    list_display = ("payment_intent", "provider", "reason", "amount", "currency", "status", "evidence_due_at")
    list_filter = ("status", "provider", "currency")
    search_fields = ("payment_intent__public_id", "provider_dispute_id", "reason")


class LedgerEntryInline(admin.TabularInline):
    model = LedgerEntry
    extra = 0


@admin.register(LedgerAccount)
class LedgerAccountAdmin(admin.ModelAdmin):
    list_display = ("merchant", "code", "name", "account_type", "currency")
    list_filter = ("account_type", "currency")
    search_fields = ("merchant__name", "code", "name")


@admin.register(LedgerTransaction)
class LedgerTransactionAdmin(admin.ModelAdmin):
    list_display = ("merchant", "transaction_type", "payment_intent", "refund", "request_id", "created_at")
    list_filter = ("transaction_type",)
    search_fields = ("payment_intent__public_id", "refund__public_id", "request_id")
    inlines = [LedgerEntryInline]


@admin.register(MerchantWebhookEndpoint)
class MerchantWebhookEndpointAdmin(admin.ModelAdmin):
    list_display = ("merchant", "url", "description", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("merchant__name", "url", "description")
    readonly_fields = ("secret_hash", "encrypted_secret", "created_at", "updated_at")


@admin.register(MerchantWebhookEvent)
class MerchantWebhookEventAdmin(admin.ModelAdmin):
    list_display = ("merchant", "event_type", "status", "idempotency_key", "created_at")
    list_filter = ("event_type", "status")
    search_fields = ("merchant__name", "event_type", "idempotency_key")


@admin.register(MerchantWebhookAttempt)
class MerchantWebhookAttemptAdmin(admin.ModelAdmin):
    list_display = ("event", "endpoint", "status_code", "next_retry_at", "created_at")
    search_fields = ("event__event_type", "endpoint__url", "error")


@admin.register(ProviderEvent)
class ProviderEventAdmin(admin.ModelAdmin):
    list_display = ("provider", "provider_event_id", "event_type", "normalized_event_type", "processed_at")
    list_filter = ("provider", "normalized_event_type")
    search_fields = ("provider_event_id", "event_type", "payment_intent__public_id")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("merchant", "actor", "action", "object_type", "object_id", "request_id", "created_at")
    list_filter = ("action", "object_type")
    search_fields = ("merchant__name", "actor__username", "action", "object_id", "request_id")


@admin.register(PaymentLink)
class PaymentLinkAdmin(admin.ModelAdmin):
    list_display = ("merchant", "token", "amount", "currency", "is_active", "expires_at")
    list_filter = ("currency", "is_active")
    search_fields = ("merchant__name", "token", "description")


@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(admin.ModelAdmin):
    list_display = ("provider", "event_type", "provider_event_id", "processed", "created_at")
    list_filter = ("provider", "processed")
    search_fields = ("provider_event_id", "event_type")
    readonly_fields = ("created_at", "updated_at")
