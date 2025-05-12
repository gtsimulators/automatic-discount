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

print("🔒 Using CA bundle:", CA_BUNDLE, "exists?", os.path.exists(CA_BUNDLE), flush=True)

app = Flask(__name__)
CORS(app)

# ✅ Store settings
SHOP_NAME      = "gtsimulators-by-global-technologies.myshopify.com"
ACCESS_TOKEN   = os.getenv("SHOPIFY_TOKEN")
API_VERSION    = "2024-01"
ALERT_EMAIL    = "fp@gtsimulators.com"    # Receiver
SENDER_EMAIL   = "nandobentzen@gmail.com" # Gmail used to send
ALERT_PASSWORD = os.getenv("PASS")        # Gmail App Password

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

# ✅ Discount lookup from tags (unchanged)
def get_discount_from_tags(product_id):
    headers  = {"X-Shopify-Access-Token": ACCESS_TOKEN}
    url      = f"https://{SHOP_NAME}/admin/api/{API_VERSION}/products/{product_id}.json"
    response = requests.get(url, headers=headers, verify=CA_BUNDLE)
    if response.status_code != 200:
        return 0.0
    product = response.json().get("product", {})
    tags    = [t.strip() for t in product.get("tags","").split(",")]
    for tag in tags:
        match = re.search(r"(\d+(\.\d+)?)%", tag)
        if match:
            return float(match.group(1))
    return 0.0

# ——————————————————————————————————————————————————————
# ► GraphQL lookup: find a variant EXACTLY by SKU
def lookup_variant_id(sku: str) -> int | None:
    endpoint = f"https://{SHOP_NAME}/admin/api/{API_VERSION}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": ACCESS_TOKEN,
        "Content-Type": "application/json"
    }
    graphql_query = """
    query findVariantBySku($sku: String!) {
      productVariants(first: 1, query: $sku) {
        edges {
          node {
            id
            sku
          }
        }
      }
    }
    """
    payload = {
        "query": graphql_query,
        "variables": {"sku": f"sku:{sku}"}
    }
    resp = requests.post(endpoint, json=payload, headers=headers, verify=CA_BUNDLE)
    if resp.status_code != 200:
        print(f"⚠️ GraphQL error for SKU {sku}: {resp.status_code}", flush=True)
        return None

    data = resp.json().get("data", {})
    edges = data.get("productVariants", {}).get("edges", [])
    if not edges:
        return None

    variant = edges[0]["node"]
    if variant.get("sku", "").upper() != sku.upper():
        # not an exact match
        return None
    # GraphQL ID is a Base64 string; to use in REST draft_orders we need the numeric portion.
    # The ID looks like "gid://shopify/ProductVariant/1234567890"
    gid = variant["id"]
    return int(gid.split("/")[-1])

# ——————————————————————————————————————————————————————
# ✅ Create draft order (cart.js flow) — unchanged
@app.route("/create-draft", methods=["POST"])
def create_draft_order():
    cart_data = request.get_json()
    line_items = []
    for item in cart_data.get("items", []):
        pid    = item["product_id"]
        price  = item["price"]
        vid    = item["variant_id"]
        qty    = item["quantity"]
        discount = get_discount_from_tags(pid)
        discount_amount = round(price * discount / 100, 2)
        if price - discount_amount < 0:
            discount_amount = price - 0.01
        line_items.append({
            "variant_id": vid,
            "quantity":   qty,
            "applied_discount": {
                "description": "GT DISCOUNT",
                "value_type":  "fixed_amount",
                "value":       f"{discount_amount:.2f}",
                "amount":      f"{discount_amount:.2f}"
            }
        })
    payload = {"draft_order": {
        "line_items": line_items,
        "use_customer_default_address": True,
        "note": ""
    }}
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": ACCESS_TOKEN
    }
    url = f"https://{SHOP_NAME}/admin/api/{API_VERSION}/draft_orders.json"
    resp = requests.post(url, headers=headers, json=payload, verify=CA_BUNDLE)
    if resp.status_code == 201:
        return jsonify({"checkout_url": resp.json()["draft_order"]["invoice_url"]})
    send_alert_email("⚠️ Draft Order Failed", f"{resp.status_code} {resp.text}")
    return jsonify({"error":"Failed to create draft","details":resp.text}), 500

# ✅ Create draft order from Method (SKU, list, disc) using GraphQL lookup
@app.route("/create-draft-from-method", methods=["POST"])
def create_draft_from_method():
    data  = request.get_json()
    items = data.get("product_list", [])
    if not items:
        return jsonify({"error":"No items received"}), 400

    line_items = []
    for it in items:
        sku      = it.get("sku","").strip()
        qty      = int(it.get("qty",1))
        disc     = float(it.get("disc",0))
        vid = lookup_variant_id(sku)
        if not vid:
            print(f"⚠️ SKU {sku} not found or mismatch, skipping", flush=True)
            continue
        line_items.append({
            "variant_id": vid,
            "quantity":   qty,
            "applied_discount": {
                "description": "GT DISCOUNT",
                "value_type":  "fixed_amount",
                "value":       f"{disc:.2f}",
                "amount":      f"{disc:.2f}"
            }
        })

    if not line_items:
        return jsonify({"error":"No valid variants found"}), 400

    payload = {"draft_order": {
        "line_items": line_items,
        "use_customer_default_address": True
    }}
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": ACCESS_TOKEN
    }
    url = f"https://{SHOP_NAME}/admin/api/{API_VERSION}/draft_orders.json"
    resp = requests.post(url, headers=headers, json=payload, verify=CA_BUNDLE)
    if resp.status_code == 201:
        return jsonify({"checkout_url": resp.json()["draft_order"]["invoice_url"]})
    send_alert_email("⚠️ Method Draft Failed", resp.text)
    return jsonify({"error":"Failed to create draft","details":resp.text}), 500

# ✅ Ping endpoint
@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
