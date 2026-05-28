"""
RateRadar scraper v2
- AutoScout24 : __NEXT_DATA__ JSON (fonctionne sans proxy)
- LeBonCoin   : API interne (nécessite SCRAPER_API_KEY pour contourner DataDome)
- La Centrale : __NEXT_DATA__ (nécessite SCRAPER_API_KEY pour contourner Cloudflare)
- L'Argus     : __NEXT_DATA__ JSON (source gratuite supplémentaire)

Variable d'env optionnelle : SCRAPER_API_KEY (scraperapi.com — 5000 req/mois gratuit)
"""

import os
import re
import json
import time
import random
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus

SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY", "")

# ── Headers ────────────────────────────────────────────────────────────────────

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
}

LBC_HEADERS = {
    **BASE_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "api_key": "ba0c2dad52b3585c9a20c3a2271d1fba",
    "Origin": "https://www.leboncoin.fr",
    "Referer": "https://www.leboncoin.fr/",
}

AS24_HEADERS = {
    **BASE_HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.autoscout24.fr/",
}

LC_HEADERS = {
    **BASE_HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.lacentrale.fr/",
}


def _sleep():
    time.sleep(random.uniform(1.2, 2.8))


def _get_html(url: str, headers: dict, timeout: int = 12, proxy: bool = False) -> BeautifulSoup | None:
    """GET direct ou via ScraperAPI si proxy=True et clé disponible."""
    try:
        _sleep()
        if proxy and SCRAPER_API_KEY:
            r = requests.get(
                f"http://api.scraperapi.com?api_key={SCRAPER_API_KEY}&url={quote_plus(url)}&render=false",
                timeout=timeout + 10,
            )
        else:
            r = requests.get(url, headers=headers, timeout=timeout)
        if r.status_code == 200:
            r.encoding = "utf-8"
            return BeautifulSoup(r.text, "html.parser")
    except Exception:
        pass
    return None


def _proxied_post(url: str, headers: dict, payload: dict, timeout: int = 20) -> requests.Response | None:
    try:
        _sleep()
        if SCRAPER_API_KEY:
            r = requests.post(
                f"http://api.scraperapi.com?api_key={SCRAPER_API_KEY}&url={quote_plus(url)}&render=false",
                headers=headers, json=payload, timeout=timeout,
            )
        else:
            r = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if r.status_code == 200:
            r.encoding = "utf-8"
            return r
    except Exception:
        pass
    return None


def _get_json(url: str, headers: dict, payload: dict, timeout: int = 12) -> dict | None:
    try:
        _sleep()
        r = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _extract_next_data(soup: BeautifulSoup) -> dict | None:
    tag = soup.find("script", {"id": "__NEXT_DATA__"})
    if tag and tag.string:
        try:
            return json.loads(tag.string)
        except Exception:
            pass
    return None


def score_vehicle(price: int, km: int, year: int, price_max: int, km_max: int, year_min: int) -> int:
    score = 100
    if price_max > 0 and price > 0:
        ratio = price / price_max
        if ratio < 0.6:
            score += 35
        elif ratio < 0.75:
            score += 20
        elif ratio < 0.88:
            score += 10
    if km > 0:
        if km < 30000:
            score += 25
        elif km < 60000:
            score += 18
        elif km < 100000:
            score += 8
        elif km > 150000:
            score -= 20
        elif km > 180000:
            score -= 35
    if year >= 2022:
        score += 25
    elif year >= 2020:
        score += 18
    elif year >= 2018:
        score += 10
    elif year >= 2016:
        score += 4
    elif year > 0 and year < year_min:
        score -= 15
    return max(0, min(200, score))


# ── LeBonCoin ─────────────────────────────────────────────────────────────────

