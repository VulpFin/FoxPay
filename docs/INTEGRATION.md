# Merchant Integration

Minimal integration path:

1. Sign in with TG11 Accounts and apply for a seller account.
2. Wait for production approval; test access is policy-controlled while pending.
3. Connect seller-owned provider accounts and configure prioritized routes.
4. Register success/cancel origins and an HTTPS merchant webhook endpoint.
5. Generate a scoped, environment-specific key; its secret is shown once.
6. Create a payment intent with an idempotency key.
7. Redirect the customer to `foxpay_checkout_url`.
8. Fulfil only after a verified FoxPay webhook or authenticated intent retrieval
   reports settlement. Never trust the browser return by itself.

## curl

```bash
curl -X POST http://127.0.0.1:8000/api/v1/payment-intents/ \
  -H "Content-Type: application/json" \
  -H "X-FoxPay-Key: foxpay_test_your_key_here" \
  -H "Idempotency-Key: ORDER-1001-create" \
  -d '{"amount":2500,"currency":"USD","payment_methods":["card","crypto"],"success_url":"https://shop.example/orders/1001/paid","cancel_url":"https://shop.example/orders/1001"}'
```

## PowerShell

```powershell
$headers = @{
  "Content-Type" = "application/json"
  "X-FoxPay-Key" = "foxpay_test_your_key_here"
  "Idempotency-Key" = "ORDER-1001-create"
}

$body = @{
  amount = 2500
  currency = "USD"
  payment_methods = @("card", "crypto")
  success_url = "https://shop.example/orders/1001/paid"
  cancel_url = "https://shop.example/orders/1001"
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/payment-intents/" -Headers $headers -Body $body
```

## Python

```python
import requests

response = requests.post(
    "http://127.0.0.1:8000/api/v1/payment-intents/",
    headers={
        "X-FoxPay-Key": "foxpay_test_your_key_here",
        "Idempotency-Key": "ORDER-1001-create",
    },
    json={
        "amount": 2500,
        "currency": "USD",
        "payment_methods": ["card", "crypto"],
        "success_url": "https://shop.example/orders/1001/paid",
        "cancel_url": "https://shop.example/orders/1001",
    },
    timeout=10,
)
response.raise_for_status()
print(response.json()["foxpay_checkout_url"])
```

## JavaScript

```javascript
const response = await fetch("http://127.0.0.1:8000/api/v1/payment-intents/", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-FoxPay-Key": "foxpay_test_your_key_here",
    "Idempotency-Key": "ORDER-1001-create"
  },
  body: JSON.stringify({
    amount: 2500,
    currency: "USD",
    payment_methods: ["card", "crypto"],
    success_url: "https://shop.example/orders/1001/paid",
    cancel_url: "https://shop.example/orders/1001"
  })
});

const paymentIntent = await response.json();
window.location.href = paymentIntent.foxpay_checkout_url;
```
