from django.db import transaction

from .models import LedgerAccount, LedgerEntry, LedgerTransaction


def get_account(merchant, code, name, account_type, currency):
    account, _ = LedgerAccount.objects.get_or_create(
        merchant=merchant,
        code=code,
        currency=currency,
        defaults={"name": name, "account_type": account_type},
    )
    return account


@transaction.atomic
def record_payment_success(intent, request_id=""):
    existing = intent.ledger_transactions.filter(transaction_type="payment_succeeded").first()
    if existing:
        return existing
    cash = get_account(intent.merchant, "external_provider_receivable", "External provider receivable", LedgerAccount.TYPE_ASSET, intent.currency)
    clearing = get_account(intent.merchant, "merchant_payment_clearing", "Merchant payment clearing", LedgerAccount.TYPE_LIABILITY, intent.currency)
    tx = LedgerTransaction.objects.create(
        merchant=intent.merchant,
        transaction_type="payment_succeeded",
        payment_intent=intent,
        request_id=request_id,
    )
    LedgerEntry.objects.create(transaction=tx, account=cash, direction=LedgerEntry.DIRECTION_DEBIT, amount=intent.amount, currency=intent.currency)
    LedgerEntry.objects.create(transaction=tx, account=clearing, direction=LedgerEntry.DIRECTION_CREDIT, amount=intent.amount, currency=intent.currency)
    return tx


@transaction.atomic
def record_refund(refund, request_id=""):
    clearing = get_account(refund.merchant, "merchant_payment_clearing", "Merchant payment clearing", LedgerAccount.TYPE_LIABILITY, refund.currency)
    cash = get_account(refund.merchant, "external_provider_receivable", "External provider receivable", LedgerAccount.TYPE_ASSET, refund.currency)
    tx = LedgerTransaction.objects.create(
        merchant=refund.merchant,
        transaction_type="refund_succeeded",
        payment_intent=refund.payment_intent,
        refund=refund,
        request_id=request_id,
    )
    LedgerEntry.objects.create(transaction=tx, account=clearing, direction=LedgerEntry.DIRECTION_DEBIT, amount=refund.amount, currency=refund.currency)
    LedgerEntry.objects.create(transaction=tx, account=cash, direction=LedgerEntry.DIRECTION_CREDIT, amount=refund.amount, currency=refund.currency)
    return tx


def transaction_balances(tx):
    debit = sum(entry.amount for entry in tx.entries.filter(direction=LedgerEntry.DIRECTION_DEBIT))
    credit = sum(entry.amount for entry in tx.entries.filter(direction=LedgerEntry.DIRECTION_CREDIT))
    return debit, credit
