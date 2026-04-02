---
name: financial-assistant
description: >
  Track personal expenses by recording purchases into a CSV ledger and providing
  spending insights. Normalizes all amounts to USD via live exchange rates (defaults
  to PHP when no currency is stated). Use when the user mentions: recording a purchase,
  logging an expense, processing a receipt, checking how much they've spent, asking
  for spending summaries or breakdowns, or any personal finance tracking task.
---

# Financial Assistant

This skill helps you record purchases and expenses into a CSV file (`purchases.csv`) in the workspace. It handles textual descriptions of purchases and can process receipt images.

## Core Workflow

When the user provides a description of a purchase or an image of a receipt:
1. Extract the total amount, the currency, and a short description of the purchase.
2. If the currency is not specified, assume it is **PHP** (Philippine Peso).
3. Use the provided script to record the purchase. The script will automatically convert the amount to USD using the current exchange rate from the Frankfurter API and append it to `purchases.csv`.

## Recording a Purchase

To record a purchase, execute the following script:

```bash
python3 skills/financial-assistant/scripts/record_purchase.py <amount> <currency> "<description>"
```

### Parameters:
- `<amount>`: The total amount of the purchase (e.g., 150.50).
- `<currency>`: The 3-letter currency code (e.g., PHP, USD, EUR). Default to PHP if not explicitly stated.
- `<description>`: A short, concise description of the purchase.

### Examples:

User: "I just bought a coffee for 150 pesos."
```bash
python3 skills/financial-assistant/scripts/record_purchase.py 150 PHP "Coffee"
```

User: "Paid $20 for a new domain name."
```bash
python3 skills/financial-assistant/scripts/record_purchase.py 20 USD "New domain name"
```

User: [Uploads an image of a grocery receipt showing a total of 1200 PHP]
```bash
python3 skills/financial-assistant/scripts/record_purchase.py 1200 PHP "Groceries"
```

## Providing Insights

When the user asks for insights, summaries, or analysis of their expenses:
1. Run the insights script to get a summary of all recorded purchases:
   ```bash
   python3 skills/financial-assistant/scripts/get_insights.py
   ```
2. Analyze the output based on the user's request (e.g., total spent, spending by category/description, trends over time).
3. Present the insights clearly and concisely to the user.
