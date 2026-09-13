from src.shop.reports.sales_report import generate_report
from src.shop.services.order_service import OrderService
from src.shop.services.payment_service import PaymentService


def main():
    order_service = OrderService([{"price": 10}, {"price": 20}, {"price": 30}])
    total = order_service.calculate_total()

    payment_service = PaymentService()
    payment_service.process_payment(total)

    report = generate_report([total])
    print(report)


if __name__ == "__main__":
    main()
