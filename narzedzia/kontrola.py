#!/usr/bin/env python3
"""Codzienna kontrola strony — uruchamiana w GitHub Actions (ma dostęp do sieci), nie w chmurze Claude (tam brak dostępu do strony).
Sprawdza: stronę główną, plik stanu automatu (meta.json: wiek, błędy, źródła bez odpowiedzi), wiek plików danych, pliki dla wyszukiwarek,
przebiegi Actions z 24 h; v115 (część C zadania z 26.09.2026): świeżość źródeł wg kategorii (godzinowe / dzienne w dni robocze / tygodniowe
/ miesięczne; żółte po przekroczeniu progu, czerwone po 2×), błędy zbieracza w 3 kolejnych uruchomieniach = czerwone (kontrola/historia.json),
zgodność liczb (kapitalizacja krypto CoinGecko vs CoinPaprika — różnica dnia wobec mediany 30 dni w kontrola/zgodnosc.csv; ceny BTC/ETH;
TGA Fiscal Data vs FRED; wieloryby: zmiana salda vs przelewy z archiwum).
Zapisuje `kontrola/ostatnia.md` (po polsku: nagłówek i linia „Wynik:” w stałym formacie — czyta je zadanie w chmurze; potem werdykt
✅/⚠️/❌ i tabela źródło → status → wiek danych → uwaga), `kontrola/ostatnia.json`, `kontrola/zgodnosc.csv`, `kontrola/historia.json`.
Kod wyjścia 1 = BŁĄD (GitHub wysyła właścicielowi e-mail o nieudanym przebiegu). Bez kluczy, tylko odczyt. Python 3.12, sama biblioteka standardowa.
Uwaga: komunikat commita bota zawiera „[skip ci]” — GitHub pomija wtedy przebiegi wyzwalane pushem (dlatego commit dodający ten plik
nie może mieć tego napisu w treści — pierwszy przebieg nie ruszył właśnie z tego powodu)."""
import csv
import datetime as dt
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

SITE = os.environ.get('SITE_URL', 'https://capitalflowai-app.github.io').rstrip('/')
REPO = os.environ.get('GITHUB_REPOSITORY', 'capitalflowai-app/capitalflowai-app.github.io')
TOKEN = os.environ.get('GITHUB_TOKEN', '')          # tylko do odczytu listy przebiegów (API publiczne działa też bez niego)
OUT_DIR = os.environ.get('KONTROLA_DIR', 'kontrola')
ARCH_DIR = os.environ.get('KONTROLA_ARCH', 'archiwum')   # archiwum własne z tego samego checkoutu (v113)
NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
PLIKI = ['meta', 'etf', 'trendy', 'oecd', 'rynki', 'dzwignia', 'wieloryby', 'energia', 'usa-makro', 'bilans-usa', 'krypto', 'instytucje', 'tic', 'cm', 'fred', 'cftc', 'ceny', 'indeksy', 'ceny-krypto', 'insider', 'stres', 'aukcje']
LIMIT_MIN = {'meta': 90, 'etf': 180, 'trendy': 180, 'oecd': 24 * 60, 'rynki': 180, 'dzwignia': 180, 'wieloryby': 90, 'energia': 24 * 60,
             'usa-makro': 24 * 60, 'bilans-usa': 48 * 60, 'krypto': 180, 'instytucje': 180, 'tic': 48 * 60, 'cm': 180, 'fred': 180, 'cftc': 24 * 60, 'ceny': 180, 'indeksy': 24 * 60, 'ceny-krypto': 180, 'insider': 48 * 60, 'stres': 24 * 60, 'aukcje': 24 * 60}
