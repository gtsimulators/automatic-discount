from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import re
import os
import smtplib
from email.message import EmailMessage

app = Flask(__name__)
CORS(app)

# ✅ Store settings
SHOP_NAME = "gtsimulators-by-global-technologies.myshopify.com"
ACCESS_TOKEN = os.getenv("SHOPIFY_TOKEN")
API_VERSION = "2024-01"
ALERT_EMAIL = "fp@gtsimulators.com"
ALERT_PASSWORD = os.getenv("PASS")  # Gmail App Password

# ✅ Alert function
def send_alert_email(subject, body):
    msg = EmailMessage()
    msg.set_content(body)
    msg["Subject"] = subject
    msg["From"] = ALERT_EMAIL
    msg["To"] = ALERT_EMAIL

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(ALERT_EMAIL, ALERT_PASSWORD)
            smtp.send_message(msg)
            print("📧 Alert email sent.")
    except Exception as e:
        print(f"❌ Failed to send alert email: {e}")

# ✅ Discount lookup from tags
def get_discount_from_tags(product_id):
    headers = {"X-Shopify-Access-Token": ACCESS_TOKEN}
    url = f"https://{SHOP_NAME}/admin/api/{API_VERSION}/products/{product_id}.json"
    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        return 0.0

    product = response.json().get("product", {})
    tags = [t.strip() for t in product.get("tags", "").split(",")]

    for tag in tags:
        match = re.search(r"(\d+(\.\d+)?)%", tag)
        if match:
            return float(match.group(1))

    return 0.0

# ✅ Create draft order
@app.route("/create-draft", methods=["POST"])
def create_draft_order():
    cart_data = request.get_json()
    line_items = []

    for item in cart_data.get("items", []):
        product_id = item["product_id"]
        price = item["price"]
        variant_id = item["variant_id"]
        quantity = item["quantity"]

        discount_percent = get_discount_from_tags(product_id)

        # 🧠 Debug info
        print(f"\n---")
        print(f"Product ID: {product_id}")
        print(f"Discount percent (from tag): {discount_percent}%")

        discount_amount = round(price * (discount_percent / 100), 2)
        if price - discount_amount < 0:
            discount_amount = price - 0.01

        line_items.append({
            "variant_id": variant_id,
            "quantity": quantity,
            "applied_discount": {
                "description": "SAVING",
                "value_type": "fixed_amount",
                "value": f"{discount_amount:.2f}",
                "amount": f"{discount_amount:.2f}"
            }
        })

    payload = {
        "draft_order": {
            "line_items": line_items,
            "use_customer_default_address": True,
            "note": "Created via custom discount app"
        }
    }

    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": ACCESS_TOKEN
    }

    url = f"https://{SHOP_NAME}/admin/api/{API_VERSION}/draft_orders.json"

    # 🔔 TEST ALERT
    send_alert_email("⚠️ Test Alert Triggered", "This is a test alert from your Flask app.")
    return jsonify({"error": "Forced test failure"}), 500

    # If you want to restore functionality after testing, just remove the two lines above and uncomment below:
    # response = requests.post(url, headers=headers, json=payload)
    # if response.status_code == 201:
    #     draft = response.json()["draft_order"]
    #     return jsonify({"checkout_url": draft["invoice_url"]})
    # else:
    #     send_alert_email(
    #         "⚠️ Draft Order Failed",
    #         f"Response: {response.status_code}\nDetails: {response.text}"
    #     )
    #     return jsonify({
    #         "error": "Failed to create draft order",
    #         "details": response.json()
    #     }), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
