import json
from datetime import timedelta
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .abuse import enforce_intent_creation, metric_value, record_payment_outcome, request_ip
from .adapters.base import ProviderOperationResult
from .adapters.square_webhooks import square_dispute_update, square_payment_update, square_refund_update
from .models import (
    APIKey,
    AbuseAlert,
    Dispute,
    Merchant,
    MerchantProviderConnection,
    MerchantWebhookAttempt,
    MerchantWebhookEndpoint,
    MerchantWebhookEvent,
    PaymentAttempt,
    PaymentIntent,
    ProviderConfig,
    ProviderEvent,
    Refund,
)
from .paypal_events import process_paypal_event, record_paypal_capture_event
from .services import APIError, create_refund, reconcile_refund
from .payment_reconciliation import reconcile_payment_attempt
from .stripe_connect_events import handle_connect_event
from .webhook_delivery import deliver_event, deliver_pending_events


class APIConnectionError(Exception):
    pass


class RefundAndAbuseTests(TestCase):
    def setUp(self):
        cache.clear()
        user = get_user_model().objects.create_user(
            username="refund-owner@example.com",
            email="refund-owner@example.com",
            password="test-pass",
        )
        self.merchant = Merchant.objects.create(
            owner=user,
            name="Refund Seller",
            slug="refund-seller",
            status=Merchant.STATUS_ACTIVE,
            live_payments_enabled=True,
            allow_legacy_provider_configs=True,
        )
        _, self.raw_key = APIKey.issue(self.merchant, "Test key", environment="test")
        self.factory = RequestFactory()

    def intent_with_attempt(self, *, config=None, provider="mock", reference="", metadata=None, amount=1000):
        intent = PaymentIntent.objects.create(
            merchant=self.merchant,
            amount=amount,
            currency="USD",
            status=PaymentIntent.STATUS_SUCCEEDED,
            environment="test",
            request_ip_hash="a" * 64,
        )
        attempt = PaymentAttempt.objects.create(
            intent=intent,
            provider_config=config,
            method=PaymentAttempt.METHOD_CARD,
            provider=provider,
            status=PaymentAttempt.STATUS_SUCCEEDED,
            amount=amount,
            currency="USD",
            provider_reference=reference,
            provider_response_metadata=metadata or {},
        )
        return intent, attempt

    def stripe_config(self):
        connection = MerchantProviderConnection.objects.create(
            merchant=self.merchant,
            provider="stripe",
            environment="test",
            status=MerchantProviderConnection.STATUS_ACTIVE,
            authorization_method="stripe_connect",
            external_account_id="acct_refundseller",
        )
        config = ProviderConfig.objects.create(
            merchant=self.merchant,
            connection=connection,
            kind=ProviderConfig.KIND_CARD,
            provider="stripe-primary",
            adapter="stripe",
            environment="test",
            is_active=True,
        )
        return connection, config

    @override_settings(STRIPE_TEST_SECRET_KEY="sk_test_platform")
    def test_stripe_refund_is_scoped_and_api_idempotent(self):
        _, config = self.stripe_config()
        intent, _ = self.intent_with_attempt(
            config=config,
            provider="stripe-primary",
            metadata={"stripe_payment_intent_id": "pi_seller_payment"},
        )
        refund_api = Mock()
        refund_api.create.return_value = {
            "id": "re_provider_1",
            "amount": 400,
            "currency": "usd",
            "status": "succeeded",
        }
        stripe = Mock(Refund=refund_api)
        with patch("apps.payments.adapters.stripe_checkout.stripe_module", return_value=stripe):
            first = self.client.post(
                reverse("payments:refunds", args=[intent.public_id]),
                data=json.dumps({"amount": 400, "reason": "requested_by_customer"}),
                content_type="application/json",
                HTTP_X_FOXPAY_KEY=self.raw_key,
                HTTP_IDEMPOTENCY_KEY="seller-refund-1",
            )
            second = self.client.post(
                reverse("payments:refunds", args=[intent.public_id]),
                data=json.dumps({"amount": 400}),
                content_type="application/json",
                HTTP_X_FOXPAY_KEY=self.raw_key,
                HTTP_IDEMPOTENCY_KEY="seller-refund-1",
            )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json()["status"], Refund.STATUS_SUCCEEDED)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(refund_api.create.call_count, 1)
        kwargs = refund_api.create.call_args.kwargs
        self.assertEqual(kwargs["stripe_account"], "acct_refundseller")
        self.assertEqual(kwargs["api_key"], "sk_test_platform")
        self.assertEqual(kwargs["payment_intent"], "pi_seller_payment")
        self.assertNotIn("refund_application_fee", kwargs)
        self.assertEqual(Refund.objects.count(), 1)

    @override_settings(STRIPE_TEST_SECRET_KEY="sk_test_platform")
    def test_timeout_stays_pending_and_reserves_refundable_amount(self):
        _, config = self.stripe_config()
        intent, _ = self.intent_with_attempt(
            config=config,
            provider="stripe-primary",
            metadata={"stripe_payment_intent_id": "pi_timeout"},
        )
        refund_api = Mock()
        refund_api.create.side_effect = APIConnectionError("network ended after write")
        stripe = Mock(Refund=refund_api)
        with patch("apps.payments.adapters.stripe_checkout.stripe_module", return_value=stripe):
            first = self.client.post(
                reverse("payments:refunds", args=[intent.public_id]),
                data=json.dumps({"amount": 700}),
                content_type="application/json",
                HTTP_X_FOXPAY_KEY=self.raw_key,
                HTTP_IDEMPOTENCY_KEY="timeout-refund",
            )
            too_much = self.client.post(
                reverse("payments:refunds", args=[intent.public_id]),
                data=json.dumps({"amount": 301}),
                content_type="application/json",
                HTTP_X_FOXPAY_KEY=self.raw_key,
                HTTP_IDEMPOTENCY_KEY="different-refund",
            )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json()["status"], Refund.STATUS_PENDING)
        self.assertTrue(first.json()["reconciliation_required"])
        self.assertEqual(too_much.status_code, 400)
        self.assertEqual(too_much.json()["error"]["code"], "amount_exceeds_refundable")
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.STATUS_SUCCEEDED)

    @override_settings(STRIPE_TEST_SECRET_KEY="sk_test_platform")
    def test_kill_switch_is_rechecked_after_refund_reservation(self):
        _, config = self.stripe_config()
        intent, _ = self.intent_with_attempt(
            config=config,
            provider="stripe-primary",
            metadata={"stripe_payment_intent_id": "pi_killed"},
        )
        request = self.factory.post("/refund")
        original = __import__("apps.payments.services", fromlist=["_reserve_refund"])._reserve_refund

        def reserve_then_kill(*args, **kwargs):
            result = original(*args, **kwargs)
            Merchant.objects.filter(pk=self.merchant.pk).update(routing_enabled=False)
            return result

        refund_api = Mock()
        stripe = Mock(Refund=refund_api)
        with (
            patch("apps.payments.services._reserve_refund", side_effect=reserve_then_kill),
            patch("apps.payments.adapters.stripe_checkout.stripe_module", return_value=stripe),
        ):
            with self.assertRaises(APIError) as raised:
                create_refund(request, self.merchant, intent, {"amount": 100}, "kill-race")

        self.assertEqual(raised.exception.code, "routing_killed")
        refund_api.create.assert_not_called()
        self.assertEqual(Refund.objects.get().status, Refund.STATUS_FAILED)

    def test_mock_refund_reconciliation_is_idempotent(self):
        intent, attempt = self.intent_with_attempt()
        refund = Refund.objects.create(
            merchant=self.merchant,
            payment_intent=intent,
            payment_attempt=attempt,
            amount=250,
            currency="USD",
            provider="mock",
            status=Refund.STATUS_PENDING,
        )
        result = ProviderOperationResult(
            status=Refund.STATUS_SUCCEEDED,
            provider_reference="mock-refund",
            provider_status="succeeded",
        )
        reconcile_refund(refund, result)
        reconcile_refund(refund, result)
        self.assertEqual(refund.ledger_transactions.count(), 1)
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.STATUS_PARTIALLY_REFUNDED)

    @override_settings(
        SQUARE_APPLICATION_ID_TEST="sandbox-app",
        SQUARE_APPLICATION_SECRET_TEST="sandbox-secret",
    )
    def test_square_refund_uses_oauth_token_payment_id_and_idempotency(self):
        connection = MerchantProviderConnection.objects.create(
            merchant=self.merchant,
            provider="square",
            environment="test",
            status=MerchantProviderConnection.STATUS_ACTIVE,
            authorization_method="square_oauth",
            external_account_id="square-merchant",
            token_refreshed_at=timezone.now(),
            token_expires_at=timezone.now() + timedelta(days=10),
        )
        connection.set_access_token("square-oauth-access")
        connection.set_refresh_token("square-oauth-refresh")
        connection.save()
        config = ProviderConfig.objects.create(
            merchant=self.merchant,
            connection=connection,
            kind=ProviderConfig.KIND_CARD,
            provider="square-primary",
            adapter="square",
            environment="test",
            settings={"location_id": "L1", "location_currency": "USD"},
        )
        intent, _ = self.intent_with_attempt(config=config, provider="square-primary", reference="sq_payment_1")
        response = Mock(status_code=200)
        response.json.return_value = {
            "refund": {
                "id": "sq_refund_1",
                "payment_id": "sq_payment_1",
                "amount_money": {"amount": 300, "currency": "USD"},
                "status": "PENDING",
            }
        }
        with patch("apps.payments.adapters.square_checkout.requests.request", return_value=response) as request_call:
            api_response = self.client.post(
                reverse("payments:refunds", args=[intent.public_id]),
                data=json.dumps({"amount": 300}),
                content_type="application/json",
                HTTP_X_FOXPAY_KEY=self.raw_key,
                HTTP_IDEMPOTENCY_KEY="square-refund",
            )

        self.assertEqual(api_response.status_code, 202)
        kwargs = request_call.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer square-oauth-access")
        self.assertEqual(kwargs["json"]["payment_id"], "sq_payment_1")
        self.assertLessEqual(len(kwargs["json"]["idempotency_key"]), 45)

    @override_settings(
        PAYPAL_PARTNER_CLIENT_ID_TEST="paypal-client",
        PAYPAL_PARTNER_CLIENT_SECRET_TEST="paypal-secret",
        PAYPAL_PARTNER_MERCHANT_ID_TEST="2ABCDEFGHJKLM",
        PAYPAL_PARTNER_ATTRIBUTION_ID_TEST="FoxPay_SP_PCP",
    )
    def test_paypal_refund_uses_capture_and_request_id(self):
        connection = MerchantProviderConnection.objects.create(
            merchant=self.merchant,
            provider="paypal",
            environment="test",
            status=MerchantProviderConnection.STATUS_ACTIVE,
            authorization_method="paypal_partner",
            external_account_id="3ABCDEFGHJKLM",
        )
        config = ProviderConfig.objects.create(
            merchant=self.merchant,
            connection=connection,
            kind=ProviderConfig.KIND_CARD,
            provider="paypal-primary",
            adapter="paypal",
            environment="test",
        )
        intent, _ = self.intent_with_attempt(config=config, provider="paypal-primary", reference="CAPTURE123")
        with patch(
            "apps.payments.adapters.paypal_checkout.PayPalCheckoutAdapter.call",
            return_value=(201, {
                "id": "PPREFUND1",
                "status": "COMPLETED",
                "amount": {"value": "4.00", "currency_code": "USD"},
            }),
        ) as call:
            response = self.client.post(
                reverse("payments:refunds", args=[intent.public_id]),
                data=json.dumps({"amount": 400}),
                content_type="application/json",
                HTTP_X_FOXPAY_KEY=self.raw_key,
                HTTP_IDEMPOTENCY_KEY="paypal-refund",
            )

        self.assertEqual(response.status_code, 201)
        args, kwargs = call.call_args
        self.assertEqual(args[1], "/v2/payments/captures/CAPTURE123/refund")
        self.assertEqual(args[2]["amount"], {"currency_code": "USD", "value": "4.00"})
        self.assertTrue(kwargs["request_id"])

    def test_paypal_capture_rechecks_the_merchant_kill_switch(self):
        config = ProviderConfig.objects.create(
            merchant=self.merchant,
            kind=ProviderConfig.KIND_CARD,
            provider="paypal-legacy-capture",
            adapter="paypal",
            environment="test",
        )
        intent = PaymentIntent.objects.create(
            merchant=self.merchant,
            amount=1000,
            currency="USD",
            environment="test",
        )
        attempt = PaymentAttempt.objects.create(
            intent=intent,
            provider_config=config,
            method=PaymentAttempt.METHOD_CARD,
            provider=config.provider,
            amount=intent.amount,
            currency=intent.currency,
            provider_reference="PAYPAL-ORDER-KILLED",
        )
        Merchant.objects.filter(pk=self.merchant.pk).update(routing_enabled=False)
        event = {
            "id": "WH-PAYPAL-KILLED",
            "event_type": "CHECKOUT.ORDER.APPROVED",
            "resource": {
                "id": attempt.provider_reference,
                "purchase_units": [{
                    "custom_id": intent.public_id,
                    "invoice_id": f"{intent.public_id}:{attempt.pk}",
                }],
            },
        }
        with patch("apps.payments.adapters.paypal_checkout.PayPalCheckoutAdapter.capture") as capture:
            result = process_paypal_event(config, event)
        capture.assert_not_called()
        self.assertTrue(result["blocked"])

    @override_settings(FOXPAY_ASYNC_TASKS_ENABLED=True)
    def test_paypal_approval_is_durable_and_redacted_before_async_capture(self):
        config = ProviderConfig.objects.create(
            merchant=self.merchant,
            kind=ProviderConfig.KIND_CARD,
            provider="paypal-async",
            adapter="paypal",
            environment="test",
        )
        intent = PaymentIntent.objects.create(
            merchant=self.merchant,
            amount=1000,
            currency="USD",
            environment="test",
        )
        attempt = PaymentAttempt.objects.create(
            intent=intent,
            provider_config=config,
            method=PaymentAttempt.METHOD_CARD,
            provider=config.provider,
            amount=intent.amount,
            currency=intent.currency,
            provider_reference="PAYPAL-ORDER-ASYNC",
        )
        event = {
            "id": "WH-PAYPAL-ASYNC",
            "event_type": "CHECKOUT.ORDER.APPROVED",
            "resource": {
                "id": attempt.provider_reference,
                "payer": {"email_address": "buyer@example.com"},
                "purchase_units": [{
                    "custom_id": intent.public_id,
                    "invoice_id": f"{intent.public_id}:{attempt.pk}",
                }],
            },
        }
        with (
            patch("apps.payments.paypal_events._enqueue_capture_event") as enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            result = record_paypal_capture_event(config, event)
        inbox = ProviderEvent.objects.get(provider_event_id=event["id"])
        enqueue.assert_called_once_with(inbox.pk)
        self.assertTrue(result["queued"])
        self.assertIsNone(inbox.processed_at)
        self.assertNotIn("buyer@example.com", json.dumps(inbox.payload))

    def test_stripe_signed_connection_event_reconciles_refund_and_dispute(self):
        connection, config = self.stripe_config()
        intent, attempt = self.intent_with_attempt(
            config=config,
            provider="stripe-primary",
            metadata={"stripe_payment_intent_id": "pi_event"},
        )
        refund = Refund.objects.create(
            merchant=self.merchant,
            payment_intent=intent,
            payment_attempt=attempt,
            provider_config=config,
            amount=200,
            currency="USD",
            provider="stripe-primary",
            status=Refund.STATUS_PENDING,
        )
        refund_event = {
            "id": "evt_refund",
            "type": "refund.updated",
            "account": connection.external_account_id,
            "livemode": False,
            "data": {"object": {
                "id": "re_event",
                "status": "succeeded",
                "amount": 200,
                "currency": "usd",
                "payment_intent": "pi_event",
                "metadata": {"foxpay_refund_id": refund.public_id},
            }},
        }
        dispute_event = {
            "id": "evt_dispute",
            "type": "charge.dispute.created",
            "account": connection.external_account_id,
            "livemode": False,
            "data": {"object": {
                "id": "dp_event",
                "status": "needs_response",
                "amount": 500,
                "currency": "usd",
                "payment_intent": "pi_event",
                "reason": "fraudulent",
                "evidence_details": {"due_by": 1893456000},
            }},
        }

        self.assertTrue(handle_connect_event(connection, refund_event))
        self.assertTrue(handle_connect_event(connection, dispute_event))
        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.STATUS_SUCCEEDED)
        dispute = Dispute.objects.get(provider_dispute_id="dp_event")
        self.assertEqual(dispute.payment_attempt, attempt)
        self.assertEqual(dispute.status, "needs_response")

    def test_distinct_stripe_deliveries_record_one_payment_outcome(self):
        connection, config = self.stripe_config()
        intent = PaymentIntent.objects.create(
            merchant=self.merchant,
            amount=1000,
            currency="USD",
            status=PaymentIntent.STATUS_REQUIRES_PAYMENT,
            environment="test",
            request_ip_hash="b" * 64,
        )
        attempt = PaymentAttempt.objects.create(
            intent=intent,
            provider_config=config,
            method=PaymentAttempt.METHOD_CARD,
            provider="stripe-primary",
            status=PaymentAttempt.STATUS_ACTION_REQUIRED,
            amount=1000,
            currency="USD",
            provider_reference="cs_outcome_once",
        )
        data = {
            "id": attempt.provider_reference,
            "mode": "payment",
            "livemode": False,
            "payment_status": "paid",
            "payment_intent": "pi_outcome_once",
            "amount_total": intent.amount,
            "currency": "usd",
            "client_reference_id": intent.public_id,
            "metadata": {
                "foxpay_attempt_id": str(attempt.pk),
                "foxpay_payment_intent": intent.public_id,
                "foxpay_merchant_id": str(self.merchant.uuid),
            },
        }
        for event_id in ("evt_outcome_1", "evt_outcome_2"):
            self.assertTrue(handle_connect_event(connection, {
                "id": event_id,
                "type": "checkout.session.completed",
                "account": connection.external_account_id,
                "livemode": False,
                "data": {"object": data},
            }))
        self.assertEqual(
            metric_value("test", "outcome:hour:success:merchant", self.merchant.pk),
            1,
        )

    def test_square_event_rejects_wrong_payment_ownership(self):
        connection = MerchantProviderConnection.objects.create(
            merchant=self.merchant,
            provider="square",
            environment="test",
            status=MerchantProviderConnection.STATUS_ACTIVE,
            authorization_method="square_oauth",
            external_account_id="square-event-merchant",
        )
        config = ProviderConfig.objects.create(
            merchant=self.merchant,
            connection=connection,
            kind=ProviderConfig.KIND_CARD,
            provider="square-event",
            adapter="square",
            environment="test",
            settings={"location_id": "LOCATION"},
        )
        intent, attempt = self.intent_with_attempt(config=config, provider="square-event", reference="payment-right")
        refund = Refund.objects.create(
            merchant=self.merchant,
            payment_intent=intent,
            payment_attempt=attempt,
            provider_config=config,
            amount=100,
            currency="USD",
            provider="square-event",
            status=Refund.STATUS_PENDING,
            provider_refund_id="refund-square",
        )
        event = {
            "event_id": "square-refund-event",
            "type": "refund.updated",
            "merchant_id": connection.external_account_id,
            "data": {"object": {"refund": {
                "id": refund.provider_refund_id,
                "payment_id": "payment-wrong",
                "location_id": "LOCATION",
                "amount_money": {"amount": 100, "currency": "USD"},
                "status": "COMPLETED",
            }}},
        }
        result, error = square_refund_update(config, event)
        self.assertIsNone(result)
        self.assertEqual(error, "refund_details_mismatch")
        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.STATUS_PENDING)

    def test_square_payment_requires_exact_connection_merchant(self):
        connection = MerchantProviderConnection.objects.create(
            merchant=self.merchant,
            provider="square",
            environment="test",
            status=MerchantProviderConnection.STATUS_ACTIVE,
            authorization_method="square_oauth",
            external_account_id="square-owner",
        )
        config = ProviderConfig.objects.create(
            merchant=self.merchant,
            connection=connection,
            kind=ProviderConfig.KIND_CARD,
            provider="square-owner-route",
            adapter="square",
            environment="test",
            settings={"location_id": "LOCATION"},
        )
        intent, _ = self.intent_with_attempt(
            config=config,
            provider=config.provider,
            metadata={"square_order_id": "order-owner", "square_location_id": "LOCATION"},
        )
        update, error = square_payment_update(config, {
            "event_id": "square-wrong-owner",
            "type": "payment.updated",
            "merchant_id": "square-someone-else",
            "data": {"type": "payment", "object": {"payment": {
                "id": "payment-owner",
                "order_id": "order-owner",
                "location_id": "LOCATION",
                "status": "COMPLETED",
                "total_money": {"amount": intent.amount, "currency": intent.currency},
            }}},
        })
        self.assertIsNone(update)
        self.assertEqual(error, "payment_details_mismatch")

    def test_legacy_square_reconciliation_uses_configured_merchant_id(self):
        config = ProviderConfig.objects.create(
            merchant=self.merchant,
            kind=ProviderConfig.KIND_CARD,
            provider="square-legacy",
            adapter="square",
            environment="test",
            settings={
                "location_id": "LEGACY_LOCATION",
                "square_merchant_id": "legacy-square-owner",
            },
        )
        intent = PaymentIntent.objects.create(
            merchant=self.merchant,
            amount=1000,
            currency="USD",
            status=PaymentIntent.STATUS_REQUIRES_PAYMENT,
            environment="test",
        )
        attempt = PaymentAttempt.objects.create(
            intent=intent,
            provider_config=config,
            method=PaymentAttempt.METHOD_CARD,
            provider=config.provider,
            status=PaymentAttempt.STATUS_ACTION_REQUIRED,
            amount=intent.amount,
            currency=intent.currency,
            provider_reference="link-legacy",
            provider_response_metadata={
                "square_order_id": "order-legacy",
                "square_location_id": "LEGACY_LOCATION",
            },
        )
        responses = [
            {"order": {"tenders": [{"payment_id": "payment-legacy"}]}},
            {"payment": {
                "id": "payment-legacy",
                "order_id": "order-legacy",
                "location_id": "LEGACY_LOCATION",
                "status": "COMPLETED",
                "total_money": {"amount": 1000, "currency": "USD"},
            }},
        ]
        with patch(
            "apps.payments.adapters.square_checkout.SquareCheckoutAdapter._refund_request",
            side_effect=responses,
        ):
            self.assertTrue(reconcile_payment_attempt(attempt.pk))
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.STATUS_SUCCEEDED)

    def test_square_dispute_is_normalized(self):
        connection = MerchantProviderConnection.objects.create(
            merchant=self.merchant,
            provider="square",
            environment="test",
            status=MerchantProviderConnection.STATUS_ACTIVE,
            authorization_method="square_oauth",
            external_account_id="square-dispute-merchant",
        )
        config = ProviderConfig.objects.create(
            merchant=self.merchant,
            connection=connection,
            kind=ProviderConfig.KIND_CARD,
            provider="square-dispute",
            adapter="square",
            environment="test",
            settings={"location_id": "LOCATION"},
        )
        _, attempt = self.intent_with_attempt(config=config, provider="square-dispute", reference="payment-disputed")
        event = {
            "event_id": "square-dispute-event",
            "type": "dispute.created",
            "merchant_id": connection.external_account_id,
            "data": {"object": {"dispute": {
                "id": "dispute-square",
                "location_id": "LOCATION",
                "disputed_payment": {"payment_id": "payment-disputed"},
                "amount_money": {"amount": 700, "currency": "USD"},
                "reason": "AMOUNT_DIFFERS",
                "state": "EVIDENCE_REQUIRED",
                "due_at": "2030-01-01T00:00:00Z",
            }}},
        }
        dispute, error = square_dispute_update(config, event)
        self.assertEqual(error, "")
        self.assertEqual(dispute.payment_attempt, attempt)
        self.assertEqual(dispute.status, "needs_response")

    @override_settings(FOXPAY_INTENT_RATE_LIMIT_PER_IP=1, FOXPAY_INTENT_RATE_LIMIT_PER_MERCHANT=100)
    def test_per_ip_intent_limit_does_not_suspend_whole_merchant(self):
        payload = {"amount": 500, "currency": "USD", "payment_methods": ["card"]}
        first = self.client.post(
            reverse("payments:payment_intents"),
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_FOXPAY_KEY=self.raw_key,
            REMOTE_ADDR="203.0.113.9",
        )
        second = self.client.post(
            reverse("payments:payment_intents"),
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_FOXPAY_KEY=self.raw_key,
            REMOTE_ADDR="203.0.113.9",
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 429)
        self.merchant.refresh_from_db()
        self.assertIsNone(self.merchant.temporarily_restricted_until)
        self.assertTrue(AbuseAlert.objects.filter(rule_code="intent_ip_velocity").exists())

    @override_settings(FOXPAY_INTENT_RATE_LIMIT_PER_IP=100, FOXPAY_INTENT_RATE_LIMIT_PER_MERCHANT=1)
    def test_merchant_velocity_triggers_temporary_routing_restriction(self):
        payload = {"amount": 500, "currency": "USD", "payment_methods": ["card"]}
        for address in ("198.51.100.1", "198.51.100.2"):
            response = self.client.post(
                reverse("payments:payment_intents"),
                data=json.dumps(payload),
                content_type="application/json",
                HTTP_X_FOXPAY_KEY=self.raw_key,
                REMOTE_ADDR=address,
            )
        self.assertEqual(response.status_code, 429)
        self.merchant.refresh_from_db()
        self.assertGreater(self.merchant.temporarily_restricted_until, timezone.now())
        self.assertEqual(self.merchant.temporary_restriction_reason, "intent_merchant_velocity")

    @override_settings(
        FOXPAY_CARD_TEST_MIN_OUTCOMES=2,
        FOXPAY_CARD_TEST_FAILURE_RATIO=1,
        FOXPAY_CARD_TEST_SMALL_RATIO=1,
    )
    def test_failed_small_payment_ratio_triggers_restriction(self):
        request = self.factory.post("/", REMOTE_ADDR="198.51.100.20")
        for _ in range(2):
            ip_hash = enforce_intent_creation(request, self.merchant, 100)
            record_payment_outcome(
                self.merchant,
                succeeded=False,
                ip_hash=ip_hash,
                amount=100,
                environment="test",
            )
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.temporary_restriction_reason, "card_testing_pattern")

    def test_raw_card_fields_are_rejected_without_storage(self):
        response = self.client.post(
            reverse("payments:payment_intents"),
            data=json.dumps({
                "amount": 1000,
                "currency": "USD",
                "card": {"number": "4242424242424242", "cvv": "123"},
            }),
            content_type="application/json",
            HTTP_X_FOXPAY_KEY=self.raw_key,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "raw_card_data_forbidden")
        self.assertFalse(PaymentIntent.objects.exists())
        top_level = self.client.post(
            reverse("payments:payment_intents"),
            data=json.dumps({"amount": 1000, "currency": "USD", "number": "4242 4242 4242 4242"}),
            content_type="application/json",
            HTTP_X_FOXPAY_KEY=self.raw_key,
        )
        self.assertEqual(top_level.status_code, 422)
        self.assertFalse(PaymentIntent.objects.exists())

    @override_settings(FOXPAY_TRUSTED_PROXY_IPS=("127.0.0.1",))
    def test_forwarded_ip_is_used_only_for_an_explicitly_trusted_proxy(self):
        trusted = self.factory.get(
            "/",
            REMOTE_ADDR="127.0.0.1",
            HTTP_X_FORWARDED_FOR="198.51.100.25, 127.0.0.1",
        )
        untrusted = self.factory.get(
            "/",
            REMOTE_ADDR="203.0.113.10",
            HTTP_X_FORWARDED_FOR="198.51.100.25",
        )
        self.assertEqual(request_ip(trusted), "198.51.100.25")
        self.assertEqual(request_ip(untrusted), "203.0.113.10")

    @override_settings(FOXPAY_WEBHOOK_MAX_ATTEMPTS=3, FOXPAY_WEBHOOK_MAX_BACKOFF_SECONDS=60)
    def test_outgoing_webhook_retries_then_succeeds(self):
        endpoint, _ = MerchantWebhookEndpoint.create_with_secret(
            self.merchant,
            "https://merchant.example/webhook",
        )
        event = MerchantWebhookEvent.objects.create(
            merchant=self.merchant,
            event_type="payment_intent.succeeded",
            payload={"payment_intent": "fp_pi_test"},
        )
        with patch("apps.payments.webhook_delivery.safe_webhook_post", side_effect=[500, 204]):
            first = deliver_event(event.pk)
            MerchantWebhookAttempt.objects.filter(event=event, endpoint=endpoint).update(
                next_retry_at=timezone.now() - timedelta(seconds=1)
            )
            second = deliver_event(event.pk)
        event.refresh_from_db()
        self.assertEqual(first["failed"], 1)
        self.assertEqual(second["delivered"], 1)
        self.assertEqual(event.status, MerchantWebhookEvent.STATUS_DELIVERED)
        self.assertEqual(event.delivery_attempts.count(), 2)

    @override_settings(FOXPAY_WEBHOOK_MAX_ATTEMPTS=3, FOXPAY_WEBHOOK_MAX_BACKOFF_SECONDS=60)
    def test_deferred_webhook_does_not_starve_newer_events(self):
        MerchantWebhookEndpoint.create_with_secret(
            self.merchant,
            "https://merchant.example/webhook",
        )
        first = MerchantWebhookEvent.objects.create(
            merchant=self.merchant,
            event_type="payment_intent.failed",
            payload={"payment_intent": "fp_pi_first"},
        )
        second = MerchantWebhookEvent.objects.create(
            merchant=self.merchant,
            event_type="payment_intent.succeeded",
            payload={"payment_intent": "fp_pi_second"},
        )
        with patch("apps.payments.webhook_delivery.safe_webhook_post", side_effect=[500, 204]):
            deliver_pending_events(limit=1)
            result = deliver_pending_events(limit=1)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, MerchantWebhookEvent.STATUS_PENDING)
        self.assertEqual(second.status, MerchantWebhookEvent.STATUS_DELIVERED)
        self.assertEqual(result["delivered"], 1)