def _parse_lbc_ads(ads: list, alert: dict, brand: str, model: str) -> list:
    results = []
    price_max = alert.get("price_max", 50000)
    km_max    = alert.get("km_max", 200000)
    year_min  = alert.get("year_min", 2010)
    for ad in ads[:20]:
        try:
            url   = ad.get("url") or ad.get("link") or ""
            title = ad.get("subject") or ad.get("title") or ""
            price_list = ad.get("price") or [0]
            price = int(price_list[0]) if isinstance(price_list, list) else int(price_list or 0)

            attrs = {a["key"]: a.get("value_label") or a.get("value", "") for a in (ad.get("attributes") or [])}
            km_raw = attrs.get("mileage", "0")
            km = int(re.sub(r"[^\d]", "", str(km_raw)) or 0)
            if km > 999999:
                km = 0
            year_raw = attrs.get("regdate", "")
            year_m = re.search(r"(\d{4})", str(year_raw))
            year = int(year_m.group(1)) if year_m else 0

            location_obj = ad.get("location") or {}
            location = location_obj.get("city", "") or location_obj.get("department_id", "")

            images = ad.get("images") or {}
            image_url = ""
            if isinstance(images, dict):
                image_url = images.get("small_url") or (images.get("urls") or [""])[0]
            elif isinstance(images, list):
                image_url = images[0] if images else ""

            if not url.startswith("http"):
                url = "https://www.leboncoin.fr" + url

            if price == 0 or price > price_max * 1.1:
                continue

            results.append({
                "alert_id":  alert["id"],
                "user_id":   alert["user_id"],
                "source":    "leboncoin",
                "url":       url,
                "title":     title,
                "price":     price,
                "km":        km,
                "year":      year,
                "brand":     brand,
                "model":     model,
                "location":  str(location),
                "image_url": image_url,
                "score":     score_vehicle(price, km, year, price_max, km_max, year_min),
            })
        except Exception:
            continue
    return results


