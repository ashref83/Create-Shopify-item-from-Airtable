import os
import requests
from flask import Blueprint, request, jsonify
from airtable import Airtable
import shopify
from typing import List, Dict, Optional

create_shopify_bp = Blueprint("create_shopify_bp", __name__)

# ---------------------------
# CONFIGURATION
# ---------------------------
SHOP = os.environ["SHOPIFY_SHOP"]                     # e.g. "fragrantsouq.myshopify.com"
TOKEN = os.environ["SHOPIFY_API_TOKEN"]
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-01")

AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]
AIRTABLE_API_KEY = os.environ["AIRTABLE_API_KEY"]
AIRTABLE_TABLE_NAME = "French Inventories"

# Airtable setup
airtable = Airtable(AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME, AIRTABLE_API_KEY)

# Shopify session setup
shopify.Session.setup(api_key="placeholder", secret="placeholder")
session = shopify.Session.temp(f"https://{SHOP}", API_VERSION, TOKEN)
shopify.ShopifyResource.activate_session(session)


# ---------------------------
# IMAGE SEARCHER CLASS
# ---------------------------
class ImageSearcher:
    """Class for searching images in Shopify store"""

    @staticmethod
    def search_by_product_name(
        product_name: str,
        limit: int = 10,
        exact_match: bool = False,
        cursor: Optional[str] = None
    ) -> Dict:
        """Search for images by product name using Shopify Files API"""
        if not product_name:
            return {"success": False, "error": "Empty product name", "images": []}

        if exact_match:
            search_pattern = f'"{product_name}"'
        else:
            words = product_name.lower().split()
            search_pattern = " OR ".join([f"{w}*" for w in words])

        after_param = f', after: "{cursor}"' if cursor else ""
        query = f"""
        query {{
          files(first: {limit}{after_param}, query: "filename:{search_pattern} AND media_type:IMAGE") {{
            edges {{
              node {{
                ... on MediaImage {{
                  id
                  alt
                  createdAt
                  updatedAt
                  image {{
                    id
                    url
                    width
                    height
                  }}
                }}
              }}
            }}
            pageInfo {{
              hasNextPage
              endCursor
            }}
          }}
        }}
        """

        try:
            gql = shopify.GraphQL()
            result = gql.execute(query)
            data = result["data"]["files"]
            images = [edge["node"] for edge in data["edges"]]
            return {"success": True, "images": images, "count": len(images)}
        except Exception as e:
            print(f"⚠️ Image search error: {e}", flush=True)
            return {"success": False, "error": str(e), "images": []}


# ---------------------------
# HELPER FUNCTIONS
# ---------------------------
def _json_headers():
    return {"Content-Type": "application/json", "X-Shopify-Access-Token": TOKEN}


def _rest_url(path: str):
    return f"https://{SHOP}/admin/api/{API_VERSION}/{path}"


def _graphql_url():
    return f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"


def _to_number(x):
    try:
        if x is None or str(x).strip() == "":
            return 0
        return float(x)
    except Exception:
        return 0


def get_linked_image_urls_from_name_field(linked_record_ids, linked_table_name="Image URLs"):
    """Fetch 'Name' field values (the image URLs) from linked records."""
    if not linked_record_ids:
        return []

    urls = []
    linked_table = Airtable(AIRTABLE_BASE_ID, linked_table_name, AIRTABLE_API_KEY)
    for rec in linked_record_ids:
        rec_id = rec.get("id")
        if not rec_id:
            continue
        try:
            linked_record = linked_table.get(rec_id)
            name_value = linked_record.get("fields", {}).get("Name")
            if isinstance(name_value, str) and name_value.startswith("http"):
                urls.append(name_value.strip())
        except Exception as e:
            print(f"⚠️ Failed to fetch linked image for {rec_id}: {e}", flush=True)
            continue

    return urls


def set_metafield(owner_id, namespace, key, mtype, value):
    """Generic helper to set a metafield via GraphQL."""
    mutation = """
    mutation metafieldsSet($metafields: [MetafieldsSetInput!]!) {
      metafieldsSet(metafields: $metafields) {
        metafields { namespace key type value }
        userErrors { field message }
      }
    }
    """
    variables = {
        "metafields": [{
            "ownerId": owner_id,
            "namespace": namespace,
            "key": key,
            "type": mtype,
            "value": str(value)
        }]
    }
    resp = requests.post(_graphql_url(), headers=_json_headers(), json={"query": mutation, "variables": variables})
    print("[GQL metafield]", resp.text, flush=True)
    return resp.json()


