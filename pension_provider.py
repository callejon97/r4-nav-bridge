import json
import re
import time
from datetime import datetime
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException

app = FastAPI(title="Wealthfolio Spanish Pension Custom Provider (Quefondos)")

PLANS = {
    "RENTPENSION-IX-GB-ACCIONES": {
        "dgs": "N4390",
        "name": "RENTA 4 GLOBAL ACCIONES",
    },
    "RENTPENSION-EUROPA-ACCIONES": {
        "dgs": "N1111",
        "name": "RENTA 4 EUROPA ACCIONES",
    },
    "RENTPENSION-XIV-TOLERANTE": {
        "dgs": "N5034",
        "name": "R4 ACTIVA GESTION TOLERANTE",
    },
}

CACHE_TTL_SECONDS = 6 * 3600
_latest_cache: dict[str, tuple[float, dict]] = {}
_history_cache: dict[str, tuple[float, list[dict]]] = {}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.quefondos.com/es/planes/",
}


def _parse_es_float(raw: str) -> float:
    return float(raw.strip().replace(".", "").replace(",", "."))


def _fetch_ficha_html(dgs: str) -> tuple[str, str]:
    url = f"https://www.quefondos.com/es/planes/ficha/index.html?isin={dgs}"
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        resp = client.get(url, headers=HEADERS)
        resp.raise_for_status()
    return url, resp.text


def fetch_quefondos_nav(symbol: str, meta: dict) -> dict:
    url, html = _fetch_ficha_html(meta["dgs"])
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

    nav_match = re.search(r"Valor liquidativo:\s*([\d.,]+)\s*EUR", text, re.IGNORECASE)
    date_match = re.search(r"Fecha:\s*(\d{2}/\d{2}/\d{4})", text)

    if not nav_match or not date_match:
        raise ValueError(f"Could not parse NAV or Date from {url}")

    price = _parse_es_float(nav_match.group(1))
    as_of = datetime.strptime(date_match.group(1), "%d/%m/%Y").strftime("%Y-%m-%d")

    return {
        "symbol": symbol,
        "dgs_code": meta["dgs"],
        "name": meta["name"],
        "price": price,
        "currency": "EUR",
        "as_of": as_of,
        "source": url,
    }


def _extract_fondo_series(html: str) -> dict[str, float]:
    match = re.search(r"\bvar\s+fondo\s*=\s*(\[[\s\S]*?\])\s*;", html)
    if not match:
        raise ValueError("Could not locate `var fondo = [...]` in Quefondos ficha HTML")

    raw_pairs = json.loads(match.group(1))
    rows_by_date: dict[str, float] = {}

    for item in raw_pairs:
        if not isinstance(item, list) or len(item) < 2:
            continue
        date_raw, val_raw = item[0], item[1]
        if val_raw is None:
            continue
        try:
            dt_str = datetime.strptime(str(date_raw).strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
            val = float(val_raw)
        except (ValueError, TypeError):
            continue
        if val > 0:
            rows_by_date[dt_str] = round(val, 6)

    return rows_by_date


def fetch_quefondos_history(symbol: str, meta: dict) -> list[dict]:
    _, html = _fetch_ficha_html(meta["dgs"])
    rows_by_date = _extract_fondo_series(html)

    # Ensure latest headline NAV is included if newer than the last point in `var fondo`
    try:
        latest = get_pension_quote(symbol)
        rows_by_date.setdefault(latest["as_of"], latest["price"])
    except Exception:
        pass

    if not rows_by_date:
        raise ValueError(f"No historical points extracted for {symbol} ({meta['dgs']})")

    # Wealthfolio 'Dated series' expects newest row first
    return [
        {
            "date": d,
            "price": rows_by_date[d],
            "currency": "EUR",
        }
        for d in sorted(rows_by_date.keys(), reverse=True)
    ]


@app.get("/api/pensiones/{symbol}")
def get_pension_quote(symbol: str):
    key = symbol.upper()
    if key not in PLANS:
        raise HTTPException(status_code=404, detail=f"Unsupported symbol: {symbol}")

    now = time.time()
    if key in _latest_cache and (now - _latest_cache[key][0]) < CACHE_TTL_SECONDS:
        return _latest_cache[key][1]

    meta = PLANS[key]
    try:
        payload = fetch_quefondos_nav(key, meta)
    except Exception as exc:
        if key in _latest_cache:
            return _latest_cache[key][1]
        raise HTTPException(status_code=502, detail=f"Error fetching NAV: {exc}") from exc

    _latest_cache[key] = (now, payload)
    return payload


@app.get("/api/pensiones/{symbol}/history")
def get_pension_history(symbol: str):
    key = symbol.upper()
    if key not in PLANS:
        raise HTTPException(status_code=404, detail=f"Unsupported symbol: {symbol}")

    now = time.time()
    if key in _history_cache and (now - _history_cache[key][0]) < CACHE_TTL_SECONDS:
        return _history_cache[key][1]

    meta = PLANS[key]
    try:
        series = fetch_quefondos_history(key, meta)
    except Exception as exc:
        if key in _history_cache:
            return _history_cache[key][1]
        raise HTTPException(status_code=502, detail=f"Error fetching history: {exc}") from exc

    _history_cache[key] = (now, series)
    return series