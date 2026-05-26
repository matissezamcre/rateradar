"""
RateRadar scraper — LeBonCoin + La Centrale + AutoScout24
Runs as a background job or standalone script.
"""

import re
import time
import random
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlencode, quote_plus

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def get(url: str, timeout: int = 10) -> BeautifulSoup | None:
    try:
        time.sleep(random.uniform(1.0, 2.5))
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code == 200:
            return BeautifulSoup(r.text, "html.parser")
    except Exception:
        pass
    return None


def score_vehicle(price: int, km: int, year: int, price_max: int, km_max: int, year_min: int) -> int:
    score = 100
    if price_max > 0 and price > 0:
        ratio = price / price_max
        if ratio < 0.7:
            score += 30
        elif ratio < 0.85:
            score += 15
    if km > 0:
        if km < 50000:
            score += 20
        elif km < 100000:
            score += 10
        elif km > 150000:
            score -= 20
    if year >= 2020:
        score += 20
    elif year >= 2018:
        score += 10
    elif year < year_min:
        score -= 15
    return max(0, min(200, score))


def scrape_lacentrale(alert: dict) -> list:
    results = []
    brand = quote_plus(alert.get('brand', ''))
    model = quote_plus(alert.get('model', ''))
    price_max = alert.get('price_max', 50000)
    km_max = alert.get('km_max', 200000)
    year_min = alert.get('year_min', 2010)

    url = f"https://www.lacentrale.fr/listing?makesModelsCommercialNames={brand}%3A{model}&priceMax={price_max}&mileageMax={km_max}&yearMin={year_min}&sortBy=priceAsc"
    soup = get(url)
    if not soup:
        return results

    cards = soup.select("[class*='searchCard']") or soup.select(".listing_item") or soup.select("article")
    for card in cards[:15]:
        try:
            title_el = card.select_one("h2") or card.select_one("[class*='title']") or card.select_one("a")
            price_el = card.select_one("[class*='price']") or card.select_one("[class*='Price']")
            link_el = card.select_one("a[href]")

            if not link_el:
                continue

            href = link_el.get('href', '')
            if not href.startswith('http'):
                href = 'https://www.lacentrale.fr' + href

            title = title_el.get_text(strip=True) if title_el else ''
            price_text = price_el.get_text(strip=True) if price_el else '0'
            price = int(re.sub(r'[^\d]', '', price_text) or 0)

            km_el = card.select_one("[class*='km']") or card.select_one("[class*='mileage']")
            km_text = km_el.get_text(strip=True) if km_el else '0'
            km = int(re.sub(r'[^\d]', '', km_text) or 0)

            year_match = re.search(r'20\d{2}|19\d{2}', title)
            year = int(year_match.group()) if year_match else year_min

            img_el = card.select_one("img")
            image_url = img_el.get('src', '') if img_el else ''

            loc_el = card.select_one("[class*='location']") or card.select_one("[class*='dept']")
            location = loc_el.get_text(strip=True) if loc_el else ''

            if price == 0 or price > price_max * 1.1:
                continue

            results.append({
                'alert_id': alert['id'],
                'user_id': alert['user_id'],
                'source': 'lacentrale',
                'url': href,
                'title': title,
                'price': price,
                'km': km,
                'year': year,
                'brand': alert.get('brand', ''),
                'model': alert.get('model', ''),
                'location': location,
                'image_url': image_url,
                'score': score_vehicle(price, km, year, price_max, km_max, year_min),
            })
        except Exception:
            continue

    return results


