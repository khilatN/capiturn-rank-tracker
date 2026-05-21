import json, base64, re, os
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

CLIENT_ID     = "MuhammdY-RankTrac-PRD-62ed07085-bbab1ddd"
CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "")

def get_token():
    creds   = f"{CLIENT_ID}:{CLIENT_SECRET}"
    encoded = base64.b64encode(creds.encode()).decode()
    r = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {encoded}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=10
    )
    r.raise_for_status()
    return r.json()["access_token"]

def ebay_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
        "X-EBAY-C-ENDUSERCTX": "contextualLocation=country%3DGB",
    }

def search_uk(query, token):
    r = requests.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        headers=ebay_headers(token),
        params={"q": query, "sort": "pricePlusShipping", "limit": 50, "marketplaceId": "EBAY_GB"},
        timeout=15
    )
    r.raise_for_status()
    return r.json().get("itemSummaries", [])

def group_prices(group_id, token):
    try:
        r = requests.get(
            "https://api.ebay.com/buy/browse/v1/item/get_items_by_item_group",
            headers=ebay_headers(token),
            params={"item_group_id": group_id},
            timeout=10
        )
        if r.status_code == 200:
            prices = [float(it["price"]["value"]) for it in r.json().get("items", []) if it.get("price", {}).get("value")]
            if prices:
                return min(prices), max(prices)
    except Exception:
        pass
    return None, None

def title_matches(title, query):
    stop = {'a','an','the','and','or','for','in','on','to','of','with'}
    tl   = (title or '').lower()
    kws  = [w for w in re.findall(r'[a-z0-9]+', (query or '').lower()) if w not in stop]
    return all(k in tl for k in kws)

def process_item(item, token):
    iid    = item.get("itemId", "")
    parts  = iid.split("|")
    is_var = len(parts) == 3 and parts[2] not in ("", "0")
    cands  = []
    sp     = item.get("price", {}).get("value")
    if sp:
        cands.append(float(sp))
    if is_var:
        gl, _ = group_prices(parts[1], token)
        if gl is not None:
            cands.append(gl)
    price = min(cands) if cands else 0.0
    ships = item.get("shippingOptions", [])
    sc    = [float(s["shippingCost"]["value"]) for s in ships if s.get("shippingCost")]
    ship  = min(sc) if sc else 0.0
    return {
        "title":  item.get("title", "")[:80],
        "seller": item.get("seller", {}).get("username", ""),
        "price":  round(price, 2),
        "ship":   round(ship, 2),
        "total":  round(price + ship, 2),
        "url":    item.get("itemWebUrl", ""),
    }

def get_top10(query, token):
    items   = search_uk(query, token)
    matched = [i for i in items if title_matches(i.get("title",""), query)]
    if not matched:
        return []
    rows = [None] * len(matched)
    with ThreadPoolExecutor(max_workers=10) as ex:
        fs = {ex.submit(process_item, it, token): idx for idx, it in enumerate(matched)}
        for f in as_completed(fs):
            idx = fs[f]
            try:
                rows[idx] = f.result()
            except Exception:
                pass
    rows = [r for r in rows if r]
    rows.sort(key=lambda x: x["total"])
    return rows[:10]

def check_title(query, seller, token):
    top10 = get_top10(query, token)
    if not top10:
        return {"title": query, "position": "No matches", "yourPrice": "N/A", "lowestPrice": "N/A", "topSeller": "N/A"}
    lowest   = top10[0]
    seller_l = (seller or '').strip().lower()
    rank = your_price = None
    for i, r in enumerate(top10, 1):
        if r["seller"].lower() == seller_l:
            rank       = i
            your_price = f"£{r['total']:.2f}"
            break
    return {
        "title":       query,
        "position":    f"#{rank}" if rank else "Not in top 10",
        "yourPrice":   your_price or "N/A",
        "lowestPrice": f"£{lowest['total']:.2f}",
        "topSeller":   lowest["seller"],
        "top10":       top10,
    }

@app.route("/")
def index():
    return send_file("index.html")

@app.route("/api/search", methods=["POST", "OPTIONS"])
def search():
    if request.method == "OPTIONS":
        return "", 200

    body   = request.get_json()
    titles = body.get("titles", [])
    seller = body.get("seller", "")
    mode   = body.get("mode", "batch")

    try:
        token = get_token()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if mode == "single":
        result = check_title(titles[0], seller, token)
        return jsonify(result)

    results = [None] * len(titles)
    with ThreadPoolExecutor(max_workers=5) as ex:
        fs = {ex.submit(check_title, t, seller, token): i for i, t in enumerate(titles)}
        for f in as_completed(fs):
            i = fs[f]
            try:
                results[i] = f.result()
            except Exception:
                results[i] = {"title": titles[i], "position": "Error", "yourPrice": "N/A", "lowestPrice": "N/A", "topSeller": "N/A"}

    return jsonify({"results": [r for r in results if r]})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port)
