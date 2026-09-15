# Crypto

The current crypto model is noncustodial by default. FoxPay can create invoices
for merchant-controlled wallet addresses or use the seller's own NOWPayments
account for hosted invoices. FoxPay observes settlement; it does not receive or
withdraw the seller's crypto.

`CryptoInvoice` tracks:

- asset
- network
- destination address
- expected amount
- received amount
- exchange-rate snapshot
- expiration
- confirmation requirements
- transaction hash
- settlement state

Asset and network are explicit. USDC on one chain is not assumed equivalent to USDC on another chain.

## Not Implemented Yet

- Consensus validation.
- Node/RPC polling.
- Reorg handling.
- Automated crypto refunds.
- Custody.
- Stablecoin provider integration.

Use provider or node APIs for production monitoring rather than implementing blockchain consensus in Fox Pay.

NOWPayments credentials are entered once through the protected seller dashboard,
encrypted immediately, and never shown again. Its signed IPN can settle only the
matching merchant route, intent, order reference, amount, and currency. This is
credential-based authorization, not OAuth. Automated crypto refunds remain out
of scope.
