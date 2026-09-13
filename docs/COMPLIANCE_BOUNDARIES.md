# Compliance Boundaries

Fox Pay currently acts as a payment orchestration layer.

Fox Pay currently does:

- expose a unified merchant API
- create payment intents
- create hosted checkout options
- route through provider adapters
- store normalized transaction state
- record ledger entries
- emit merchant webhook events
- create non-custodial crypto invoices

Fox Pay deliberately delegates:

- raw card entry
- card credential storage
- acquiring
- card network certification
- 3-D Secure/SCA execution
- bank rails
- custody
- KYC/KYB
- AML/sanctions screening
- payout money movement

Do not describe Fox Pay as PCI certified, licensed, or regulated unless those reviews and approvals have actually happened.

