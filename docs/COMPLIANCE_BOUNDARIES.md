# Compliance Boundaries

FoxPay is designed as payment orchestration software. The seller is the merchant of record and connects its own provider account. Provider-side charges, settlement, reserves, negative balances, disputes, and payouts remain in that seller account.

FoxPay does:

- authenticate sellers and authorize merchant-scoped users and API keys;
- create normalized payment intents and seller-scoped hosted checkout options;
- route through multiple authorized provider connections;
- verify provider events and reconcile payments, refunds, and disputes;
- record operational ledger observations, audit events, and signed merchant webhooks;
- apply platform-level velocity, amount, and routing controls;
- present direct seller wallet or seller-provider crypto invoices where configured.

FoxPay deliberately does not:

- collect PAN, CVV, magnetic-stripe, or EMV data;
- provide acquiring or card-network processing;
- hold customer or seller funds;
- maintain a seller balance or initiate seller payouts;
- control provider reserves, settlement timing, or negative balances;
- provide custody, mass payouts, or seller withdrawals;
- replace provider KYC/KYB, sanctions, AML, fraud, or dispute programs;
- determine a seller's taxes or file information returns for the seller;
- promise that provider-hosted checkout removes all PCI responsibility.

Stripe uses Standard Connect direct charges, Square uses seller OAuth, PayPal uses Partner Referrals only when approved, and NOWPayments uses that seller's encrypted account credential because no delegated OAuth flow is assumed. Application fees are zero by default. Any future fee must be centrally configured, clearly disclosed, recorded as FoxPay/TG11 revenue, and governed by an explicit refund policy.

Do not describe FoxPay as PCI certified, a bank, acquirer, money transmitter, custodian, licensed payment institution, or tax-reporting exemption unless qualified advisers and the relevant authorities have established that statement for the actual deployed model and jurisdictions.

Before public seller signup or expansion into a new country/product, obtain legal, accounting, privacy, sanctions, consumer-protection, and PCI advice based on real transaction flows and provider contracts. The [merchant agreement draft](MERCHANT_AGREEMENT_DRAFT.md) is a review artifact, not final legal advice.
