import os
import certifi
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import re
import smtplib
from email.message import EmailMessage

# Determine which CA bundle to use
CA_BUNDLE = os.getenv("REQUESTS_CA_BUNDLE", "/etc/ssl/certs/ca-certificates.crt")
print("🔒 Using CA bundle:", CA_BUNDLE, "exists?", os.path.exists(CA_BUNDLE), flush=True)

app = Flask(__name__)
CORS(app)

# ✅ Store settings
SHOP_NAME      = "gtsimulators-by-global-technologies.myshopify.com"
ACCESS_TOKEN   = os.getenv("SHOPIFY_TOKEN")
API_VERSION    = "2024-01"
ALERT_EMAIL    = "fp@gtsimulators.com"
SENDER_EMAIL   = "nandobentzen@gmail.com"
ALERT_PASSWORD = os.getenv("PASS")

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

def get_discount_from_tags(product_id):
    headers  = {"X-Shopify-Access-Token": ACCESS_TOKEN}
    url      = f"https://{SHOP_NAME}/admin/api/{API_VERSION}/products/{product_id}.json"
    resp     = requests.get(url, headers=headers, verify=CA_BUNDLE)
    if resp.status_code != 200:
        return 0.0
    tags = resp.json().get("product", {}).get("tags", "")
    for t in tags.split(","):
        m = re.search(r"(\d+(\.\d+)?)%", t.strip())
        if m:
            return float(m.group(1))
    return 0.0

def lookup_variant_id(sku: str) -> int | None:
    endpoint = f"https://{SHOP_NAME}/admin/api/{API_VERSION}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": ACCESS_TOKEN,
        "Content-Type": "application/json"
    }
    query = """
    query findVariantBySku($sku: String!) {
      productVariants(first: 1, query: $sku) {
        edges {
          node { id sku }
        }
      }
    }
    """
    payload = {"query": query, "variables": {"sku": f"sku:{sku}"}}
    resp = requests.post(endpoint, json=payload, headers=headers, verify=CA_BUNDLE)
    if resp.status_code != 200:
        return None
    edges = resp.json().get("data", {}) \
                    .get("productVariants", {}) \
                    .get("edges", [])
    if not edges:
        return None
    node = edges[0]["node"]
    if node.get("sku","").upper() != sku.upper():
        return None
    gid = node["id"]  # "gid://shopify/ProductVariant/123123"
    return int(gid.split("/")[-1])

@app.route("/create-draft", methods=["POST"])
def create_draft_order():
    cart = request.get_json().get("items", [])
    line = []
    for i in cart:
        pid      = i["product_id"]
        price    = i["price"]
        vid      = i["variant_id"]
        qty      = i["quantity"]
        disc_pct = get_discount_from_tags(pid)
        disc_amt = round(price * disc_pct / 100, 2)
        if price - disc_amt < 0:
            disc_amt = price - 0.01
        line.append({
            "variant_id": vid,
            "quantity":   qty,
            "applied_discount": {
                "description":"GT DISCOUNT",
                "value_type": "fixed_amount",
                "value":      f"{disc_amt:.2f}",
                "amount":     f"{disc_amt:.2f}"
            }
        })
    payload = {"draft_order":{
        "line_items":                    line,
        "use_customer_default_address": True,
        "note":                         ""
    }}
    headers = {
        "Content-Type":           "application/json",
        "X-Shopify-Access-Token": ACCESS_TOKEN
    }
    resp = requests.post(
        f"https://{SHOP_NAME}/admin/api/{API_VERSION}/draft_orders.json",
        headers=headers, json=payload, verify=CA_BUNDLE
    )
    if resp.status_code == 201:
        return jsonify({"checkout_url": resp.json()["draft_order"]["invoice_url"]})
    send_alert_email("⚠️ Draft Order Failed", f"{resp.status_code} {resp.text}")
    return jsonify({"error":"Failed to create draft","details":resp.text}), 500

@app.route("/create-draft-from-method", methods=["POST"])
def create_draft_from_method():
    items = request.get_json().get("product_list", [])
    if not items:
        return jsonify({"error":"No items received"}), 400

    line = []
    for it in items:
        sku       = it.get("sku","").strip()
        list_price= float(it.get("list",0))
        disc_price= float(it.get("disc",0))
        qty       = int(it.get("qty",1))
        vid       = lookup_variant_id(sku)
        if not vid:
            print(f"⚠️ SKU {sku} not found, skipping", flush=True)
            continue

        # —— HERE: compute discount_amount = list_price - disc_price
        discount_amount = round(list_price - disc_price, 2)
        if discount_amount < 0:
            discount_amount = 0.0

        line.append({
            "variant_id": vid,
            "quantity":   qty,
            "price":      list_price,      # start from your list price
            "applied_discount": {
                "description":"GT DISCOUNT",
                "value_type": "fixed_amount",
                "value":      f"{discount_amount:.2f}",
                "amount":     f"{discount_amount:.2f}"
            }
        })

    if not line:
        return jsonify({"error":"No valid variants found"}), 400

    payload = {"draft_order":{
        "line_items":                    line,
        "use_customer_default_address": True
    }}
    headers = {
        "Content-Type":           "application/json",
        "X-Shopify-Access-Token": ACCESS_TOKEN
    }
    resp = requests.post(
        f"https://{SHOP_NAME}/admin/api/{API_VERSION}/draft_orders.json",
        headers=headers, json=payload, verify=CA_BUNDLE
    )
    if resp.status_code == 201:
        return jsonify({"checkout_url": resp.json()["draft_order"]["invoice_url"]})
    send_alert_email("⚠️ Method Draft Failed", resp.text)
    return jsonify({"error":"Failed to create draft","details":resp.text}), 500

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
