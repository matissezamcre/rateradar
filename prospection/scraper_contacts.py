#!/usr/bin/env python3
"""
Scrape contacts de garages VO — Belgique + France
Sources : AutoScout24 dealers, Pages Jaunes, Pages d'Or Belgique
Output  : prospection/contacts.csv

Usage:
    cd rateradar
    SCRAPER_API_KEY=xxx python prospection/scraper_contacts.py
    python prospection/scraper_contacts.py   # sans proxy (peut être bloqué)
"""

import csv
import json
import os
import re
import time
import random
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urljoin

OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "contacts.csv")
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def get_html(url, use_proxy=False):
    if use_proxy and SCRAPER_API_KEY:
        proxy_url = f"http://api.scraperapi.com?api_key={SCRAPER_API_KEY}&url={quote_plus(url)}&render=false"
        r = requests.get(proxy_url, timeout=25)
    else:
        r = requests.get(url, headers=HEADERS, timeout=15)
    r.encoding = "utf-8"
    return r


def extract_emails(text):
    found = re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', text)
    blocked = {"noreply", "no-reply", "example", "test@", "sentry", "privacy", "legal", "support@pagesjaunes"}
    return list({e for e in found if not any(b in e.lower() for b in blocked)})


def sleep():
    time.sleep(random.uniform(1.5, 3.5))


# ── AutoScout24 dealers ────────────────────────────────────────────────────────

def scrape_autoscout24_dealers(max_pages=20):
    contacts = []
    for pays, cy in [("Belgique", "B"), ("France", "F")]:
        print(f"[AS24] {pays}...")
        for page in range(1, max_pages + 1):
            url = f"https://www.autoscout24.fr/concessionnaires/voitures-occasion/?page={page}&cy={cy}&atype=C"
            try:
                r = get_html(url)
                soup = BeautifulSoup(r.text, "html.parser")

                # Try __NEXT_DATA__
                nd_tag = soup.find("script", {"id": "__NEXT_DATA__"})
                if nd_tag and nd_tag.string:
                    nd = json.loads(nd_tag.string)
                    pp = nd.get("props", {}).get("pageProps", {})
                    dealers = []
                    for key in ["dealers", "dealerList", "items", "results"]:
                        val = pp.get(key)
                        if isinstance(val, list) and val:
                            dealers = val
                            break
                        if isinstance(val, dict):
                            for sub in ["dealers", "items", "results"]:
                                if isinstance(val.get(sub), list):
                                    dealers = val[sub]
                                    break
                    if not dealers:
                        print(f"  [AS24] {pays} page {page} — structure inconnue, debug keys: {list(pp.keys())[:8]}")
                        break
                    for d in dealers:
                        addr = d.get("address") or {}
                        contacts.append({
                            "nom":       d.get("name") or d.get("dealerName") or "",
                            "ville":     addr.get("city") or d.get("city") or "",
                            "pays":      pays,
                            "telephone": d.get("phone") or d.get("phoneNumber") or "",
                            "email":     d.get("email") or "",
                            "source":    "AutoScout24",
                            "url":       d.get("url") or d.get("profileUrl") or "",
                        })
                else:
                    # CSS fallback
                    cards = soup.select("article, [data-testid*='dealer'], [class*='DealerCard']")
                    if not cards:
                        print(f"  [AS24] {pays} page {page} — pas de cards CSS, arrêt")
                        break
                    for card in cards:
                        name_el = card.select_one("h2, h3, [class*='name']")
                        name = name_el.get_text(strip=True) if name_el else ""
                        phone_el = card.select_one("[href^='tel:']")
                        phone = phone_el.get("href", "").replace("tel:", "") if phone_el else ""
                        link = card.select_one("a[href*='/concessionnaire/']")
                        dealer_url = urljoin("https://www.autoscout24.fr", link["href"]) if link else ""
                        if name:
                            contacts.append({
                                "nom": name, "ville": "", "pays": pays,
                                "telephone": phone, "email": "",
                                "source": "AutoScout24", "url": dealer_url
                            })

                print(f"  [AS24] {pays} p{page} → {len(contacts)} total")
                sleep()
            except Exception as e:
                print(f"  [AS24] Erreur page {page}: {e}")
                sleep()
    return contacts


# ── Pages Jaunes France ────────────────────────────────────────────────────────

PJ_QUERIES = [
    "garage voiture occasion",
    "concessionnaire occasion",
    "négociant automobile",
    "marchand voitures occasion",
]

