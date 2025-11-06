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
SHOP = os.environ.get("SHOPIFY_SHOP")
TOKEN = os.environ.get("SHOPIFY_API_TOKEN")
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-01")

AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_TABLE_NAME = "French Inventories"

# Initialize Airtable only if credentials exist
airtable = None
if AIRTABLE_BASE_ID and AIRTABLE_API_KEY:
    try:
        airtable = Airtable(AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME, AIRTABLE_API_KEY)
    except Exception as e:
        print(f"⚠️ Airtable initialization failed: {e}")
else:
    print("⚠️ Airtable credentials not configured")

# ---------------------------
# SHOPIFY SESSION MANAGEMENT
# ---------------------------
def setup_shopify_session():
    """Setup and activate Shopify session"""
    try:
        if not all([SHOP, TOKEN]):
            raise Exception("Shopify credentials not configured")
        
        # These are required for the library but not used when using access token
        shopify.Session.setup(api_key="dummy", secret="dummy")
        session = shopify.Session(f"https://{SHOP}", API_VERSION, TOKEN)
        shopify.ShopifyResource.activate_session(session)
        return True
    except Exception as e:
        print(f"⚠️ Shopify session setup failed: {e}")
        return False

def clear_shopify_session():
    """Clear Shopify session"""
    try:
        shopify.ShopifyResource.clear_session()
    except Exception as e:
        print(f"⚠️ Error clearing Shopify session: {e}")

# ---------------------------
# ENVIRONMENT VALIDATION
# ---------------------------
def validate_environment():
    """Validate that all required environment variables are set"""
    required_vars = {
        "SHOPIFY_SHOP": SHOP,
        "SHOPIFY_API_TOKEN": TOKEN,
        "AIRTABLE_BASE_ID": AIRTABLE_BASE_ID,
        "AIRTABLE_API_KEY": AIRTABLE_API_KEY
    }
    
    missing = [var for var, value in required_vars.items() if not value]
    if missing:
        raise Exception(f"Missing environment variables: {', '.join(missing)}")
    
    return True

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

        try:
            # Setup fresh session for this request
            if not setup_shopify_session():
                return {"success": False, "error": "Failed to setup Shopify session", "images": []}

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

            gql = shopify.GraphQL()
            result = gql.execute(query)
            
            # Check for GraphQL errors
            if "errors" in result:
                error_msg = result["errors"][0]["message"] if isinstance(result["errors"], list) else str(result["errors"])
                return {"success": False, "error": error_msg, "images": []}
                
            data = result.get("data", {}).get("files", {})
            images = [edge["node"] for edge in data.get("edges", []) if edge.get("node")]
            return {"success": True, "images": images, "count": len(images)}
            
        except Exception as e:
            print(f"⚠️ Image search error: {e}", flush=True)
            return {"success": False, "error": str(e), "images": []}
        finally:
            # Always clear session
            clear_shopify_session()

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
    """Safely convert to number"""
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
    try:
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
    except Exception as e:
        print(f"⚠️ Error accessing linked table {linked_table_name}: {e}", flush=True)

    return urls

