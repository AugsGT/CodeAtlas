class PaymentService:
    def process_payment(self, amount):
        return self._charge(amount)

    def _charge(self, amount):
        return {"status": "ok", "amount": amount}
