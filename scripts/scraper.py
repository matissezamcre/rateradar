"""
RateRadar scraper v2 — API internes + JSON embarqué
- LeBonCoin  : API interne JSON (api.leboncoin.fr)
- AutoScout24 : __NEXT_DATA__ JSON embarqué dans la page
- La Centrale : __NEXT_DATA__ ou données JSON script
Aucune dépendance payante.
"""

import re
import json
import time
import random
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus

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


def _get_html(url: str, headers: dict, timeout: int = 12) -> BeautifulSoup | None:
    try:
        _sleep()
        r = requests.get(url, headers=headers, timeout=timeout)
        if r.status_code == 200:
            return BeautifulSoup(r.text, "html.parser")
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
    soup = _get_html(search_url, html_headers)
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

def scrape_autoscout24(alert: dict) -> list:
    results = []
    brand     = alert.get("brand", "").lower()
    model     = alert.get("model", "").lower().replace(" ", "-")
    price_max = alert.get("price_max", 50000)
    km_max    = alert.get("km_max", 200000)
    year_min  = alert.get("year_min", 2010)

    url = (
        f"https://www.autoscout24.fr/lst/{brand}/{model}"
        f"?sort=price&desc=0&ustate=N%2CU&size=20&page=1"
        f"&fregfrom={year_min}&kmto={km_max}&priceto={price_max}&cy=F&atype=C"
    )

    soup = _get_html(url, AS24_HEADERS)
    if not soup:
        return results

    # Tente extraction __NEXT_DATA__
    nd = _extract_next_data(soup)
    if nd:
        try:
            listings = (
                nd.get("props", {})
                  .get("pageProps", {})
                  .get("listings", {})
                  .get("ads") or
                nd.get("props", {})
                  .get("pageProps", {})
                  .get("initialState", {})
                  .get("listings", {})
                  .get("data", {})
                  .get("ads") or []
            )
            for ad in listings[:20]:
                try:
                    ad_url   = ad.get("url") or ad.get("listingUrl") or ad.get("link") or ""
                    title    = ad.get("title") or f"{ad.get('make','')} {ad.get('model','')}".strip()
                    price    = int(ad.get("price") or ad.get("pricing", {}).get("amount") or 0)
                    km_raw   = ad.get("mileage") or ad.get("mileageInKm") or ad.get("km") or 0
                    km       = int(re.sub(r"[^\d]", "", str(km_raw)) or 0)
                    if km > 999999: km = 0
                    year_raw = ad.get("firstRegistrationYear") or ad.get("firstRegYear") or ad.get("year") or 0
                    year     = int(str(year_raw)[:4]) if year_raw else 0
                    location = ad.get("location") or ad.get("seller", {}).get("city") or ""
                    imgs     = ad.get("images") or ad.get("media", {}).get("photos") or []
                    image_url = imgs[0].get("url") or imgs[0].get("src") or "" if imgs else ""

                    if not ad_url.startswith("http"):
                        ad_url = "https://www.autoscout24.fr" + ad_url
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
                href = "https://www.autoscout24.fr" + href
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

    soup = _get_html(url, LC_HEADERS)
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


# ── Runner ─────────────────────────────────────────────────────────────────────

def run_alert(alert: dict) -> list:
    all_results = []
    for fn in [scrape_leboncoin, scrape_autoscout24, scrape_lacentrale]:
        try:
            r = fn(alert)
            print(f"    {fn.__name__}: {len(r)} résultats")
            all_results.extend(r)
        except Exception as e:
            print(f"    {fn.__name__} erreur: {e}")

    seen, unique = set(), []
    for v in all_results:
        if v["url"] not in seen:
            seen.add(v["url"])
            unique.append(v)

    unique.sort(key=lambda x: x["score"], reverse=True)
    return unique