def scrape_leboncoin(alert: dict) -> list:
    brand     = alert.get("brand", "")
    model     = alert.get("model", "")
    price_max = alert.get("price_max", 50000)
    km_max    = alert.get("km_max", 200000)
    year_min  = alert.get("year_min", 2010)
    keyword   = " ".join(filter(None, [brand, model])).strip()

    # ── Tentative 1 : API interne JSON ──────────────────────────────────────────
    payload = {
        "limit": 35,
        "limit_alu": 3,
        "offset": 0,
        "filters": {
            "category": {"id": "2"},
            "enums": {"ad_type": ["offer"]},
            "ranges": {
                "price": {"max": str(price_max)},
                "mileage": {"max": str(km_max)},
                "regdate": {"min": str(year_min)},
            },
            "keywords": {"text": keyword} if keyword else {},
        },
        "sort_by": "price",
        "sort_order": "asc",
    }
    data = _get_json("https://api.leboncoin.fr/api/frontend/v4/search", LBC_HEADERS, payload)
    if data:
        ads = data.get("ads") or []
        if ads:
            return _parse_lbc_ads(ads, alert, brand, model)

    # ── Tentative 2 : page HTML publique + __NEXT_DATA__ ────────────────────────
    search_url = (
        f"https://www.leboncoin.fr/recherche?category=2"
        f"&text={quote_plus(keyword)}"
        f"&price=0-{price_max}"
        f"&mileage=0-{km_max}"
        f"&regdate={year_min}-max"
        f"&sort=time&order=desc"
    )
    html_headers = {
        **LBC_HEADERS,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    soup = _get_html(search_url, html_headers, proxy=True)
    if not soup:
        return []

    # Essai __NEXT_DATA__
    nd = _extract_next_data(soup)
    if nd:
        ads = (
            nd.get("props", {}).get("pageProps", {}).get("searchData", {}).get("ads") or
            nd.get("props", {}).get("pageProps", {}).get("ads") or
            []
        )
        if ads:
            return _parse_lbc_ads(ads, alert, brand, model)

    # Essai window.__STORE__ ou FLUX_STATE
    for script in soup.find_all("script"):
        txt = script.string or ""
        for marker in ["window.__STORE__=", "window.FLUX_STATE=", "__DATA__="]:
            if marker in txt:
                try:
                    raw = txt[txt.index(marker) + len(marker):]
                    raw = raw.split(";\n")[0].split("</script>")[0]
                    store = json.loads(raw)
                    ads = (
                        store.get("searchResults", {}).get("ads") or
                        store.get("ads") or []
                    )
                    if ads:
                        return _parse_lbc_ads(ads, alert, brand, model)
                except Exception:
                    continue

    # Fallback CSS : lire les annonces depuis la page HTML
    results = []
    km_re = re.compile(r'\b(\d{1,3}(?:[\s.]\d{3})+|\d{3,6})\s*km\b', re.I)
    year_re = re.compile(r'\b(20[01]\d|19[89]\d)\b')
    price_re = re.compile(r'(\d[\d\s]*)\s*€')

    for card in soup.select('a[href*="/voitures/"]')[:25]:
        try:
            href = card.get("href", "")
            if not href:
                continue
            ad_url = href if href.startswith("http") else "https://www.leboncoin.fr" + href
            text = card.get_text(" ", strip=True)

            p_m = price_re.search(text)
            price = int(re.sub(r"\s", "", p_m.group(1))) if p_m else 0
            if price == 0 or price > price_max * 1.1:
                continue

            km_m = km_re.search(text)
            km = int(re.sub(r"[\s.]", "", km_m.group(1))) if km_m else 0
            if km > 999999:
                km = 0

            yr_m = year_re.search(text)
            year = int(yr_m.group(1)) if yr_m else 0

            img = card.find("img")
            image_url = img.get("src", "") if img else ""
            title_el = card.find(attrs={"data-qa-id": "aditem_title"}) or card.find("h2") or card.find("p")
            title = title_el.get_text(strip=True) if title_el else keyword

            results.append({
                "alert_id":  alert["id"],
                "user_id":   alert["user_id"],
                "source":    "leboncoin",
                "url":       ad_url,
                "title":     title,
                "price":     price,
                "km":        km,
                "year":      year,
                "brand":     brand,
                "model":     model,
                "location":  "",
                "image_url": image_url,
                "score":     score_vehicle(price, km, year, price_max, km_max, year_min),
            })
        except Exception:
            continue

    return results


# ── AutoScout24 (__NEXT_DATA__ JSON) ───────────────────────────────────────────

REGION_CONFIG = {
    "France":      {"cy": "F",  "domain": "autoscout24.fr",  "lbc": True,  "lacentrale": True, "largus": True},
    "Belgique":    {"cy": "B",  "domain": "autoscout24.be",  "lbc": False, "lacentrale": False, "largus": False},
    "Suisse":      {"cy": "CH", "domain": "autoscout24.ch",  "lbc": False, "lacentrale": False, "largus": False},
    "Luxembourg":  {"cy": "L",  "domain": "autoscout24.lu",  "lbc": False, "lacentrale": False, "largus": False},
}

def _region_cfg(alert: dict) -> dict:
    region = alert.get("region", "France")
    for key in REGION_CONFIG:
        if key.lower() in region.lower():
            return REGION_CONFIG[key]
    return REGION_CONFIG["France"]

def scrape_autoscout24(alert: dict) -> list:
    results = []
    brand     = alert.get("brand", "").lower()
    model     = alert.get("model", "").lower().replace(" ", "-")
    price_max = alert.get("price_max", 50000)
    km_max    = alert.get("km_max", 200000)
    year_min  = alert.get("year_min", 2010)
    cfg       = _region_cfg(alert)

    zip_code  = alert.get("zip", "").strip()
    radius_km = alert.get("radius_km", 0)
    location_params = f"&zip={zip_code}&zipr={radius_km}" if zip_code and radius_km else ""

    url = (
        f"https://www.{cfg['domain']}/lst/{brand}/{model}"
        f"?sort=price&desc=0&ustate=N%2CU&size=20&page=1"
        f"&fregfrom={year_min}&kmto={km_max}&priceto={price_max}&cy={cfg['cy']}&atype=C"
        f"{location_params}"
    )

    soup = _get_html(url, AS24_HEADERS)
    if not soup:
        return results

    # Tente extraction __NEXT_DATA__
    # Structure réelle : pageProps.listings = liste directe d'annonces
    # Chaque annonce : {id, url, price, vehicle, location, images, ...}
    nd = _extract_next_data(soup)
    if nd:
        try:
            pp = nd.get("props", {}).get("pageProps", {})
            raw_listings = pp.get("listings") or []
            # Au cas où c'est un dict avec sous-clé
            if isinstance(raw_listings, dict):
                raw_listings = raw_listings.get("ads") or raw_listings.get("items") or []

            for ad in raw_listings[:20]:
                try:
                    ad_url = ad.get("url") or ad.get("listingUrl") or ad.get("link") or ""

                    vehicle = ad.get("vehicle") or {}
                    make  = vehicle.get("make") or ""
                    model_v = vehicle.get("modelVersionInput") or vehicle.get("model") or vehicle.get("modelGroup") or ""
                    title = f"{make} {model_v}".strip() or "BMW"

                    # price.priceFormatted = "€ 7 990" — pas de champ numérique
                    price_raw = ad.get("price") or {}
                    if isinstance(price_raw, dict):
                        fmt = price_raw.get("priceFormatted") or price_raw.get("value") or price_raw.get("amount") or "0"
                        price = int(re.sub(r"[^\d]", "", str(fmt)) or 0)
                    else:
                        price = int(re.sub(r"[^\d]", "", str(price_raw)) or 0)

                    # vehicle.mileageInKm = "172 000 km"
                    km_raw = vehicle.get("mileageInKm") or vehicle.get("mileage") or ad.get("mileage") or "0"
                    km = int(re.sub(r"[^\d]", "", str(km_raw)) or 0)
                    if km > 999999:
                        km = 0

                    # year: tracking.firstRegistration = "09-2016"
                    tracking = ad.get("tracking") or {}
                    first_reg = tracking.get("firstRegistration") or vehicle.get("firstRegistration") or ""
                    yr_m = re.search(r"(20\d{2}|19\d{2})", str(first_reg))
                    year = int(yr_m.group(1)) if yr_m else 0

                    loc = ad.get("location") or {}
                    location = loc.get("city") or loc.get("zip") or "" if isinstance(loc, dict) else str(loc)

                    # images = liste de strings directement
                    imgs = ad.get("images") or []
                    if imgs:
                        first_img = imgs[0]
                        image_url = first_img if isinstance(first_img, str) else (first_img.get("url") or "")
                    else:
                        image_url = ""

                    if not ad_url.startswith("http"):
                        ad_url = f"https://www.{cfg['domain']}" + ad_url
                    if price == 0 or price > price_max * 1.1:
                        continue

                    results.append({
                        "alert_id":  alert["id"],
                        "user_id":   alert["user_id"],
                        "source":    "autoscout24",
                        "url":       ad_url,
                        "title":     title,
                        "price":     price,
                        "km":        km,
                        "year":      year,
                        "brand":     brand,
                        "model":     model,
                        "location":  str(location),
                        "image_url": str(image_url),
                        "score":     score_vehicle(price, km, year, price_max, km_max, year_min),
                    })
                except Exception:
                    continue
            if results:
                return results
        except Exception:
            pass

    # Fallback : parser HTML classique avec meilleurs sélecteurs
    articles = soup.select("article[class*='ListItem']") or soup.select("article")
    for card in articles[:20]:
        try:
            # AutoScout24 uses /offres/ for listings, NOT /annonces/
            link = (card.select_one("a[href*='/offres/']") or
                    card.select_one("a[href*='/annonces/']") or
                    card.select_one("a[href*='autoscout24']"))
            if not link:
                continue
            href = link["href"]
            if not href.startswith("http"):
                href = f"https://www.{cfg['domain']}" + href
            # Skip dealer/seller pages
            if "/offres/" not in href and "/annonces/" not in href:
                continue

            title_el = card.select_one("h2") or card.select_one("[class*='title']")
            title    = title_el.get_text(strip=True) if title_el else ""

            price_el = card.select_one("[class*='Price']") or card.select_one("[data-testid='price-label']")
            price_t  = price_el.get_text(strip=True) if price_el else "0"
            price    = int(re.sub(r"[^\d]", "", price_t) or 0)

            text   = card.get_text(" ", strip=True)
            # Regex précis : "126 895 km" ou "126895 km" — jamais l'année
            km_m   = re.search(r'\b(\d{1,3}(?:[\s.]\d{3})+|\d{3,6})\s*km\b', text, re.I)
            km     = int(re.sub(r'[\s.]', '', km_m.group(1))) if km_m else 0
            if km > 999999: km = 0
            yr_m   = re.search(r"(20\d{2}|19\d{2})", text)
            year   = int(yr_m.group()) if yr_m else 0

            img    = card.select_one("img")
            image_url = img.get("src") or img.get("data-src") or "" if img else ""

            loc_el = card.select_one("[class*='location']")
            location = loc_el.get_text(strip=True) if loc_el else ""

            if price == 0 or price > price_max * 1.1:
                continue

            results.append({
                "alert_id":  alert["id"],
                "user_id":   alert["user_id"],
                "source":    "autoscout24",
                "url":       href,
                "title":     title,
                "price":     price,
                "km":        km,
                "year":      year,
                "brand":     brand,
                "model":     model,
                "location":  location,
                "image_url": image_url,
                "score":     score_vehicle(price, km, year, price_max, km_max, year_min),
            })
        except Exception:
            continue

    return results


# ── La Centrale (__NEXT_DATA__ JSON) ───────────────────────────────────────────

def scrape_lacentrale(alert: dict) -> list:
    results = []
    brand     = quote_plus(alert.get("brand", ""))
    model     = quote_plus(alert.get("model", ""))
    price_max = alert.get("price_max", 50000)
    km_max    = alert.get("km_max", 200000)
    year_min  = alert.get("year_min", 2010)

    url = (
        f"https://www.lacentrale.fr/listing"
        f"?makesModelsCommercialNames={brand}%3A{model}"
        f"&priceMax={price_max}&mileageMax={km_max}&yearMin={year_min}&sortBy=priceAsc"
    )

    soup = _get_html(url, LC_HEADERS, proxy=True)
    if not soup:
        return results

    # Tente __NEXT_DATA__
    nd = _extract_next_data(soup)
    if nd:
        try:
            vehicles = (
                nd.get("props", {}).get("pageProps", {}).get("vehicles") or
                nd.get("props", {}).get("pageProps", {}).get("ads") or
                nd.get("props", {}).get("pageProps", {}).get("searchResults", {}).get("vehicles") or []
            )
            for v in vehicles[:20]:
                try:
                    v_url  = v.get("url") or v.get("adUrl") or ""
                    title  = v.get("title") or v.get("description") or f"{v.get('make','')} {v.get('model','')}".strip()
                    price  = int(v.get("price") or v.get("priceHT") or 0)
                    km     = int(v.get("mileage") or v.get("km") or 0)
                    year   = int(v.get("year") or v.get("registrationYear") or 0)
                    location = v.get("location") or v.get("department") or v.get("city") or ""
                    image_url = v.get("photo") or v.get("image") or v.get("photoUrl") or ""

                    if not v_url.startswith("http"):
                        v_url = "https://www.lacentrale.fr" + v_url
                    if price == 0 or price > price_max * 1.1:
                        continue

                    results.append({
                        "alert_id":  alert["id"],
                        "user_id":   alert["user_id"],
                        "source":    "lacentrale",
                        "url":       v_url,
                        "title":     title,
                        "price":     price,
                        "km":        km,
                        "year":      year,
                        "brand":     alert.get("brand", ""),
                        "model":     alert.get("model", ""),
                        "location":  str(location),
                        "image_url": str(image_url),
                        "score":     score_vehicle(price, km, year, price_max, km_max, year_min),
                    })
                except Exception:
                    continue
            if results:
                return results
        except Exception:
            pass

    # Fallback HTML — sélecteurs robustes
    # La Centrale embed parfois les données dans un script window.__STORE__
    for script in soup.find_all("script"):
        text = script.string or ""
        if "window.__STORE__" in text or '"vehicles"' in text:
            try:
                match = re.search(r"window\.__STORE__\s*=\s*({.+?});?\s*</", text, re.S)
                if not match:
                    match = re.search(r'"vehicles"\s*:\s*(\[.+?\])', text, re.S)
                if match:
                    raw = match.group(1)
                    vehicles = json.loads(raw) if raw.startswith("[") else json.loads(match.group(0).split(":", 1)[1].strip().rstrip(";"))
                    for v in (vehicles if isinstance(vehicles, list) else []):
                        try:
                            v_url  = v.get("url") or ""
                            title  = v.get("title") or ""
                            price  = int(v.get("price") or 0)
                            km     = int(v.get("mileage") or v.get("km") or 0)
                            year   = int(v.get("year") or 0)
                            location = str(v.get("location") or "")
                            image_url = str(v.get("photo") or "")
                            if not v_url.startswith("http"):
                                v_url = "https://www.lacentrale.fr" + v_url
                            if price > 0:
                                results.append({
                                    "alert_id":  alert["id"],
                                    "user_id":   alert["user_id"],
                                    "source":    "lacentrale",
                                    "url":       v_url,
                                    "title":     title,
                                    "price":     price,
                                    "km":        km,
                                    "year":      year,
                                    "brand":     alert.get("brand", ""),
                                    "model":     alert.get("model", ""),
                                    "location":  location,
                                    "image_url": image_url,
                                    "score":     score_vehicle(price, km, year, price_max, km_max, year_min),
                                })
                        except Exception:
                            continue
                    if results:
                        return results
            except Exception:
                pass

    # Dernier recours : sélecteurs CSS
    cards = soup.select("[class*='searchCard']") or soup.select(".listing_item") or soup.select("article")
    for card in cards[:20]:
        try:
            link = card.select_one("a[href]")
            if not link:
                continue
            href = link["href"]
            if not href.startswith("http"):
                href = "https://www.lacentrale.fr" + href

            title_el = card.select_one("h2") or card.select_one("[class*='title']")
            title    = title_el.get_text(strip=True) if title_el else ""

            price_el = card.select_one("[class*='price']") or card.select_one("[class*='Price']")
            price_t  = price_el.get_text(strip=True) if price_el else "0"
            price    = int(re.sub(r"[^\d]", "", price_t) or 0)

            text   = card.get_text(" ", strip=True)
            km_m   = re.search(r"(\d[\d\s]*)\s*km", text, re.I)
            km     = int(re.sub(r"\s", "", km_m.group(1))) if km_m else 0
            yr_m   = re.search(r"(20\d{2}|19\d{2})", text)
            year   = int(yr_m.group()) if yr_m else 0

            img    = card.select_one("img")
            image_url = img.get("src") or "" if img else ""

            loc_el = card.select_one("[class*='location']") or card.select_one("[class*='dept']")
            location = loc_el.get_text(strip=True) if loc_el else ""

            if price == 0 or price > price_max * 1.1:
                continue

            results.append({
                "alert_id":  alert["id"],
                "user_id":   alert["user_id"],
                "source":    "lacentrale",
                "url":       href,
                "title":     title,
                "price":     price,
                "km":        km,
                "year":      year,
                "brand":     alert.get("brand", ""),
                "model":     alert.get("model", ""),
                "location":  location,
                "image_url": image_url,
                "score":     score_vehicle(price, km, year, price_max, km_max, year_min),
            })
        except Exception:
            continue

    return results


# ── L'Argus (annonces.largus.fr — __NEXT_DATA__) ──────────────────────────────

ARGUS_HEADERS = {
    **BASE_HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.largus.fr/",
}

def scrape_largus(alert: dict) -> list:
    results = []
    brand     = quote_plus(alert.get("brand", ""))
    model     = quote_plus(alert.get("model", ""))
    price_max = alert.get("price_max", 50000)
    km_max    = alert.get("km_max", 200000)
    year_min  = alert.get("year_min", 2010)

    url = (
        f"https://www.largus.fr/annonces-voitures/{brand.lower()}-{model.lower().replace('+', '-')}.html"
        f"?prix_max={price_max}&km_max={km_max}&annee_min={year_min}&tri=prix_croissant"
    )

    soup = _get_html(url, ARGUS_HEADERS)
    if not soup:
        return results

    nd = _extract_next_data(soup)
    if nd:
        try:
            pp = nd.get("props", {}).get("pageProps", {})
            ads = pp.get("ads") or pp.get("vehicles") or pp.get("listings") or []
            if isinstance(ads, dict):
                ads = ads.get("items") or ads.get("results") or []
            for ad in ads[:20]:
                try:
                    ad_url = ad.get("url") or ad.get("link") or ""
                    title  = ad.get("title") or f"{alert.get('brand','')} {alert.get('model','')}".strip()
                    price  = int(re.sub(r"[^\d]", "", str(ad.get("price") or "0")) or 0)
                    km     = int(re.sub(r"[^\d]", "", str(ad.get("mileage") or ad.get("km") or "0")) or 0)
                    if km > 999999: km = 0
                    yr_m   = re.search(r"(20\d{2}|19\d{2})", str(ad.get("year") or ad.get("firstRegistration") or ""))
                    year   = int(yr_m.group(1)) if yr_m else 0
                    location = str(ad.get("location") or ad.get("city") or ad.get("department") or "")
                    image_url = str(ad.get("image") or ad.get("photo") or ad.get("thumbnail") or "")
                    if not ad_url.startswith("http"):
                        ad_url = "https://www.largus.fr" + ad_url
                    if price == 0 or price > price_max * 1.1:
                        continue
                    results.append({
                        "alert_id":  alert["id"],
                        "user_id":   alert["user_id"],
                        "source":    "largus",
                        "url":       ad_url,
                        "title":     title,
                        "price":     price,
                        "km":        km,
                        "year":      year,
                        "brand":     alert.get("brand", ""),
                        "model":     alert.get("model", ""),
                        "location":  location,
                        "image_url": image_url,
                        "score":     score_vehicle(price, km, year, price_max, km_max, year_min),
                    })
                except Exception:
                    continue
            if results:
                return results
        except Exception:
            pass

    # Fallback CSS
    km_re = re.compile(r'\b(\d{1,3}(?:[\s.]\d{3})+|\d{3,6})\s*km\b', re.I)
    year_re = re.compile(r'\b(20[01]\d|19[89]\d)\b')
    price_re = re.compile(r'(\d[\d\s]*)\s*€')
    for card in (soup.select(".announcement-item") or soup.select("article") or soup.select("[class*='card']"))[:20]:
        try:
            link = card.select_one("a[href]")
            if not link: continue
            href = link["href"]
            if not href.startswith("http"): href = "https://www.largus.fr" + href
            text = card.get_text(" ", strip=True)
            p_m = price_re.search(text)
            price = int(re.sub(r"\s", "", p_m.group(1))) if p_m else 0
            if price == 0 or price > price_max * 1.1: continue
            km_m = km_re.search(text)
            km = int(re.sub(r"[\s.]", "", km_m.group(1))) if km_m else 0
            if km > 999999: km = 0
            yr_m = year_re.search(text)
            year = int(yr_m.group(1)) if yr_m else 0
            img = card.select_one("img")
            image_url = img.get("src") or img.get("data-src") or "" if img else ""
            results.append({
                "alert_id":  alert["id"],
                "user_id":   alert["user_id"],
                "source":    "largus",
                "url":       href,
                "title":     f"{alert.get('brand','')} {alert.get('model','')}".strip(),
                "price":     price, "km": km, "year": year,
                "brand":     alert.get("brand", ""),
                "model":     alert.get("model", ""),
                "location":  "", "image_url": image_url,
                "score":     score_vehicle(price, km, year, price_max, km_max, year_min),
            })
        except Exception:
            continue
    return results


# ── Filtre pertinence ──────────────────────────────────────────────────────────

def _is_relevant(v: dict, alert: dict) -> bool:
    title = v.get("title", "").lower()
    brand = alert.get("brand", "").lower().strip()
    model = alert.get("model", "").lower().strip()

    # La marque doit apparaître dans le titre
    if brand and brand.split()[0] not in title:
        return False

    # Si le modèle contient un numéro (ex: "3" dans "Serie 3"),
    # vérifier qu'un numéro de série cohérent apparaît dans le titre
    model_num = re.search(r'\b(\d+)\b', model)
    if model_num:
        n = model_num.group(1)
        # Accepte : "3", "320", "318d", "330e", "3er", "serie 3" etc.
        if not re.search(rf'\b{n}\d*\b|\b\d*{re.escape(n)}\b', title):
            return False

    return True


# ── Runner ─────────────────────────────────────────────────────────────────────

def run_alert(alert: dict) -> list:
    cfg = _region_cfg(alert)
    scrapers = [scrape_autoscout24]
    if cfg["lbc"]:
        scrapers.append(scrape_leboncoin)
    if cfg["lacentrale"]:
        scrapers.append(scrape_lacentrale)
    if cfg["largus"]:
        scrapers.append(scrape_largus)

    all_results = []
    for fn in scrapers:
        try:
            r = fn(alert)
            print(f"    {fn.__name__}: {len(r)} résultats")
            all_results.extend(r)
        except Exception as e:
            print(f"    {fn.__name__} erreur: {e}")

    seen, unique = set(), []
    for v in all_results:
        if v["url"] not in seen and _is_relevant(v, alert):
            seen.add(v["url"])
            unique.append(v)

    unique.sort(key=lambda x: x["score"], reverse=True)
    return unique
