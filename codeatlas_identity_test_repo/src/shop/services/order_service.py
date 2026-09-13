class OrderService:
    def __init__(self, items):
        self.items = items

    def calculate_total(self):
        total = 0
        for item in self.items:
            total += self.item_price(item)
        return total

    def item_price(self, item):
        return item.get("price", 0)
