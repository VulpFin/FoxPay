# FoxPay Merchant Agreement (Draft for Attorney Review)

Version: draft-2026-09-15

This document is an operational drafting aid, not legal advice and not an approved production agreement. Qualified counsel must review, revise, and approve it before public seller signup is enabled. Bracketed items are unresolved drafting instructions, not agreed terms.

## 1. Parties, effective date, and contact information

This agreement is between TG11 LLC, operator of FoxPay ("TG11"), and the person or entity identified in the seller application ("Seller"). It becomes effective when an authorized Seller representative accepts the identified version in FoxPay, subject to any approval condition finalized by counsel.

- TG11 LLC legal address: **[COUNSEL/COMPANY TO INSERT]**
- Legal notices email: **[COUNSEL/COMPANY TO INSERT]**
- Support contact: `support@tg11.org`

The Seller represents that its applicant may bind it and that its application information is complete and accurate.

## 2. Seller and merchant-of-record responsibility

The Seller, not FoxPay, is the merchant of record for its sales. The Seller is responsible for its products and services, pricing, customer disclosures, order acceptance, fulfillment, delivery, support, cancellations, refund policy, warranties, licenses, and compliance in every place it sells.

## 3. FoxPay's technical-service role

FoxPay supplies software that creates and tracks payment requests and routes them to a provider account authorized by the Seller. FoxPay does not collect raw card numbers, hold customer or Seller funds, maintain a Seller payout balance, or pay Sellers. The provider controls authorization, capture, settlement, reserves, negative balances, and payouts under its own agreement with the Seller. Nothing here promises a particular legal or regulatory classification.

## 4. Provider accounts, authorization, and prohibited activity

The Seller must maintain each connected provider account in good standing, accept provider terms, and complete provider identity, sanctions, risk, and business reviews. The Seller authorizes FoxPay to perform only the payment, refund, status, and connection actions supported by the access it grants. The Seller is responsible for provider account, bank, wallet, and payout settings.

The Seller must comply with FoxPay's acceptable-use policy and every provider's restricted/prohibited-business rules. The Seller may not use FoxPay for unlawful, deceptive, infringing, abusive, sanctions-evasive, card-testing, credential-trafficking, or unauthorized transactions. **[COUNSEL TO ALIGN THE FINAL LIST, APPEAL RIGHTS, AND JURISDICTION-SPECIFIC REQUIREMENTS.]**

## 5. Fees and application-fee refunds

Providers determine their processing, foreign-exchange, refund, dispute, reserve, and other charges. FoxPay application fees are zero unless a separately disclosed fee schedule is accepted and technically enabled by TG11.

Before any application fee is introduced, the final agreement and product must identify its amount or calculation, timing, taxes, and refund treatment. Proposed default for counsel review: **a FoxPay application fee is refunded proportionally when the underlying provider refund succeeds, except where the disclosed fee schedule expressly identifies a nonrefundable service already performed and applicable law permits that treatment.** Partial refunds would return the corresponding proportion; failed or canceled refund requests would not create an application-fee refund. **[COUNSEL AND ACCOUNTING TO APPROVE OR REPLACE THIS POLICY BEFORE FEES ARE ENABLED.]**

## 6. Taxes and information reporting

The Seller is responsible for product taxability, registrations, collection, invoices, records, remittance, income reporting, and information returns applicable to its business. Provider or FoxPay tax features are tools, not tax advice or a guarantee of correct treatment. This agreement creates no exemption from tax or reporting duties. **[COUNSEL/ACCOUNTING TO ADDRESS ANY TG11 OR PROVIDER REPORTING obligations.]**

## 7. Refunds, disputes, chargebacks, and negative balances

The Seller owns its customer refund policy and authorizes each refund. FoxPay may transmit the instruction, but the provider determines execution and fund movement. The Seller is responsible for disputes, chargebacks, evidence, fees, reversals, and resulting provider negative balances, and must maintain any funds or reserves required by the provider. FoxPay may show dispute status but does not promise to submit evidence unless that feature is expressly offered.

## 8. Security and credentials

The Seller must protect FoxPay keys, webhook secrets, provider accounts, connected applications, and authorized users; use least privilege and multifactor authentication where available; and promptly report compromise. The Seller must never send card numbers, CVV, magnetic-stripe, or EMV data to FoxPay. It must use provider-hosted checkout and keep its integration, return URLs, webhook receiver, and order fulfillment logic secure.

