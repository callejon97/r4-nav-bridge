import json
import os
import re
import time
from datetime import datetime
from typing import Literal, Optional

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query

app = FastAPI(title="Wealthfolio Spanish Pension Custom Provider (R4 + Quefondos)")

# Habilita/deshabilita Renta 4 como primera opción por defecto (env: R4_PRIMARY=true/false)
R4_PRIMARY_DEFAULT = os.getenv("R4_PRIMARY", "true").strip().lower() in {"1", "true", "yes", "on"}

PLANS = {
    "RENTPENSION-IX-GB-ACCIONES": {
        "dgs": "N4390",
        "r4_code": "F0815",
        "r4_url": "https://www.r4.com/planes-de-pensiones/fichas/F0815",
        "name": "RENTA 4 GLOBAL ACCIONES",
    },
    "RENTPENSION-EUROPA-ACCIONES": {
        "dgs": "N1111",
        "r4_code": "F0475",
        "r4_url": "https://www.r4.com/planes-de-pensiones/fichas/F0475",
        "name": "RENTA 4 EUROPA ACCIONES",
    },
    "RENTPENSION-XIV-TOLERANTE": {
        "dgs": "N5034",
        "r4_code": "F2013",
        "r4_url": "https://www.r4.com/planes-de-pensiones/fichas/F2013",
        "name": "R4 ACTIVA GESTION TOLERANTE",
    },
}

CACHE_TTL_SECONDS = 6 * 3600
_latest_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_history_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}

COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

QF_HEADERS = {**COMMON_HEADERS, "Referer": "https://www.quefondos.com/es/planes/"}
R4_HEADERS = {**COMMON_HEADERS, "Referer": "https://www.r4.com/planes-de-pensiones"}


def _parse_es_float(raw: str) -> float:
    return float(raw.strip().replace(".", "").replace(",", "."))


def _fetch_html(url: str, headers: dict) -> str:
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------------
# Fuente 1 (Primaria): Renta 4
# ---------------------------------------------------------------------------

def _r4_candidate_urls(meta: dict) -> list[str]:
    urls = []
    if meta.get("r4_url"):
        urls.append(meta["r4_url"])
    code = meta.get("r4_code")
    if code:
        urls.extend([
            f"https://www.r4.com/planes-de-pensiones/fichas/{code}",
            f"https://www.r4.com/planes-de-pensiones/ficha?codigo={code}",
        ])
    # Deduplicar manteniendo orden
    return list(dict.fromkeys(urls))


def fetch_r4_nav(symbol: str, meta: dict) -> dict:
    last_err: Optional[Exception] = None

    for url in _r4_candidate_urls(meta):
        try:
            html = _fetch_html(url, R4_HEADERS)
            soup = BeautifulSoup(html, "html.parser")
            text = soup.get_text(" ", strip=True)

            # Patrón 1: "Valor liquidativo ... 15,8603 € ... (24/11/2026)" o "Fecha: 24/11/2026"
            nav_match = re.search(
                r"Valor\s+liquidativo[^0-9]{0,40}([\d.,]+)\s*(?:€|EUR)",
                text,
                re.IGNORECASE,
            )
            date_match = re.search(
                r"(?:Valor\s+liquidativo[^0-9]{0,80}|Fecha(?:\s+valor)?[:\s]+)[^\d]{0,15}(\d{2}/\d{2}/\d{4})",
                text,
                re.IGNORECASE,
            )

            # Fallback JSON embebido / scripts si el texto plano no captura ambos
            if not nav_match:
                nav_match = re.search(r'"valorLiquidativo"\s*:\s*"?([\d.,]+)"?', html, re.IGNORECASE)
            if not date_match:
                date_match = re.search(r'"fechaValor"\s*:\s*"(\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})"', html, re.IGNORECASE)

            if not nav_match or not date_match:
                raise ValueError(f"Could not parse R4 NAV or Date from {url}")

            price = round(_parse_es_float(nav_match.group(1)), 6)
            raw_date = date_match.group(1)
            if "-" in raw_date:
                as_of = datetime.strptime(raw_date, "%Y-%m-%d").strftime("%Y-%m-%d")
            else:
                as_of = datetime.strptime(raw_date, "%d/%m/%Y").strftime("%Y-%m-%d")

            if price <= 0:
                raise ValueError(f"Invalid R4 price ({price}) from {url}")

            return {
                "symbol": symbol,
                "dgs_code": meta["dgs"],
                "r4_code": meta.get("r4_code"),
                "name": meta["name"],
                "price": price,
                "currency": "EUR",
                "as_of": as_of,
                "provider": "r4",
                "source": url,
            }
        except Exception as exc:
            last_err = exc

    raise ValueError(f"All R4 URLs failed for {symbol}: {last_err}")


