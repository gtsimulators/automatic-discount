import os
import certifi
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import re
import smtplib
from email.message import EmailMessage

# Determine which CA bundle to use: environment override or system default
CA_BUNDLE = os.getenv("REQUESTS_CA_BUNDLE", "/etc/ssl/certs/ca-certificates.crt")

# Debug: confirm the bundle exists on startup
print("🔒 Using CA bundle:", CA_BUNDLE, "exists?", os.path.exists(CA_BUNDLE), flush=True)

app = Flask(__name__)
CORS(app)

# ✅ Store settings
SHOP_NAME     = "gtsimulators-by-global-technologies.myshopify.com"
ACCESS_TOKEN  = os.getenv("SHOPIFY_TOKEN")
API_VERSION   = "2024-01"
ALERT_EMAIL   = "fp@gtsimulators.com"         # Receiver
SENDER_EMAIL  = "nandobentzen@gmail.com"      # Gmail used to send
ALERT_PASSWORD= os.getenv("PASS")             # Gmail App Password

# ✅ Alert function
def send_alert_email(subject, body):
    msg = EmailMessage()
    msg.set_content(body)
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = ALERT_EMAIL

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(SENDER_EMAIL, ALERT_PASSWORD)
            smtp.send_message(msg)
            print("📧 Alert email sent.", flush=True)
    except Exception as e:
        print(f"❌ Failed to send alert email: {e}", flush=True)

# ✅ Discount lookup from tags
def get_discount_from_tags(product_id):
    headers = {"X-Shopify-Access-Token": ACCESS_TOKEN}
    url     = f"https://{SHOP_NAME}/admin/api/{API_VERSION}/products/{product_id}.json"
    response= requests.get(url, headers=headers, verify=CA_BUNDLE)

    if response.status_code != 200:
        return 0.0

    product = response.json().get("product", {})
    tags    = [t.strip() for t in product.get("tags", "").split(",")]

    for tag in tags:
        match = re.search(r"(\d+(\.\d+)?)%", tag)
        if match:
            percent = float(match.group(1))
            print(f"✅ Found discount tag: {tag} => {percent}%", flush=True)
            return percent

    print("ℹ️ No discount tag found. Defaulting to 0%.", flush=True)
    return 0.0

# ─── SKU → variant_id lookup, filtered by exact SKU ─────────────────────────
def lookup_variant_id(sku: str) -> int | None:
    url     = f"https://{SHOP_NAME}/admin/api/{API_VERSION}/variants.json"
    headers = {"X-Shopify-Access-Token": ACCESS_TOKEN}
    resp    = requests.get(url, headers=headers, verify=CA_BUNDLE)
    if resp.status_code != 200:
        print(f"❌ Failed to fetch variants for SKU lookup: {resp.status_code}", flush=True)
        return None

    variants = resp.json().get("variants", [])
    for v in variants:
        if v.get("sku", "").strip().lower() == sku.strip().lower():
            return v["id"]

    print(f"⚠️ No matching variant found for SKU '{sku}'", flush=True)
    return None

# ✅ Create draft order
@app.route("/create-draft", methods=["POST"])
def create_draft_order():
    cart_data = request.get_json()
    line_items= []

    for item in cart_data.get("items", []):
        product_id      = item["product_id"]
        price           = item["price"]
        variant_id      = item["variant_id"]
        quantity        = item["quantity"]
        product_title   = item["title"]

        discount_percent= get_discount_from_tags(product_id)

        # 🧠 Debug info
        print(f"Product ID: {product_id}", flush=True)
        print(f"Product name: {product_title}", flush=True)

        discount_amount = round(price * (discount_percent / 100), 2)
        if price - discount_amount < 0:
            discount_amount = price - 0.01

        line_items.append({
            "variant_id": variant_id,
            "quantity": quantity,
            "applied_discount": {
                "description": "GT DISCOUNT",
                "value_type":   "fixed_amount",
                "value":        f"{discount_amount:.2f}",
                "amount":       f"{discount_amount:.2f}"
            }
        })

    payload = {
        "draft_order": {
            "line_items": line_items,
            "use_customer_default_address": True,
            "note": ""
        }
    }

    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": ACCESS_TOKEN
    }

    url      = f"https://{SHOP_NAME}/admin/api/{API_VERSION}/draft_orders.json"
    response = requests.post(url, headers=headers, json=payload, verify=CA_BUNDLE)

    if response.status_code == 201:
        draft = response.json()["draft_order"]
        return jsonify({"checkout_url": draft["invoice_url"]})
    else:
        send_alert_email(
            "⚠️ Draft Order Failed",
            f"Response: {response.status_code}\nDetails: {response.text}"
        )
        return jsonify({
            "error":   "Failed to create draft order",
            "details": response.json()
        }), 500

# ✅ Create draft order from Method (SKU, list, disc)
@app.route("/create-draft-from-method", methods=["POST"])
def create_draft_from_method():
    data  = request.get_json()
    items = data.get("product_list", [])
    if not items:
        return jsonify({"error": "No items received"}), 400

    line_items = []
    for item in items:
        sku        = item.get("sku", "")
        list_price = float(item.get("list", 0))
        discount   = float(item.get("disc", 0))
        qty        = int(item.get("qty", 1))

        variant_id = lookup_variant_id(sku)
        if not variant_id:
            continue

        line_items.append({
            "variant_id": variant_id,
            "quantity":   qty,
            "applied_discount": {
                "description": "GT Discount",
                "value_type":  "fixed_amount",
                "value":       f"{discount:.2f}",
                "amount":      f"{discount:.2f}"
            }
        })

    if not line_items:
        return jsonify({"error": "No valid variants found"}), 400

    payload = {
        "draft_order": {
            "line_items": line_items,
            "use_customer_default_address": True
        }
    }

    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": ACCESS_TOKEN
    }

    url      = f"https://{SHOP_NAME}/admin/api/{API_VERSION}/draft_orders.json"
    response = requests.post(url, headers=headers, json=payload, verify=CA_BUNDLE)

    if response.status_code == 201:
        draft = response.json()["draft_order"]
        return jsonify({"checkout_url": draft["invoice_url"]})
    else:
        send_alert_email("⚠️ Method Draft Failed", response.text)
        return jsonify({
            "error":   "Failed to create draft",
            "details": response.text
        }), 500

# ✅ Ping endpoint for availability check
@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
