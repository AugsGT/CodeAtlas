def generate_report(totals):
    total = sum(totals)
    return format_report(total)


def format_report(total):
    return f"Sales report: total={total}"