def set_metafield(owner_id, namespace, key, mtype, value):
    """Generic helper to set a metafield via GraphQL."""
    try:
        mutation = """
        mutation metafieldsSet($metafields: [MetafieldsSetInput!]!) {
          metafieldsSet(metafields: $metafields) {
            metafields { 
                id
                namespace 
                key 
                type 
                value 
            }
            userErrors { 
                field 
                message 
            }
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
        
        resp = requests.post(
            _graphql_url(), 
            headers=_json_headers(), 
            json={"query": mutation, "variables": variables},
            timeout=30
        )
        resp.raise_for_status()
        
        result = resp.json()
        if "errors" in result:
            print(f"⚠️ Metafield GraphQL errors: {result['errors']}", flush=True)
            return False
            
        user_errors = result.get("data", {}).get("metafieldsSet", {}).get("userErrors", [])
        if user_errors:
            print(f"⚠️ Metafield user errors: {user_errors}", flush=True)
            return False
            
        print(f"✅ Metafield set: {namespace}.{key} = {value}", flush=True)
        return True
        
    except Exception as e:
        print(f"⚠️ Error setting metafield {namespace}.{key}: {e}", flush=True)
        return False

def get_shopify_locations():
    """Get available Shopify locations"""
    try:
        resp = requests.get(_rest_url("locations.json"), headers=_json_headers(), timeout=30)
        resp.raise_for_status()
        locations = resp.json().get("locations", [])
        if not locations:
            raise Exception("No locations found in Shopify store")
        return locations
    except Exception as e:
        print(f"⚠️ Error fetching locations: {e}", flush=True)
        raise

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
        # Validate environment first
        validate_environment()
        
        data = request.get_json(force=True)
        record_id = data.get("record_id")
        record = data.get("fields", {})

        if not record_id or not record:
            return jsonify({"error": "Missing record_id or fields"}), 400

        # Validate required fields
        required_fields = ["Product Name", "SKU"]
        missing_fields = [field for field in required_fields if not record.get(field)]
        if missing_fields:
            return jsonify({
                "error": f"Missing required fields: {', '.join(missing_fields)}"
            }), 400

        qty = _to_number(record.get("Qty given in shopify", 0))
        status = "active" if qty > 0 else "draft"

        # ---------------- 1️⃣ Prepare Product ----------------
        product_data = {
            "product": {
                "title": record.get("Product Name", "").strip(),
                "body_html": record.get("ShopifyDesc", "") or "",
                "vendor": record.get("Brand", "") or "",
                "product_type": record.get("Type", "") or "",
                "tags": record.get("Category", "") or "",
                "status": status,
                "variants": [{
                    "sku": record.get("SKU", ""),
                    "price": str(_to_number(record.get("UAE Price", 0))),
                    "barcode": record.get("Barcode", "") or "",
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

        # If no linked images, try searching by product name
        if not image_urls:
            product_name = record.get("Product Name", "")
            if product_name:
                print(f"🔍 Searching images for: {product_name}", flush=True)
                search_result = ImageSearcher.search_by_product_name(product_name, limit=5)
                if search_result["success"] and search_result["count"] > 0:
                    image_urls = [
                        img["image"]["url"] for img in search_result["images"] 
                        if img.get("image") and img["image"].get("url")
                    ]
                    print(f"✅ Found {len(image_urls)} images via search", flush=True)
                else:
                    print(f"❌ No images found via search: {search_result.get('error', 'Unknown error')}", flush=True)

        # Add images to product data
        for url in image_urls[:10]:  # Limit to 10 images
            product_data["product"]["images"].append({"src": url})

        # ---------------- 3️⃣ Create Product ----------------
        print(f"🛍️ Creating product: {product_data['product']['title']}", flush=True)
        resp = requests.post(_rest_url("products.json"), headers=_json_headers(), json=product_data, timeout=30)
        
        if resp.status_code != 201:
            error_text = resp.text
            print(f"❌ Shopify API error: {resp.status_code} - {error_text}", flush=True)
            return jsonify({
                "error": "Shopify API error",
                "status": resp.status_code,
                "details": error_text
            }), resp.status_code

        result = resp.json()
        product = result.get("product", {})
        shopify_product_id = product.get("id")
        variant = product.get("variants", [{}])[0]
        variant_id = variant.get("id")
        inventory_item_id = variant.get("inventory_item_id")

        if not all([shopify_product_id, variant_id, inventory_item_id]):
            return jsonify({
                "error": "Incomplete product creation response",
                "details": result
            }), 500

        print(f"✅ Product created: {shopify_product_id}, Variant: {variant_id}", flush=True)

        # ---------------- 4️⃣ Set Inventory ----------------
        try:
            locations = get_shopify_locations()
            location_id = locations[0]["id"]
            
            inv_payload = {
                "location_id": location_id, 
                "inventory_item_id": inventory_item_id, 
                "available": int(qty)
            }
            inv_resp = requests.post(
                _rest_url("inventory_levels/set.json"), 
                headers=_json_headers(), 
                json=inv_payload,
                timeout=30
            )
            inv_resp.raise_for_status()
            inv_result = inv_resp.json()
            print(f"✅ Inventory set: {qty} at location {location_id}", flush=True)
        except Exception as e:
            print(f"⚠️ Inventory setting failed: {e}", flush=True)
            inv_result = {"error": str(e)}

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
            if airtable:
                airtable.update(record_id, {
                    "ShopifyID": str(shopify_product_id), 
                    "Create in Shopify": False
                })
                print(f"✅ Airtable updated for record: {record_id}", flush=True)
        except Exception as e:
            print(f"⚠️ Airtable update failed: {e}", flush=True)

        # ---------------- ✅ Final Success ----------------
        return jsonify({
            "success": True,
            "shopify_id": shopify_product_id,
            "variant_id": variant_id,
            "product_title": product_data["product"]["title"],
            "images_used": len(image_urls),
            "inventory_set": qty,
            "status": status
        }), 201

    except Exception as e:
        print(f"❌ Unexpected error: {e}", flush=True)
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500

# Health check endpoint
@create_shopify_bp.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint"""
    try:
        validate_environment()
        return jsonify({
            "status": "healthy",
            "shopify_configured": bool(SHOP and TOKEN),
            "airtable_configured": bool(AIRTABLE_BASE_ID and AIRTABLE_API_KEY)
        })
    except Exception as e:
        return jsonify({"status": "unhealthy", "error": str(e)}), 500