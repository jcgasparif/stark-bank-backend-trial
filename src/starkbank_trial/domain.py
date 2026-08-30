from dataclasses import dataclass
from decimal import Decimal
from typing import Any

DESTINATION = {
    "bank_code": "20018183",
    "branch_code": "0001",
    "account_number": "6341320293482496",
    "name": "Stark Bank S.A.",
    "tax_id": "20.018.183/0001-80",
    "account_type": "payment",
}


@dataclass(frozen=True)
class Receipt:
    invoice_id: str
    amount: int
    fee: int

    @property
    def net_amount(self):
        value = self.amount - self.fee
        if value <= 0:
            raise ValueError("valor líquido deve ser positivo")
        return value


def _read(source: Any, *names):
    if isinstance(source, dict):
        return next((source[n] for n in names if n in source), None)
    return next((v for n in names if (v := getattr(source, n, None)) is not None), None)


def receipt_from(invoice_id, payment, fallback=None):
    source = payment or fallback or {}
    return Receipt(
        invoice_id,
        int(Decimal(str(_read(source, "amount") or 0))),
        int(Decimal(str(_read(source, "fee", "fees") or 0))),
    )