# ---------------------------
# MAIN ROUTE
# ---------------------------
@create_shopify_bp.route("/create-shopify-item", methods=["POST"])
def create_shopify_item():
    """
    Create a new Shopify product from Airtable record.
    Uses modern inventory API (two-step: product → inventory level).
    Includes automatic image search & metafield setup.
    """
    try:
        data = request.get_json(force=True)
        record_id = data.get("record_id")
        record = data.get("fields", {})

        if not record_id or not record:
            return jsonify({"error": "Missing record_id or fields"}), 400

        qty = _to_number(record.get("Qty given in shopify", 0))
        status = "active" if qty > 0 else "draft"

        # ---------------- 1️⃣ Prepare Product ----------------
        product_data = {
            "product": {
                "title": record.get("Product Name", "").strip(),
                "body_html": record.get("ShopifyDesc", ""),
                "vendor": record.get("Brand", ""),
                "product_type": record.get("Type", ""),
                "tags": record.get("Category", ""),
                "status": status,
                "variants": [{
                    "sku": record.get("SKU", ""),
                    "price": str(record.get("UAE Price", 0)),
                    "barcode": record.get("Barcode", ""),
                    "weight": _to_number(record.get("Weight", 850)),
                    "weight_unit": "g",
                    "inventory_management": "shopify",
                    "inventory_policy": "deny"
                }],
                "options": [{"name": "Title", "values": ["Default Title"]}],
                "images": []
            }
        }

        # ---------------- 2️⃣ Add Images ----------------
        linked_image_records = record.get("Image URLs", [])
        image_urls = get_linked_image_urls_from_name_field(linked_image_records, linked_table_name="Image URLs")

        if not image_urls:
            product_name = record.get("Product Name", "")
            search_result = ImageSearcher.search_by_product_name(product_name, limit=5)
            if search_result["success"] and search_result["count"] > 0:
                image_urls = [img["image"]["url"] for img in search_result["images"] if img.get("image")]

        for url in image_urls:
            product_data["product"]["images"].append({"src": url})

        # ---------------- 3️⃣ Create Product ----------------
        resp = requests.post(_rest_url("products.json"), headers=_json_headers(), json=product_data)
        if resp.status_code != 201:
            return jsonify({
                "error": "Shopify API error",
                "status": resp.status_code,
                "details": resp.text
            }), resp.status_code

        result = resp.json()
        product = result.get("product", {})
        shopify_product_id = product.get("id")
        variant = product.get("variants", [{}])[0]
        variant_id = variant.get("id")
        inventory_item_id = variant.get("inventory_item_id")

        # ---------------- 4️⃣ Set Inventory ----------------
        loc_resp = requests.get(_rest_url("locations.json"), headers=_json_headers())
        loc_resp.raise_for_status()
        location_id = loc_resp.json()["locations"][0]["id"]

        inv_payload = {"location_id": location_id, "inventory_item_id": inventory_item_id, "available": int(qty)}
        inv_resp = requests.post(_rest_url("inventory_levels/set.json"), headers=_json_headers(), json=inv_payload)
        inv_result = inv_resp.json()

        # ---------------- 5️⃣ Add Metafields ----------------
        product_gid = f"gid://shopify/Product/{shopify_product_id}"
        variant_gid = f"gid://shopify/ProductVariant/{variant_id}"

        # Product-level metafields
        set_metafield(product_gid, "custom", "size", "single_line_text_field", record.get("Size", ""))
        set_metafield(product_gid, "custom", "brands", "single_line_text_field", record.get("Brand", ""))

        # Google Shopping metafields
        raw_gender = (record.get("Category") or "").strip().lower()
        gender_value = raw_gender if raw_gender in ["male", "female", "unisex"] else "unisex"
        set_metafield(variant_gid, "google_shopping", "age_group", "single_line_text_field", "adult")
        set_metafield(variant_gid, "google_shopping", "condition", "single_line_text_field", "new")
        set_metafield(variant_gid, "google_shopping", "gender", "single_line_text_field", gender_value)
        set_metafield(variant_gid, "google_shopping", "mpn", "single_line_text_field", record.get("SKU", ""))

        # ---------------- 6️⃣ Update Airtable ----------------
        try:
            airtable.update(record_id, {"ShopifyID": str(shopify_product_id), "Create in Shopify": False})
        except Exception as e:
            print(f"⚠️ Airtable update failed: {e}", flush=True)

        # ---------------- ✅ Final Success ----------------
        return jsonify({
            "success": True,
            "shopify_id": shopify_product_id,
            "images_used": image_urls,
            "inventory": inv_result
        }), 201

    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500
