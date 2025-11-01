from flask import Flask, request, jsonify
from your_file_name import generate_description_from_web  # replace with your filename (without .py extension)

app = Flask(__name__)

@app.route("/generate", methods=["POST"])
def generate():
    try:
        data = request.get_json()
        perfume_name = data.get("perfume_name")
        brand_name = data.get("brand_name")

        if not perfume_name:
            return jsonify({"error": "perfume_name is required"}), 400

        print(f"🧠 Generating description for {brand_name or ''} {perfume_name}")
        description_html = generate_description_from_web(perfume_name, brand_name)

        return jsonify({"description": description_html})

    except Exception as e:
        print(f"❌ Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/", methods=["GET"])
def healthcheck():
    return jsonify({"status": "ok", "message": "Perfume description API running"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