def scrape_autoscout24(alert: dict) -> list:
    results = []
    brand = alert.get('brand', '').lower()
    model = alert.get('model', '').lower().replace(' ', '-')
    price_max = alert.get('price_max', 50000)
    km_max = alert.get('km_max', 200000)
    year_min = alert.get('year_min', 2010)

    url = f"https://www.autoscout24.fr/lst/{brand}/{model}?sort=price&desc=0&ustate=N%2CU&size=20&page=1&fregfrom={year_min}&kmto={km_max}&priceto={price_max}&cy=F&atype=C"
    soup = get(url)
    if not soup:
        return results

    cards = soup.select("article[class*='ListItem']") or soup.select("[data-testid='regular-ad']") or soup.select("article")
    for card in cards[:15]:
        try:
            title_el = card.select_one("h2") or card.select_one("[class*='title']")
            price_el = card.select_one("[class*='Price']") or card.select_one("[data-testid='price-label']")
            link_el = card.select_one("a[href*='/annonces/']") or card.select_one("a")

            if not link_el:
                continue

            href = link_el.get('href', '')
            if not href.startswith('http'):
                href = 'https://www.autoscout24.fr' + href

            title = title_el.get_text(strip=True) if title_el else ''
            price_text = price_el.get_text(strip=True) if price_el else '0'
            price = int(re.sub(r'[^\d]', '', price_text) or 0)

            details = card.get_text(' ', strip=True)
            km_match = re.search(r'(\d[\d\s]*)\s*km', details, re.IGNORECASE)
            km = int(re.sub(r'\s', '', km_match.group(1))) if km_match else 0

            year_match = re.search(r'(20\d{2}|19\d{2})', details)
            year = int(year_match.group()) if year_match else year_min

            img_el = card.select_one("img")
            image_url = img_el.get('src', '') if img_el else ''

            loc_el = card.select_one("[class*='location']") or card.select_one("[data-testid='dealer-name']")
            location = loc_el.get_text(strip=True) if loc_el else ''

            if price == 0 or price > price_max * 1.1:
                continue

            results.append({
                'alert_id': alert['id'],
                'user_id': alert['user_id'],
                'source': 'autoscout24',
                'url': href,
                'title': title,
                'price': price,
                'km': km,
                'year': year,
                'brand': alert.get('brand', ''),
                'model': alert.get('model', ''),
                'location': location,
                'image_url': image_url,
                'score': score_vehicle(price, km, year, price_max, km_max, year_min),
            })
        except Exception:
            continue

    return results


def scrape_leboncoin(alert: dict) -> list:
    results = []
    brand = alert.get('brand', '')
    model = alert.get('model', '')
    price_max = alert.get('price_max', 50000)
    km_max = alert.get('km_max', 200000)
    year_min = alert.get('year_min', 2010)

    params = {
        'category': '2',
        'brand': brand,
        'model': model,
        'price': f'0-{price_max}',
        'mileage': f'0-{km_max}',
        'regdate': f'{year_min}-',
        'sort': 'price',
        'order': 'asc',
    }
    url = f"https://www.leboncoin.fr/recherche?{urlencode(params)}"
    soup = get(url)
    if not soup:
        return results

    cards = soup.select("a[data-test-id='ad']") or soup.select("[data-qa-id='aditem_container']") or soup.select("article")
    for card in cards[:15]:
        try:
            href = card.get('href', '')
            if not href.startswith('http'):
                href = 'https://www.leboncoin.fr' + href

            title_el = card.select_one("p[class*='title']") or card.select_one("h3") or card.select_one("p")
            price_el = card.select_one("[class*='price']") or card.select_one("[data-qa-id='aditem_price']")

            title = title_el.get_text(strip=True) if title_el else ''
            price_text = price_el.get_text(strip=True) if price_el else '0'
            price = int(re.sub(r'[^\d]', '', price_text) or 0)

            details = card.get_text(' ', strip=True)
            km_match = re.search(r'(\d[\d\s]*)\s*km', details, re.IGNORECASE)
            km = int(re.sub(r'\s', '', km_match.group(1))) if km_match else 0

            year_match = re.search(r'(20\d{2}|19\d{2})', details)
            year = int(year_match.group()) if year_match else year_min

            img_el = card.select_one("img")
            image_url = img_el.get('src', '') if img_el else ''

            loc_el = card.select_one("[class*='location']") or card.select_one("[data-qa-id='aditem_location']")
            location = loc_el.get_text(strip=True) if loc_el else ''

            if price == 0 or price > price_max * 1.1:
                continue

            results.append({
                'alert_id': alert['id'],
                'user_id': alert['user_id'],
                'source': 'leboncoin',
                'url': href,
                'title': title,
                'price': price,
                'km': km,
                'year': year,
                'brand': brand,
                'model': model,
                'location': location,
                'image_url': image_url,
                'score': score_vehicle(price, km, year, price_max, km_max, year_min),
            })
        except Exception:
            continue

    return results


def run_alert(alert: dict) -> list:
    """Run all scrapers for one alert, return new vehicles found."""
    all_results = []
    for scrape_fn in [scrape_lacentrale, scrape_autoscout24, scrape_leboncoin]:
        try:
            results = scrape_fn(alert)
            all_results.extend(results)
        except Exception as e:
            print(f"  ⚠ Scraper error: {e}")
    seen_urls = set()
    unique = []
    for v in all_results:
        if v['url'] not in seen_urls:
            seen_urls.add(v['url'])
            unique.append(v)
    unique.sort(key=lambda x: x['score'], reverse=True)
    return unique
