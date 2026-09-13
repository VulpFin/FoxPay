# TG11 Authentication

FoxPay uses the pinned `tg11-auth` Django OIDC client from TG11Accounts. Its
authorization-code/PKCE flow validates ID tokens and keeps one local identity
link per TG11 subject. Register `foxpay` at the issuer with the exact callback
`https://foxpay.fyi/auth/tg11/callback/`, and configure the four `TG11_OIDC_*`
environment variables in `.env.example`. Never commit the client secret.

Verified-email auto-linking is disabled. Existing FoxPay users must link from
their Connections page while signed in locally. New TG11 users get a local
account with an unusable password. Unlinking is refused when TG11 is the user's
only sign-in method. Staff TG11 sign-in requires a second factor. Staff accounts
provisioned through TG11 should have an unusable local password.

The customer dashboard reads only `Customer` rows explicitly assigned the
signed-in TG11 UUID. It never matches a payment, order reference, subscription,
or saved method by email. Merchants may include `customer.tg11_user_uuid` in
their authenticated payment-intent requests after they have verified the buyer
through TG11. The subscription-reference API uses the same merchant-owned
mapping. FoxPay does not import Shop orders automatically; the Orders tab shows
FoxPay payments carrying a merchant order reference.

Saved-card setup uses Stripe-hosted Checkout. Raw card data never enters FoxPay.
The setup webhook stores only provider references and masked display details.
Adding or removing a method requires a TG11 sign-in with MFA in the last five
minutes. Active subscriptions block self-service card removal. A merchant must
set `allow_customer_method_setup` on its Stripe `ProviderConfig` to offer this
workflow. The current `SubscriptionReference` API is a display sync, not a
recurring-billing engine or a cancellation API.
