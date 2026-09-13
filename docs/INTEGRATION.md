# Merchant Integration

Minimal integration path:

1. Create a Fox Pay account.
2. Create a merchant.
3. Configure provider routes.
4. Generate a scoped secret key.
5. Create a payment intent.
6. Redirect the customer to `foxpay_checkout_url`.
7. Receive merchant webhook events.

## curl

```bash
curl -X POST http://127.0.0.1:8000/api/v1/payment-intents/ \
  -H "Content-Type: application/json" \
  -H "X-FoxPay-Key: foxpay_test_your_key_here" \
  -H "Idempotency-Key: ORDER-1001-create" \
  -d '{"amount":2500,"currency":"USD","payment_methods":["card","crypto"]}'
```

## PowerShell

```powershell
$headers = @{
  "Content-Type" = "application/json"
  "X-FoxPay-Key" = "foxpay_test_your_key_here"
  "Idempotency-Key" = "ORDER-1001-create"
}

$body = @{ amount = 2500; currency = "USD"; payment_methods = @("card", "crypto") } | ConvertTo-Json
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
    json={"amount": 2500, "currency": "USD", "payment_methods": ["card", "crypto"]},
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
  body: JSON.stringify({ amount: 2500, currency: "USD", payment_methods: ["card", "crypto"] })
});

const paymentIntent = await response.json();
window.location.href = paymentIntent.foxpay_checkout_url;
```
