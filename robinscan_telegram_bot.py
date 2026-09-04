#!/usr/bin/env python3
"""
robinscan_telegram_bot.py
--------------------------------
robinscan.io/tokens sayfasini DOGRUDAN (API kullanmadan, tarayici gibi) okur,
yeni eklenen tokenlari tespit eder ve Telegram grubuna bildirim gonderir.

NASIL CALISIR
- Playwright ile gercek bir Chromium tarayicisi acar, sayfayi yukler ve
  JS ile olusan tabloyu render edildikten sonra okur (requests/urllib gibi
  "ham HTML indir" yontemleri bu sayfada eksik veri verebiliyor).
- Tablodaki her satiri (token adi + sutunlar) cikarir.
- Daha once gordugu tokenlari data/seen_tokens.json dosyasinda tutar.
- Yeni bir token gorurse Telegram Bot API ile mesaj gonderir.

KURULUM (kendi bilgisayarinda / sunucunda, terminalde):
    python3 -m venv venv
    source venv/bin/activate            # Windows: venv\\Scripts\\activate
    pip install playwright beautifulsoup4 requests
    playwright install chromium --with-deps

CALISTIRMA (tek seferlik kontrol):
    python3 robinscan_telegram_bot.py

5 DAKIKADA BIR OTOMATIK CALISTIRMA (Linux/Mac - cron):
    crontab -e
    */5 * * * * cd /tam/yol/klasor && /tam/yol/venv/bin/python3 robinscan_telegram_bot.py >> monitor.log 2>&1

5 DAKIKADA BIR OTOMATIK CALISTIRMA (Windows):
    Gorev Zamanlayicisi (Task Scheduler) -> Yeni Gorev -> Tetikleyici: her 5 dakikada
    -> Eylem: python.exe robinscan_telegram_bot.py

ONEMLI
- BOT_TOKEN'ini kimseyle paylasma; bu dosyayi public bir yere (GitHub vb.) atarsan
  once ortam degiskenine tasi.
- Site sahibinin robots.txt'i /api/... uzerinde otomatik erisimi kapatmis; bu yuzden
  bu script API'ye degil, dogrudan herkese acik https://robinscan.io/tokens
  sayfasina gidiyor (senin de istedigin bu).
- Playwright'in her calistirmada tarayici acip kapatmasi, guvenilirlik acisindan
  surekli acik kalan bir dongudense (ozellikle gunler suren calismalarda bellek
  sizintisi riskine karsi) daha saglam; o yuzden cron ile "tek seferlik calistir"
  mantigina gore yazildi.
"""

import json
import os
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ------------------------------------------------------------------
# AYARLAR - kendi bilgilerinle degistir
# ------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "BOT_TOKEN_BURAYA")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-1004484615121")

TARGET_URL = "https://robinscan.io/tokens"

# Bu dosyada "daha once gorulen" tokenlarin listesi tutulur.
STATE_FILE = Path(__file__).parent / "data" / "seen_tokens.json"

# Sayfadaki basliklarla eslesecek hedef sutunlar (kucuk harf, kismi eslesme yeterli)
TARGET_COLUMNS = ["price", "24h", "7d", "volume", "market cap", "liquidity", "holders", "dex"]

REQUEST_TIMEOUT = 20


