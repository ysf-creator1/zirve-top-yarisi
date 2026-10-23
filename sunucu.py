#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Zirve Top Yarışı - yerel oyun sunucusu

- Oyunu http://127.0.0.1:8765 adresinde açar (sadece bu bilgisayardan erişilebilir).
- Oyuncu kayıtlarını kayit/kayit.json dosyasına yazar; tarayıcı verisi silinse de kayıtlar kaybolmaz.
- kayit/kayit.js dosyasını da günceller: oyun doğrudan index.html'den açılsa bile kayıtları buradan okur.
- En fazla 10 dakikada bir kayit/yedekler/ klasörüne yedek alır (son 50 yedek saklanır).
- Oyun sekmesi kapandıktan 30 dakika sonra kendiliğinden kapanır.

Çalıştırma: python3 sunucu.py   (ya da klasördeki "Zirve Top Yarışı" uygulamasına çift tıkla)
"""
import argparse
import glob
import json
import os
import shutil
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

APP_ID = 'zirve-top-yarisi'
BASE = os.path.dirname(os.path.abspath(__file__))
KAYIT_DIR = os.path.join(BASE, 'kayit')
JSON_PATH = os.path.join(KAYIT_DIR, 'kayit.json')
JS_PATH = os.path.join(KAYIT_DIR, 'kayit.js')
YEDEK_DIR = os.path.join(KAYIT_DIR, 'yedekler')
MAX_YEDEK = 50
YEDEK_ARALIK = 600          # saniye
MAX_BOYUT = 3 * 1024 * 1024 # bayt

LOCK = threading.Lock()
son_istek = time.time()
son_yedek = 0.0


def tr_lower(s):
    return str(s or '').strip().replace('I', 'ı').replace('İ', 'i').lower()


def oyuncu_anahtari(p):
    # Toplam kazanılan puan ve yarış sayısı sadece artar: daha büyük olan daha ilerlemiş kayıttır.
    # Eşitlikte en son değişen (upd) kazanır.
    return (p.get('earned') or 0, p.get('races') or 0, p.get('upd') or 0)


def birlestir(a, b):
    """İki kaydı birleştirir: her oyuncu için daha fazla ilerlemiş olan kalır, hiçbir ilerleme kaybolmaz.
    (index.html içindeki mergeSaves ile aynı kural.)"""
    def gecerli(s):
        return isinstance(s, dict) and isinstance(s.get('players'), dict)
    if not gecerli(a):
        return b if gecerli(b) else None
    if not gecerli(b):
        return a
    if (a.get('updatedAt') or 0) >= (b.get('updatedAt') or 0):
        yeni, eski = a, b
    else:
        yeni, eski = b, a
    out = dict(eski)
    out.update(yeni)
    silinen = {}
    for s in (a, b):
        for n, t in (s.get('deleted') or {}).items():
            silinen[n] = max(silinen.get(n, 0), t or 0)
    en_iyi = {}
    for s in (eski, yeni):
        for ad, p in s['players'].items():
            if not isinstance(p, dict):
                continue
            k = tr_lower(p.get('name') or ad)
            if silinen.get(k, 0) > (p.get('upd') or 0):
                continue
            if k not in en_iyi or oyuncu_anahtari(p) >= oyuncu_anahtari(en_iyi[k]):
                en_iyi[k] = p
    out['players'] = {(p.get('name') or k): p for k, p in en_iyi.items()}
    out['deleted'] = silinen
    out['updatedAt'] = max(a.get('updatedAt') or 0, b.get('updatedAt') or 0)
    return out


def kaydi_oku():
    for yol in [JSON_PATH] + sorted(glob.glob(os.path.join(YEDEK_DIR, '*.json')), reverse=True):
        try:
            with open(yol, encoding='utf-8') as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get('players'), dict):
                return d
        except (OSError, ValueError):
            continue
    return None


def guvenli_yaz(yol, metin):
    gecici = yol + '.tmp'
    with open(gecici, 'w', encoding='utf-8') as f:
        f.write(metin)
        f.flush()
        os.fsync(f.fileno())
    os.replace(gecici, yol)


def kaydi_yaz(veri, yedek_zorla=False):
    global son_yedek
    os.makedirs(KAYIT_DIR, exist_ok=True)
    guvenli_yaz(JSON_PATH, json.dumps(veri, ensure_ascii=False, indent=1))
    guvenli_yaz(JS_PATH, '// Zirve Top Yarışı oyuncu kayıtları. Oyun tarafından otomatik yazılır, elle değiştirmeyin.\n'
                         'window.ZIRVE_KAYIT = ' + json.dumps(veri, ensure_ascii=False) + ';\n')
    simdi = time.time()
    if yedek_zorla or simdi - son_yedek > YEDEK_ARALIK:
        os.makedirs(YEDEK_DIR, exist_ok=True)
        shutil.copyfile(JSON_PATH, os.path.join(YEDEK_DIR, time.strftime('kayit-%Y%m%d-%H%M%S.json')))
        son_yedek = simdi
        for eski in sorted(glob.glob(os.path.join(YEDEK_DIR, 'kayit-*.json')))[:-MAX_YEDEK]:
            os.remove(eski)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=BASE, **k)

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def _izinli(self):
        # yalnızca bu bilgisayardaki oyun sayfası (başka siteler kayda yazamaz)
        host = (self.headers.get('Host') or '').rsplit(':', 1)[0]
        if host not in ('127.0.0.1', 'localhost'):
            return False
        origin = self.headers.get('Origin')
        if origin and not (origin.startswith('http://127.0.0.1:') or origin.startswith('http://localhost:')):
            return False
        return True

    def _json(self, obj, kod=200):
        b = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(kod)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        global son_istek
        son_istek = time.time()
        if not self._izinli():
            return self.send_error(403)
        yol = self.path.split('?')[0]
        if yol == '/api/ping':
            return self._json({'ok': True, 'app': APP_ID})
        if yol == '/api/kayit':
            return self._json(kaydi_oku() or {})
        if yol.startswith('/kayit/') and yol != '/kayit/kayit.js':
            return self.send_error(404)
        if yol.endswith('.py') or '.app/' in yol:
            return self.send_error(404)
        return super().do_GET()

    def do_POST(self):
        global son_istek
        son_istek = time.time()
        if not self._izinli() or self.headers.get('X-Zirve') != '1':
            return self.send_error(403)
        if self.path.split('?')[0] != '/api/kayit':
            return self.send_error(404)
        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0 or n > MAX_BOYUT:
            return self.send_error(413)
        try:
            veri = json.loads(self.rfile.read(n).decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return self.send_error(400)
        if not isinstance(veri, dict) or not isinstance(veri.get('players'), dict):
            return self.send_error(400)
        with LOCK:
            kaydi_yaz(birlestir(kaydi_oku() or {}, veri))
        return self._json({'ok': True})


def calisiyor_mu(port):
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/api/ping' % port, timeout=1) as r:
            return json.loads(r.read().decode('utf-8')).get('app') == APP_ID
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description='Zirve Top Yarışı yerel sunucusu')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--no-browser', action='store_true', help='tarayıcıyı açma')
    ap.add_argument('--idle', type=int, default=1800, help='bu kadar saniye istek gelmezse kapan')
    a = ap.parse_args()

    srv = None
    for port in range(a.port, a.port + 15):
        if calisiyor_mu(port):           # zaten açık: sadece oyunu aç
            url = 'http://127.0.0.1:%d/index.html' % port
            print('Oyun zaten açık:', url)
            if not a.no_browser:
                webbrowser.open(url)
            return
        try:
            srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
            break
        except OSError:
            continue
    if srv is None:
        print('Boş port bulunamadı.')
        sys.exit(1)

    url = 'http://127.0.0.1:%d/index.html' % srv.server_address[1]
    print('Zirve Top Yarışı çalışıyor:', url)
    print('Kayıt dosyası:', JSON_PATH)

    def bekci():
        while True:
            time.sleep(15)
            if time.time() - son_istek > a.idle:
                srv.shutdown()
                return
    threading.Thread(target=bekci, daemon=True).start()
    if not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    print('Sunucu kapandı.')


if __name__ == '__main__':
    main()
