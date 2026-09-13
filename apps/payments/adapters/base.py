from dataclasses import dataclass, field


class Capability:
    CARD = "card"
    DEBIT = "debit"
    ACH = "ach"
    BANK_TRANSFER = "bank_transfer"
    OPEN_BANKING = "open_banking"
    WALLET = "wallet"
    CRYPTO = "crypto"
    STABLECOIN = "stablecoin"
    HOSTED_CHECKOUT = "hosted_checkout"
    EMBEDDED_CHECKOUT = "embedded_checkout"
    AUTHORIZE_CAPTURE = "authorize_capture"
    REFUNDS = "refunds"
    PARTIAL_REFUNDS = "partial_refunds"
    DISPUTES = "disputes"
    WEBHOOKS = "webhooks"
    IDEMPOTENCY = "idempotency"
    MULTICURRENCY = "multicurrency"
    ASYNC_SETTLEMENT = "async_settlement"


@dataclass(frozen=True)
class ProviderOperationResult:
    status: str
    provider_reference: str = ""
    provider_status: str = ""
    checkout_url: str = ""
    safe_metadata: dict = field(default_factory=dict)
    failure_code: str = ""
    failure_category: str = ""


class ProviderAdapterError(Exception):
    """Raised when a provider is configured but cannot create an attempt."""


class PaymentProviderAdapter:
    provider = "base"
    capabilities = []

    def __init__(self, provider_config=None):
        self.provider_config = provider_config
        if provider_config:
            self.provider = provider_config.provider

    def get_capabilities(self):
        configured = getattr(self.provider_config, "capabilities", None)
        return configured or self.capabilities

    def create_checkout_session(self, request, intent, payload):
        raise NotImplementedError

    def create_invoice(self, request, intent, payload):
        raise NotImplementedError

    def refund(self, refund):
        raise NotImplementedError

    def verify_webhook(self, request):
        return False

    def normalize_webhook(self, payload):
        return payload
