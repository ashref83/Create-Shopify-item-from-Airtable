from flask import jsonify, request
import os

from shopify_utils import (
    _to_number, MARKET_NAMES,
    get_variant_product_and_inventory_by_sku, update_variant_default_price,
    update_variant_details, update_product_title, set_metafield,
    get_primary_location_id, set_inventory_absolute, get_market_price_lists,
    update_price_list
)

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]


def handle_airtable_webhook():
    """
    Old working route logic refactored into a function.
    Mirrors the exact steps/prints/field handling from old code_app.py.
    """
    try:
        # ---- Security ----
        secret = request.headers.get("X-Secret-Token")
        print("Secret header:", secret, flush=True)
        if secret != WEBHOOK_SECRET:
            print("Unauthorized!", flush=True)
            return jsonify({"error": "Unauthorized"}), 401

        # ---- Payload ----
        data = request.json or {}
        print("Received data:", data, flush=True)

        sku = data.get("SKU")
        prices = {
            "UAE": _to_number(data.get("UAE price")),
            "Asia": _to_number(data.get("Asia Price")),
            "America": _to_number(data.get("America Price")),
        }
        uae_compare_price = _to_number(data.get("UAE Comparison Price"))
        qty_abs = _to_number(data.get("Qty given in shopify"))
        title = data.get("Title")
        barcode = data.get("Barcode")
        size_value = data.get("Size")

        print("SKU:", sku, flush=True)
        print("Prices:", prices, flush=True)
        print("UAE Comparison Price:", uae_compare_price, flush=True)
        print("Qty given in shopify:", qty_abs, flush=True)
        print("Title:", title, flush=True)
        print("Barcode:", barcode, flush=True)
        print("Size:", size_value, flush=True)

        if not sku:
            return jsonify({"error": "SKU missing"}), 400

        # ---- Find variant / product / inventory_item_id ----
        variant_gid, product_gid, variant_num, inventory_item_id = get_variant_product_and_inventory_by_sku(sku)
        if not variant_gid:
            return jsonify({"error": f"Variant with SKU {sku} not found"}), 404

        print(
            "variant_gid:", variant_gid,
            "product_gid:", product_gid,
            "variant_num:", variant_num,
            "inventory_item_id:", inventory_item_id,
            flush=True
        )

        # ---- CRITICAL: Validate inventory_item_id ----
        if not inventory_item_id:
            print("ERROR: inventory_item_id is None or empty!", flush=True)
            return jsonify({"error": "Inventory item ID not found for variant"}), 500

        # ---- Variant + Product fields ----
        if title or barcode:
            update_variant_details(variant_gid, title=title, barcode=barcode)
        if title:
            update_product_title(product_gid, title)

        # ---- Default (store) price + optional compare_at ----
        if prices.get("UAE") is not None:
            update_variant_default_price(variant_num, prices["UAE"], compare_at_price=uae_compare_price)

        # ---- Metafields (size) ----
        if size_value is not None and str(size_value).strip() != "":
            set_metafield(
                owner_id_gid=variant_gid,
                namespace="custom",
                key="size",
                mtype="single_line_text_field",
                value=str(size_value)
            )

        # ---- Inventory (absolute set at primary location) ----
        inventory_update = None
        inventory_error = None
        
        if qty_abs is not None:
            print(f"Attempting to update inventory to {qty_abs}...", flush=True)
            
            try:
                loc_id = get_primary_location_id()
                print(f"Primary location ID: {loc_id}", flush=True)
                
                if not loc_id:
                    inventory_error = "Primary location ID not found"
                    print(f"ERROR: {inventory_error}", flush=True)
                else:
                    inventory_update = set_inventory_absolute(inventory_item_id, loc_id, qty_abs)
                    print(f"Inventory update result: {inventory_update}", flush=True)
                    
                    # Check if the update was successful
                    if inventory_update and isinstance(inventory_update, dict):
                        if inventory_update.get("error"):
                            inventory_error = inventory_update.get("error")
                            print(f"ERROR: Inventory update failed - {inventory_error}", flush=True)
                        else:
                            print(f"SUCCESS: Inventory updated to {qty_abs}", flush=True)
                    elif inventory_update is None:
                        inventory_error = "Inventory update returned None"
                        print(f"ERROR: {inventory_error}", flush=True)
                        
            except Exception as inv_err:
                inventory_error = str(inv_err)
                print(f"ERROR during inventory update: {inventory_error}", flush=True)
                import traceback
                print(traceback.format_exc(), flush=True)

        # ---- Markets / Price Lists ----
        price_lists = get_market_price_lists()
        print("Price lists:", price_lists, flush=True)

        price_updates = {}
        for market_key, amount in prices.items():
            if amount is None:
                continue

            mname = MARKET_NAMES.get(market_key)
            if not mname or mname not in price_lists:
                print(f"No price list for market {market_key}", flush=True)
                continue

            pl = price_lists[mname]
            compare_amt = uae_compare_price if (market_key == "UAE" and uae_compare_price is not None) else None

            print(
                f"Updating PL={pl['id']} Market={market_key} "
                f"price={amount} {pl['currency']} compare_at={compare_amt}",
                flush=True
            )

            res = update_price_list(
                pl["id"], variant_gid, amount, pl["currency"], compare_at_amount=compare_amt
            )
            price_updates[market_key] = res

        # ---- Response ----
        response_data = {
            "status": "success",
            "variant_id": variant_gid,
            "product_id": product_gid,
            "inventory_update": inventory_update,
            "price_list_updates": price_updates
        }
        
        # Add warning if inventory update failed
        if inventory_error:
            response_data["inventory_error"] = inventory_error
            response_data["status"] = "partial_success"
        
        return jsonify(response_data), 200

    except Exception as e:
        import traceback
        print("ERROR:", str(e), flush=True)
        print(traceback.format_exc(), flush=True)
        return jsonify({"error": str(e)}), 500