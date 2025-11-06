from flask import jsonify, request
import os, time
import requests

from shopify_utils import (
    _to_number,
    get_variant_product_and_inventory_by_sku,
    update_variant_default_price,
    update_variant_details,
    update_product_title,
    set_metafield,
    get_primary_location_id,
    set_inventory_absolute,
)

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
SHOPIFY_STORE = "https://ec207e-a2.myshopify.com"
SHOPIFY_TOKEN = os.environ.get("SHOPIFY_TOKEN")

# -------------------------------------------------------
#  SAFE GRAPHQL CALLER WITH RETRY & RATE-LIMIT HANDLING
# -------------------------------------------------------
def shopify_graphql(query, variables, max_retries=3):
    url = f"{SHOPIFY_STORE}/admin/api/2024-07/graphql.json"
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json",
    }

    for attempt in range(max_retries):
        res = requests.post(url, headers=headers, json={"query": query, "variables": variables})

        # Handle rate limit (429) or throttle header near capacity
        if res.status_code == 429 or "X-Shopify-Shop-Api-Call-Limit" in res.headers:
            limit_info = res.headers.get("X-Shopify-Shop-Api-Call-Limit", "")
            print(f"⚠️ Shopify throttle: {limit_info} | attempt {attempt+1}", flush=True)
            wait_time = 2 * (attempt + 1)
            print(f"Waiting {wait_time}s before retry...", flush=True)
            time.sleep(wait_time)
            continue

        try:
            res.raise_for_status()
            return res.json()
        except Exception as err:
            print(f"✗ Shopify request failed (attempt {attempt+1}): {err}", flush=True)
            time.sleep(2 * (attempt + 1))
    raise Exception("❌ Shopify GraphQL failed after retries")

