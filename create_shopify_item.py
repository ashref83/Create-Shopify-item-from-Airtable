import os
import requests
from flask import Blueprint, request, jsonify
from airtable import Airtable

create_shopify_bp = Blueprint("create_shopify_bp", __name__)

# ---------------------------
# CONFIGURATION (Using Env Vars)
# ---------------------------
SHOP = os.environ["SHOPIFY_SHOP"]                     # e.g., "yourstore.myshopify.com"
TOKEN = os.environ["SHOPIFY_API_TOKEN"]               # your Admin API access token
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-01")

AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]
AIRTABLE_API_KEY = os.environ["AIRTABLE_API_KEY"]
AIRTABLE_TABLE_NAME = "French Inventories"

# Airtable SDK setup
airtable = Airtable(AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME, AIRTABLE_API_KEY)

# ---------------------------
# FLASK ROUTE
# ---------------------------
@create_shopify_bp.route("/create-shopify-item", methods=["POST"])
def create_shopify_item():
    data = request.get_json(force=True)
    record_id = data.get("record_id")
    record = data.get("fields", {})

    if not record_id or not record:
        return jsonify({"error": "Missing record_id or fields"}), 400

    # 🧩 Mapping from Airtable → Shopify (based on your doc)
    product_data = {
        "product": {
            "title": record.get("Product Name", ""),
            "body_html": record.get("ShopifyDesc", ""),
            "vendor": record.get("Brand", ""),
            "product_type": record.get("Type", ""),
            "tags": record.get("Category", ""),
            "variants": [
                {
                    "sku": record.get("SKU", ""),
                    "price": record.get("UAE Price", 0),
                    "barcode": record.get("Barcode", ""),
                    "inventory_quantity": record.get("Qty given in shopify", 0),
                    "weight": 850,
                    "weight_unit": "g"
                }
            ],
            "options": [
                {"name": "Size", "values": [record.get("Size", "")]},
                {"name": "Scent", "values": [record.get("Category", "")]},
                {"name": "Target Gender", "values": [record.get("Category", "")]},
            ],
            "images": [{"src": img.get("url") if isinstance(img, dict) else img}
                       for img in record.get("Image URLs", []) if img]
        }
    }

    # 🛍️ Create Product in Shopify
    try:
        resp = requests.post(
            f"https://{SHOP}/admin/api/{API_VERSION}/products.json",
            headers={
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": TOKEN
            },
            json=product_data
        )

        result = resp.json()
    except Exception as e:
        return jsonify({"error": f"Shopify API request failed: {e}"}), 500

    if "product" not in result:
        return jsonify({"error": "Failed to create product", "details": result}), 400

    # ✅ Update Airtable
    shopify_id = result["product"]["id"]
    airtable.update(record_id, {"ShopifyID": str(shopify_id), "Create in Shopify": False})

    return jsonify({"success": True, "shopify_id": shopify_id})