def scrape_pages_jaunes(max_pages=8):
    contacts = []
    for query in PJ_QUERIES:
        print(f"[PJ] '{query}'...")
        for page in range(1, max_pages + 1):
            url = (f"https://www.pagesjaunes.fr/annuaire/chercherlespros"
                   f"?quoi={quote_plus(query)}&ou=France&page={page}")
            try:
                r = get_html(url, use_proxy=True)
                soup = BeautifulSoup(r.text, "html.parser")

                listings = (soup.select(".bi-pro")
                            or soup.select("article.bi")
                            or soup.select("[class*='biAnnuaire']")
                            or soup.select(".result-item"))
                if not listings:
                    print(f"  [PJ] page {page} vide, arrêt")
                    break

                for item in listings:
                    name_el = (item.select_one(".denomination-links")
                               or item.select_one("h2") or item.select_one("h3"))
                    name = name_el.get_text(strip=True) if name_el else ""

                    city_el = item.select_one(".ville, .locality, [class*='ville']")
                    city = city_el.get_text(strip=True) if city_el else ""

                    phone_el = item.select_one("[href^='tel:']")
                    phone = phone_el.get("href", "").replace("tel:", "") if phone_el else ""

                    email_el = item.select_one("[href^='mailto:']")
                    email = email_el.get("href", "").replace("mailto:", "") if email_el else ""

                    link = item.select_one("a[href*='/pros/']")
                    detail_url = urljoin("https://www.pagesjaunes.fr", link["href"]) if link else ""

                    if name:
                        contacts.append({
                            "nom": name, "ville": city, "pays": "France",
                            "telephone": phone, "email": email,
                            "source": "Pages Jaunes", "url": detail_url
                        })

                print(f"  [PJ] '{query}' p{page} → {len(contacts)} total")
                sleep()
            except Exception as e:
                print(f"  [PJ] Erreur: {e}")
                sleep()
    return contacts


# ── Pages d'Or Belgique ────────────────────────────────────────────────────────

PO_QUERIES = [
    "garage voiture occasion",
    "concessionnaire automobile",
    "négociant voitures",
]

def scrape_pages_or(max_pages=5):
    contacts = []
    for query in PO_QUERIES:
        print(f"[PO] Belgique '{query}'...")
        for page in range(1, max_pages + 1):
            url = f"https://www.pagesdor.be/r/{quote_plus(query)}/belgique/page-{page}/"
            try:
                r = get_html(url, use_proxy=True)
                soup = BeautifulSoup(r.text, "html.parser")

                listings = (soup.select(".listing-item")
                            or soup.select("article")
                            or soup.select("[class*='result']"))
                if not listings:
                    print(f"  [PO] page {page} vide, arrêt")
                    break

                for item in listings:
                    name_el = item.select_one("h2, h3, .title, [class*='name']")
                    name = name_el.get_text(strip=True) if name_el else ""

                    city_el = item.select_one(".city, .locality, address, [class*='address']")
                    city = city_el.get_text(strip=True) if city_el else ""

                    phone_el = item.select_one("[href^='tel:'], [class*='phone'], [class*='tel']")
                    phone = phone_el.get("href", "").replace("tel:", "") if phone_el else ""
                    if not phone and phone_el:
                        phone = phone_el.get_text(strip=True)

                    email_el = item.select_one("[href^='mailto:']")
                    email = email_el.get("href", "").replace("mailto:", "") if email_el else ""

                    link = item.select_one("a[href]")
                    detail_url = urljoin("https://www.pagesdor.be", link["href"]) if link else ""

                    if name:
                        contacts.append({
                            "nom": name, "ville": city, "pays": "Belgique",
                            "telephone": phone, "email": email,
                            "source": "Pages d'Or", "url": detail_url
                        })

                print(f"  [PO] '{query}' p{page} → {len(contacts)} total")
                sleep()
            except Exception as e:
                print(f"  [PO] Erreur: {e}")
                sleep()
    return contacts


# ── Enrichissement email via page individuelle ─────────────────────────────────

def enrich_emails(contacts):
    to_enrich = [c for c in contacts if not c.get("email") and c.get("url")]
    print(f"\nEnrichissement emails: {len(to_enrich)} contacts sans email...")
    for i, c in enumerate(to_enrich):
        try:
            r = get_html(c["url"], use_proxy=bool(SCRAPER_API_KEY))
            emails = extract_emails(r.text)
            if emails:
                c["email"] = emails[0]
            if i % 20 == 0:
                print(f"  {i}/{len(to_enrich)} ({sum(1 for x in contacts if x.get('email'))} emails trouvés)")
            time.sleep(random.uniform(1.0, 2.0))
        except Exception:
            pass
    return contacts


# ── Dédoublonnage ──────────────────────────────────────────────────────────────

def deduplicate(contacts):
    seen = set()
    result = []
    for c in contacts:
        key = (c["nom"].lower().strip(), re.sub(r"\D", "", c.get("telephone", "")))
        if key[0] and key not in seen:
            seen.add(key)
            result.append(c)
    return result


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    all_contacts = []

    all_contacts += scrape_autoscout24_dealers(max_pages=20)
    all_contacts += scrape_pages_jaunes(max_pages=8)
    all_contacts += scrape_pages_or(max_pages=5)

    all_contacts = deduplicate(all_contacts)
    print(f"\nAprès déduplication : {len(all_contacts)} contacts")

    all_contacts = enrich_emails(all_contacts)

    fields = ["nom", "ville", "pays", "telephone", "email", "source", "url"]
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for c in all_contacts:
            w.writerow(c)

    with_email = sum(1 for c in all_contacts if c.get("email"))
    with_phone = sum(1 for c in all_contacts if c.get("telephone"))
    belgique   = sum(1 for c in all_contacts if c.get("pays") == "Belgique")
    france     = sum(1 for c in all_contacts if c.get("pays") == "France")

    print(f"\n{'─'*40}")
    print(f"  Fichier : {OUTPUT_FILE}")
    print(f"  Total   : {len(all_contacts)}")
    print(f"  Email   : {with_email}")
    print(f"  Tel     : {with_phone}")
    print(f"  Belgique: {belgique}  |  France: {france}")
    print(f"{'─'*40}")


if __name__ == "__main__":
    main()