def _extract_r4_series(html: str) -> dict[str, float]:
    """Intenta extraer serie histórica si la ficha pública de R4 la embebe en el HTML/JS."""
    rows_by_date: dict[str, float] = {}

    # Buscar pares fecha/valor en JSON embebidos típicos de gráficas de R4
    for m in re.finditer(
        r'"(?:fecha|date)"\s*:\s*"(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})"[^}]{0,80}?"(?:valor|vl|value|close|price)"\s*:\s*"?([\d.,]+)"?',
        html,
        re.IGNORECASE,
    ):
        raw_dt, raw_val = m.group(1), m.group(2)
        try:
            dt_str = (
                datetime.strptime(raw_dt, "%Y-%m-%d").strftime("%Y-%m-%d")
                if "-" in raw_dt
                else datetime.strptime(raw_dt, "%d/%m/%Y").strftime("%Y-%m-%d")
            )
            val = round(_parse_es_float(raw_val), 6)
            if val > 0:
                rows_by_date[dt_str] = val
        except ValueError:
            continue

    return rows_by_date


# ---------------------------------------------------------------------------
# Fuente 2 (Fallback + Histórico largo): Quefondos
# ---------------------------------------------------------------------------

def _fetch_quefondos_html(dgs: str) -> tuple[str, str]:
    url = f"https://www.quefondos.com/es/planes/ficha/index.html?isin={dgs}"
    return url, _fetch_html(url, QF_HEADERS)


