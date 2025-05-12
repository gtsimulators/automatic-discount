import os
import certifi
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import re
import smtplib
from email.message import EmailMessage

# ─── Configuration ─────────────────────────────────────────────────────────────
CA_BUNDLE     = os.getenv("REQUESTS_CA_BUNDLE", certifi.where())
print("🔒 Using CA bundle:", CA_BUNDLE, "exists?", os.path.exists(CA_BUNDLE), flush=True)

SHOP_NAME     = "gtsimulators-by-global-technologies.myshopify.com"
API_VERSION   = "2024-01"
ACCESS_TOKEN  = os.getenv("SHOPIFY_TOKEN")
ALERT_EMAIL   = "fp@gtsimulators.com"    # Receiver
SENDER_EMAIL  = "nandobentzen@gmail.com" # Gmail used to send
ALERT_PASS    = os.getenv("PASS")

app = Flask(__name__)
CORS(app)


# ─── Helpers ────────────────────────────────────────────────────────────────────
def send_alert_email(subject: str, body: str):
    msg = EmailMessage()
    msg.set_content(body)
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = ALERT_EMAIL

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(SENDER_EMAIL, ALERT_PASS)
            smtp.send_message(msg)
            print("📧 Alert email sent.", flush=True)
    except Exception as e:
        print(f"❌ Failed to send alert email: {e}", flush=True)


def lookup_variant_id(sku: str) -> int | None:
    """
    Query Shopify for variants matching this SKU.
    Returns the first variant_id found, or None.
    """
    url     = f"https://{SHOP_NAME}/admin/api/{API_VERSION}/variants.json"
    headers = {"X-Shopify-Access-Token": ACCESS_TOKEN}
    params  = {"sku": sku}

    resp = requests.get(url, headers=headers, params=params, verify=CA_BUNDLE)
    if resp.status_code != 200:
        print(f"❌ Variant lookup failed for SKU {sku}: {resp.status_code} {resp.text}", flush=True)
        return None

    variants = resp.json().get("variants", [])
    if not variants:
        print(f"⚠️ No variant found for SKU {sku}", flush=True)
        return None

    return variants[0]["id"]


# ─── Endpoints ─────────────────────────────────────────────────────────────────
@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200


@app.route("/create-draft", methods=["POST"])
def create_draft_order():
    """
    Existing route: consumes Shopify cart.js payload
    """
    cart_data = request.get_json(force=True) or {}
    line_items = []

    for item in cart_data.get("items", []):
        pid   = item["product_id"]
        vid   = item["variant_id"]
        qty   = item["quantity"]
        price = item["price"]
        title = item["title"]

        # find discount% from product tags
        headers = {"X-Shopify-Access-Token": ACCESS_TOKEN}
        url     = f"https://{SHOP_NAME}/admin/api/{API_VERSION}/products/{pid}.json"
        resp    = requests.get(url, headers=headers, verify=CA_BUNDLE)
        percent = 0.0
        if resp.status_code == 200:
            tags = resp.json().get("product", {}).get("tags","").split(",")
            for t in tags:
                m = re.search(r"(\d+(\.\d+)?)%", t)
                if m:
                    percent = float(m.group(1))
                    break

        disc_amount = round(price * percent/100, 2)
        if price - disc_amount < 0:
            disc_amount = price - 0.01

        line_items.append({
            "variant_id": vid,
            "quantity":   qty,
            "applied_discount": {
                "description": "GT DISCOUNT",
                "value_type":  "fixed_amount",
                "value":       f"{disc_amount:.2f}",
                "amount":      f"{disc_amount:.2f}"
            }
        })

    draft_payload = {
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
    url = f"https://{SHOP_NAME}/admin/api/{API_VERSION}/draft_orders.json"
    resp = requests.post(url, headers=headers, json=draft_payload, verify=CA_BUNDLE)

    if resp.status_code == 201:
        invoice = resp.json()["draft_order"]["invoice_url"]
        return jsonify({"checkout_url": invoice})
    else:
        send_alert_email("⚠️ Draft Order Failed", f"{resp.status_code}\n{resp.text}")
        return jsonify({"error":"Failed to create draft","details":resp.json()}), 500


@app.route("/create-draft-from-method", methods=["POST"])
def create_draft_from_method():
    """
    Consumes MethodCRM JSON:
      { "product_list":[{"sku":"C18","list":"298.00","disc":"252.00","qty":"1"}, …] }
    Looks up each SKU → variant_id, then creates a Shopify Draft Order.
    """
    data  = request.get_json(force=True) or {}
    items = data.get("product_list", [])
    if not items:
        return jsonify({"error": "No product_list provided"}), 400

    line_items = []
    for entry in items:
        sku       = entry.get("sku", "").strip()
        qty       = int(entry.get("qty", 1))
        discount  = float(entry.get("disc", 0))
        variant_id = lookup_variant_id(sku)
        if not variant_id:
            # skip SKUs we can’t resolve
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
        return jsonify({"error":"No valid variants found"}), 400

    draft_payload = {
        "draft_order": {
            "line_items": line_items,
            "use_customer_default_address": True
        }
    }
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": ACCESS_TOKEN
    }
    url  = f"https://{SHOP_NAME}/admin/api/{API_VERSION}/draft_orders.json"
    resp = requests.post(url, headers=headers, json=draft_payload, verify=CA_BUNDLE)

    if resp.status_code == 201:
        invoice = resp.json()["draft_order"]["invoice_url"]
        return jsonify({"checkout_url": invoice})
    else:
        send_alert_email("⚠️ Method Draft Failed", f"{resp.status_code}\n{resp.text}")
        return jsonify({"error":"Failed to create draft","details":resp.json()}), 500


# ─── Run ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
