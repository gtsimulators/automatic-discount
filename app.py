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
    except Exception as e:
        print(f"❌ Failed to send alert email: {e}", flush=True)

def get_discount_from_tags(product_id):
    headers = {"X-Shopify-Access-Token": ACCESS_TOKEN}
    url     = f"https://{SHOP_NAME}/admin/api/{API_VERSION}/products/{product_id}.json"
    resp    = requests.get(url, headers=headers, verify=CA_BUNDLE)
    if resp.status_code != 200:
        return 0.0
    tags = resp.json().get("product", {}).get("tags","")
    for t in tags.split(","):
        m = re.search(r"(\d+(\.\d+)?)%", t.strip())
        if m:
            return float(m.group(1))
    return 0.0

# — fetch full variant info via GraphQL — returns dict with id and price
def fetch_variant_info(sku: str):
    endpoint = f"https://{SHOP_NAME}/admin/api/{API_VERSION}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": ACCESS_TOKEN,
        "Content-Type": "application/json"
    }
    query = """
    query findVariant($sku: String!) {
      productVariants(first: 1, query: $sku) {
        edges {
          node {
            id
            sku
            price
          }
        }
      }
    }
    """
    payload = {"query": query, "variables": {"sku": f"sku:{sku}"}}
    resp = requests.post(endpoint, json=payload, headers=headers, verify=CA_BUNDLE)
    if resp.status_code != 200:
        print(f"⚠️ GraphQL error for SKU {sku}: {resp.status_code}", flush=True)
        return None
    edges = resp.json().get("data", {}) \
                   .get("productVariants", {}) \
                   .get("edges", [])
    if not edges:
        return None
    node = edges[0]["node"]
    if node.get("sku","").upper() != sku.upper():
        return None
    gid = node["id"]  # e.g. "gid://shopify/ProductVariant/1234567890"
    vid = int(gid.rsplit("/",1)[-1])
    return {
        "id":    vid,
        "price": float(node["price"])
    }

@app.route("/create-draft", methods=["POST"])
def create_draft_order():
    items = request.get_json().get("items", [])
    line_items = []
    for i in items:
        pid   = i["product_id"]
        price = float(i["price"])
        vid   = i["variant_id"]
        qty   = i["quantity"]
        pct   = get_discount_from_tags(pid)
        amt   = round(price * pct/100, 2)
        if price - amt < 0:
            amt = price - 0.01
        line_items.append({
            "variant_id": vid,
            "quantity":   qty,
            "applied_discount": {
                "description": "GT DISCOUNT",
                "value_type":  "fixed_amount",
                "value":       f"{amt:.2f}",
                "amount":      f"{amt:.2f}"
            }
        })

    payload = {"draft_order":{
        "line_items":                    line_items,
        "use_customer_default_address": True,
        "note":                          ""
    }}
    headers = {
        "Content-Type":           "application/json",
        "X-Shopify-Access-Token": ACCESS_TOKEN
    }
    resp = requests.post(
        f"https://{SHOP_NAME}/admin/api/{API_VERSION}/draft_orders.json",
        headers=headers, json=payload, verify=CA_BUNDLE
    )

    # ←— accept any 2xx and return the invoice_url
    if 200 <= resp.status_code < 300:
        invoice_url = resp.json().get("draft_order", {}).get("invoice_url")
        return jsonify({"checkout_url": invoice_url}), 200

    send_alert_email("⚠️ Draft Order Failed", f"{resp.status_code} {resp.text}")
    return jsonify({"error":"Failed","details":resp.text}), 500

@app.route("/create-draft-from-method", methods=["POST"])
def create_draft_from_method():
    data       = request.get_json()
    items      = data.get("product_list", [])
    quote_info = data.get("quote_info", [])

    if not items:
        return jsonify({"error":"No items received"}), 400

    quote_number = None
    if quote_info and isinstance(quote_info, list):
        quote_number = quote_info[0].get("quote_number")

    line_items    = []
    shipping_line = None

    for it in items:
        sku  = it.get("sku","").strip()
        qty  = int(it.get("qty",1))
        disc = float(it.get("disc","0").replace(",",""))

        if sku.upper() == "S&H - QUOTE" and quote_number:
            shipping_line = {
                "title":  f"QUOTE # {quote_number}",
                "custom": True,
                "price":  f"{disc:.2f}"
            }
            continue

        info = fetch_variant_info(sku)
        if not info:
            print(f"⚠️ SKU {sku} not found, skipping", flush=True)
            continue

        base_price      = info["price"]
        discount_amount = round(base_price - disc, 2)
        if discount_amount < 0:
            discount_amount = 0.0

        line_items.append({
            "variant_id":       info["id"],
            "quantity":         qty,
            "price":            f"{base_price:.2f}",
            "applied_discount": {
                "description": "GT DISCOUNT",
                "value_type":  "fixed_amount",
                "value":       f"{discount_amount:.2f}",
                "amount":      f"{discount_amount:.2f}"
            }
        })

    if not line_items and not shipping_line:
        return jsonify({"error":"No valid variants found"}), 400

    draft_body = {
        "line_items":                    line_items,
        "use_customer_default_address": True
    }
    if shipping_line:
        draft_body["shipping_line"] = shipping_line

    payload = {"draft_order": draft_body}
    headers = {
        "Content-Type":           "application/json",
        "X-Shopify-Access-Token": ACCESS_TOKEN
    }
    resp = requests.post(
        f"https://{SHOP_NAME}/admin/api/{API_VERSION}/draft_orders.json",
        headers=headers, json=payload, verify=CA_BUNDLE
    )

    # ←— accept any 2xx and return the invoice_url
    if 200 <= resp.status_code < 300:
        invoice_url = resp.json().get("draft_order", {}).get("invoice_url")
        return jsonify({"checkout_url": invoice_url}), 200

    send_alert_email("⚠️ Method Draft Failed", f"{resp.status_code} {resp.text}")
    return jsonify({"error":"Failed","details":resp.text}), 500

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
