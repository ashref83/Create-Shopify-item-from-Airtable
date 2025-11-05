import os
import requests
from flask import Blueprint, request, jsonify
from airtable import Airtable

create_shopify_bp = Blueprint("create_shopify_bp", __name__)

# ---------------------------
# CONFIGURATION (Using Env Vars)
# ---------------------------
SHOP = os.environ["SHOPIFY_SHOP"]                     # e.g., "yourstore.myshopify.com"
TOKEN = os.environ["SHOPIFY_API_TOKEN"]               # Admin API token
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-01")

AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]
AIRTABLE_API_KEY = os.environ["AIRTABLE_API_KEY"]
AIRTABLE_TABLE_NAME = "French Inventories"

# Airtable setup
airtable = Airtable(AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME, AIRTABLE_API_KEY)

# ---------------------------
# HELPER FUNCTIONS
# ---------------------------

def _json_headers():
    return {"Content-Type": "application/json", "X-Shopify-Access-Token": TOKEN}


def _rest_url(path: str):
    return f"https://{SHOP}/admin/api/{API_VERSION}/{path}"


def _to_number(x):
    """Safely convert Airtable numeric fields to number"""
    try:
        if x is None or str(x).strip() == "":
            return 0
        return float(x)
    except Exception:
        return 0


# ---------------------------
# ROUTE: Create Shopify Product
# ---------------------------
@create_shopify_bp.route("/create-shopify-item", methods=["POST"])
def create_shopify_item():
    """
    Create a new Shopify product from Airtable record.
    Uses modern inventory API (two-step: product → inventory level).
    """
    try:
        data = request.get_json(force=True)
        record_id = data.get("record_id")
        record = data.get("fields", {})

        if not record_id or not record:
            return jsonify({"error": "Missing record_id or fields"}), 400

        # ---------------------------
        # 1️⃣ Prepare Product Data
        # ---------------------------
        product_data = {
            "product": {
                "title": record.get("Product Name", "").strip(),
                "body_html": record.get("ShopifyDesc", ""),
                "vendor": record.get("Brand", ""),
                "product_type": record.get("Type", ""),
                "tags": record.get("Category", ""),
                "status": "draft",  # or "active" if ready to publish
                "variants": [
                    {
                        "sku": record.get("SKU", ""),
                        "price": str(record.get("UAE Price", 0)),
                        "barcode": record.get("Barcode", ""),
                        "weight": _to_number(record.get("Weight", 850)),
                        "weight_unit": "g",
                        "inventory_management": "shopify",
                        "inventory_policy": "deny"
                    }
                ],
                "options": [{"name": "Title", "values": ["Default Title"]}],
                "images": []
            }
        }

        # Add image URLs if any
        image_urls = record.get("Image URLs", [])
        if image_urls and isinstance(image_urls, list):
            for img in image_urls:
                if isinstance(img, dict) and "url" in img:
                    product_data["product"]["images"].append({"src": img["url"]})
                elif isinstance(img, str) and img.strip():
                    product_data["product"]["images"].append({"src": img.strip()})

        # ---------------------------
        # 2️⃣ Create Product in Shopify
        # ---------------------------
        resp = requests.post(
            _rest_url("products.json"),
            headers=_json_headers(),
            json=product_data
        )

        if resp.status_code != 201:
            return jsonify({
                "error": "Shopify API error",
                "status": resp.status_code,
                "details": resp.text
            }), resp.status_code

        result = resp.json()
        product = result.get("product", {})
        shopify_product_id = product.get("id")

        if not shopify_product_id:
            return jsonify({"error": "Product creation failed", "details": result}), 400

        # ---------------------------
        # 3️⃣ Set Inventory via Inventory API
        # ---------------------------
        variant = product.get("variants", [{}])[0]
        inventory_item_id = variant.get("inventory_item_id")
        qty = _to_number(record.get("Qty given in shopify", 0))

        # Get primary location ID (once)
        loc_resp = requests.get(_rest_url("locations.json"), headers=_json_headers())
        loc_resp.raise_for_status()
        locations = loc_resp.json().get("locations", [])
        if not locations:
            return jsonify({"error": "No Shopify locations found"}), 400
        location_id = locations[0]["id"]

        # Set inventory level
        inv_payload = {
            "location_id": location_id,
            "inventory_item_id": inventory_item_id,
            "available": int(qty)
        }
        inv_resp = requests.post(
            _rest_url("inventory_levels/set.json"),
            headers=_json_headers(),
            json=inv_payload
        )
        inv_result = inv_resp.json()

        # ---------------------------
        # 4️⃣ Update Airtable
        # ---------------------------
        airtable.update(record_id, {
            "ShopifyID": str(shopify_product_id),
            "Create in Shopify": False
        })

        return jsonify({
            "success": True,
            "shopify_id": shopify_product_id,
            "inventory": inv_result
        }), 201

    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500