# v115: świeżość ŹRÓDEŁ (data danych, nie czas pliku). (etykieta, plik, kategoria, próg w minutach). Kategorie: 'h' = godzinowe (czas części
# pliku), 'd' = dzienne w dni robocze (koniec dnia danych, liczone godzinami roboczymi bez sobót i niedziel), 'w' = tygodniowe (koniec dnia danych),
# 'm' = miesięczne (koniec miesiąca danych). Progi z zadania: 3 h / 36 h / 9 dni / 45 dni; CFTC +3 dni (raport wtorkowy publikowany w piątek),
# TIC +40 dni (Skarb USA publikuje dane miesiąca 46–51 dni po jego końcu; tuż przed publikacją wiek = 30 + 51 = 81 dni) — inaczej żółte świeciłoby co miesiąc bez powodu.
# EIA: ceny dzienne ropy są publikowane raz w tygodniu (środa, za poprzedni tydzień) — próg tygodniowy 9 dni, nie 36 h (26.09: dane z wtorku w sobotę = 3 dni).
SWIEZOSC = [
    ('rynki (kursy EBC, rentowności)', 'rynki', 'h', 180), ('wieloryby (salda portfeli giełd)', 'wieloryby', 'h', 180), ('dźwignia (giełdy pochodnych)', 'dzwignia', 'h', 180),
    ('TGA (Fiscal Data, dziennie)', 'instytucje', 'd', 36 * 60), ('ETF krypto (SoSoValue, dziennie)', 'etf', 'd', 36 * 60),
    ('FRED dzienne (RRPONTSYD)', 'fred', 'd', 36 * 60), ('EIA ceny dzienne (publikowane co tydzień)', 'energia', 'w', 9 * 24 * 60),
    ('CFTC (raport tygodniowy)', 'cftc', 'w', (9 + 3) * 24 * 60), ('FRED tygodniowe (WALCL)', 'fred', 'w', 9 * 24 * 60),
    ('TIC (miesięcznie)', 'tic', 'm', (45 + 40) * 24 * 60), ('OECD (miesięcznie)', 'oecd', 'm', 45 * 24 * 60), ('BLS (miesięcznie)', 'usa-makro', 'm', 45 * 24 * 60),
]
CG_GLOBAL = 'https://api.coingecko.com/api/v3/global'
CP_GLOBAL = 'https://api.coinpaprika.com/v1/global'
CG_PRICE = 'https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd'
CP_TICKER = 'https://api.coinpaprika.com/v1/tickers/{id}?quotes=USD'
ZG_DNI = 30          # mediana z ostatnich 30 dni różnicy kapitalizacji
ZG_MIN = 7           # do zebrania tylu dni historii — tylko informacja, bez koloru
ZG_ZOLTE, ZG_CZERWONE = 2.0, 5.0   # pkt proc. odchylenia od mediany
CENA_PROG = 1.0      # % różnicy cen BTC/ETH między źródłami
ETF_PROG = 1.0       # % różnicy zamknięcia ETF tej samej daty: Twelve Data (ceny.json, mapa) vs Massive/Tiingo (indeksy.json → etf) — v117.1
TGA_PROG = 1.0       # pkt proc. — odchylenie dzisiejszej różnicy TGA (Fiscal Data vs FRED WTREGEN, ta sama data) od mediany 30 dni; różnica sama w sobie
                     # jest stała (~3%: H.4.1 liczy zobowiązanie Fed na środę, DTS — gotówkę operacyjną Skarbu), więc próg 1% na poziomach świeciłby codziennie
WH_PROG = 5.0        # % — |zmiana salda − przelewy netto| wobec większej z tych liczb
WH_MIN_USD = 1e6     # poniżej miliona USD rozbieżność nie jest uwagą (przelewy < 1 mln nie są skanowane)
HIST_N = 30          # przechowywane przebiegi w historia.json
HIST_CZERWONE = 3    # tyle kolejnych przebiegów z błędami zbieracza = czerwone


def get(url, timeout=25, headers=None):
    req = urllib.request.Request(url, headers={'User-Agent': 'CapitalFlowAI-kontrola/1.0', **(headers or {})})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return r.status, body, int((time.monotonic() - t0) * 1000)


def get_json(url, timeout=25):
    return json.loads(get(url, timeout)[1])


