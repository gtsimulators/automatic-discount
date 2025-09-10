import os
import certifi
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import re
import json
import smtplib
from datetime import datetime
from email.message import EmailMessage
import base64
import traceback
from pprint import pprint


# Determine which CA bundle to use: environment override or system default
_default_ca = os.getenv("REQUESTS_CA_BUNDLE", None)
if _default_ca and os.path.exists(_default_ca):
    CA_BUNDLE = _default_ca
else:
    CA_BUNDLE = certifi.where()

print("🔒 Using CA bundle:", CA_BUNDLE, "exists?", os.path.exists(CA_BUNDLE), flush=True)


app = Flask(__name__)
CORS(app)

# ✅ Store settings
SHOP_NAME      = "gtsimulators-by-global-technologies.myshopify.com"
ACCESS_TOKEN   = os.getenv("SHOPIFY_TOKEN")
RECAPTCHA_SECRET = os.getenv("RECAPTCHA_SECRET")
API_VERSION    = "2025-01"
ALERT_EMAIL    = "fp@gtsimulators.com"
SENDER_EMAIL   = "nandobentzen@gmail.com"
ALERT_PASSWORD = os.getenv("PASS")
ZAPIER_WEBHOOK = os.getenv("ZAPIER_WEBHOOK")


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


def log_captcha_v2(result: dict) -> None:
    """
    For reCAPTCHA v2 responses.
    Writes one concise line to Render logs, e.g.
    [captcha] 2025-06-18 22:07:49  ✅ PASS  host=discount.gtsimulators.com
    """
    stamp   = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    status  = "✅ PASS" if result.get("success") else "❌ FAIL"
    host    = result.get("hostname", "-")
    print(f"[captcha] {stamp}  {status}  host={host}", flush=True)


def get_discount_from_tags(product_id):
    headers = {"X-Shopify-Access-Token": ACCESS_TOKEN}
    url     = f"https://{SHOP_NAME}/admin/api/{API_VERSION}/products/{product_id}.json"
    resp    = requests.get(url, headers=headers, verify=CA_BUNDLE)
    if resp.status_code != 200:
        return 0.0
    tags = resp.json().get("product", {}).get("tags", "")
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
    payload = {"query": query, "variables": {"sku": f'sku:"{sku}"'}}
    resp = requests.post(endpoint, json=payload, headers=headers, verify=CA_BUNDLE)
    if resp.status_code != 200:
        print(f"⚠️ GraphQL error for SKU {sku}: {resp.status_code}", flush=True)
        return None
    edges = (
        resp.json()
        .get("data", {})
        .get("productVariants", {})
        .get("edges", [])
    )
    if not edges:
        return None
    node = edges[0]["node"]
    if node.get("sku", "").upper() != sku.upper():
        return None
    gid = node["id"]  # e.g. "gid://shopify/ProductVariant/1234567890"
    vid = int(gid.rsplit("/", 1)[-1])
    return {"id": vid, "price": float(node["price"])}





