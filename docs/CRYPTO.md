# Crypto

The current crypto model is non-custodial by default. Fox Pay creates invoices that instruct the customer to pay merchant-controlled wallet addresses, then observes settlement through webhooks or later monitoring adapters.

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