def wiek_min(iso):
    try:
        t = dt.datetime.fromisoformat(str(iso).replace('Z', '+00:00'))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return max(0, int((NOW - t).total_seconds() // 60))
    except Exception:
        return None


def czas_pl(iso):
    try:
        t = dt.datetime.fromisoformat(str(iso).replace('Z', '+00:00')).astimezone(dt.timezone(dt.timedelta(hours=2)))   # czas polski (letni)
        return t.strftime('%d.%m.%Y, %H:%M')
    except Exception:
        return '—'


def fmt_wiek(m):
    """Minuty → „0 h 34 min” / „2 d 3 h” / „—”."""
    if m is None:
        return '—'
    if m < 60 * 48:
        return f'{m // 60} h {m % 60:02d} min'
    return f'{m // 1440} d {(m % 1440) // 60} h'


# ---------------------------------------------------------------- v115: świeżość źródeł ----------------------------------------------------------------
def godziny_robocze(od, do):
    """Godziny między od i do (UTC) bez sobót i niedziel — dane dzienne nie starzeją się przez weekend."""
    if do <= od:
        return 0.0
    h, t = 0.0, od
    while t < do:
        nxt = min(do, (t + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
        if t.weekday() < 5:
            h += (nxt - t).total_seconds() / 3600
        t = nxt
    return h


def data_danych(name, j):
    """Data/czas danych z pliku wg jego kształtu → (tekst daty, rodzaj: 'ts' | 'day' | 'month') albo None."""
    try:
        if name in ('rynki', 'dzwignia'):
            pa = j.get('part_at') or {}
            v = max((x for x in pa.values() if isinstance(x, str)), default=None)
            return (v, 'ts') if v else None
        if name == 'wieloryby':
            v = (j.get('part_at') or {}).get('salda') or j.get('at')
            return (v, 'ts') if isinstance(v, str) else None
        if name == 'instytucje':
            return (j['tga']['asof'], 'day')
        if name == 'etf':
            return (j['asof'], 'day')
        if name == 'energia':
            d = j['s']['wti']['d']; return (d[-1][0], 'day')
        if name == 'cftc':
            return (j['asof'], 'day')
        if name == 'tic':
            return (j['asof'], 'month')
        if name == 'oecd':
            last = None
            for part in ('cli', 'irlt', 'share'):
                for rows in (j.get(part) or {}).values():
                    if rows and isinstance(rows[-1], list):
                        last = max(last or '', str(rows[-1][0]))
            return (last, 'month') if last else None
        if name == 'usa-makro':
            d = j['s']['cpi']['d']; return (d[-1][0], 'month')
    except Exception:
        return None
    return None


def wiek_danych(txt, kind, kat, now=None):
    """Wiek danych w minutach dla kategorii progu: 'ts' → od znacznika czasu; 'day' → od końca dnia danych; 'month' → od końca
    miesiąca danych; kategoria 'd' liczy godziny robocze (bez sobót i niedziel). None = nie da się policzyć."""
    now = now or NOW
    try:
        if kind == 'ts':
            t = dt.datetime.fromisoformat(str(txt).replace('Z', '+00:00'))
            if t.tzinfo is None:
                t = t.replace(tzinfo=dt.timezone.utc)
            return max(0, int((now - t).total_seconds() // 60))
        if kind == 'day':
            d = dt.datetime.fromisoformat(str(txt)[:10]).replace(tzinfo=dt.timezone.utc) + dt.timedelta(days=1)
        else:   # month 'YYYY-MM' → pierwszy dzień następnego miesiąca
            y, m = int(str(txt)[:4]), int(str(txt)[5:7])
            d = dt.datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=dt.timezone.utc)
        if kat == 'd':
            return int(godziny_robocze(d, now) * 60)
        return max(0, int((now - d).total_seconds() // 60))
    except Exception:
        return None


def ocena(wiek, prog):
    if wiek is None:
        return '?'
    return '❌' if wiek > 2 * prog else ('⚠️' if wiek > prog else '✅')


def swiezosc(files, now=None):
    """files: nazwa → wczytany JSON (albo None). Zwraca listę wierszy tabeli: (etykieta, status, wiek min, data danych, uwaga)."""
    rows = []
    for label, name, kat, prog in SWIEZOSC:
        j = files.get(name)
        if not isinstance(j, dict):
            rows.append((label, '?', None, None, 'plik nie wczytany')); continue
        if name == 'fred':
            sid = 'RRPONTSYD' if kat == 'd' else 'WALCL'
            try:
                txt, kind = j['series'][sid]['asof'], 'day'
            except Exception:
                rows.append((label, '?', None, None, f'brak serii {sid}')); continue
        else:
            dd = data_danych(name, j)
            if not dd:
                rows.append((label, '?', None, None, 'brak daty danych w pliku')); continue
            txt, kind = dd
        w = wiek_danych(txt, kind, kat, now)
        st = ocena(w, prog)
        note = '' if st == '✅' else (f'próg {fmt_wiek(prog)}' + (' (godziny robocze)' if kat == 'd' else '') + (', ponad 2× progu' if st == '❌' else ''))
        rows.append((label, st, w, txt, note))
    return rows


# ---------------------------------------------------------------- v115: zgodność liczb ----------------------------------------------------------------
ZG_KOL = ['date', 'cap_gap_pct', 'tga_gap_pct']   # kontrola/zgodnosc.csv: tylko różnice procentowe (bez wartości źródeł); puste pole = brak odczytu


def zgodnosc_csv(path):
    """{dzień: {'cap': %, 'tga': %}} — pusta komórka = brak (klucz pominięty)."""
    rows = {}
    if os.path.exists(path):
        with open(path, encoding='utf-8', newline='') as f:
            r = csv.reader(f); next(r, None)
            for row in r:
                if len(row) >= 2 and row[0] != 'date':
                    rec = {}
                    for i, kk in ((1, 'cap'), (2, 'tga')):
                        try:
                            rec[kk] = float(row[i])
                        except (ValueError, IndexError):
                            pass
                    rows[row[0]] = rec
    return rows


def zgodnosc_zapisz(path, rows):
    with open(path, 'w', encoding='utf-8', newline='') as f:
        wtr = csv.writer(f, lineterminator='\n'); wtr.writerow(ZG_KOL)
        for d in sorted(rows):
            wtr.writerow([d] + [('%.3f' % rows[d][kk]) if isinstance(rows[d].get(kk), (int, float)) else '' for kk in ('cap', 'tga')])


def mediana_ocena(rows, kk, gap, today, zolte, czerwone):
    """rows: dzień → {kk: różnica %} z poprzednich dni; gap: dzisiejsza różnica. Zwraca (status, mediana, n, opis) — żółte/czerwone,
    gdy dzisiejsza różnica odbiega od mediany ostatnich ZG_DNI dni o więcej niż progi (pkt proc.); < ZG_MIN dni historii = tylko informacja."""
    hist = [v[kk] for d, v in sorted(rows.items()) if d < today and isinstance(v.get(kk), (int, float))][-ZG_DNI:]
    if gap is None:
        return '?', None, len(hist), 'brak odczytu'
    if len(hist) < ZG_MIN:
        return 'ℹ️', (statistics.median(hist) if hist else None), len(hist), f'historia {len(hist)} z {ZG_MIN} dni — bez oceny'
    med = statistics.median(hist)
    d = abs(gap - med)
    st = '❌' if (czerwone is not None and d > czerwone) else ('⚠️' if d > zolte else '✅')
    return st, med, len(hist), f'odchylenie od mediany {d:.2f} pkt proc. (progi {zolte:g}' + (f' / {czerwone:g}' if czerwone is not None else '') + ')'


def kapitalizacja(rows, gap, today):
    return mediana_ocena(rows, 'cap', gap, today, ZG_ZOLTE, ZG_CZERWONE)


def procent(a, b):
    try:
        return abs(a - b) / abs(b) * 100.0 if b else None
    except Exception:
        return None


def tga_porownanie(inst, fred):
    """Ostatnia wspólna data Fiscal Data (instytucje.tga.d) i FRED WTREGEN (obie w mln USD) → (data, fiscal, fred, różnica %)."""
    try:
        a = {str(r[0]): float(r[1]) for r in inst['tga']['d'] if isinstance(r, list) and len(r) >= 2 and r[1] is not None}
        b = {str(r[0]): float(r[1]) for r in fred['series']['WTREGEN']['d'] if isinstance(r, list) and len(r) >= 2 and r[1] is not None}
    except Exception:
        return None
    wspolne = sorted(set(a) & set(b))
    if not wspolne:
        return None
    d = wspolne[-1]
    return d, a[d], b[d], procent(a[d], b[d])


def etf_porownanie(ceny, ix):
    """14 symboli ETF mapy: ostatnia wspólna data zamknięcia w ceny.json (Twelve Data, `q[SYM].d = [[data, close, wolumen]]`) i w indeksy.json
    (`etf.q[SYM] = [[data, close]]`, Massive/Tiingo) → lista (symbol, data, a, b, różnica %); symbol bez wspólnej daty = pominięty. None = brak plików."""
    try:
        q1, q2 = ceny['q'], ix['etf']['q']
    except Exception:
        return None
    out = []
    for sym, rec in q1.items():
        d1 = {str(r[0]): float(r[1]) for r in ((rec.get('d') if isinstance(rec, dict) else None) or []) if isinstance(r, list) and len(r) >= 2 and isinstance(r[1], (int, float))}
        d2 = {str(r[0]): float(r[1]) for r in (q2.get(sym) or []) if isinstance(r, list) and len(r) >= 2 and isinstance(r[1], (int, float))}
        wsp = sorted(set(d1) & set(d2))
        if wsp:
            d = wsp[-1]; out.append((sym, d, d1[d], d2[d], procent(d1[d], d2[d])))
    return out


def wieloryby_porownanie(path):
    """archiwum/wieloryby.csv: dla ostatniego dnia z poprzednim dniem — |zmiana salda − przelewy netto| na giełdę i aktywo; zmiana salda liczona
    w jednostkach aktywa po dzisiejszym kursie (saldo w USD zmienia się też przez kurs ETH, a to nie przelew). Pusta komórka przepływów (brak sum
    dobowych) = para pominięta. Zwraca (dzień, poprzedni, lista (giełda, aktywo, zmiana, netto, rozbieżność) ponad progiem, liczba porównanych) albo None."""
    if not os.path.exists(path):
        return None
    by = {}
    with open(path, encoding='utf-8', newline='') as f:
        r = csv.reader(f); head = next(r, None)
        if not head or head[:5] != ['date', 'exchange', 'asset', 'balance', 'balance_usd']:
            return None
        for row in r:
            if len(row) >= 8:
                by.setdefault(row[0], {})[(row[1], row[2])] = row
    days = sorted(by)
    if len(days) < 2:
        return None
    d, p = days[-1], days[-2]
    zle, n = [], 0
    for k, row in by[d].items():
        prev = by[p].get(k)
        if not prev:
            continue
        try:
            u, u0, usd, net = float(row[3]), float(prev[3]), float(row[4]), float(row[7])
        except ValueError:
            continue
        if not u:
            continue
        n += 1
        delta = (u - u0) * (usd / u)   # zmiana w jednostkach × dzisiejszy kurs — ruch kursu ETH nie jest przelewem (v117)
        roz = abs(delta - net)
        if roz > WH_MIN_USD and roz > WH_PROG / 100 * max(abs(delta), abs(net), WH_MIN_USD):
            zle.append((k[0], k[1], delta, net, roz))
    return d, p, zle, n


def czerwone_z_historii(hist, n=None):
    """Błędy zbieracza w n kolejnych kontrolach z RÓŻNYCH dni UTC (kontrola rusza też po pushu — kilka przebiegów jednego dnia to nie „3 dni z rzędu”)."""
    n = HIST_CZERWONE if n is None else n
    ost = {}
    for h in hist:
        if isinstance(h, dict) and isinstance(h.get('at'), str):
            ost[h['at'][:10]] = h   # ostatni przebieg dnia
    dni = sorted(ost)[-n:]
    return len(dni) >= n and all((ost[d].get('bledy_zbieracza') or 0) > 0 for d in dni)


def historia(path, wpis):
    """Dopisuje wpis do kontrola/historia.json (ostatnie HIST_N) i zwraca całą listę (najnowszy na końcu)."""
    hist = []
    try:
        with open(path, encoding='utf-8') as f:
            hist = json.load(f)
        if not isinstance(hist, list):
            hist = []
    except Exception:
        hist = []
    hist = [h for h in hist if isinstance(h, dict) and h.get('at') != wpis.get('at')] + [wpis]
    hist = hist[-HIST_N:]
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(hist, f, ensure_ascii=False, indent=0)
    return hist


# ---------------------------------------------------------------- kontrola ----------------------------------------------------------------
def kontrola():
    R = {'at': NOW.isoformat(), 'strona': {}, 'meta': {}, 'pliki': {}, 'actions': {}, 'swiezosc': [], 'zgodnosc': {}, 'uwagi': [], 'bledy': []}
    files = {}
    # 1. strona główna
    try:
        st, body, ms = get(f'{SITE}/index.html?nc={int(time.time())}')
        ok = st == 200 and len(body) > 1_000_000 and b'const EXTRA' in body
        R['strona'] = {'http': st, 'bajty': len(body), 'ms': ms, 'ok': ok}
        if not ok:
            R['bledy'].append(f'strona główna: HTTP {st}, {len(body)} B')
    except Exception as e:  # noqa
        R['strona'] = {'ok': False, 'blad': str(e)[:200]}
        R['bledy'].append(f'strona główna nie odpowiada: {str(e)[:120]}')
    # 2. plik stanu automatu
    try:
        st, body, ms = get(f'{SITE}/data/meta.json?nc={int(time.time())}')
        m = json.loads(body); files['meta'] = m
        w = wiek_min(m.get('at'))
        nie = sorted(k for k, v in (m.get('ok') or {}).items() if v is False)
        R['meta'] = {'at': m.get('at'), 'wiek_min': w, 'zrodla': len(m.get('ok') or {}), 'bez_odpowiedzi': nie,
                     'errors': [str(x)[:160] for x in (m.get('errors') or [])], 'notes': [str(x)[:160] for x in (m.get('notes') or [])]}
        if w is None:
            R['bledy'].append('plik stanu bez czasu przebiegu')
        elif w > 180:
            R['bledy'].append(f'automat nie odświeżył danych od {w // 60} godz. (ostatni przebieg {czas_pl(m.get("at"))})')
        elif w > LIMIT_MIN['meta']:
            R['uwagi'].append(f'ostatni przebieg automatu sprzed {w} min (zwykle co 20 min)')
        if nie:
            R['uwagi'].append('źródła bez odpowiedzi w ostatnim przebiegu: ' + ', '.join(nie))
        for e in R['meta']['errors']:
            R['uwagi'].append('błąd zbieracza: ' + e)
    except Exception as e:  # noqa
        R['meta'] = {'blad': str(e)[:200]}
        R['bledy'].append(f'plik stanu (meta.json) nie odpowiada: {str(e)[:120]}')
    # 3. wiek plików danych
    for n in PLIKI:
        if n == 'meta':
            continue
        try:
            st, body, ms = get(f'{SITE}/data/{n}.json?nc={int(time.time())}')
            j = json.loads(body); files[n] = j if isinstance(j, dict) else None
            w = wiek_min(j.get('at')) if isinstance(j, dict) else None
            okp = j.get('ok') if isinstance(j, dict) else None
            nie = sorted(k for k, v in okp.items() if v is False) if isinstance(okp, dict) else []
            R['pliki'][n] = {'http': st, 'bajty': len(body), 'wiek_min': w, 'czesci_bez_odpowiedzi': nie}
            if w is not None and w > LIMIT_MIN.get(n, 24 * 60):
                R['uwagi'].append(f'{n}.json sprzed {w // 60} godz. {w % 60} min (limit {LIMIT_MIN.get(n, 1440) // 60} godz.)')
            if nie:
                R['uwagi'].append(f'{n}.json: części bez odpowiedzi: ' + ', '.join(nie))
        except urllib.error.HTTPError as e:
            R['pliki'][n] = {'http': e.code}
            if n in ('etf', 'trendy', 'oecd', 'rynki'):
                R['uwagi'].append(f'{n}.json: HTTP {e.code}')
            elif n != 'indeksy':
                R['uwagi'].append(f'{n}.json: HTTP {e.code} (brak pliku)')
        except Exception as e:  # noqa
            R['pliki'][n] = {'blad': str(e)[:160]}
            R['uwagi'].append(f'{n}.json: {str(e)[:100]}')
    # 3b. v111: pliki dla wyszukiwarek (robots.txt, sitemap.xml, plik weryfikacji Google) — tylko kod HTTP
    for f in ('robots.txt', 'sitemap.xml', 'google433f7c24524100a9.html'):
        try:
            st, body, ms = get(f'{SITE}/{f}?nc={int(time.time())}')
            R['pliki'][f] = {'http': st, 'bajty': len(body)}
            if st != 200 or len(body) < 20:
                R['uwagi'].append(f'{f}: HTTP {st}, {len(body)} B')
        except urllib.error.HTTPError as e:   # v111.1: kod HTTP w wierszu plików, nie „HTTP ?”
            R['pliki'][f] = {'http': e.code}; R['uwagi'].append(f'{f}: HTTP {e.code}')
        except Exception as e:  # noqa
            R['pliki'][f] = {'blad': str(e)[:120]}; R['uwagi'].append(f'{f}: {str(e)[:80]}')
    # 3c. v115: świeżość źródeł wg kategorii — żółte po progu, czerwone po 2×
    for label, st, w, txt, note in swiezosc(files):
        R['swiezosc'].append({'zrodlo': label, 'status': st, 'wiek_min': w, 'data': txt, 'uwaga': note})
        if st == '❌':
            R['bledy'].append(f'{label}: dane z {txt} — {fmt_wiek(w)} temu ({note})')
        elif st == '⚠️':
            R['uwagi'].append(f'{label}: dane z {txt} — {fmt_wiek(w)} temu ({note})')
        elif st == '?':
            R['uwagi'].append(f'{label}: {note}')
    # 3d. v115: zgodność liczb — kapitalizacja (mediana 30 dni), ceny BTC/ETH, TGA, wieloryby
    Z = R['zgodnosc']
    today = NOW.date().isoformat()
    os.makedirs(OUT_DIR, exist_ok=True)
    zg_path = os.path.join(OUT_DIR, 'zgodnosc.csv')
    rows = zgodnosc_csv(zg_path)
    rows.setdefault(today, {})
    gap = None
    try:
        cg = get_json(CG_GLOBAL)['data']['total_market_cap']['usd']; cp = get_json(CP_GLOBAL)['market_cap_usd']
        gap = round((float(cp) - float(cg)) / float(cg) * 100.0, 3)
        rows[today]['cap'] = gap
    except Exception as e:  # noqa
        Z['kapitalizacja_blad'] = str(e)[:120]; R['uwagi'].append(f'zgodność kapitalizacji: brak odczytu ({str(e)[:80]})')
    st, med, n, opis = kapitalizacja(rows, gap, today)
    Z['kapitalizacja'] = {'status': st, 'dzis_pct': gap, 'mediana_pct': (round(med, 3) if med is not None else None), 'dni': n, 'opis': opis}
    if st == '❌':
        R['bledy'].append(f'kapitalizacja krypto: różnica źródeł dziś {gap:.2f}% wobec normy {med:.2f}% — {opis}')
    elif st == '⚠️':
        R['uwagi'].append(f'kapitalizacja krypto: różnica źródeł dziś {gap:.2f}% wobec normy {med:.2f}% — {opis}')
    try:
        p1 = get_json(CG_PRICE); c = {}
        for cid, key in (('btc-bitcoin', 'bitcoin'), ('eth-ethereum', 'ethereum')):
            p2 = get_json(CP_TICKER.format(id=cid))['quotes']['USD']['price']
            r = procent(float(p1[key]['usd']), float(p2))
            c[key] = {'a': float(p1[key]['usd']), 'b': float(p2), 'roznica_pct': (round(r, 3) if r is not None else None)}
            if r is not None and r > CENA_PROG:
                R['uwagi'].append(f'cena {key}: dwa źródła różnią się o {r:.2f}% (próg {CENA_PROG:g}%)')
        Z['ceny'] = c
    except Exception as e:  # noqa
        Z['ceny_blad'] = str(e)[:120]; R['uwagi'].append(f'zgodność cen BTC/ETH: brak odczytu ({str(e)[:80]})')
    t = tga_porownanie(files.get('instytucje') or {}, files.get('fred') or {})
    if t:
        d, a, b, r = t
        if r is not None:
            rows[today]['tga'] = round(r, 3)
        st2, med2, n2, opis2 = mediana_ocena(rows, 'tga', r, today, TGA_PROG, None)
        Z['tga'] = {'data': d, 'fiscal_mln': a, 'fred_mln': b, 'roznica_pct': (round(r, 3) if r is not None else None), 'status': st2,
                    'mediana_pct': (round(med2, 3) if med2 is not None else None), 'dni': n2, 'opis': opis2}
        if st2 == '⚠️':
            R['uwagi'].append(f'TGA {d}: Fiscal Data {a:,.0f} vs FRED {b:,.0f} mln USD — różnica {r:.2f}% wobec normy {med2:.2f}% ({opis2})')
    else:
        Z['tga'] = None
    try:
        zgodnosc_zapisz(zg_path, rows)
    except Exception as e:  # noqa
        R['uwagi'].append(f'zgodnosc.csv: nie zapisano ({str(e)[:80]})')
    e = etf_porownanie(files.get('ceny') or {}, files.get('indeksy') or {})
    if e:
        zle = [x for x in e if x[4] is not None and x[4] > ETF_PROG]
        Z['etf'] = {'porownane': len(e), 'roznice': [{'symbol': s, 'data': d, 'a': a, 'b': b, 'roznica_pct': round(r, 3)} for s, d, a, b, r in zle]}
        if zle:
            R['uwagi'].append('ETF (mapa): zamknięcia z dwóch źródeł różnią się > ' + f'{ETF_PROG:g}% dla ' + ', '.join(f'{s} ({d}: {a:g} vs {b:g})' for s, d, a, b, r in zle[:6]))
    else:
        Z['etf'] = None
    w = wieloryby_porownanie(os.path.join(ARCH_DIR, 'wieloryby.csv'))
    if w:
        d, p, zle, n = w
        Z['wieloryby'] = {'dzien': d, 'poprzedni': p, 'porownane': n, 'rozbieznosci': [{'gielda': g, 'aktywo': a, 'zmiana_usd': round(x, 2), 'netto_usd': round(y, 2), 'roznica_usd': round(z, 2)} for g, a, x, y, z in zle]}
        if zle:
            R['uwagi'].append('wieloryby: zmiana salda ≠ przelewy netto (> 5%) dla ' + ', '.join(f'{g} {a}' for g, a, *_ in zle[:6])
                              + ' — możliwe przelewy spoza zakresu skanu (< 1 mln USD, ETH przez kontrakty)')
    else:
        Z['wieloryby'] = None
    # 4. przebiegi Actions z ostatnich 24 h (API publiczne; token tylko podnosi limit zapytań)
    try:
        hdr = {'Accept': 'application/vnd.github+json'}
        if TOKEN:
            hdr['Authorization'] = 'Bearer ' + TOKEN
        st, body, ms = get(f'https://api.github.com/repos/{REPO}/actions/runs?per_page=100', headers=hdr)
        runs = json.loads(body).get('workflow_runs', [])
        od = NOW - dt.timedelta(hours=24)
        ost = [r for r in runs if r.get('name', '').startswith('Strona') and wiek_min(r.get('run_started_at')) is not None
               and dt.datetime.fromisoformat(r['run_started_at'].replace('Z', '+00:00')) >= od]
        z = {}
        for r in ost:
            z[r.get('conclusion') or r.get('status')] = z.get(r.get('conclusion') or r.get('status'), 0) + 1
        R['actions'] = {'przebiegi_24h': len(ost), 'wg_wyniku': z,
                        'ostatni': (ost[0]['run_started_at'] if ost else None), 'ostatnia_porazka': next((r['run_started_at'] for r in ost if r.get('conclusion') == 'failure'), None)}
        if z.get('failure', 0) >= 3:
            R['bledy'].append(f'{z["failure"]} nieudanych przebiegów automatu w 24 h')
        elif z.get('failure', 0):
            R['uwagi'].append(f'{z["failure"]} nieudany przebieg automatu w 24 h (ostatni: {czas_pl(R["actions"]["ostatnia_porazka"])})')
        if len(ost) < 20:
            R['uwagi'].append(f'tylko {len(ost)} przebiegów w 24 h (harmonogram co 20 min ≈ 72; GitHub bywa opóźniony)')
    except Exception as e:  # noqa
        R['actions'] = {'blad': str(e)[:160]}
        R['uwagi'].append('nie udało się odczytać listy przebiegów Actions: ' + str(e)[:100])
    # 5. v115: historia — błędy zbieracza w 3 kolejnych przebiegach kontroli = czerwone
    n_err = len(R['meta'].get('errors') or []) if isinstance(R['meta'], dict) else 0
    hist = historia(os.path.join(OUT_DIR, 'historia.json'), {'at': R['at'], 'bledy_zbieracza': n_err, 'uwagi': len(R['uwagi']), 'bledy': len(R['bledy'])})
    if czerwone_z_historii(hist):
        R['bledy'].append(f'zbieracz zgłasza błędy w {HIST_CZERWONE} kolejnych dniach kontroli (' + '; '.join((R['meta'].get('errors') or ['?'])[:2]) + ')')
    R['historia_n'] = len(hist)
    R['wynik'] = 'BŁĄD' if R['bledy'] else ('UWAGA' if R['uwagi'] else 'OK')
    return R


def raport_md(R):
    m = R.get('meta') or {}
    werdykt = ('✅ Wszystko w normie.' if R['wynik'] == 'OK' else
               f'⚠️ Uwag: {len(R["uwagi"])} — nic nie wymaga natychmiastowej reakcji.' if R['wynik'] == 'UWAGA' else
               f'❌ Błędów: {len(R["bledy"])} — wymagają uwagi (szczegóły niżej).')
    L = [f'# Kontrola strony — {czas_pl(R["at"])} (czas polski)', '',
         f'**Wynik: {R["wynik"]}**', '', werdykt, '',
         f'- Strona główna: {"działa" if (R.get("strona") or {}).get("ok") else "PROBLEM"} (HTTP {(R.get("strona") or {}).get("http", "—")}, {(R.get("strona") or {}).get("ms", "—")} ms).',
         f'- Ostatni przebieg automatu: {czas_pl(m.get("at"))} — {("sprzed " + str(m.get("wiek_min")) + " min") if m.get("wiek_min") is not None else "brak"}; '
         f'źródeł: {m.get("zrodla", "—")}, bez odpowiedzi: {", ".join(m.get("bez_odpowiedzi") or []) or "żadne"}; błędów zbieracza: {len(m.get("errors") or [])}.']
    a = R.get('actions') or {}
    if 'przebiegi_24h' in a:
        L.append(f'- Przebiegi Actions w 24 h: {a["przebiegi_24h"]} ({", ".join(f"{k}: {v}" for k, v in a["wg_wyniku"].items()) or "—"}).')
    L.append('- Pliki danych (wiek): ' + ', '.join(f'{n} {("%dh%02d" % divmod(p["wiek_min"], 60)) if p.get("wiek_min") is not None else ("HTTP " + str(p.get("http", "?")))}'
                                             for n, p in (R.get('pliki') or {}).items()) + '.')
    if m.get('notes'):
        L.append('- Notatki automatu: ' + ' · '.join(m['notes']) + '.')
    if R.get('swiezosc'):
        L += ['', '## Świeżość źródeł', '', '| Źródło | Status | Wiek danych | Data danych | Uwaga |', '|---|---|---|---|---|']
        for s in R['swiezosc']:
            L.append(f'| {s["zrodlo"]} | {s["status"]} | {fmt_wiek(s["wiek_min"])} | {s["data"] or "—"} | {s["uwaga"] or "—"} |')
    Z = R.get('zgodnosc') or {}
    if Z:
        L += ['', '## Zgodność liczb (porównania krzyżowe)', '']
        k = Z.get('kapitalizacja') or {}
        if k:
            dz = f'{k["dzis_pct"]:.2f}%' if k.get('dzis_pct') is not None else '—'
            md = f'{k["mediana_pct"]:.2f}%' if k.get('mediana_pct') is not None else '—'
            L.append(f'- Kapitalizacja krypto, dwa źródła: różnica dziś {dz}, norma (mediana {k.get("dni", 0)} dni) {md} — {k.get("status", "?")} {k.get("opis", "")}.')
        c = Z.get('ceny') or {}
        for key, nm in (('bitcoin', 'BTC'), ('ethereum', 'ETH')):
            if key in c:
                r = c[key].get('roznica_pct')
                L.append(f'- Cena {nm}: {c[key]["a"]:,.0f} vs {c[key]["b"]:,.0f} USD — różnica {r:.2f}% {"⚠️" if r > CENA_PROG else "✅"}.' if r is not None else f'- Cena {nm}: brak porównania.')
        t = Z.get('tga')
        if t:
            r = t.get('roznica_pct'); md2 = f'{t["mediana_pct"]:.2f}%' if t.get('mediana_pct') is not None else '—'
            L.append(f'- TGA {t["data"]}: Fiscal Data {t["fiscal_mln"]:,.0f} vs FRED {t["fred_mln"]:,.0f} mln USD — różnica {r:.2f}%, norma (mediana {t.get("dni", 0)} dni) {md2} — {t.get("status", "?")} {t.get("opis", "")}.'
                     if r is not None else f'- TGA {t["data"]}: brak porównania.')
        else:
            L.append('- TGA: brak wspólnej daty Fiscal Data i FRED.')
        e = Z.get('etf')
        if e:
            L.append(f'- ETF mapy (dwa źródła, ta sama data): porównane {e["porownane"]} symboli, różnice > {ETF_PROG:g}%: {len(e["roznice"])} {"⚠️" if e["roznice"] else "✅"}' + (' — ' + ', '.join(x["symbol"] for x in e["roznice"][:6]) if e["roznice"] else '') + '.')
        else:
            L.append('- ETF mapy: brak wspólnej daty zamknięć w dwóch źródłach.')
        w = Z.get('wieloryby')
        if w:
            L.append(f'- Wieloryby {w["dzien"]} vs {w["poprzedni"]}: {w["porownane"]} par giełda/aktywo, rozbieżności > 5%: {len(w["rozbieznosci"])} {"⚠️" if w["rozbieznosci"] else "✅"}.')
        else:
            L.append('- Wieloryby: archiwum ma mniej niż dwa dni — porównanie od jutra.')
    if R['bledy']:
        L += ['', '## Błędy (wymagają uwagi)'] + [f'- {x}' for x in R['bledy']]
    if R['uwagi']:
        L += ['', '## Uwagi'] + [f'- {x}' for x in R['uwagi']]
    if not R['bledy'] and not R['uwagi']:
        L += ['', 'Wszystko w normie.']
    L += ['', 'Kontrola wykonana przez GitHub Actions (plik `narzedzia/kontrola.py`), bez kluczy, tylko odczyt.']
    return '\n'.join(L) + '\n'


def main():
    R = kontrola()
    md = raport_md(R)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, 'ostatnia.md'), 'w', encoding='utf-8') as f:
        f.write(md)
    with open(os.path.join(OUT_DIR, 'ostatnia.json'), 'w', encoding='utf-8') as f:
        json.dump(R, f, ensure_ascii=False, indent=1)
    print(md)
    summ = os.environ.get('GITHUB_STEP_SUMMARY')
    if summ:
        with open(summ, 'a', encoding='utf-8') as f:
            f.write(md)
    print('::notice title=kontrola::' + (R['wynik'] + ' — ' + '; '.join(R['bledy'] + R['uwagi'])[:900]).replace('%', '%25').replace('\n', '%0A'))
    sys.exit(1 if R['bledy'] else 0)


if __name__ == '__main__':
    main()