# ── REPLACE ONLY THIS ROUTE ───────────────────────────────────────────
@app.route("/submit-quote", methods=["POST"])
def submit_quote():
    """
    Handles multipart quote requests + optional file uploads.
    Uploads go through Shopify staged-upload -> Google Cloud.
    For FILE resources we treat staged `resourceUrl` as the final
    public link (Shopify 2023-10 change).
    """
    try:
        print("\n=== /submit-quote ==========================================")
        # ---------- 1. payload & files ----------
        if request.content_type.startswith("multipart/form-data"):
            payload_raw   = request.form.get("payload", "{}")
            data          = json.loads(payload_raw or "{}")
            uploaded      = request.files.getlist("files")
        else:
            data, uploaded = request.get_json() or {}, []

        print("[dbg] keys:", list(data.keys()))
        print("[dbg] files:", [f.filename for f in uploaded])

        # ---------- 2. reCAPTCHA ----------
        token = data.get("recaptcha_token")
        if not token:
            return jsonify({"error": "missing recaptcha_token"}), 400

        rc = requests.post(
            "https://www.google.com/recaptcha/api/siteverify",
            data={"secret": RECAPTCHA_SECRET, "response": token},
            verify=CA_BUNDLE
        ).json()

        log_captcha_v2(rc)               # ← ADD THIS LINE

        if not rc.get("success"):
            # (optional) e-mail yourself on failure
            send_alert_email("⚠️ Bad reCAPTCHA", json.dumps(rc, indent=2))
            return jsonify({"error": "recaptcha failed"}), 400


        file_urls = []
        if uploaded:
            gql = f"https://{SHOP_NAME}/admin/api/{API_VERSION}/graphql.json"
            hdr = {
                "X-Shopify-Access-Token": ACCESS_TOKEN,
                "Content-Type": "application/json"
            }

            # ---------- 3. stagedUploadsCreate ----------
            inputs = [{
                "filename"  : f.filename,
                "mimeType"  : f.content_type,
                "fileSize"  : str(f.seek(0, 2) or f.tell()),  # str!
                "httpMethod": "POST",
                "resource"  : "FILE"
            } for f in uploaded]
            for f in uploaded: f.seek(0)

            staged_q = """
              mutation($input:[StagedUploadInput!]!){
                stagedUploadsCreate(input:$input){
                  stagedTargets{ url resourceUrl parameters{ name value } }
                  userErrors{ field message }
                }
              }"""
            sj = requests.post(gql, headers=hdr,
                               json={"query": staged_q,
                                     "variables":{"input":inputs}},
                               verify=CA_BUNDLE).json()
            if sj.get("errors"):
                print("[dbg] staged transport errors:", sj["errors"])
                return jsonify({"error":"stagedUploadsCreate transport"}), 500
            ue = sj["data"]["stagedUploadsCreate"]["userErrors"]
            if ue:
                print("[dbg] staged userErrors:", ue)
                return jsonify({"error":"stagedUploadsCreate userErrors"}), 500
            targets = sj["data"]["stagedUploadsCreate"]["stagedTargets"]

            # ---------- 4. POST each file ----------
            for f, tgt in zip(uploaded, targets):
                fields = {p["name"]: p["value"] for p in tgt["parameters"]}
                resp_up = requests.post(
                    tgt["url"],
                    data=fields,
                    files={"file": (f.filename, f, f.content_type)}
                )
                print(f"[dbg] upload {f.filename} → {resp_up.status_code}")
                resp_up.raise_for_status()

                # ---------- 5. fileCreate ----------
                fc_q = """
                  mutation($files:[FileCreateInput!]!){
                    fileCreate(files:$files){
                      files{ id fileStatus }
                      userErrors{ field message }
                    }
                  }"""
                fc_vars = {"files":[{
                    "originalSource": tgt["resourceUrl"],
                    "contentType"   : "FILE"
                }]}
                fc = requests.post(gql, headers=hdr,
                                   json={"query":fc_q,"variables":fc_vars},
                                   verify=CA_BUNDLE).json()
                if fc.get("errors") or fc["data"]["fileCreate"]["userErrors"]:
                    print("[dbg] fileCreate issue:", fc)
                    return jsonify({"error":"fileCreate failed"}), 500

                # Use staged resourceUrl as the public link
                file_urls.append(tgt["resourceUrl"])

        # ---------- 6. Zapier ----------
        zap_payload = {
            "product_list" : data.get("product_list", []),
            "customer_info": data.get("customer_info", []),
            "created_at"   : datetime.utcnow().isoformat(),
            "file_urls"    : file_urls
        }
        zr = requests.post(ZAPIER_WEBHOOK, json=zap_payload,
                           headers={"Content-Type":"application/json"},
                           verify=CA_BUNDLE)
        print("[dbg] zapier status:", zr.status_code)
        zr.raise_for_status()

        print("[dbg] done OK; urls:", file_urls)
        return jsonify({"success":True}), 200

    except Exception as exc:
        print("❌ fatal:", exc)
        traceback.print_exc()
        return jsonify({"error":"Internal server error"}), 500
# ───────────────────────────────────────────────────────────────────────







@app.route("/create-draft", methods=["POST"])
def create_draft_order():
    data  = request.get_json() or {}                       # NEW
    items = data.get("items", [])                          # CHANGED (from request.get_json()...)
    attrs = data.get("attributes", {}) or {}               # NEW

    line_items = []
    for i in items:
        pid  = i["product_id"]
        price = float(i["price"])
        vid  = i["variant_id"]
        qty  = i["quantity"]
        pct  = get_discount_from_tags(pid)
        amt  = round(price * pct / 100, 2)
        if price - amt < 0:
            amt = price - 0.01
        line_items.append(
            {
                "variant_id": vid,
                "quantity": qty,
                "applied_discount": {
                    "description": "GT DISCOUNT",
                    "value_type": "fixed_amount",
                    "value": f"{amt:.2f}",
                    "amount": f"{amt:.2f}",
                },
            }
        )

    # NEW: convert to REST note_attributes
    note_attributes = [
        {"name": str(k), "value": "" if v is None else str(v)}
        for k, v in attrs.items() if str(v) != ""
    ] if attrs else []

    payload = {
        "draft_order": {
            "line_items": line_items,
            "use_customer_default_address": True,
            "note": "",
            **({"note_attributes": note_attributes} if note_attributes else {})  # NEW
        }
    }

    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": ACCESS_TOKEN,
    }
    resp = requests.post(
        f"https://{SHOP_NAME}/admin/api/{API_VERSION}/draft_orders.json",
        headers=headers,
        json=payload,
        verify=CA_BUNDLE,
    )

    if 200 <= resp.status_code < 300:
        invoice_url = resp.json().get("draft_order", {}).get("invoice_url")
        return jsonify({"checkout_url": invoice_url}), 200

    send_alert_email("⚠️ Website Draft Order Failed", f"{resp.status_code} {resp.text}")
    return jsonify({"error": "Failed", "details": resp.text}), 500




# ───────────────────────────────────────────────────────────────────────


