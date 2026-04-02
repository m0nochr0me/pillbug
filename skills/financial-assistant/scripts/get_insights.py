import csv
import os


def main():
    csv_path = os.path.join(os.environ.get("WORKSPACE_DIR", "."), "purchases.csv")
    if not os.path.isfile(csv_path):
        print("No purchases recorded yet.")
        return

    total_usd = 0.0
    transactions = []

    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                amount = float(row["amount_usd"])
                total_usd += amount
                transactions.append(row)
            except ValueError:
                continue

    print(f"Total Spent: ${total_usd:.2f} USD")
    print(f"Total Transactions: {len(transactions)}")
    print("-" * 50)
    for t in transactions:
        print(f"[{t['timestamp']}] ${t['amount_usd']} - {t['description']}")


if __name__ == "__main__":
    main()