FoxPay may revoke its own keys or provider authorization after suspected compromise. Revoking FoxPay access does not replace revocation or rotation at the provider.

## 9. Data processing, privacy, and retention

FoxPay processes account identifiers, transaction metadata, provider references, technical logs, risk signals, and support/audit records to operate and secure the service. Provider credentials are encrypted and API keys are hashed. The final agreement and privacy notice must identify roles, lawful bases, subprocessors, cross-border transfers, retention periods, deletion limits, incident duties, and any required data-processing addendum. Financial, security, dispute, and legal records may remain after account closure where retention is required or reasonably necessary.

## 10. Suspension, routing termination, and disconnection

TG11 may reject an application, temporarily restrict or stop FoxPay routing, revoke FoxPay credentials, or disconnect FoxPay's provider access for violations, fraud/security risk, provider instruction, nonpayment, or legal requirements. FoxPay cannot pause a provider payout it does not control. The final agreement must specify notice, cure, appeal, emergency action, termination effect, and data-export rights. The Seller may stop using FoxPay and disconnect providers, subject to pending transactions, disputes, and retention duties.

## 11. Warranties and service availability

**[COUNSEL TO DRAFT PERMITTED REPRESENTATIONS, WARRANTY DISCLAIMERS, SERVICE-LEVEL TERMS, AND TREATMENT OF PROVIDER/NETWORK OUTAGES.]** FoxPay should not promise uninterrupted provider availability, guaranteed authorization, irreversible crypto settlement, fraud prevention, or recovery of provider-held funds.

## 12. Indemnity

**[COUNSEL TO DRAFT MUTUAL OR APPROPRIATELY ALLOCATED INDEMNITIES.]** Topics should include Seller products/services and customer claims, unlawful or prohibited activity, taxes, Seller-provided content/data, credential compromise within a party's control, privacy/security breaches, intellectual property, provider-rule violations, and each party's defense/settlement obligations. No indemnity is created merely by this placeholder.

## 13. Limitation of liability

**[COUNSEL TO DRAFT EXCLUSIONS, LIABILITY CAP, CARVE-OUTS, AND JURISDICTION-SPECIFIC LIMITS.]** Counsel should address direct and consequential loss, lost profit/data, provider outages/actions, fraud and chargebacks, confidentiality/security breaches, indemnities, gross negligence/willful misconduct, and claims that cannot legally be limited. No liability allocation is agreed merely by this placeholder.

## 14. Governing law and dispute process

- Governing law: **[COUNSEL TO INSERT]**
- Exclusive venue or arbitration forum: **[COUNSEL TO INSERT]**
- Class/collective-action or jury provisions, if any: **[COUNSEL TO INSERT AND CONFIRM ENFORCEABILITY]**

Counsel must account for mandatory consumer, privacy, employment, and commercial law that cannot be waived.

## 15. Notices

Formal notices to TG11 must use the legal address/email in Section 1 once completed. Seller notices go to its current account contact. **[COUNSEL TO DEFINE DELIVERY METHODS, WHEN NOTICE IS EFFECTIVE, AND HANDLING OF SECURITY/LEGAL EMERGENCIES.]** Routine service messages may be delivered in-product or by email but should not silently substitute for formal notice where law or the final agreement requires otherwise.

## 16. Changes and reacceptance

Each accepted agreement and acceptable-use version is recorded with its content hash, user, timestamp, IP, and user agent. TG11 should give the notice period required by the final agreement and law. Material changes to fees, money flow, data use, dispute allocation, prohibited activity, or liability require explicit reacceptance before continued affected use unless counsel approves another lawful process. Historical acceptance records remain immutable.

## 17. Remaining general terms

**[COUNSEL TO COMPLETE ASSIGNMENT, FORCE MAJEURE, SEVERABILITY, WAIVER, ENTIRE AGREEMENT, ORDER OF PRECEDENCE, SURVIVAL, EXPORT/SANCTIONS, ELECTRONIC SIGNATURE, AND THIRD-PARTY BENEFICIARY TERMS.]** Counsel and accounting advisers must assess payment, PCI, money-transmission, sanctions, consumer-protection, privacy, tax, and reporting obligations against the real operating model, provider contracts, and jurisdictions.
