from flask import jsonify, request
from shopify_utils import (
    _to_number, MARKET_NAMES,
    get_variant_product_and_inventory_by_sku, update_variant_default_price,
    update_variant_details, update_product_title, set_metafield,
    get_primary_location_id, set_inventory_absolute, get_market_price_lists,
    update_price_list
)

import os

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

def handle_airtable_webhook():
    try:
        secret = request.headers.get("X-Secret-Token")
        if secret != WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 401

        data = request.json or {}
        print("Received data:", data, flush=True)

        sku = data.get("SKU")
        prices = {
            "UAE": _to_number(data.get("UAE price")),
            "Asia": _to_number(data.get("Asia Price")),
            "America": _to_number(data.get("America Price"))
        }
        uae_compare_price = _to_number(data.get("UAE Comparison Price"))
        qty_abs = _to_number(data.get("Qty given in shopify"))
        title = data.get("Title")
        barcode = data.get("Barcode")
        size_value = data.get("Size")

        if not sku:
            return jsonify({"error": "SKU missing"}), 400

        variant_gid, product_gid, variant_num, inventory_item_id = get_variant_product_and_inventory_by_sku(sku)
        if not variant_gid:
            return jsonify({"error": f"Variant with SKU {sku} not found"}), 404

        if title or barcode:
            update_variant_details(variant_gid, title=title, barcode=barcode)
        if title:
            update_product_title(product_gid, title)

        if prices.get("UAE") is not None:
            update_variant_default_price(variant_num, prices["UAE"], compare_at_price=uae_compare_price)

        if size_value:
            set_metafield(variant_gid, "custom", "size", "single_line_text_field", str(size_value))

        if qty_abs is not None:
            loc_id = get_primary_location_id()
            set_inventory_absolute(inventory_item_id, loc_id, qty_abs)

        price_lists = get_market_price_lists()
        price_updates = {}
        for market_key, amount in prices.items():
            if amount is None:
                continue
            mname = MARKET_NAMES.get(market_key)
            if not mname or mname not in price_lists:
                continue
            pl = price_lists[mname]
            compare_amt = uae_compare_price if market_key == "UAE" else None
            res = update_price_list(pl["id"], variant_gid, amount, pl["currency"], compare_at_amount=compare_amt)
            price_updates[market_key] = res

        return jsonify({
            "status": "success",
            "variant_id": variant_gid,
            "product_id": product_gid,
            "price_list_updates": price_updates
        }), 200

    except Exception as e:
        import traceback
        print("ERROR:", e, traceback.format_exc(), flush=True)
        return jsonify({"error": str(e)}), 500
