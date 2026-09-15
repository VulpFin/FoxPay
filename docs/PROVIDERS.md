# Provider Connections

Provider authorization is merchant-scoped and environment-specific. `MerchantProviderConnection` holds authorization and health; `ProviderConfig` holds the route name, adapter, priority, capabilities, and non-sensitive settings. A merchant may connect multiple accounts of the same provider type and assign multiple routes so an unavailable or regionally unsuitable provider does not become the only checkout option.

A second route is an option, not an automatic retry. FoxPay never submits the same charge to a backup provider without a new explicit customer action.

## Common activation rules

A route is offered only when all of these are true:

- the merchant is approved and allowed for the requested test/live environment;
- the merchant kill switch and temporary risk restriction allow a provider call;
- the `ProviderConfig` is active in the same environment;
- its linked connection is active, healthy, and has the required capability;
- required webhook verification is configured.

Live seller signup and provider onboarding are feature-flagged off by default. Provider secrets never belong in `ProviderConfig.settings`, URLs, browser HTML, logs, or audit metadata.

## Stripe Standard Connect

Stripe seller onboarding uses Standard Connect OAuth. FoxPay stores the connected account ID and makes direct-charge Checkout and refund requests with the FoxPay platform secret scoped to that account. Seller secret keys are never requested or stored. Application fees default to zero and are controlled centrally, not by payment-intent input.

Platform configuration:

```text
STRIPE_SECRET_KEY=
STRIPE_CONNECT_CLIENT_ID_TEST=
STRIPE_CONNECT_CLIENT_ID_LIVE=
STRIPE_CONNECT_WEBHOOK_SECRET_TEST=
STRIPE_CONNECT_WEBHOOK_SECRET_LIVE=
FOXPAY_STRIPE_CONNECT_ENABLED=0
```

Register the exact OAuth redirect URIs:

```text
https://foxpay.fyi/seller/stripe/test/callback/
https://foxpay.fyi/seller/stripe/live/callback/
```

Configure Connect webhook destinations for events on connected accounts:

```text
https://foxpay.fyi/api/v1/webhooks/stripe/connect/test/
https://foxpay.fyi/api/v1/webhooks/stripe/connect/live/
```

FoxPay verifies the environment-specific platform Connect signing secret, resolves the top-level Stripe `account`, and then validates the Checkout/refund/dispute object against the linked merchant attempt. Account updates and `account.application.deauthorized` change connection health immediately.

The older `configure_stripe_provider` command remains for an operator-controlled migration bridge. It is not the seller onboarding path and must not be used to collect seller secret keys.

## Square OAuth

Square uses its confidential server-side OAuth flow. Access and refresh tokens are encrypted immediately. FoxPay verifies the token's client ID, seller merchant ID, scopes, expiry, and usable locations. When multiple locations are available, the seller selects one before the route becomes active. A locked worker refreshes tokens before expiry and marks revoked or insufficient-scope connections unhealthy.

Requested scopes are currently:

```text
MERCHANT_PROFILE_READ ORDERS_READ ORDERS_WRITE PAYMENTS_READ PAYMENTS_WRITE DISPUTES_READ
```

Platform configuration:

```text
SQUARE_APPLICATION_ID_TEST=
SQUARE_APPLICATION_SECRET_TEST=
SQUARE_APPLICATION_ID_LIVE=
SQUARE_APPLICATION_SECRET_LIVE=
SQUARE_OAUTH_WEBHOOK_SIGNATURE_KEY_TEST=
SQUARE_OAUTH_WEBHOOK_SIGNATURE_KEY_LIVE=
SQUARE_OAUTH_WEBHOOK_URL_TEST=https://foxpay.fyi/api/v1/webhooks/square/oauth/test/
SQUARE_OAUTH_WEBHOOK_URL_LIVE=https://foxpay.fyi/api/v1/webhooks/square/oauth/live/
FOXPAY_SQUARE_OAUTH_ENABLED=0
```

Register the exact OAuth redirect URIs:

```text
https://foxpay.fyi/seller/square/test/callback/
https://foxpay.fyi/seller/square/live/callback/
```

Create Square webhook subscriptions using the two `SQUARE_OAUTH_WEBHOOK_URL_*` values exactly, including the trailing slash. Subscribe to payment, refund, dispute, and OAuth-revocation events used by the account. Square signs the notification URL plus raw body, so a spelling or slash mismatch fails verification.

The legacy per-merchant Square webhook and access-token commands remain for migration compatibility; new sellers use OAuth.

## PayPal Partner Referrals

PayPal seller onboarding is available only when TG11 has the required partner approval and `FOXPAY_PAYPAL_PARTNER_ENABLED=1`. FoxPay creates a Partner Referral using the immutable FoxPay merchant UUID as the tracking ID, then verifies onboarding status through PayPal. Callback query parameters alone never activate a seller.

FoxPay stores the PayPal merchant ID and safe consent/capability state. Seller-scoped order, capture, and refund calls use FoxPay's partner credentials, `PayPal-Auth-Assertion` containing the immutable payer/merchant ID, and the partner attribution ID. A seller PayPal client secret is never requested or stored.

Configuration uses environment-specific `PAYPAL_PARTNER_*_TEST` and `PAYPAL_PARTNER_*_LIVE` variables from `.env.example`. Register:

```text
https://foxpay.fyi/seller/paypal/test/callback/
https://foxpay.fyi/seller/paypal/live/callback/
https://foxpay.fyi/api/v1/webhooks/paypal/partner/test/
https://foxpay.fyi/api/v1/webhooks/paypal/partner/live/
```

Payment approval is not capture. An approved order is durably queued, then the worker rechecks merchant authorization and abuse limits immediately before capture. Consent-revocation events disable the connection.

## NOWPayments

NOWPayments does not use an invented OAuth flow. Before connecting it, the seller must add an outcome wallet and enable at least one payment currency in NOWPayments. An owner or administrator with fresh TG11 MFA then enters that seller account's API key and IPN secret once over HTTPS. FoxPay encrypts both immediately, returns only a masked fingerprint, and never renders them again. The seller can test, replace, or remove the credentials; removal erases FoxPay ciphertext and disables all linked routes. The seller should also rotate or revoke the old credential at NOWPayments.

Each configuration receives a merchant- and route-scoped IPN URL:

```text
https://foxpay.fyi/api/v1/webhooks/nowpayments/{merchant-slug}/{provider-code}/
```

For the initial VulpFin route this is:

```text
https://foxpay.fyi/api/v1/webhooks/nowpayments/vulpfin/nowpayments-primary/
```

The route remains inactive until a harmless API check succeeds and an IPN secret exists. FoxPay verifies the HMAC signature before parsing, then checks merchant, invoice/order reference, amount, and currency. `finished` can settle a matching invoice; partial or intermediate payment does not. Crypto refunds, custody, mass payouts, and FoxPay-controlled withdrawals are outside this implementation.

## Development adapters

- `mock`: local/test hosted card flow.
- `hosted`: generic provider-owned checkout URL for operator integration work.
- `manual`: direct noncustodial crypto address/invoice presentation.

They do not weaken live activation checks. Mock routes cannot be used as a production card processor.