# ------------------------------------------------------------------
# YARDIMCI FONKSIYONLAR
# ------------------------------------------------------------------
def load_seen() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_seen(seen: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_rendered_html(url: str) -> str:
    """Sayfayi gercek bir tarayici ile acip, JS calistiktan sonra HTML'i dondurur."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ))
        page.goto(url, wait_until="networkidle", timeout=45000)
        # Tablo satirlarinin gelmesini bekle (yoksa bos DOM okunabilir)
        try:
            page.wait_for_selector("table tbody tr", timeout=15000)
        except Exception:
            pass
        # React/Next.js verisinin oturmasi icin kucuk bir bekleme
        page.wait_for_timeout(1500)
        html = page.content()
        browser.close()
        return html


def parse_tokens(html: str) -> list[dict]:
    """
    Sayfadaki tum <table> etiketlerini tarar, en cok satira sahip olani
    (asil token listesi) secer ve satirlari sozluk listesine cevirir.
    """
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        return []

    # En cok <tr> iceren tabloyu asil veri tablosu kabul et
    best_table = max(tables, key=lambda t: len(t.find_all("tr")))

    # Basliklari oku
    header_cells = [th.get_text(strip=True).lower() for th in best_table.find_all("th")]

    rows_out = []
    body_rows = best_table.find("tbody")
    trs = body_rows.find_all("tr") if body_rows else best_table.find_all("tr")[1:]

    for tr in trs:
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue

        cell_texts = [c.get_text(" ", strip=True) for c in cells]

        # Token adini/sembolunu bul: genelde /token/0x... linkine sahip <a> icinde
        link = tr.find("a", href=True)
        token_symbol = None
        token_url = None
        if link:
            token_url = link["href"]
            if not token_url.startswith("http"):
                token_url = "https://robinscan.io" + token_url
            token_symbol = link.get_text(" ", strip=True)

        if not token_symbol:
            # Bulamazsa ilk hucreleri fallback olarak kullan
            token_symbol = cell_texts[1] if len(cell_texts) > 1 else cell_texts[0]

        row = {
            "symbol": token_symbol.strip(),
            "url": token_url,
            "raw_cells": cell_texts,
        }

        # Basliklarla hucreleri eslestirmeyi dene
        if header_cells and len(header_cells) == len(cell_texts):
            for h, v in zip(header_cells, cell_texts):
                for target in TARGET_COLUMNS:
                    if target in h:
                        row[target] = v

        rows_out.append(row)

    return rows_out


def token_key(token: dict) -> str:
    """Tokeni benzersiz sekilde tanimlayan anahtar (adres varsa adres, yoksa sembol)."""
    if token.get("url"):
        return token["url"]
    return token["symbol"]


def format_message(token: dict) -> str:
    lines = [f"🆕 Yeni token: {token['symbol']}"]
    for key in ["price", "24h", "7d", "volume", "market cap", "liquidity", "holders", "dex"]:
        if token.get(key):
            label = key.capitalize()
            lines.append(f"{label}: {token[key]}")
    if token.get("url"):
        lines.append(token["url"])
    return "\n".join(lines)


def send_telegram_message(text: str) -> None:
    if TELEGRAM_BOT_TOKEN == "BOT_TOKEN_BURAYA":
        print("HATA: TELEGRAM_BOT_TOKEN ayarlanmamis. Script icindeki AYARLAR bolumunu doldur.")
        sys.exit(1)

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        api_url,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        print(f"Telegram gonderim hatasi: {resp.status_code} {resp.text}")


# ------------------------------------------------------------------
# ANA AKIS
# ------------------------------------------------------------------
def main():
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Sayfa kontrol ediliyor: {TARGET_URL}")

    html = fetch_rendered_html(TARGET_URL)
    tokens = parse_tokens(html)

    if not tokens:
        print("Uyari: Tabloda hic satir bulunamadi. Sayfa yapisi degismis olabilir "
              "(ilk kurulumda beklenen bir durum - script'teki secicileri gozden gecir).")
        return

    seen = load_seen()
    new_tokens = [t for t in tokens if token_key(t) not in seen]

    print(f"Toplam {len(tokens)} token okundu, {len(new_tokens)} tanesi yeni.")

    for token in new_tokens:
        msg = format_message(token)
        send_telegram_message(msg)
        seen[token_key(token)] = {
            "symbol": token["symbol"],
            "first_seen": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        time.sleep(1)  # Telegram rate-limit icin kucuk bir bekleme

    save_seen(seen)
    print("Bitti.")


if __name__ == "__main__":
    main()