# -------------------------------------------------------
#  MAIN HANDLER
# -------------------------------------------------------
def handle_airtable_webhook():
    """
    Handles Airtable → Shopify updates.
    Updates variant details, inventory, default price,
    and regional (UAE / Asia / America) price lists safely.
    """
    try:
        # ---- Security ----
        secret = request.headers.get("X-Secret-Token")
        print("Secret header:", secret, flush=True)
        if secret != WEBHOOK_SECRET:
            print("Unauthorized!", flush=True)
            return jsonify({"error": "Unauthorized"}), 401

        # ---- Parse incoming data ----
        data = request.json or {}
        print("=" * 80, flush=True)
        print("WEBHOOK RECEIVED", flush=True)
        print("=" * 80, flush=True)
        print("Received data:", data, flush=True)

        sku = data.get("SKU")
        prices = {
            "UAE": _to_number(data.get("UAE price")),
            "Asia": _to_number(data.get("Asia Price")),
            "America": _to_number(data.get("America Price")),
        }
        compare_prices = {
            "UAE": _to_number(data.get("UAE Comparison Price")),
            "Asia": _to_number(data.get("Asia Comparison Price")),
            "America": _to_number(data.get("America Comparison Price")),
        }
        qty_abs = _to_number(data.get("Qty given in shopify"))
        title = data.get("Title")
        barcode = data.get("Barcode")
        size_value = data.get("Size")

        print(f"SKU: {sku}\nPrices: {prices}\nCompare: {compare_prices}\nQty: {qty_abs}", flush=True)
        if not sku:
            return jsonify({"error": "SKU missing"}), 400

        # ---- STEP 1: Find variant / product ----
        print("\n" + "=" * 80)
        print("STEP 1: FINDING VARIANT", flush=True)
        variant_gid, product_gid, variant_num, inventory_item_id = get_variant_product_and_inventory_by_sku(sku)
        if not variant_gid:
            return jsonify({"error": f"Variant with SKU {sku} not found"}), 404
        print(f"✓ variant_gid={variant_gid} | product_gid={product_gid}", flush=True)

        # ---- STEP 2: Update variant & product details ----
        print("\n" + "=" * 80)
        print("STEP 2: UPDATING VARIANT & PRODUCT DETAILS", flush=True)
        if title or barcode:
            update_variant_details(variant_gid, title=title, barcode=barcode)
        if title:
            update_product_title(product_gid, title)

        # ---- STEP 3: Default Shopify price ----
        print("\n" + "=" * 80)
        print("STEP 3: UPDATING DEFAULT PRICE", flush=True)
        if prices.get("UAE") is not None:
            update_variant_default_price(variant_num, prices["UAE"], compare_at_price=compare_prices["UAE"])
            print(f"✓ Default price={prices['UAE']} | compare={compare_prices['UAE']}", flush=True)

        # ---- STEP 4: Size metafield ----
        print("\n" + "=" * 80)
        print("STEP 4: UPDATING METAFIELDS", flush=True)
        if size_value:
            set_metafield(
                owner_id_gid=variant_gid,
                namespace="custom",
                key="size",
                mtype="single_line_text_field",
                value=str(size_value),
            )
            print(f"✓ Metafield size set: {size_value}", flush=True)

        # ---- STEP 5: Inventory ----
        print("\n" + "=" * 80)
        print("STEP 5: UPDATING INVENTORY", flush=True)
        inventory_update = None
        if qty_abs is not None:
            try:
                loc_id = get_primary_location_id()
                if loc_id:
                    inventory_update = set_inventory_absolute(inventory_item_id, loc_id, qty_abs)
                    print(f"✓ Inventory updated to {qty_abs}", flush=True)
                else:
                    print("✗ Primary location not found", flush=True)
            except Exception as inv_err:
                print(f"✗ Inventory error: {inv_err}", flush=True)
        else:
            print("⊘ Skipping inventory (no qty provided)", flush=True)

        # ---- STEP 6: Regional market prices ----
        print("\n" + "=" * 80)
        print("STEP 6: UPDATING MARKET PRICES", flush=True)
        PRICE_LIST_IDS = {
            "UAE": "gid://shopify/PriceList/31168201019",
            "Asia": "gid://shopify/PriceList/31168266555",
            "America": "gid://shopify/PriceList/31168233787",
        }

        price_updates = {}
        for region, amount in prices.items():
            if amount is None:
                continue
            price_list_id = PRICE_LIST_IDS.get(region)
            if not price_list_id:
                continue

            compare_val = compare_prices.get(region)
            currency = "AED"

            mutation = """
            mutation priceListFixedPricesAdd($priceListId: ID!, $prices: [PriceListPriceInput!]!) {
              priceListFixedPricesAdd(priceListId: $priceListId, prices: $prices) {
                prices { price { amount currencyCode } compareAtPrice { amount currencyCode } variant { id } }
                userErrors { field code message }
              }
            }
            """

            price_input = {
                "variantId": variant_gid,
                "price": {"amount": str(amount), "currencyCode": currency},
            }
            if compare_val:
                price_input["compareAtPrice"] = {"amount": str(compare_val), "currencyCode": currency}

            variables = {"priceListId": price_list_id, "prices": [price_input]}

            print(f"→ Updating {region} | Price={amount} | Compare={compare_val} | {currency}", flush=True)
            res = shopify_graphql(mutation, variables)
            price_updates[region] = res
            print("✓ Price update result:", res, flush=True)

            # small delay to avoid throttle burst
            time.sleep(1)

        # ---- FINAL RESPONSE ----
        response_data = {
            "status": "success",
            "variant_id": variant_gid,
            "product_id": product_gid,
            "inventory_update": inventory_update,
            "price_list_updates": price_updates,
        }
        print("=" * 80, flush=True)
        print("FINAL RESPONSE:", response_data, flush=True)
        print("=" * 80, flush=True)
        return jsonify(response_data), 200

    except Exception as e:
        import traceback
        print("\n" + "=" * 80, flush=True)
        print("FATAL ERROR:", e, flush=True)
        print(traceback.format_exc(), flush=True)
        return jsonify({"error": str(e)}), 500
