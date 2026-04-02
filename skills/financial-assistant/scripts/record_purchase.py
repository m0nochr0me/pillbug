import csv
import json
import os
import sys
import urllib.request
from datetime import datetime


def get_exchange_rate(from_currency):
    if from_currency == "USD":
        return 1.0

    url = f"https://api.frankfurter.app/latest?from=USD&to={from_currency}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read())
            # This gives us how many units of `from_currency` make 1 USD.
            # So 1 unit of `from_currency` = 1 / rate USD.
            rate = data["rates"][from_currency]
            return 1.0 / rate
    except Exception as e:
        print(f"Error fetching exchange rate: {e}. Using fallback rate for PHP.", file=sys.stderr)
        if from_currency == "PHP":
            return 1.0 / 56.0
        sys.exit(1)


def main():
    if len(sys.argv) < 4:
        print("Usage: python3 record_purchase.py <amount> <currency> <description>")
        sys.exit(1)

    amount = float(sys.argv[1])
    currency = sys.argv[2].upper()
    description = sys.argv[3]

    # Convert to USD if not already
    if currency != "USD":
        rate = get_exchange_rate(currency)
        amount_usd = amount * rate
    else:
        amount_usd = amount

    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M")

    # Append to CSV
    csv_path = os.path.join(os.environ.get("WORKSPACE_DIR", "."), "purchases.csv")
    file_exists = os.path.isfile(csv_path)

    with open(csv_path, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "amount_usd", "description"])
        writer.writerow([timestamp, f"{amount_usd:.2f}", description])

    print(f"Successfully recorded purchase: {timestamp}, {amount_usd:.2f} USD, {description}")


if __name__ == "__main__":
    main()
