from flask import jsonify, request
import os

from shopify_utils import (
    _to_number,
    get_variant_product_and_inventory_by_sku,
    update_variant_default_price,
    update_variant_details,
    update_product_title,
    set_metafield,
    get_primary_location_id,
    set_inventory_absolute,
    shopify_graphql
)

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]


def handle_airtable_webhook():
    """
    Enhanced webhook handler: updates Shopify product fields, stock, and
    regional market prices (UAE, Asia, America) when triggered from Airtable.
    Includes Asia Comparison Price and America Comparison Price.
    """
    try:
        # ---- Security Validation ----
        secret = request.headers.get("X-Secret-Token")
        print("Secret header:", secret, flush=True)
        if secret != WEBHOOK_SECRET:
            print("Unauthorized!", flush=True)
            return jsonify({"error": "Unauthorized"}), 401

        # ---- Parse Payload ----
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

        print("SKU:", sku, flush=True)
        print("Prices:", prices, flush=True)
        print("Compare Prices:", compare_prices, flush=True)
        print("Qty given in shopify (RAW):", data.get("Qty given in shopify"), flush=True)
        print("Qty given in shopify (PARSED):", qty_abs, flush=True)
        print("Title:", title, flush=True)
        print("Barcode:", barcode, flush=True)
        print("Size:", size_value, flush=True)

        if not sku:
            return jsonify({"error": "SKU missing"}), 400

        # ---- STEP 1: Find variant / product / inventory ----
        print("\n" + "=" * 80, flush=True)
        print("STEP 1: FINDING VARIANT", flush=True)
        print("=" * 80, flush=True)

        variant_gid, product_gid, variant_num, inventory_item_id = get_variant_product_and_inventory_by_sku(sku)
        if not variant_gid:
            return jsonify({"error": f"Variant with SKU {sku} not found"}), 404

        print("✓ variant_gid:", variant_gid, flush=True)
        print("✓ product_gid:", product_gid, flush=True)

        # ---- STEP 2: Update product and variant details ----
        print("\n" + "=" * 80, flush=True)
        print("STEP 2: UPDATING VARIANT & PRODUCT DETAILS", flush=True)
        print("=" * 80, flush=True)

        if title or barcode:
            print(f"Updating variant details: title={title}, barcode={barcode}", flush=True)
            update_variant_details(variant_gid, title=title, barcode=barcode)
        if title:
            print(f"Updating product title: {title}", flush=True)
            update_product_title(product_gid, title)

        # ---- STEP 3: Update default Shopify price ----
        print("\n" + "=" * 80, flush=True)
        print("STEP 3: UPDATING DEFAULT PRICE", flush=True)
        print("=" * 80, flush=True)

        if prices.get("UAE") is not None:
            update_variant_default_price(
                variant_num,
                prices["UAE"],
                compare_at_price=compare_prices["UAE"]
            )
            print(f"✓ Default price updated: {prices['UAE']} | compare_at={compare_prices['UAE']}", flush=True)

        # ---- STEP 4: Update size metafield ----
        print("\n" + "=" * 80, flush=True)
        print("STEP 4: UPDATING METAFIELDS", flush=True)
        print("=" * 80, flush=True)

        if size_value:
            print(f"Setting size metafield: {size_value}", flush=True)
            set_metafield(
                owner_id_gid=variant_gid,
                namespace="custom",
                key="size",
                mtype="single_line_text_field",
                value=str(size_value)
            )

        # ---- STEP 5: Update inventory ----
        print("\n" + "=" * 80, flush=True)
        print("STEP 5: UPDATING INVENTORY", flush=True)
        print("=" * 80, flush=True)

        inventory_update = None
        inventory_error = None

        if qty_abs is not None:
            try:
                loc_id = get_primary_location_id()
                if loc_id:
                    inventory_update = set_inventory_absolute(inventory_item_id, loc_id, qty_abs)
                    print(f"✓ Inventory updated to {qty_abs}", flush=True)
                else:
                    inventory_error = "Primary location ID not found"
            except Exception as inv_err:
                inventory_error = str(inv_err)
                print(f"✗ Inventory update failed: {inventory_error}", flush=True)
        else:
            print("⊘ Skipping inventory update (qty_abs is None)", flush=True)

        # ---- STEP 6: Update regional market prices ----
        print("\n" + "=" * 80, flush=True)
        print("STEP 6: UPDATING MARKET PRICES", flush=True)
        print("=" * 80, flush=True)

        PRICE_LIST_IDS = {
            "UAE": "gid://shopify/PriceList/31168201019",      # United Arab Emirates
            "Asia": "gid://shopify/PriceList/31168266555",     # Asia Market
            "America": "gid://shopify/PriceList/31168233787",  # America Market
        }

        price_updates = {}
        for region, price_val in prices.items():
            if price_val is None:
                continue

            price_list_id = PRICE_LIST_IDS.get(region)
            if not price_list_id:
                print(f"⊘ No PriceList ID configured for {region}", flush=True)
                continue

            compare_val = compare_prices.get(region)
            currency = "AED" if region == "UAE" else "USD"

            mutation = """
            mutation priceListFixedPricesAdd($priceListId: ID!, $prices: [PriceListPriceInput!]!) {
              priceListFixedPricesAdd(priceListId: $priceListId, prices: $prices) {
                prices {
                  price { amount currencyCode }
                  compareAtPrice { amount currencyCode }
                  variant { id }
                }
                userErrors { field code message }
              }
            }
            """

            price_input = {
                "variantId": variant_gid,
                "price": {"amount": str(price_val), "currencyCode": currency},
            }
            if compare_val:
                price_input["compareAtPrice"] = {"amount": str(compare_val), "currencyCode": currency}

            variables = {"priceListId": price_list_id, "prices": [price_input]}

            print(f"→ Updating {region} | PL={price_list_id} | Price={price_val} | Compare={compare_val} | {currency}", flush=True)
            res = shopify_graphql(mutation, variables)
            price_updates[region] = res
            print("✓ Price update result:", res, flush=True)

        # ---- Final Response ----
        response_data = {
            "status": "success",
            "variant_id": variant_gid,
            "product_id": product_gid,
            "inventory_update": inventory_update,
            "price_list_updates": price_updates,
        }

        if inventory_error:
            response_data["inventory_error"] = inventory_error
            response_data["status"] = "partial_success"
            print(f"⚠ Inventory error: {inventory_error}", flush=True)

        print("=" * 80, flush=True)
        print("FINAL RESPONSE:", response_data, flush=True)
        print("=" * 80, flush=True)
        return jsonify(response_data), 200

    except Exception as e:
        import traceback
        print("\n" + "=" * 80, flush=True)
        print("FATAL ERROR", flush=True)
        print("=" * 80, flush=True)
        print(traceback.format_exc(), flush=True)
        return jsonify({"error": str(e)}), 500