@app.route("/create-draft-from-method", methods=["POST"])
def create_draft_from_method():
    data        = request.get_json()
    items       = data.get("product_list", [])
    quote_info  = data.get("quote_info", [])

    if not items:
        return jsonify({"error": "No items received"}), 400

    quote_number = None
    quote_tax    = None  # value from tax_info
    if quote_info and isinstance(quote_info, list):
        for q in quote_info:
            if "quote_number" in q:
                quote_number = q["quote_number"]
            if "tax_info" in q:
                quote_tax = float(str(q["tax_info"]).replace(",", ""))

    line_items            = []
    shipping_line         = None
    order_discount_total  = 0.0
    tax_exempt            = False    # final flag applied to draft
    any_st_seen           = False    # track if ANY ST (ignored or not) appeared
    any_variant_matched   = False    # track if any SKU matched a Shopify variant

    ignored_st = {"STCA", "STIN", "STNY", "STPA", "STTX", "STWA"}

    for it in items:
        sku_raw = it.get("sku", "").strip()
        sku     = sku_raw
        qty     = int(it.get("qty", 1))
        disc    = float(it.get("disc", "0").replace(",", ""))

        sku_upper = sku_raw.upper()

        # Skip subtotal rows
        if sku_upper in ("SUBTOTAL", "SUB-TOTAL"):
            continue

        # Shipping line
        if sku_upper.startswith("S&H") and quote_number:
            shipping_line = {
                "title": f"QUOTE # {quote_number}",
                "custom": True,
                "price": f"{disc:.2f}",
            }
            continue

        # Negative price → order-level discount
        if disc < 0:
            order_discount_total += abs(disc)
            continue

        # ST-prefixed logic
        if sku_upper.startswith("ST"):
            any_st_seen = True
            if sku_upper in ignored_st:
                continue  # ignored state codes – taxable
            tax_exempt = True   # other ST codes → non-taxable order
            line_items.append(
                {
                    "title":    "SALES TAX",
                    "price":    f"{disc:.2f}",
                    "quantity": qty,
                    "custom":   True,
                    "taxable":  False,
                }
            )
            continue

        # Blank SKU with zero price → ignore
        if not sku and disc == 0:
            continue

        info = fetch_variant_info(sku)

        # Unrecognized SKU → custom item
        if not info:
            custom_item = {
                "title":    sku or "Custom Item",
                "price":    f"{disc:.2f}",
                "quantity": qty,
                "custom":   True,
            }
            if not any_variant_matched:
                custom_item["requires_shipping"] = True
            line_items.append(custom_item)
            continue



        # If we got here, the variant exists — but switch to CUSTOM if we must charge above base or base is $0
        shop_price   = float(info["price"])
        method_price = float(disc)

        if shop_price <= 0 or method_price > shop_price:
            # Use a custom item at the Method (estimate) price
            custom_item = {
                "title":    sku or "Custom Item",
                "price":    f"{method_price:.2f}",
                "quantity": qty,
                "custom":   True,
            }
            if not any_variant_matched:
                custom_item["requires_shipping"] = True
            line_items.append(custom_item)
            continue

        # proceed with variant (equal or lower than base → discount if needed)
        any_variant_matched = True
        base_price      = shop_price
        discount_amount = round(base_price - method_price, 2)
        if discount_amount < 0:
            discount_amount = 0.0

        line_items.append(
            {
                "variant_id": info["id"],
                "quantity":   qty,
                "price":      f"{base_price:.2f}",
                **( {
                    "applied_discount": {
                        "description": "GT DISCOUNT",
                        "value_type":  "fixed_amount",
                        "value":       f"{discount_amount:.2f}",
                        "amount":      f"{discount_amount:.2f}",
                    }
                } if discount_amount > 0 else {} ),
            }
        )



    # Apply tax_info fallback only if NO ST of any kind present
    if (not any_st_seen) and (quote_tax is not None):
        if quote_tax == 0:
            tax_exempt = True
        # quote_tax > 0 keeps tax_exempt False

    if not line_items and not shipping_line:
        return jsonify({"error": "No valid variants found"}), 400

    draft_body = {
        "line_items":                    line_items,
        "use_customer_default_address": True,
    }
    if shipping_line:
        draft_body["shipping_line"] = shipping_line
    if order_discount_total > 0:
        draft_body["applied_discount"] = {
            "description": "GT Discount",
            "value_type":  "fixed_amount",
            "value":       f"{order_discount_total:.2f}",
            "amount":      f"{order_discount_total:.2f}",
        }
    if tax_exempt:
        draft_body["tax_exempt"] = True

    payload = {"draft_order": draft_body}
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": ACCESS_TOKEN,
    }
    resp = requests.post(
        f"https://{SHOP_NAME}/admin/api/{API_VERSION}/draft_orders.json",
        headers=headers,
        json=payload,
        verify=CA_BUNDLE,
    )

    if 200 <= resp.status_code < 300:
        invoice_url = resp.json().get("draft_order", {}).get("invoice_url")
        return jsonify({"checkout_url": invoice_url}), 200

    send_alert_email("⚠️ Method Draft Failed", f"{resp.status_code} {resp.text}")
    return jsonify({"error": "Failed", "details": resp.text}), 500


@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