def fetch_quefondos_nav(symbol: str, meta: dict) -> dict:
    url, html = _fetch_quefondos_html(meta["dgs"])
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

    nav_match = re.search(r"Valor liquidativo:\s*([\d.,]+)\s*EUR", text, re.IGNORECASE)
    date_match = re.search(r"Fecha:\s*(\d{2}/\d{2}/\d{4})", text)

    if not nav_match or not date_match:
        raise ValueError(f"Could not parse NAV or Date from {url}")

    price = round(_parse_es_float(nav_match.group(1)), 6)
    as_of = datetime.strptime(date_match.group(1), "%d/%m/%Y").strftime("%Y-%m-%d")

    return {
        "symbol": symbol,
        "dgs_code": meta["dgs"],
        "r4_code": meta.get("r4_code"),
        "name": meta["name"],
        "price": price,
        "currency": "EUR",
        "as_of": as_of,
        "provider": "quefondos",
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


# ---------------------------------------------------------------------------
# Orquestación con prioridad R4 -> Quefondos
# ---------------------------------------------------------------------------

def resolve_latest_nav(symbol: str, meta: dict, source: str) -> dict:
    errors: list[str] = []

    if source in {"auto", "r4"}:
        try:
            return fetch_r4_nav(symbol, meta)
        except Exception as exc:
            errors.append(f"R4: {exc}")
            if source == "r4":
                raise RuntimeError("; ".join(errors)) from exc

    if source in {"auto", "quefondos"}:
        try:
            return fetch_quefondos_nav(symbol, meta)
        except Exception as exc:
            errors.append(f"Quefondos: {exc}")

    raise RuntimeError("; ".join(errors) or "No data source available")


def resolve_history(symbol: str, meta: dict, source: str) -> list[dict]:
    rows_by_date: dict[str, float] = {}
    errors: list[str] = []

    # 1. Base histórica de Quefondos (serie larga desde 2016)
    if source in {"auto", "quefondos"}:
        try:
            _, qf_html = _fetch_quefondos_html(meta["dgs"])
            rows_by_date.update(_extract_fondo_series(qf_html))
        except Exception as exc:
            errors.append(f"Quefondos history: {exc}")

    # 2. Serie pública de R4 (si existe) + último VL de R4 sobreescribiendo/añadiendo fecha más reciente
    if source in {"auto", "r4"}:
        for url in _r4_candidate_urls(meta):
            try:
                r4_html = _fetch_html(url, R4_HEADERS)
                rows_by_date.update(_extract_r4_series(r4_html))
                break
            except Exception:
                continue

        try:
            r4_latest = fetch_r4_nav(symbol, meta)
            rows_by_date[r4_latest["as_of"]] = r4_latest["price"]
        except Exception as exc:
            errors.append(f"R4 latest: {exc}")
            if source == "r4" and not rows_by_date:
                raise RuntimeError("; ".join(errors)) from exc

    # Asegurar también último titular de Quefondos si no estaba
    if source in {"auto", "quefondos"}:
        try:
            qf_latest = fetch_quefondos_nav(symbol, meta)
            rows_by_date.setdefault(qf_latest["as_of"], qf_latest["price"])
        except Exception:
            pass

    if not rows_by_date:
        raise RuntimeError(f"No historical points extracted for {symbol}: {'; '.join(errors)}")

    # Wealthfolio 'Dated series' espera orden descendente (más reciente primero)
    return [
        {
            "date": d,
            "price": rows_by_date[d],
            "currency": "EUR",
        }
        for d in sorted(rows_by_date.keys(), reverse=True)
    ]


def _effective_source(source: Optional[str]) -> str:
    if source:
        return source
    return "auto" if R4_PRIMARY_DEFAULT else "quefondos"


@app.get("/api/pensiones/{symbol}")
def get_pension_quote(
    symbol: str,
    source: Optional[Literal["auto", "r4", "quefondos"]] = Query(
        default=None,
        description="auto (R4 primero + fallback Quefondos), r4 (solo R4), quefondos (solo Quefondos)",
    ),
    refresh: bool = Query(default=False, description="Ignora caché en memoria"),
):
    key = symbol.upper()
    if key not in PLANS:
        raise HTTPException(status_code=404, detail=f"Unsupported symbol: {symbol}")

    eff_source = _effective_source(source)
    cache_key = (key, eff_source)
    now = time.time()

    if not refresh and cache_key in _latest_cache and (now - _latest_cache[cache_key][0]) < CACHE_TTL_SECONDS:
        return _latest_cache[cache_key][1]

    meta = PLANS[key]
    try:
        payload = resolve_latest_nav(key, meta, eff_source)
    except Exception as exc:
        if cache_key in _latest_cache:
            return _latest_cache[cache_key][1]
        raise HTTPException(status_code=502, detail=f"Error fetching NAV: {exc}") from exc

    _latest_cache[cache_key] = (now, payload)
    return payload


@app.get("/api/pensiones/{symbol}/history")
def get_pension_history(
    symbol: str,
    source: Optional[Literal["auto", "r4", "quefondos"]] = Query(
        default=None,
        description="auto (histórico Quefondos + último VL R4), r4 (solo R4), quefondos (solo Quefondos)",
    ),
    refresh: bool = Query(default=False, description="Ignora caché en memoria"),
):
    key = symbol.upper()
    if key not in PLANS:
        raise HTTPException(status_code=404, detail=f"Unsupported symbol: {symbol}")

    eff_source = _effective_source(source)
    cache_key = (key, eff_source)
    now = time.time()

    if not refresh and cache_key in _history_cache and (now - _history_cache[cache_key][0]) < CACHE_TTL_SECONDS:
        return _history_cache[cache_key][1]

    meta = PLANS[key]
    try:
        series = resolve_history(key, meta, eff_source)
    except Exception as exc:
        if cache_key in _history_cache:
            return _history_cache[cache_key][1]
        raise HTTPException(status_code=502, detail=f"Error fetching history: {exc}") from exc

    _history_cache[cache_key] = (now, series)
    return series