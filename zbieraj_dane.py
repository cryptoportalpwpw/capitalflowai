#!/usr/bin/env python3
"""CapitalFlowAI — zbieranie danych wymagających klucza API (uruchamiane przez GitHub Actions).

Wynik: data/etf.json (napływy ETF, SoSoValue + kapitalizacje CoinGecko),
       data/dzis.json (notowania ETF-ów krajowych USA, Finnhub — okres DZIŚ),
       data/ceny.json (dzienne notowania 14 ETF-ów zastępczych, Twelve Data — okresy 1T i 1M),
       data/cmc.json (CoinMarketCap: kapitalizacja rynku, dominacja BTC/ETH, stablecoiny, DeFi),
       data/instytucje.json (bez klucza: Skarb USA — saldo TGA; NY Fed — reverse repo i portfel SOMA; EBC — salda
       TARGET; MOF Japonia — tygodniowe transakcje w papierach wartościowych),
       data/cm.json (bez klucza: Coin Metrics Community — wpłaty i wypłaty BTC/ETH na giełdy, zapas monet na giełdach),
       data/meta.json (kiedy, co się udało, błędy).
Klucze wyłącznie ze zmiennych środowiskowych: SOSOVALUE_KEY, COINGECKO_KEY, FINNHUB_KEY, TWELVEDATA_KEY, COINMARKETCAP_KEY.
Decyzja właściciela 24.09.2026 (wieczór): dane z jego kluczy Finnhub, Twelve Data i CoinMarketCap są publikowane na stronie
mimo planów „do użytku osobistego" — właściciel przyjął ryzyko i zapowiedział plany płatne (checkpoints/DECYZJA_...).
SITE_URL (opcjonalnie): adres opublikowanej strony — gdy źródło zawiedzie, zachowujemy poprzedni plik
zamiast pustki (data w polu "at" pokazuje wtedy prawdziwy wiek danych).
Tylko biblioteka standardowa — zero zależności.
"""
import csv, io, json, os, re, sys, time, datetime, urllib.request, urllib.error

SOSO = 'https://openapi.sosovalue.com/openapi/v1'
ETF_SYMS = ['btc', 'eth', 'sol', 'xrp']
CG_IDS = {'btc': 'bitcoin', 'eth': 'ethereum', 'sol': 'solana', 'xrp': 'ripple'}
_DEADLINE = [None]   # v49: po tym czasie (monotonic) SoSoValue nie czeka na 429 i pomija listy funduszy
SOSO_BUDGET = 8 * 60
SOSO_SLEEP = 4.0   # limit 20 zapytań/min — 15/min zostawia zapas
# te same ETF-y zastępcze co w index.html (GPROXY)
DAY_SYMS = ['SPY', 'EWC', 'ILF', 'VGK', 'KSA', 'TUR', 'EIS', 'EZA', 'INDA', 'MCHI', 'EWJ', 'EWY', 'ASEA', 'EWA']
TD = 'https://api.twelvedata.com'
TD_BATCH = 7        # limit 8 kredytów/min (1 symbol = 1 kredyt): dwie paczki po 7 z minutą przerwy
TD_SLEEP = 61
TD_OUTPUT = 260     # v64: ponad rok sesji (1R = 252) — koszt zapytania ten sam (1 kredyt na symbol niezależnie od liczby świec)
TD_MIN_SYMBOLS = 10
TD_MIN_CANDLES = 22
CMC = 'https://pro-api.coinmarketcap.com'
# TIC (Skarb USA, dane rządowe): pliki SLT tabulatorowe; pobierane najwyżej raz na dobę (publikacja ok. 15–18 dnia miesiąca)
TIC_BASE = 'https://ticdata.treasury.gov/resource-center/data-chart-center/tic/Documents/'
TIC_MONTHS = 13
TIC_REGIONS = {   # region strony → wiersze TIC (kraje i sumy urzędowe); 'usa' nie ma sensu (TIC = zagranica vs USA)
    'can': ['Canada'],
    'lat': ['Total Latin America'],                         # bez Karaibów (centra finansowe — osobno jako 'carib')
    'eur': ['Memo: European Union', 'United Kingdom', 'Switzerland', 'Norway'],
    'rus': ['Russia'],
    'mea': ['Saudi Arabia', 'United Arab Emirates', 'Kuwait', 'Israel', 'Turkey'],   # tabela 2 nie ma Arabii Saudyjskiej
    'afr': ['Total Africa'],
    'ind': ['India'],
    'chn': ['China, Mainland', 'Hong Kong'],
    'jpn': ['Japan', 'Korea, South'],                  # Tajwan osobno (nie ma go na mapie strony)
    'asean': ['Singapore', 'Malaysia', 'Thailand', 'Indonesia', 'Philippines'],
    'oce': ['Australia', 'New Zealand'],
}
CG = 'https://api.coingecko.com/api/v3'      # plan Demo: klucz w nagłówku, atrybucja „Data by CoinGecko” wymagana
FNG_URL = 'https://api.alternative.me/fng/?limit=31'   # 31 dni: dziś + wartość sprzed 30 dni   # wskaźnik nastroju (model), podać źródło z linkiem
# FRED (Federal Reserve Bank of St. Louis) — tylko serie Rady Gubernatorów Fed: domena publiczna, „citation requested”.
# Serie firm trzecich na FRED (SP500, VIXCLS, BAMLH0A0HYM2 …) wymagają zgody właściciela — nie pobieramy.
FRED = 'https://api.stlouisfed.org/fred/series/observations'
FRED_SERIES = {
    'WALCL': {'unit': 'mln USD', 'freq': 'W', 'name': 'Fed: aktywa razem (H.4.1), środa'},
    'RRPONTSYD': {'unit': 'mld USD', 'freq': 'D', 'name': 'Reverse repo overnight, wolumen dnia'},
    'DTWEXBGS': {'unit': 'indeks (styczeń 2006 = 100)', 'freq': 'D', 'name': 'Szeroki nominalny indeks dolara'},
    'WTREGEN': {'unit': 'mln USD', 'freq': 'W', 'name': 'Konto rządu USA w Fed (TGA) wg H.4.1, środa'},
    # v50: H.4.1 Table 1A (Memorandum items, Wednesday level) — papiery w depozycie Fed dla zagranicznych instytucji oficjalnych
    'WSEFINTL1': {'unit': 'mln USD', 'freq': 'W', 'name': 'Fed: papiery w depozycie dla zagranicznych instytucji oficjalnych i międzynarodowych (H.4.1), środa'},
    'WMTSECL1': {'unit': 'mln USD', 'freq': 'W', 'name': '… w tym rynkowe papiery Skarbu USA (H.4.1), środa'},
    'WFASECL1': {'unit': 'mln USD', 'freq': 'W', 'name': '… w tym dług agencji federalnych i MBS (H.4.1), środa'},
    'WSEFINOL': {'unit': 'mln USD', 'freq': 'W', 'name': '… w tym pozostałe papiery (H.4.1), środa'},
}
FRED_LIMIT = 60      # ostatnie 60 obserwacji: ~1 rok tygodniowych, ~3 miesiące dziennych
FRED_SLEEP = 0.6     # limit FRED: 120 zapytań/min — 8 zapytań na przebieg z odstępem (v50: + 4 serie depozytu H.4.1)
FRED_CITE = 'Board of Governors of the Federal Reserve System (US), via FRED, Federal Reserve Bank of St. Louis'
FRED_API_NOTE = 'This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis.'
OUT = 'data'
NOW = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
META = {'at': NOW, 'ok': {}, 'errors': [], 'notes': []}   # notes: informacje (np. brak poprzedniego pliku), nie błędy
SECRETS = []      # wartości kluczy — maskowane w każdym komunikacie błędu
SAVED = {}        # v89: obiekty zapisane w tym przebiegu (wejście dla TRENDÓW)
TICKER = re.compile(r'^[A-Z0-9.]{1,10}$')


def mask(text):
    """Komunikat błędu nigdy nie zawiera klucza (nawet gdy dostawca odbije adres z parametrem)."""
    text = str(text)
    for k in SECRETS:
        if k:
            text = text.replace(k, '***')
    return text


def get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': 'CapitalFlowAI-collector/1.0', **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode('utf-8', 'replace')


def get_json(url, headers=None, timeout=30):
    st, body = get(url, headers, timeout)
    return json.loads(body)


def get_bytes(url, headers=None, timeout=60):
    req = urllib.request.Request(url, headers={'User-Agent': 'CapitalFlowAI-collector/1.0', **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def soso(path, key, _retry=True):
    time.sleep(SOSO_SLEEP)
    try:
        j = get_json(SOSO + path, {'x-soso-api-key': key})
    except urllib.error.HTTPError as e:
        if e.code == 429 and _retry and (_DEADLINE[0] is None or time.monotonic() < _DEADLINE[0]):          # limit minutowy — odczekaj pełną minutę i spróbuj raz jeszcze
            print('SoSoValue 429 — czekam 65 s')
            time.sleep(65)
            return soso(path, key, _retry=False)
        raise
    if isinstance(j, dict) and j.get('code') not in (None, 0):
        raise RuntimeError(f'SoSoValue {path}: {j.get("message")}')
    return j['data'] if isinstance(j, dict) and 'data' in j else j


def _prev_site(name):
    site = os.environ.get('SITE_URL', '').rstrip('/')
    if not site:
        return None
    try:
        return get_json(f'{site}/data/{name}.json?t={int(time.time())}')
    except urllib.error.HTTPError as e:
        if e.code == 404:   # pliku jeszcze nie ma (pierwszy przebieg) — informacja, nie błąd
            META['notes'].append(f'poprzedni {name}.json: brak na stronie (404)'); return None
        META['errors'].append(mask(f'poprzedni {name}.json: {e}')); return None
    except Exception as e:  # noqa
        META['errors'].append(mask(f'poprzedni {name}.json: {e}'))
        return None


def _prev_cache(name):
    """v51: plik z pamięci GitHub Actions (CACHE_DIR) — przeżywa awarię publikacji strony."""
    d = os.environ.get('CACHE_DIR', '').strip()
    if not d:
        return None
    p = os.path.join(d, name + '.json')
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:  # noqa
        META['errors'].append(mask(f'pamięć {name}.json: {e}'))
        return None


def previous(name):
    """Poprzedni plik: nowszy (wg pola at) z pamięci Actions i z opublikowanej strony — awaria API nie wymaże danych,
    a awaria publikacji nie zwielokrotni zapytań (v51)."""
    cands = [c for c in (_prev_cache(name), _prev_site(name)) if isinstance(c, dict)]
    if not cands:
        return None
    return max(cands, key=lambda c: str(c.get('at') or ''))


def fresh(prev, minutes):
    """Czy poprzedni plik (z opublikowanej strony) jest młodszy niż `minutes` minut."""
    try:
        at = datetime.datetime.fromisoformat(prev['at'])
        return (datetime.datetime.now(datetime.timezone.utc) - at).total_seconds() < minutes * 60
    except Exception:
        return False


def save(name, obj):
    os.makedirs(OUT, exist_ok=True)
    with open(f'{OUT}/{name}.json', 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, separators=(',', ':'))
    SAVED[name] = obj
    print(f'zapisano {OUT}/{name}.json ({os.path.getsize(f"{OUT}/{name}.json")} B)')


def ts(date_str):
    return int(datetime.datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=datetime.timezone.utc).timestamp())


# --- źródła urzędowe bez klucza (licencje: strona/LICENCJE-zrodel.md i checkpoints/PUBLIC_RIGHTS_*_PL.md) ---
TGA_URL = ('https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/dts/operating_cash_balance'
           '?filter=account_type:eq:Treasury%20General%20Account%20(TGA)%20Closing%20Balance'
           '&sort=-record_date&page[size]=70&fields=record_date,account_type,open_today_bal')
RRP_URL = 'https://markets.newyorkfed.org/api/rp/reverserepo/all/results/last/30.json'
SOMA_URL = 'https://markets.newyorkfed.org/api/soma/summary.json'
TGB_COUNTRIES = ['DE', 'IT', 'ES', 'NL', 'FR', 'IE', 'PT', 'GR', 'LU']
TGB_URL = ('https://data-api.ecb.europa.eu/service/data/TGB/M.' + '+'.join(TGB_COUNTRIES)
           + '.N.A094T.U2.EUR.E?format=jsondata&lastNObservations=13')
ILM_URL = 'https://data-api.ecb.europa.eu/service/data/ILM/W.U2.C.T000000.Z5.Z01?lastNObservations=13&format=jsondata'
M3_URL = 'https://data-api.ecb.europa.eu/service/data/BSI/M.U2.Y.V.M30.X.1.U2.2300.Z01.E?lastNObservations=14&format=jsondata'
BOP_CA_URL = ('https://data-api.ecb.europa.eu/service/data/BPS/M.N.U2.W1.S1.S1.T.B.CA._Z._Z._Z.EUR._T._X.N.ALL'
              '?lastNObservations=13&format=jsondata')
BOP_FA_URL = ('https://data-api.ecb.europa.eu/service/data/BPS/M.N.U2.W1.S1.S1.T.N.FA........'   # jedno zapytanie: wszystkie serie FA netto
              '?lastNObservations=13&format=jsondata')
BOP_FA_KEYS = {   # klucze sprawdzone 24.09.2026 (U2 = strefa euro w zmiennym składzie; serie I9 skończyły się na 2025-12)
    'M.N.U2.W1.S1.S1.T.N.FA._T.F._Z.EUR._T._X.N.ALL': 'fa',       # rachunek finansowy netto, razem
    'M.N.U2.W1.S1.S1.T.N.FA.D.F._Z.EUR._T._X.N.ALL': 'di',        # inwestycje bezpośrednie netto
    'M.N.U2.W1.S1.S1.T.N.FA.P.F._Z.EUR._T.M.N.ALL': 'pi',         # inwestycje portfelowe netto, razem
    'M.N.U2.W1.S1.S1.T.N.FA.P.F5._Z.EUR._T.M.N.ALL': 'pi_eq',     # … akcje i fundusze
    'M.N.U2.W1.S1.S1.T.N.FA.P.F3.T.EUR._T.M.N.ALL': 'pi_debt',    # … papiery dłużne
    'M.N.U2.W1.S1.S1.T.N.FA.O.F._Z.EUR._T._X.N.ALL': 'oi',        # pozostałe inwestycje netto
}
ECB_SLEEP = 1.2      # EBC blokuje serie szybkich zapytań („access blocked”): odstęp między zapytaniami do EBC
_ECB_LAST = -1e9
MOF_URL = 'https://www.mof.go.jp/policy/international_policy/reference/itn_transactions_in_securities/week.csv'
MOF_WEEKS = 26
_FULLWIDTH = str.maketrans({'．': '.', '～': '~', '　': ' '})


def _num(token):
    """Liczba z tekstu dostawcy ('1,689 ', '-15,228', '957409'); brak/nie-liczba → None (nigdy 0)."""
    t = str(token).replace(',', '').strip()
    if t in ('', '-', '－', 'null', 'None'):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def parse_tga(j):
    """FiscalData DTS: saldo zamknięcia konta TGA (mln USD) rosnąco po dacie."""
    rows = []
    for r in j.get('data', []):
        if 'Closing Balance' not in str(r.get('account_type', '')):
            continue
        v = _num(r.get('open_today_bal'))
        d = str(r.get('record_date', ''))[:10]
        if v is None or not re.match(r'^\d{4}-\d{2}-\d{2}$', d):
            continue
        rows.append([d, int(round(v))])
    rows.sort(key=lambda x: x[0])
    if not rows:
        raise RuntimeError('FiscalData: brak wierszy TGA')
    return {'src': 'U.S. Treasury, Fiscal Data — Daily Treasury Statement', 'unit': 'mln USD', 'asof': rows[-1][0],
            'url': 'https://fiscaldata.treasury.gov/datasets/daily-treasury-statement/', 'd': rows}


def parse_rrp(j):
    """NY Fed: przyjęte kwoty w operacjach reverse repo (mln USD) rosnąco po dacie."""
    rows = []
    for o in (j.get('repo') or {}).get('operations') or []:
        if o.get('operationType') != 'Reverse Repo':
            continue
        v = _num(o.get('totalAmtAccepted'))
        d = str(o.get('operationDate', ''))[:10]
        if v is None or not re.match(r'^\d{4}-\d{2}-\d{2}$', d):
            continue
        rows.append([d, int(round(v / 1e6))])
    rows.sort(key=lambda x: x[0])
    if not rows:
        raise RuntimeError('NY Fed: brak operacji reverse repo')
    return {'src': 'Federal Reserve Bank of New York — reverse repo operations', 'unit': 'mln USD', 'asof': rows[-1][0],
            'url': 'https://www.newyorkfed.org/markets/desk-operations/reverse-repo', 'd': rows}


def parse_soma(j):
    """NY Fed: łączny portfel SOMA (mln USD), ostatnie 12 tygodni, rosnąco."""
    rows = []
    for r in (j.get('soma') or {}).get('summary') or []:
        v = _num(r.get('total'))
        d = str(r.get('asOfDate', ''))[:10]
        if v is None or not re.match(r'^\d{4}-\d{2}-\d{2}$', d):
            continue
        rows.append([d, int(round(v / 1e6))])
    rows.sort(key=lambda x: x[0])
    rows = rows[-12:]
    if not rows:
        raise RuntimeError('NY Fed: brak wierszy SOMA')
    return {'src': 'Federal Reserve Bank of New York — SOMA holdings', 'unit': 'mln USD', 'asof': rows[-1][0],
            'url': 'https://www.newyorkfed.org/markets/soma-holdings', 'd': rows}


def parse_tgb(j):
    """EBC Data Portal (SDMX-JSON): salda TARGET krajów (mln EUR), miesięcznie, rosnąco."""
    dims = j['structure']['dimensions']
    series_dims = dims['series']
    area_i = next(i for i, d in enumerate(series_dims) if d['id'] == 'REF_AREA')
    areas = [v['id'] for v in series_dims[area_i]['values']]
    periods = [v['id'] for v in dims['observation'][0]['values']]
    q = {}
    for key, ser in j['dataSets'][0]['series'].items():
        area = areas[int(key.split(':')[area_i])]
        rows = []
        for oi, o in ser['observations'].items():
            v = _num(o[0] if o else None)
            if v is None:
                continue
            rows.append([periods[int(oi)], round(v, 2)])
        rows.sort(key=lambda x: x[0])
        if rows:
            q[area] = rows
    if not q:
        raise RuntimeError('EBC: brak serii TGB')
    asof = sorted({rows[-1][0] for rows in q.values()})
    return {'src': 'European Central Bank — TARGET balances (TGB)', 'unit': 'mln EUR',
            'asof': asof[0] if len(asof) == 1 else f'{asof[0]} – {asof[-1]}',
            'url': 'https://data.ecb.europa.eu/data/datasets/TGB', 'q': q}


def _mof_period(text):
    """'2026．9．6～9．12' → ('2026-09-06', '2026-09-12'); koniec bez roku bierze rok początku (przełom roku: +1)."""
    t = text.translate(_FULLWIDTH).replace(' ', '')
    m = re.match(r'^(\d{4})\.(\d{1,2})\.(\d{1,2})~(?:(\d{4})\.)?(\d{1,2})\.(\d{1,2})$', t)
    if not m:
        return None
    y1, m1, d1, y2, m2, d2 = m.groups()
    y1, m1, d1, m2, d2 = int(y1), int(m1), int(d1), int(m2), int(d2)
    y2 = int(y2) if y2 else (y1 + 1 if m2 < m1 else y1)
    try:
        a = datetime.date(y1, m1, d1); b = datetime.date(y2, m2, d2)
    except ValueError:
        return None
    return a.isoformat(), b.isoformat()


def parse_mof(raw):
    """MOF Japonia, week.csv (cp932): tygodniowe transakcje w papierach (100 mln JPY); ostatnie MOF_WEEKS tygodni."""
    txt = raw.decode('cp932') if isinstance(raw, bytes) else raw
    out = []
    for row in csv.reader(io.StringIO(txt)):
        if not row or not row[0].strip() or not row[0].strip()[0].isdigit():
            continue
        per = _mof_period(row[0])
        if not per or len(row) < 23:
            continue
        col = lambda i: _num(row[i])
        out.append({'from': per[0], 'to': per[1],
                    'assets': {'equity_net': col(3), 'ltdebt_net': col(6), 'stdebt_net': col(10), 'total_net': col(11)},
                    'liabilities': {'equity_net': col(14), 'ltdebt_net': col(17), 'stdebt_net': col(21), 'total_net': col(22)}})
    out.sort(key=lambda r: r['from'])
    out = out[-MOF_WEEKS:]
    if not out:
        raise RuntimeError('MOF: brak tygodni')
    return {'src': 'Ministry of Finance, Japan — International Transactions in Securities (weekly)', 'unit': '100 mln JPY',
            'asof': f"{out[-1]['from']} – {out[-1]['to']}",
            'url': 'https://www.mof.go.jp/english/policy/international_policy/reference/itn_transactions_in_securities/index.htm',
            'd': out}


def _ecb_json(url):
    """EBC blokuje serie szybkich zapytań: odstęp ECB_SLEEP od poprzedniego zapytania do EBC (User-Agent ustawia get)."""
    global _ECB_LAST
    wait = ECB_SLEEP - (time.monotonic() - _ECB_LAST)
    if wait > 0:
        time.sleep(wait)
    try:
        return get_json(url)
    finally:
        _ECB_LAST = time.monotonic()


def _ecb_try(url, label):
    """v49: zapytanie do EBC, które przy błędzie zwraca None i zapisuje błąd (druga seria nadal może się udać)."""
    try:
        return _ecb_json(url)
    except Exception as e:
        META['errors'].append(mask(f'{label}: {e}'))
        return None


def _iso_week_end(period):
    """'2026-W38' → piątek tego tygodnia ISO (dzień, na który EBC sporządza tygodniowe sprawozdanie); inne → None."""
    m = re.match(r'^(\d{4})-W(\d{2})$', str(period))
    if not m:
        return None
    return datetime.date.fromisocalendar(int(m.group(1)), int(m.group(2)), 5).isoformat()


def parse_ecb_single(j, label):
    """EBC Data Portal (SDMX-JSON), dokładnie jedna seria: [[okres, wartość]] rosnąco; brak obserwacji pominięty (nigdy 0)."""
    dims = j['structure']['dimensions']
    periods = [v['id'] for v in dims['observation'][0]['values']]
    series = j['dataSets'][0]['series']
    if len(series) != 1:
        raise RuntimeError(f'{label}: oczekiwano jednej serii, jest {len(series)}')
    ser = next(iter(series.values()))
    rows = []
    for oi, o in ser['observations'].items():
        v = _num(o[0] if o else None)
        if v is None:
            continue
        rows.append([periods[int(oi)], v])
    rows.sort(key=lambda x: x[0])
    if not rows:
        raise RuntimeError(f'{label}: brak obserwacji')
    return rows


def parse_ilm(j):
    """Eurosystem, aktywa razem z tygodniowego sprawozdania (mln EUR): [[piątek tygodnia, wartość, tydzień ISO]]."""
    d = [[_iso_week_end(p) or p, int(round(v)), p] for p, v in parse_ecb_single(j, 'ILM')]
    return {'src': 'European Central Bank — Eurosystem weekly financial statement (ILM)', 'unit': 'mln EUR',
            'asof': d[-1][0], 'period': d[-1][2], 'url': 'https://data.ecb.europa.eu/data/datasets/ILM', 'd': d}


def parse_m3(j):
    """Agregat M3 strefy euro (mln EUR, wyrównany sezonowo), miesięcznie: [[YYYY-MM, wartość]]."""
    d = [[p, int(round(v))] for p, v in parse_ecb_single(j, 'BSI M3')]
    return {'src': 'European Central Bank — monetary aggregate M3 (BSI), seasonally adjusted', 'unit': 'mln EUR',
            'asof': d[-1][0], 'url': 'https://data.ecb.europa.eu/data/datasets/BSI', 'd': d}


def parse_ecb_multi(j):
    """EBC Data Portal (SDMX-JSON) z wieloma seriami → {pełny klucz serii: [[okres, wartość]] rosnąco}; brak pominięty."""
    dims = j['structure']['dimensions']
    sdims = dims['series']
    periods = [v['id'] for v in dims['observation'][0]['values']]
    out = {}
    for key, ser in j['dataSets'][0]['series'].items():
        full = '.'.join(sdims[i]['values'][int(p)]['id'] for i, p in enumerate(key.split(':')))
        rows = []
        for oi, o in ser['observations'].items():
            v = _num(o[0] if o else None)
            if v is not None:
                rows.append([periods[int(oi)], v])
        rows.sort(key=lambda x: x[0])
        if rows:
            out[full] = rows
    return out


def parse_bop(j_ca, j_fa):
    """Bilans płatniczy strefy euro (mln EUR, miesięcznie, nieskorygowany sezonowo): saldo rachunku bieżącego oraz rachunek
    finansowy netto (aktywa − pasywa) z podziałem; plus = kapitał netto wypływa ze strefy euro. Brak serii = None (nie zero)."""
    s = {}
    try:
        s['ca'] = [[p, int(round(v))] for p, v in parse_ecb_single(j_ca, 'BPS CA')] if j_ca is not None else None
    except Exception as e:
        META['errors'].append(mask(f'bop ca: {e}')); s['ca'] = None
    multi = parse_ecb_multi(j_fa) if j_fa is not None else {}
    for key, name in BOP_FA_KEYS.items():
        rows = multi.get(key)
        s[name] = [[p, int(round(v))] for p, v in rows] if rows else None
    if not any(s.get(n) for n in s):
        raise RuntimeError('BPS: żadna seria bilansu płatniczego nie odpowiedziała')
    asof = max(r[-1][0] for r in s.values() if r)
    return {'src': 'European Central Bank — euro area balance of payments (BPS), monthly, not seasonally adjusted', 'unit': 'mln EUR',
            'asof': asof, 'url': 'https://data.ecb.europa.eu/data/datasets/BPS',
            'sign': 'net = assets minus liabilities; positive = net outflow from the euro area', 's': s}


def build_instytucje():
    """data/instytucje.json — każde źródło osobno: awaria jednego nie kasuje pozostałych (brak nie jest zerem)."""
    out = {'at': NOW, 'src': 'instytucje'}
    jobs = [('tga', lambda: parse_tga(get_json(TGA_URL))), ('rrp', lambda: parse_rrp(get_json(RRP_URL))),
            ('soma', lambda: parse_soma(get_json(SOMA_URL))), ('tgb', lambda: parse_tgb(_ecb_json(TGB_URL))),
            ('ilm', lambda: parse_ilm(_ecb_json(ILM_URL))), ('m3', lambda: parse_m3(_ecb_json(M3_URL))),
            ('bop', lambda: parse_bop(_ecb_try(BOP_CA_URL, 'bop ca'), _ecb_try(BOP_FA_URL, 'bop fa'))),
            ('mof', lambda: parse_mof(get_bytes(MOF_URL)))]
    for name, job in jobs:
        try:
            out[name] = job(); META['ok'][name] = True
            print(f"{name}: stan {out[name]['asof']}")
        except Exception as e:
            META['errors'].append(mask(f'{name}: {e}')); META['ok'][name] = False
    if not any(k in out for k, _ in jobs):
        raise RuntimeError('żadne źródło urzędowe nie odpowiedziało')
    return out


def parse_td(j, syms):
    """Odpowiedź Twelve Data (zbiorcza: klucze = symbole; pojedyncza: obiekt z 'values') → (notowania, błędy).

    Notowanie symbolu: {'asof': ostatnia świeca, 'ex': kod giełdy, 'd': [[data, close, volume|None], …] rosnąco}.
    Świeca bez poprawnego close jest pomijana (brak nie jest zerem); symbol z status != ok trafia do błędów, nie do q.
    """
    if not isinstance(j, dict):
        raise RuntimeError('Twelve Data: odpowiedź nie jest obiektem JSON')
    if j.get('status') == 'error' and 'values' not in j and not any(s in j for s in syms):
        raise RuntimeError(f'Twelve Data: {j.get("code")} {j.get("message")}')
    if 'values' in j and len(syms) == 1:
        j = {syms[0]: j}
    q, errors = {}, []
    for sym in syms:
        o = j.get(sym)
        if not isinstance(o, dict):
            errors.append(f'Twelve Data {sym}: brak w odpowiedzi')
            continue
        if o.get('status') != 'ok' or not isinstance(o.get('values'), list):
            errors.append(f'Twelve Data {sym}: {o.get("message") or o.get("status") or "błąd"}')
            continue
        rows = []
        for v in o['values']:
            try:
                date = str(v['datetime'])[:10]
                datetime.datetime.strptime(date, '%Y-%m-%d')
                close = round(float(v['close']), 4)
            except (KeyError, TypeError, ValueError):
                continue
            if not close > 0:
                continue
            try:
                volume = int(float(v['volume'])) if v.get('volume') not in (None, '') else None
            except (TypeError, ValueError):
                volume = None
            rows.append([date, close, volume])
        if not rows:
            errors.append(f'Twelve Data {sym}: brak poprawnych świec')
            continue
        rows.sort(key=lambda r: r[0])
        meta = o.get('meta') or {}
        q[sym] = {'asof': rows[-1][0], 'ex': meta.get('mic_code') or meta.get('exchange') or '', 'd': rows}
    return q, errors


def td_batch(syms, key, _retry=True):
    url = f'{TD}/time_series?symbol={",".join(syms)}&interval=1day&outputsize={TD_OUTPUT}&apikey={key}'
    try:
        st, body = get(url)
    except urllib.error.HTTPError as e:
        # treść błędu bez adresu (adres zawiera klucz); 401 = zły klucz
        raise RuntimeError(f'HTTP {e.code}') from None
    j = json.loads(body)
    q, errors = parse_td(j, syms)
    if _retry and any(': 429' in e or 'credits' in e.lower() or 'limit' in e.lower() for e in errors) and not q:
        print('Twelve Data 429 — czekam 61 s')
        time.sleep(TD_SLEEP)
        return td_batch(syms, key, _retry=False)
    return q, errors


def _ny_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo('America/New_York'))
    except Exception:   # brak bazy stref — przyjmij czas letni (UTC−4)
        return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=4)


def _drop_open_session(q, now_ny=None):
    """v51: świeca z dzisiejszą datą przed 16:15 czasu Nowego Jorku to trwająca sesja, nie zamknięcie — pomijamy ją,
    żeby wszystkie regiony liczyły zmianę do tego samego rodzaju ceny (ostatnie zamknięcie)."""
    now_ny = now_ny or _ny_now()
    today = now_ny.date().isoformat()
    if (now_ny.hour, now_ny.minute) >= (16, 15):
        return 0
    n = 0
    for s, v in q.items():
        d = v.get('d') or []
        if d and str(d[-1][0])[:10] == today:
            v['d'] = d[:-1]; n += 1
            if v['d']:
                v['asof'] = str(v['d'][-1][0])[:10]
    if n:
        META['notes'].append(f'Twelve Data: pominięto {n} świec trwającej sesji ({today})')
    return n


def _align_calendar(q):
    """v69: wszystkie ETF-y na wspólnym kalendarzu sesji (daty SPY; bez SPY — daty obecne w co najmniej połowie symboli).
    Świeca spoza kalendarza (np. 25.12 przy święcie w USA) jest usuwana — każdy region liczy zmianę z tych samych sesji."""
    if not q:
        return 0
    spy = q.get('SPY') if isinstance(q.get('SPY'), dict) else None
    lens = sorted(len(v.get('d') or []) for v in q.values())
    if spy and spy.get('d') and len(spy['d']) >= lens[len(lens) // 2] - 5:   # v73: SPY tylko z pełną historią
        cal = {str(r[0])[:10] for r in spy['d']}
    else:
        cnt = {}
        for v in q.values():
            for r in (v.get('d') or []):
                cnt[str(r[0])[:10]] = cnt.get(str(r[0])[:10], 0) + 1
        cal = {d for d, c in cnt.items() if c * 2 >= len(q)}
    n, gone = 0, []
    for sym, v in list(q.items()):
        d = v.get('d') or []
        keep = [r for r in d if str(r[0])[:10] in cal]
        if not keep:
            gone.append(sym); del q[sym]; continue   # v73: bez żadnej świecy w kalendarzu — poza plikiem (build_prices nie padnie)
        if len(keep) != len(d):
            n += len(d) - len(keep); v['d'] = keep
            v['asof'] = str(keep[-1][0])[:10]
    if gone:
        META['notes'].append('Twelve Data: bez świec we wspólnym kalendarzu: ' + ', '.join(sorted(gone)))
    if n:
        META['notes'].append(f'Twelve Data: świece spoza wspólnego kalendarza sesji pominięte: {n}')
    return n


def build_prices(key):
    """data/ceny.json: dzienne zamknięcia 14 ETF-ów zastępczych (te same co DZIŚ), rosnąco po dacie."""
    q, errors = {}, []
    batches = [DAY_SYMS[i:i + TD_BATCH] for i in range(0, len(DAY_SYMS), TD_BATCH)]
    for n, syms in enumerate(batches):
        if n:
            time.sleep(TD_SLEEP)
        bq, be = td_batch(syms, key)
        q.update(bq)
        errors.extend(be)
    META['errors'].extend(errors)
    _drop_open_session(q)
    _align_calendar(q)   # v69: jeden kalendarz sesji dla wszystkich ETF-ów
    good = [s for s, v in q.items() if len(v['d']) >= TD_MIN_CANDLES]
    if len(good) < TD_MIN_SYMBOLS:
        raise RuntimeError(f'tylko {len(good)} symboli z {len(DAY_SYMS)} ma ≥ {TD_MIN_CANDLES} świec')
    dates = sorted({v['asof'] for v in q.values()})
    asof = dates[0] if len(dates) == 1 else f'{dates[0]} – {dates[-1]}'
    for s, v in q.items():
        print(f'{s}: {len(v["d"])} świec, ostatnia {v["asof"]} close {v["d"][-1][1]}')
    return {'at': NOW, 'src': 'Twelve Data', 'plan': 'basic', 'asof': asof, 'q': q}


def build_day(key):
    q = {}
    for sym in DAY_SYMS:
        try:
            st, body = get(f'https://finnhub.io/api/v1/quote?symbol={sym}&token={key}')
            j = json.loads(body)
        except Exception as e:   # v49: jeden symbol z błędem nie przerywa pozostałych (próg ≥10 z 14 zostaje)
            META['errors'].append(mask(f'Finnhub {sym}: {e}')); time.sleep(0.2); continue
        if j.get('c') and j.get('pc'):
            q[sym] = {'c': j['c'], 'pc': j['pc'], 'dp': j['dp'] if j.get('dp') is not None else (j['c'] / j['pc'] - 1) * 100,
                      't': j.get('t')}
        time.sleep(0.2)
    if len(q) < 10:
        raise RuntimeError(f'Finnhub: tylko {len(q)} notowań z {len(DAY_SYMS)}')
    return {'at': NOW, 'src': 'Finnhub', 'q': q}


def build_cmc(key):
    """data/cmc.json — CoinMarketCap global metrics (klucz w nagłówku). Pola nieobecne → None, nigdy 0."""
    j = get_json(f'{CMC}/v1/global-metrics/quotes/latest', {'X-CMC_PRO_API_KEY': key, 'Accept': 'application/json'})
    st = j.get('status') or {}
    if st.get('error_code') not in (None, 0):
        raise RuntimeError(f'CoinMarketCap: {st.get("error_code")} {st.get("error_message")}')
    d = j.get('data') or {}
    usd = (d.get('quote') or {}).get('USD') or {}
    num = lambda v: (float(v) if isinstance(v, (int, float)) else None)
    out = {'at': NOW, 'src': 'CoinMarketCap', 'asof': str(d.get('last_updated') or usd.get('last_updated') or ''),
           'total_mcap': num(usd.get('total_market_cap')), 'total_vol24': num(usd.get('total_volume_24h')),
           'mcap_chg24_pct': num(usd.get('total_market_cap_yesterday_percentage_change')),
           'btc_dom': num(d.get('btc_dominance')), 'eth_dom': num(d.get('eth_dominance')),
           'stable_mcap': num(usd.get('stablecoin_market_cap') if 'stablecoin_market_cap' in usd else d.get('stablecoin_market_cap')),
           'defi_mcap': num(usd.get('defi_market_cap') if 'defi_market_cap' in usd else d.get('defi_market_cap')),
           'altcoin_mcap': num(usd.get('altcoin_market_cap')), 'active': d.get('active_cryptocurrencies')}
    if out['total_mcap'] is None:
        raise RuntimeError('CoinMarketCap: brak total_market_cap')
    return out


def parse_fred(j, sid):
    """FRED observations → [[data, wartość]] rosnąco; '.' albo pusta wartość = brak (pomijamy, nigdy 0)."""
    if not isinstance(j, dict) or 'observations' not in j:
        raise RuntimeError(f'{sid}: odpowiedź bez observations' + (f" ({j.get('error_message')})" if isinstance(j, dict) and j.get('error_message') else ''))
    rows = []
    for o in j.get('observations', []):
        d = str(o.get('date', ''))[:10]
        v = _num(o.get('value')) if str(o.get('value', '')).strip() != '.' else None
        if v is None or not re.match(r'^\d{4}-\d{2}-\d{2}$', d):
            continue
        rows.append([d, v])
    rows.sort(key=lambda x: x[0])
    if not rows:
        raise RuntimeError(f'{sid}: brak obserwacji z wartością')
    meta = FRED_SERIES[sid]
    return {'unit': meta['unit'], 'freq': meta['freq'], 'name': meta['name'], 'asof': rows[-1][0], 'd': rows}


def build_fred(key):
    """data/fred.json — serie Rady Gubernatorów Fed przez FRED API (klucz tylko w adresie zapytania, nigdy w komunikatach).
    Każda seria osobno: awaria jednej nie kasuje pozostałych (brak nie jest zerem)."""
    out = {'at': NOW, 'src': FRED_CITE, 'api_note': FRED_API_NOTE, 'url': 'https://fred.stlouisfed.org/', 'series': {}}
    for sid in FRED_SERIES:
        time.sleep(FRED_SLEEP)
        try:
            j = get_json(f'{FRED}?series_id={sid}&api_key={key}&file_type=json&sort_order=desc&limit={FRED_LIMIT}')
            out['series'][sid] = parse_fred(j, sid)
            print(f"FRED {sid}: stan {out['series'][sid]['asof']}")
        except Exception as e:
            META['errors'].append(mask(f'FRED {sid}: {e}'))
    if not out['series']:
        raise RuntimeError('żadna seria FRED nie odpowiedziała')
    try:   # v50: podsumowanie depozytu H.4.1 — jego błąd nie może zatrzymać zapisu pozostałych serii FRED
        out['custody'] = custody_summary(out['series'])
    except Exception as e:
        out['custody'] = None; META['errors'].append(mask(f'FRED custody: {e}'))
    return out


def parse_deriv(j):
    """CoinGecko /derivatives/exchanges: otwarte pozycje w BTC per giełda; giełda bez liczby pominięta (nie zero)."""
    if not isinstance(j, list):
        raise RuntimeError('derivatives: odpowiedź nie jest listą')
    rows = []
    for x in j:
        oi = x.get('open_interest_btc') if isinstance(x, dict) else None
        if isinstance(oi, bool) or not isinstance(oi, (int, float)) or oi < 0:
            continue
        rows.append([str(x.get('name', ''))[:60], round(float(oi), 2)])
    if not rows:
        raise RuntimeError('derivatives: żadna giełda bez liczby otwartych pozycji')
    rows.sort(key=lambda r: -r[1])
    return {'src': 'CoinGecko — derivatives exchanges', 'unit': 'BTC', 'n': len(rows), 'total_oi_btc': round(sum(r[1] for r in rows), 2), 'top': rows[:5]}


def parse_defi(j):
    """CoinGecko /global/decentralized_finance_defi: liczby jako teksty → float; brak → None (nigdy 0)."""
    d = j.get('data') if isinstance(j, dict) else None
    if not isinstance(d, dict):
        raise RuntimeError('defi: brak pola data')
    out = {'src': 'CoinGecko — global DeFi'}
    for k in ('defi_market_cap', 'eth_market_cap', 'defi_to_eth_ratio', 'trading_volume_24h', 'defi_dominance'):
        out[k] = _num(d.get(k))
    if out['defi_market_cap'] is None:
        raise RuntimeError('defi: brak defi_market_cap')
    return out


def parse_fng(j):
    """Alternative.me Fear & Greed: [[data UTC, wartość 0–100, klasa]] rosnąco (wskaźnik nastroju — model, nie pomiar)."""
    data = j.get('data') if isinstance(j, dict) else None
    if not isinstance(data, list):
        raise RuntimeError('fng: brak pola data')
    rows = []
    for x in data:
        v = _num(x.get('value')); t = _num(x.get('timestamp'))
        if v is None or t is None or not 0 <= v <= 100:
            continue
        day = datetime.datetime.fromtimestamp(int(t), datetime.timezone.utc).date().isoformat()
        rows.append([day, int(round(v)), str(x.get('value_classification', ''))[:20]])
    rows.sort(key=lambda r: r[0])
    if not rows:
        raise RuntimeError('fng: brak wartości')
    return {'src': 'Alternative.me — Crypto Fear & Greed Index', 'url': 'https://alternative.me/crypto/fear-and-greed-index/',
            'kind': 'indicator', 'asof': rows[-1][0], 'd': rows}


def parse_tic_table(raw):
    """TIC SLT (tekst rozdzielany tabulatorami, nagłówek techniczny w wierszu zaczynającym się od 'country\t'):
    {kraj: {YYYY-MM: {kolumna: liczba}}}; puste pola = brak (None), wiersze stopki (bez daty YYYY-MM) pominięte."""
    text = raw.decode('utf-8', errors='replace') if isinstance(raw, (bytes, bytearray)) else str(raw)
    lines = text.splitlines()
    cols = None
    out = {}
    for line in lines:
        parts = line.split('\t')
        if cols is None:
            if parts and parts[0].strip() == 'country':
                cols = [p.strip() for p in parts]
            continue
        if len(parts) < 4 or not re.match(r'^\d{4}-\d{2}$', parts[2].strip()):
            continue
        name = parts[0].strip(); month = parts[2].strip()
        rec = {}
        for i, c in enumerate(cols[3:], start=3):
            rec[c] = _num(parts[i]) if i < len(parts) else None
        out.setdefault(name, {})[month] = rec
    if cols is None or not out:
        raise RuntimeError('TIC: brak nagłówka technicznego albo wierszy z datą')
    return out


def parse_tic_holders(raw):
    """TIC Table 5 (Major Foreign Holders of Treasury Securities, mld USD): {'months': [...], 'rows': [[kraj, [wartości]]]}."""
    text = raw.decode('utf-8', errors='replace') if isinstance(raw, (bytes, bytearray)) else str(raw)
    months = None; rows = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split('\t')]
        if months is None:
            if parts and parts[0] == 'Country' and len(parts) > 1 and re.match(r'^\d{4}-\d{2}$', parts[1]):
                months = parts[1:]
            continue
        if not parts or not parts[0] or parts[0].startswith('Of Which') or parts[0] in ('All Other', 'Grand Total'):
            if parts and parts[0] == 'Grand Total':
                rows.append(['Grand Total', [_num(v) for v in parts[1:len(months) + 1]]])
            continue
        vals = [_num(v) for v in parts[1:len(months) + 1]]
        if any(v is not None for v in vals):
            rows.append([parts[0], vals])
    if not months or not rows:
        raise RuntimeError('TIC tabela 5: brak nagłówka z miesiącami albo wierszy')
    return {'months': months, 'rows': rows}


def _tic_sum(table, members, months, col):
    """Suma kolumny po członkach regionu w każdym miesiącu: [[miesiąc, suma, liczba obecnych członków]]; brak = None, nie 0."""
    rows = []
    for m in months:
        vals = [table.get(name, {}).get(m, {}).get(col) for name in members]
        present = [v for v in vals if v is not None]
        rows.append([m, int(round(sum(present))) if present else None, len(present)])
    return rows


def _tic_net(t1, t2, members, months):
    """Netto do USA = zakupy zagranicy (tabela 1) − zakupy USA (tabela 2) tylko dla krajów obecnych w OBU tabelach w danym
    miesiącu: [[miesiąc, suma, liczba krajów]]; brak = None (np. tabela 2 nie ma Arabii Saudyjskiej)."""
    rows = []
    for m in months:
        vals = []
        for name in members:
            a = t1.get(name, {}).get(m, {}).get('for_lt_total_net')
            b = t2.get(name, {}).get(m, {}).get('us_lt_total_net')
            if a is not None and b is not None:
                vals.append(a - b)
        rows.append([m, int(round(sum(vals))) if vals else None, len(vals)])
    return rows


def build_tic():
    """data/tic.json — przepływy papierów wartościowych USA ↔ regiony strony (mln USD, miesięcznie, TIC SLT).
    in = netto zakupy amerykańskich papierów przez zagranicę (plus = kapitał do USA); out = netto zakupy zagranicznych
    papierów przez USA (plus = kapitał z USA). Tabela 2 i 5 osobno: ich awaria nie kasuje tabeli 1."""
    t1 = parse_tic_table(get_bytes(TIC_BASE + 'slt_table1.txt', timeout=120))
    months = sorted({m for c in t1.values() for m in c})[-TIC_MONTHS:]
    if not months:
        raise RuntimeError('TIC: brak miesięcy')
    try:
        t2 = parse_tic_table(get_bytes(TIC_BASE + 'slt_table2.txt', timeout=120))
    except Exception as e:
        META['errors'].append(mask(f'TIC tabela 2: {e}')); t2 = None
    try:
        holders = parse_tic_holders(get_bytes(TIC_BASE + 'slt_table5.txt'))
    except Exception as e:
        META['errors'].append(mask(f'TIC tabela 5: {e}')); holders = None
    last = months[-1]

    def region(members):
        r = {'members': members, 'n': len(members),
             'in': _tic_sum(t1, members, months, 'for_lt_total_net'), 'in_tr': _tic_sum(t1, members, months, 'for_lt_treas_net'),
             'in_eq': _tic_sum(t1, members, months, 'for_lt_eqty_net'), 'hold_in': _tic_sum(t1, members, [last], 'for_lt_total_pos')[0]}
        if t2 is not None:
            r['out'] = _tic_sum(t2, members, months, 'us_lt_total_net'); r['out_eq'] = _tic_sum(t2, members, months, 'us_lt_eqty_net')
            r['out_gov'] = _tic_sum(t2, members, months, 'us_lt_govt_bond_net'); r['hold_out'] = _tic_sum(t2, members, [last], 'us_lt_total_pos')[0]
            r['net'] = _tic_net(t1, t2, members, months)
        else:
            r['out'] = None; r['out_eq'] = None; r['out_gov'] = None; r['hold_out'] = None; r['net'] = None
        return r

    out = {'at': NOW, 'src': 'U.S. Department of the Treasury — Treasury International Capital (TIC), SLT tables 1, 2, 5',
           'url': 'https://home.treasury.gov/data/treasury-international-capital-tic-system', 'unit': 'mln USD', 'asof': last, 'months': months,
           'sign': 'in: net foreign purchases of U.S. long-term securities (positive = capital into the USA); out: net U.S. purchases of foreign long-term securities (positive = capital out of the USA)',
           'regions': {rid: region(members) for rid, members in TIC_REGIONS.items()},
           'world': region(['Grand Total']), 'carib': region(['Total Caribbean']), 'twn': region(['Taiwan']), 'holders': None}
    if holders:
        hm = holders['months']
        top = []
        for name, vals in holders['rows']:
            if name == 'Grand Total' or vals[0] is None:
                continue
            d1 = (vals[0] - vals[1]) if len(vals) > 1 and vals[1] is not None else None
            d12 = (vals[0] - vals[12]) if len(vals) > 12 and vals[12] is not None else None
            top.append([name, vals[0], None if d1 is None else round(d1, 1), None if d12 is None else round(d12, 1)])
        total = next((vals for name, vals in holders['rows'] if name == 'Grand Total'), None)
        out['holders'] = {'asof': hm[0], 'unit': 'mld USD', 'top': top[:15], 'total': total[0] if total else None,
                          'total_d12': (round(total[0] - total[12], 1) if total and len(total) > 12 and total[12] is not None and total[0] is not None else None)}
    return out


def parse_mk(pages):
    """CoinGecko /coins/markets (strony po 250): [[SYMBOL, kapitalizacja, zm.24h, 7d, 30d, 1y]] — pierwszy (większy) symbol
    wygrywa; brak liczby = None (nigdy 0)."""
    rows, seen, last = [], set(), ''
    for page in pages:
        if not isinstance(page, list):
            raise RuntimeError('markets: odpowiedź nie jest listą')
        for c in page:
            if not isinstance(c, dict):
                continue
            sy = str(c.get('symbol') or '').upper()
            if not sy or sy in seen or not re.match(r'^[A-Z0-9.$-]{1,15}$', sy):
                continue
            seen.add(sy)
            num = lambda k: c.get(k) if isinstance(c.get(k), (int, float)) and not isinstance(c.get(k), bool) else None
            rows.append([sy, num('market_cap'), num('price_change_percentage_24h_in_currency'), num('price_change_percentage_7d_in_currency'),
                         num('price_change_percentage_30d_in_currency'), num('price_change_percentage_1y_in_currency')])
            last = max(last, str(c.get('last_updated') or ''))
    if not rows:
        raise RuntimeError('markets: brak monet')
    return {'src': 'CoinGecko — coins/markets', 'asof': last[:19], 'cols': ['sym', 'mcap', 'p24h', 'p7d', 'p30d', 'p1y'], 'rows': rows}


def parse_stabc(j, top=14):
    """v58: DefiLlama /stablecoins → podaż stablecoinów dolarowych per sieć: teraz, zmiana 1, 7 i 30 dni (USD).
    Zmiana liczona tylko z aktywów, które mają obie wartości (brak poprzedniej = poza oknem, nie zero)."""
    A = j.get('peggedAssets') if isinstance(j, dict) else None
    if not isinstance(A, list) or not A:
        raise RuntimeError('stablecoins: brak peggedAssets')
    num = lambda o: (o.get('peggedUSD') if isinstance(o, dict) and isinstance(o.get('peggedUSD'), (int, float)) and not isinstance(o.get('peggedUSD'), bool) else None)
    ch = {}
    for a in A:
        if not isinstance(a, dict) or a.get('pegType') != 'peggedUSD' or not isinstance(a.get('chainCirculating'), dict):
            continue
        for name, v in a['chainCirculating'].items():
            if not isinstance(v, dict):
                continue
            cur = num(v.get('current'))
            if cur is None or cur < 0:
                continue
            c = ch.setdefault(str(name)[:40], {'cur': 0.0, 'd1': [0.0, 0.0], 'd7': [0.0, 0.0], 'd30': [0.0, 0.0]})
            c['cur'] += cur
            for k, f in (('d1', 'circulatingPrevDay'), ('d7', 'circulatingPrevWeek'), ('d30', 'circulatingPrevMonth')):
                p = num(v.get(f))
                if p is not None and p >= 0:
                    c[k][0] += cur; c[k][1] += p
    if not ch:
        raise RuntimeError('stablecoins: żadna sieć')
    rows = [[n, round(c['cur']), round(c['d1'][0] - c['d1'][1]), round(c['d7'][0] - c['d7'][1]), round(c['d30'][0] - c['d30'][1])]
            for n, c in sorted(ch.items(), key=lambda x: -x[1]['cur'])]
    tot = lambda i: sum(r[i] for r in rows)
    return {'src': 'DefiLlama — stablecoins (chainCirculating, peggedUSD)', 'unit': 'USD', 'asof': NOW[:10],
            'cols': ['sieć', 'podaż', 'zmiana 1 dzień', 'zmiana 7 dni', 'zmiana 30 dni'], 'n': len(rows),
            'total': [round(tot(1)), round(tot(2)), round(tot(3)), round(tot(4))], 'rows': rows[:top]}


def parse_stabh(j):
    """DefiLlama stablecoincharts/all → podaż stablecoinów w USD (totalCirculatingUSD.peggedUSD) teraz i zmiany za 1, 7, 30, 91, 365 dni
    (wartość z ostatniego dnia nie później niż N dni wstecz). To zmiana podaży = emisja − umorzenia, nie zmiana ceny."""
    if not isinstance(j, list) or len(j) < 40:
        raise RuntimeError('stablecoincharts: za krótka seria')
    pts = []
    for o in j:
        t = _num(o.get('date')) if isinstance(o, dict) else None
        tc = (o.get('totalCirculatingUSD') or o.get('totalCirculating') or {}) if isinstance(o, dict) else {}
        v = tc.get('peggedUSD') if isinstance(tc, dict) else None
        if t is None or not isinstance(v, (int, float)) or v <= 0:
            continue
        pts.append((int(t), float(v)))
    pts.sort()
    if len(pts) < 40:
        raise RuntimeError('stablecoincharts: za mało punktów')
    t_last, cur = pts[-1]
    out = {'src': 'DefiLlama — stablecoincharts/all (peggedUSD)', 'unit': 'USD',
           'asof': datetime.datetime.fromtimestamp(t_last, datetime.timezone.utc).date().isoformat(), 'cur': round(cur), 'd': {}, 'pct': {}}
    for n in (1, 7, 30, 91, 365):
        target = t_last - n * 86400
        prev = [v for t, v in pts if t <= target]
        if prev:
            out['d'][str(n)] = round(cur - prev[-1]); out['pct'][str(n)] = round((cur / prev[-1] - 1) * 100, 4)
    out['dd'] = [[datetime.datetime.fromtimestamp(t, datetime.timezone.utc).date().isoformat(), round(v)] for t, v in pts[-71:]]   # v89: 70 dni do TRENDÓW
    return out


# BIS LBS (v50): kwartalne przepływy bankowe między regionami strony — statystyki lokalizacyjne, miara F (zmiana należności
# skorygowana o kursy i przerwy w seriach), bez klucza. Warunki data.bis.org/help/legal: BIS jako źródło, tłumaczenie
# oznaczone jako nieoficjalne, bez sugerowania poparcia BIS. Wynik: data/bis.json (mln USD), pobierany najwyżej raz na dobę.
BIS_ORDER = ['usa', 'can', 'lat', 'eur', 'rus', 'mea', 'afr', 'ind', 'chn', 'jpn', 'asean', 'oce']   # kolejność GREG
BIS_REP = {   # region strony → kraje raportujące (jak GBISREP w index.html); TR, IN, SG, MY publikują tylko sumy (5J)
    'usa': ['US'], 'can': ['CA'], 'lat': ['BR', 'MX', 'CL'], 'eur': ['GB', 'DE', 'FR', 'IT', 'ES', 'NL', 'CH', 'SE'],
    'mea': ['TR'], 'afr': ['ZA'], 'ind': ['IN'], 'chn': ['HK'], 'jpn': ['JP', 'KR'], 'asean': ['SG', 'MY', 'PH'], 'oce': ['AU']}
BIS_CP = {    # region strony → kraje kontrahentów (jak GBISCP w index.html); te same kody mapują też raportujących
    'usa': ['US'], 'can': ['CA'], 'lat': ['BR', 'MX', 'CL', 'CO', 'AR'], 'eur': ['GB', 'DE', 'FR', 'IT', 'ES', 'NL', 'CH', 'SE'],
    'rus': ['RU'], 'mea': ['TR', 'SA', 'AE', 'IL'], 'afr': ['ZA', 'NG', 'EG'], 'ind': ['IN'], 'chn': ['CN', 'HK', 'TW'],
    'jpn': ['JP', 'KR'], 'asean': ['SG', 'ID', 'TH', 'MY', 'VN', 'PH'], 'oce': ['AU', 'NZ']}
BIS_Q = 4             # okno: 4 kolejne kwartały kończące się na ostatnim pełnym
BIS_LASTN = 5         # o jeden więcej niż BIS_Q: serie spóźnione o kwartał nadal pokrywają okno
BIS_URL = ('https://stats.bis.org/api/v2/data/dataflow/BIS/WS_LBS_D_PUB/1.0/Q.F.C.A.TO1.A.5J.A.'
           + '+'.join(c for r in BIS_ORDER for c in BIS_REP.get(r, [])) + '.A.'
           + '+'.join(dict.fromkeys(c for r in BIS_ORDER for c in BIS_CP[r]))
           + f'.N?lastNObservations={BIS_LASTN}&format=csv')
# wymiary klucza, których się spodziewamy — wiersz z innym kluczem nie trafia do sum (ochrona przed zmianą API)
BIS_KEY = {'FREQ': 'Q', 'L_MEASURE': 'F', 'L_POSITION': 'C', 'L_INSTR': 'A', 'L_DENOM': 'TO1', 'L_CURR_TYPE': 'A',
           'L_PARENT_CTY': '5J', 'L_REP_BANK_TYPE': 'A', 'L_CP_SECTOR': 'A', 'L_POS_TYPE': 'N'}
BIS_MISSING = ('H', 'K', 'L', 'M', 'Q')   # OBS_STATUS: braki (Q = „suppressed”, poufne; K = ujęte w innej kategorii) — nigdy 0
_BIS_PERIOD = re.compile(r'^\d{4}-Q[1-4]$')
BIS_SIGN = ('flows["a>b"]: quarterly FX- and break-adjusted change in cross-border claims (all instruments, all sectors) '
            'of banks located in region a on residents of region b; positive = banks in a lent/placed more in b '
            '(bank capital a -> b), negative = they cut exposure (b -> a). pairs["a|b"]: a>b plus b>a. '
            'regions[r].out = sum of r>x; regions[r].in = sum of x>r; regions[r].net = sum over matched country '
            'pairs (both countries report in that quarter) of [claims of r on x] - [claims of x on r]; positive = r is '
            'a net supplier of bank credit to the other regions (net outflow), negative = net recipient (net inflow). '
            'Sum of net over all regions = 0. Residence principle: London branches of US banks count as "eur". '
            'regions[r].rep_q = contribution of each reporting country of r to r.out in the latest quarter.')


def _bis_num(row):
    """OBS_VALUE → mln USD; 'NaN', pusty, status braku → None (nigdy 0). Inna waluta miary = błąd całej odpowiedzi."""
    if row.get('OBS_STATUS') in BIS_MISSING:
        return None
    t = str(row.get('OBS_VALUE', '')).replace(',', '').strip()
    try:
        v = float(t)
    except ValueError:
        return None
    if not (-1e15 < v < 1e15):   # NaN i ±inf nie spełniają nierówności
        return None
    if row.get('UNIT_MEASURE') != 'USD':
        raise RuntimeError(f'nieoczekiwana jednostka {row.get("UNIT_MEASURE")!r} (oczekiwano USD)')
    try:
        mult = int(row.get('UNIT_MULT'))
    except (TypeError, ValueError):
        raise RuntimeError(f'nieczytelny mnożnik jednostki {row.get("UNIT_MULT")!r}')
    return v * 10 ** (mult - 6)          # UNIT_MULT 6 = miliony → bez zmian


def _bis_rows(d, quarters):
    """{kwartał: [suma, n]} → [[kwartał, suma|None, n]] (brak = None i n = 0, nie 0)."""
    return [[q, round(d[q][0], 1) + 0.0, d[q][1]] if q in d and d[q][1] else [q, None, 0] for q in quarters]   # + 0.0: bez „-0.0”


def _bis_total(rows):
    """Suma z pełnego okna; gdy którykolwiek kwartał nie ma danych → None."""
    if len(rows) != BIS_Q or any(r[1] is None for r in rows):
        return None
    return round(sum(r[1] for r in rows), 1) + 0.0


def _bis_qshift(q, k):
    """'2026-Q1' cofnięty o k kwartałów (k=1 → '2025-Q4')."""
    i = int(q[:4]) * 4 + int(q[-1]) - 1 - k
    return f'{i // 4}-Q{i % 4 + 1}'


def parse_bis_flows(raw, at=None):
    """CSV z BIS (miara F) → struktura data/bis.json. Jednostka: mln USD. Brak ≠ 0 na każdym poziomie.
    Okno = BIS_Q kolejnych kwartałów kończących się na ostatnim „pełnym” (co najmniej połowa najliczniejszego);
    kwartał w oknie bez danych zostaje jako brak (None), sumy 4 kwartałów liczone tylko z kompletu."""
    text = raw.decode('utf-8-sig', 'replace') if isinstance(raw, (bytes, bytearray)) else str(raw)
    rd = csv.DictReader(io.StringIO(text))
    need = {'L_REP_CTY', 'L_CP_COUNTRY', 'TIME_PERIOD', 'OBS_VALUE', 'UNIT_MEASURE', 'UNIT_MULT', *BIS_KEY}
    if not rd.fieldnames or not need <= set(rd.fieldnames):
        raise RuntimeError('odpowiedź bez kolumn ' + ', '.join(sorted(need - set(rd.fieldnames or []))))
    c2r = {c: rid for rid, cs in BIS_CP.items() for c in cs}
    obs, count = {}, {}                   # (kraj raportujący, kraj kontrahenta) → {kwartał: mln USD}
    for row in rd:
        if any(row.get(k) != v for k, v in BIS_KEY.items()):
            continue
        rep, cp, q = row['L_REP_CTY'], row['L_CP_COUNTRY'], row['TIME_PERIOD']
        if rep == cp or rep not in c2r or cp not in c2r or not _BIS_PERIOD.match(q or ''):
            continue
        v = _bis_num(row)
        if v is None:
            continue
        obs.setdefault((rep, cp), {})[q] = v
        count[q] = count.get(q, 0) + 1
    if not count:
        raise RuntimeError('brak liczb w odpowiedzi LBS')
    top = max(count.values())
    # ostatni kwartał „pełny” (co najmniej połowa najliczniejszego): stare końcówki zamkniętych serii i świeże
    # szczątkowe publikacje nie wyznaczają okna
    last = max(q for q, c in count.items() if 2 * c >= top)
    quarters = [_bis_qshift(last, k) for k in range(BIS_Q - 1, -1, -1)]
    acc = {}                              # (region a, region b) → {kwartał: [suma, liczba par krajów]}
    for (rep, cp), ser in obs.items():
        a, b = c2r[rep], c2r[cp]
        if a == b:
            continue
        for q in quarters:
            if q in ser:
                s = acc.setdefault((a, b), {}).setdefault(q, [0.0, 0])
                s[0] += ser[q]; s[1] += 1
    if len(acc) < 6:
        raise RuntimeError(f'za mało par regionów w LBS ({len(acc)})')
    pos = {r: i for i, r in enumerate(BIS_ORDER)}
    flows = {f'{a}>{b}': _bis_rows(d, quarters) for (a, b), d in sorted(acc.items(), key=lambda kv: (pos[kv[0][0]], pos[kv[0][1]]))}
    pairs, oneway = {}, []
    for i, a in enumerate(BIS_ORDER):
        for b in BIS_ORDER[i + 1:]:
            d1, d2 = acc.get((a, b), {}), acc.get((b, a), {})
            if not d1 and not d2:
                continue
            m = {}
            for d in (d1, d2):
                for q, (v, n) in d.items():
                    s = m.setdefault(q, [0.0, 0]); s[0] += v; s[1] += n
            key = '|'.join(sorted((a, b)))    # jak klucze GLINK w index.html (a<b alfabetycznie)
            pairs[key] = _bis_rows(m, quarters)
            if not d1 or not d2:
                oneway.append(key)            # tylko jeden kierunek: druga strona nie ma banków raportujących
    net = {r: {} for r in BIS_ORDER}      # saldo na parach krajów, gdzie oba kraje raportują w danym kwartale
    for (rep, cp), ser in obs.items():
        a, b = c2r[rep], c2r[cp]
        back = obs.get((cp, rep))
        if a == b or rep > cp or not back:
            continue
        for q in quarters:
            if q in ser and q in back:
                d = ser[q] - back[q]
                s = net[a].setdefault(q, [0.0, 0]); s[0] += d; s[1] += 1
                s = net[b].setdefault(q, [0.0, 0]); s[0] -= d; s[1] += 1
    seen = {rep for (rep, _), ser in obs.items() if any(q in ser for q in quarters)}
    rep_q = {}                            # wkład kraju raportującego w wypływ regionu w ostatnim kwartale
    for (rep, cp), ser in obs.items():
        if c2r[rep] != c2r[cp] and last in ser:
            s = rep_q.setdefault(rep, [0.0, 0]); s[0] += ser[last]; s[1] += 1
    regions = {}
    for r in BIS_ORDER:
        out_d, in_d = {}, {}
        for (a, b), d in acc.items():
            tgt = out_d if a == r else in_d if b == r else None
            if tgt is None:
                continue
            for q, (v, n) in d.items():
                s = tgt.setdefault(q, [0.0, 0]); s[0] += v; s[1] += n
        reg = {'rep': [c for c in BIS_REP.get(r, []) if c in seen], 'cp': list(BIS_CP[r]),
               'out': _bis_rows(out_d, quarters), 'in': _bis_rows(in_d, quarters), 'net': _bis_rows(net[r], quarters),
               'rep_q': sorted(([c, round(rep_q[c][0], 1) + 0.0, rep_q[c][1]] for c in BIS_REP.get(r, []) if c in rep_q),
                               key=lambda x: -abs(x[1]))}
        for k in ('out', 'in', 'net'):
            reg[k + '4'] = _bis_total(reg[k])
        regions[r] = reg
    return {'at': at, 'src': 'BIS Locational Banking Statistics (WS_LBS_D_PUB), measure F: FX and break adjusted change',
            'url': 'https://data.bis.org/topics/LBS', 'api': BIS_URL, 'unit': 'mln USD', 'asof': last,
            'quarters': quarters, 'sign': BIS_SIGN,
            'no_reporter': [r for r in BIS_ORDER if not regions[r]['rep']],
            'regions': regions, 'flows': flows, 'pairs': pairs, 'oneway': oneway}


def build_bis():
    """data/bis.json — kwartalne przepływy bankowe BIS LBS między regionami strony (mln USD); jedno zapytanie bez klucza."""
    return parse_bis_flows(get_bytes(BIS_URL, timeout=90), NOW)


# --- v50: CFTC Commitments of Traders — Traders in Financial Futures (TFF), tylko futures; dane rządu USA (domena publiczna) ---
# Plik tygodniowy FinFutWk.txt nie ma nagłówka: kolejność 87 kolumn z dokumentacji CFTC (cotvariablestfm.html), sprawdzona
# 24.09.2026 z nagłówkiem pliku rocznego (identyczna). Stan na wtorek, publikacja zwykle w piątek ok. 19:30 UTC.
import zipfile   # v50 CFTC: plik roczny to archiwum zip (biblioteka standardowa)

CFTC_WEEK_URL = 'https://www.cftc.gov/dea/newcot/FinFutWk.txt'
CFTC_YEAR_URL = 'https://www.cftc.gov/files/dea/history/fut_fin_txt_{}.zip'
CFTC_HOME = 'https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm'
CFTC_MARKETS = {'eur': '099741', 'btc': '133741', 'eth': '146021'}   # EURO FX, BITCOIN, ETHER CASH SETTLED — wszystkie CME
CFTC_EXTRA = {'usd': '098662', 'jpy': '097741', 'gbp': '096742', 'chf': '092741', 'cad': '090741', 'aud': '232741', 'mxn': '095741',
              'brl': '102741', 'ust10': '043602', 'spx': '13874A', 'msciem': '244042'}   # v68: dodatkowe rynki z tych samych plików; brak = notatka
CFTC_WEEKS = 13
CFTC_SPAN_DAYS = CFTC_WEEKS * 7 - 1    # historia = raporty z 90 dni przed najnowszym (bez dziur na przełomie roku)
CFTC_KEEP_DAYS = 35                    # rynek nieobecny w obu plikach: poprzedni stan najwyżej 5 tygodni (jego data mówi, jak stary)
CFTC_GROUPS = (('dealer', 'Dealer_Positions', 'Dealer'), ('asset_mgr', 'Asset_Mgr_Positions', 'Asset_Mgr'),
               ('lev_funds', 'Lev_Money_Positions', 'Lev_Money'), ('other_rept', 'Other_Rept_Positions', 'Other_Rept'),
               ('nonrept', 'NonRept_Positions', 'NonRept'))
CFTC_COLS = tuple((
    'Market_and_Exchange_Names As_of_Date_In_Form_YYMMDD Report_Date_as_YYYY-MM-DD CFTC_Contract_Market_Code '
    'CFTC_Market_Code CFTC_Region_Code CFTC_Commodity_Code Open_Interest_All Dealer_Positions_Long_All Dealer_Positions_Short_All '
    'Dealer_Positions_Spread_All Asset_Mgr_Positions_Long_All Asset_Mgr_Positions_Short_All Asset_Mgr_Positions_Spread_All '
    'Lev_Money_Positions_Long_All Lev_Money_Positions_Short_All Lev_Money_Positions_Spread_All Other_Rept_Positions_Long_All '
    'Other_Rept_Positions_Short_All Other_Rept_Positions_Spread_All Tot_Rept_Positions_Long_All Tot_Rept_Positions_Short_All '
    'NonRept_Positions_Long_All NonRept_Positions_Short_All Change_in_Open_Interest_All Change_in_Dealer_Long_All '
    'Change_in_Dealer_Short_All Change_in_Dealer_Spread_All Change_in_Asset_Mgr_Long_All Change_in_Asset_Mgr_Short_All '
    'Change_in_Asset_Mgr_Spread_All Change_in_Lev_Money_Long_All Change_in_Lev_Money_Short_All Change_in_Lev_Money_Spread_All '
    'Change_in_Other_Rept_Long_All Change_in_Other_Rept_Short_All Change_in_Other_Rept_Spread_All Change_in_Tot_Rept_Long_All '
    'Change_in_Tot_Rept_Short_All Change_in_NonRept_Long_All Change_in_NonRept_Short_All Pct_of_Open_Interest_All '
    'Pct_of_OI_Dealer_Long_All Pct_of_OI_Dealer_Short_All Pct_of_OI_Dealer_Spread_All Pct_of_OI_Asset_Mgr_Long_All '
    'Pct_of_OI_Asset_Mgr_Short_All Pct_of_OI_Asset_Mgr_Spread_All Pct_of_OI_Lev_Money_Long_All Pct_of_OI_Lev_Money_Short_All '
    'Pct_of_OI_Lev_Money_Spread_All Pct_of_OI_Other_Rept_Long_All Pct_of_OI_Other_Rept_Short_All Pct_of_OI_Other_Rept_Spread_All '
    'Pct_of_OI_Tot_Rept_Long_All Pct_of_OI_Tot_Rept_Short_All Pct_of_OI_NonRept_Long_All Pct_of_OI_NonRept_Short_All '
    'Traders_Tot_All Traders_Dealer_Long_All Traders_Dealer_Short_All Traders_Dealer_Spread_All Traders_Asset_Mgr_Long_All '
    'Traders_Asset_Mgr_Short_All Traders_Asset_Mgr_Spread_All Traders_Lev_Money_Long_All Traders_Lev_Money_Short_All '
    'Traders_Lev_Money_Spread_All Traders_Other_Rept_Long_All Traders_Other_Rept_Short_All Traders_Other_Rept_Spread_All '
    'Traders_Tot_Rept_Long_All Traders_Tot_Rept_Short_All Conc_Gross_LE_4_TDR_Long_All Conc_Gross_LE_4_TDR_Short_All '
    'Conc_Gross_LE_8_TDR_Long_All Conc_Gross_LE_8_TDR_Short_All Conc_Net_LE_4_TDR_Long_All Conc_Net_LE_4_TDR_Short_All '
    'Conc_Net_LE_8_TDR_Long_All Conc_Net_LE_8_TDR_Short_All Contract_Units CFTC_Contract_Market_Code_Quotes CFTC_Market_Code_Quotes '
    'CFTC_Commodity_Code_Quotes CFTC_SubGroup_Code FutOnly_or_Combined').split())
_CFTC_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _cftc_int(token):
    """Liczba kontraktów: '  41113' → 41113; '.', '', 'nan', 'inf' i nie-liczba → None (nigdy 0)."""
    v = _num(token)
    try:
        return None if v is None else int(round(v))
    except (ValueError, OverflowError):
        return None


def _cftc_iso(day):
    """'RRRR-MM-DD' → datetime.date; zły format albo nieistniejąca data (np. 2026-13-45) → None."""
    if not isinstance(day, str) or not _CFTC_DATE.match(day):
        return None
    try:
        return datetime.date.fromisoformat(day)
    except ValueError:
        return None


def parse_cftc_csv(text, header=None, codes=None):
    """CSV CFTC TFF → {kod rynku: {data raportu: wiersz jako dict nazwa→tekst}}.
    header=None: pierwszy wiersz to nagłówek (plik roczny FinFutYY.txt); plik tygodniowy nie ma nagłówka — podaj CFTC_COLS.
    Wiersz z inną liczbą pól niż nagłówek jest pomijany (zmiana układu pliku ⇒ brak rynku i błąd, nigdy przesunięte liczby)."""
    codes = set(codes or list(CFTC_MARKETS.values()) + list(CFTC_EXTRA.values()))   # v68
    reader = csv.reader(io.StringIO(text))
    cols = [c.strip() for c in (header or next(reader, []))]
    need = {'Report_Date_as_YYYY-MM-DD', 'CFTC_Contract_Market_Code', 'Open_Interest_All'}
    need |= {f'{pos}_{side}_All' for _, pos, _ in CFTC_GROUPS for side in ('Long', 'Short')}
    missing = need - set(cols)
    if missing:
        raise RuntimeError('brak kolumn ' + ', '.join(sorted(missing))[:200])
    out = {}
    for parts in reader:
        if len(parts) != len(cols):
            continue
        r = {c: p.strip() for c, p in zip(cols, parts)}
        code, day = r['CFTC_Contract_Market_Code'], r['Report_Date_as_YYYY-MM-DD']
        if code not in codes or r.get('FutOnly_or_Combined', 'FutOnly') != 'FutOnly' or _cftc_iso(day) is None:
            continue
        out.setdefault(code, {})[day] = r
    return out


def cftc_record(r):
    """Jeden wiersz → liczby dla strony. net = long − short (spreading liczy się po obu stronach, więc się znosi);
    chg_net = zmiana long − zmiana short z kolumn CFTC „Change_in_…” (względem poprzedniego raportu). Brak = None."""
    g = {}
    for key, pos, chg in CFTC_GROUPS:
        lo, sh = _cftc_int(r.get(f'{pos}_Long_All')), _cftc_int(r.get(f'{pos}_Short_All'))
        clo, csh = _cftc_int(r.get(f'Change_in_{chg}_Long_All')), _cftc_int(r.get(f'Change_in_{chg}_Short_All'))
        g[key] = {'long': lo, 'short': sh,
                  'spread': None if key == 'nonrept' else _cftc_int(r.get(f'{pos}_Spread_All')),   # małe pozycje: CFTC nie dzieli
                  'net': lo - sh if lo is not None and sh is not None else None,
                  'chg_net': clo - csh if clo is not None and csh is not None else None}
    return {'date': r.get('Report_Date_as_YYYY-MM-DD'), 'name': r.get('Market_and_Exchange_Names', ''),
            'units': r.get('Contract_Units', ''), 'oi': _cftc_int(r.get('Open_Interest_All')),
            'oi_chg': _cftc_int(r.get('Change_in_Open_Interest_All')), 'g': g}


def cftc_consistent(rec):
    """Strażnik przesunięcia kolumn: suma long (i osobno short) wszystkich grup + spreading = open interest.
    W danych CFTC 2025–2026 różnica wynosi najwyżej 0,0014% (dla EUR/BTC/ETH zawsze 0); tolerancja 0,5%."""
    oi = rec['oi']
    if oi is None or oi <= 0:
        return False
    for side in ('long', 'short'):
        vals = [x[side] for x in rec['g'].values()] + [x['spread'] for k, x in rec['g'].items() if k != 'nonrept']
        if any(v is None for v in vals) or abs(sum(vals) - oi) > max(5, oi * 0.005):
            return False
    return True


def _cftc_hv(seq, i):
    """Wartość z poprzedniej historii: tylko liczba całkowita; inaczej brak (None), nigdy 0."""
    v = seq[i] if isinstance(seq, list) and i < len(seq) else None
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def build_cftc(fetch=None, today=None, prev=None):
    """data/cftc.json — pozycje grup uczestników (TFF, futures-only) dla EUR, BTC, ETH na CME: stan z ostatniego raportu
    (plik tygodniowy) + netto z raportów z ostatnich 90 dni (plik roczny; w styczniu dociągany też rok poprzedni).
    Każda część osobno: awaria pliku rocznego nie kasuje bieżącego tygodnia i odwrotnie. prev = poprzedni cftc.json ze strony:
    gdy rynku brak w obu plikach — zostaje poprzedni stan (kept=True, najwyżej 35 dni; jego asof mówi, jak stary); gdy historia
    jest krótsza niż 13 raportów — brakujące starsze tygodnie z okna 90 dni uzupełnia poprzednia historia. Gdy żaden rynek
    nie ma danych z tego pobrania — wyjątek (main zachowuje wtedy poprzedni plik z jego prawdziwym „at”)."""
    fetch = fetch or (lambda u: get_bytes(u, timeout=120))
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    errors = []
    week = {}
    try:
        week = parse_cftc_csv(fetch(CFTC_WEEK_URL).decode('utf-8', 'replace'), header=CFTC_COLS)
    except Exception as e:
        errors.append(f'CFTC tydzień: {e}')
    hist = {}

    def load_year(y):
        try:
            with zipfile.ZipFile(io.BytesIO(fetch(CFTC_YEAR_URL.format(y)))) as z:
                names = [n for n in z.namelist() if n.lower().endswith('.txt')]
                if not names:
                    raise RuntimeError('brak pliku .txt w archiwum')
                text = z.read(names[0]).decode('utf-8', 'replace')
            for code, rows in parse_cftc_csv(text).items():
                for day, r in rows.items():
                    hist.setdefault(code, {}).setdefault(day, r)
        except urllib.error.HTTPError as e:
            errors.append(f'CFTC rok {y}: HTTP {e.code}' + (' (w pierwszych dniach stycznia to normalne)' if e.code == 404 else ''))
        except Exception as e:
            errors.append(f'CFTC rok {y}: {e}')

    load_year(today.year)
    if any(len(set(hist.get(c, {})) | set(week.get(c, {}))) < CFTC_WEEKS + 1 for c in CFTC_MARKETS.values()):
        load_year(today.year - 1)
    fields = ['oi'] + [gk for gk, _, _ in CFTC_GROUPS]
    markets = {}
    notes = []   # v68: problemy dodatkowych rynków to notatki, nie błędy
    for key, code in list(CFTC_MARKETS.items()) + list(CFTC_EXTRA.items()):
        rows = dict(hist.get(code, {}))
        rows.update(week.get(code, {}))           # ten sam tydzień: wygrywa plik tygodniowy (liczby są identyczne)
        recs = []
        for day in sorted(rows):
            rec = cftc_record(rows[day])
            if cftc_consistent(rec):
                recs.append(rec)
            else:
                (errors if key in CFTC_MARKETS else notes).append(f'CFTC {key} {day}: suma pozycji ≠ open interest — wiersz pominięty')
        if recs:                                  # rok poprzedni (lub stara historia) nie może wejść do „13 tygodni” z dziurą
            last_d = _cftc_iso(recs[-1]['date'])
            recs = [x for x in recs if (last_d - _cftc_iso(x['date'])).days <= CFTC_SPAN_DAYS]
        prev_m = ((prev.get('markets') or {}).get(key) if isinstance(prev.get('markets'), dict) else None) if isinstance(prev, dict) else None
        prev_m = prev_m if isinstance(prev_m, dict) else None
        if not recs:
            (errors if key in CFTC_MARKETS else notes).append(f'CFTC {key}: brak rynku {code} w raporcie')
            pd = _cftc_iso(prev_m.get('asof')) if prev_m else None
            kept_m = dict(prev_m, kept=True) if pd and 0 <= (today - pd).days <= CFTC_KEEP_DAYS else None
            if kept_m is not None or key in CFTC_MARKETS:   # v68: brakujący dodatkowy rynek w ogóle nie trafia do pliku
                markets[key] = kept_m
            continue
        last = recs[-1]
        last_d = _cftc_iso(last['date'])
        before = recs[-2] if len(recs) > 1 and (last_d - _cftc_iso(recs[-2]['date'])).days <= 10 else None
        for gk, x in last['g'].items():           # zapas: gdy CFTC nie podał zmiany, różnica do poprzedniego raportu
            if x['chg_net'] is None and before and x['net'] is not None and before['g'][gk]['net'] is not None:
                x['chg_net'] = x['net'] - before['g'][gk]['net']
        if last['oi_chg'] is None and before and before['oi'] is not None:
            last['oi_chg'] = last['oi'] - before['oi']
        byday = {}
        ph = prev_m.get('hist') if prev_m and len(recs) < CFTC_WEEKS else None
        if isinstance(ph, dict) and isinstance(ph.get('dates'), list):
            for i, day in enumerate(ph['dates']):
                d = _cftc_iso(day)
                if d and day < recs[0]['date'] and (last_d - d).days <= CFTC_SPAN_DAYS:   # starsze niż pobrane, w oknie 90 dni
                    byday[day] = {f: _cftc_hv(ph.get(f), i) for f in fields}
        for r in recs:
            byday[r['date']] = dict({'oi': r['oi']}, **{gk: r['g'][gk]['net'] for gk, _, _ in CFTC_GROUPS})
        days = sorted(byday)[-CFTC_WEEKS:]
        h = {'dates': days}
        for f in fields:
            h[f] = [byday[d][f] for d in days]
        markets[key] = {'code': code, 'name': last['name'], 'units': last['units'], 'asof': last['date'],
                        'in_week_file': last['date'] in week.get(code, {}), 'oi': last['oi'], 'oi_chg': last['oi_chg'],
                        'groups': {gk: {k: v for k, v in x.items() if k in ('long', 'short', 'spread', 'net', 'chg_net')}
                                   for gk, x in last['g'].items()},
                        'hist': h}
    for e in errors:
        META['errors'].append(mask(e))
    for n in notes:
        META['notes'].append(mask(n))
    live = [m for m in markets.values() if m]
    if not any(not m.get('kept') for m in live):
        raise RuntimeError('żaden rynek nie ma danych z tego pobrania (szczegóły w osobnych błędach CFTC)')
    return {'at': NOW, 'src': 'CFTC — Commitments of Traders: Traders in Financial Futures (futures only)',
            'url': CFTC_HOME, 'data_url': CFTC_WEEK_URL, 'unit': 'kontrakty', 'asof': max(m['asof'] for m in live),
            'net': 'long − short; spreading is counted on both sides and cancels out',
            'order': [gk for gk, _, _ in CFTC_GROUPS], 'markets': markets}


# v92: CFTC — raport „disaggregated” (tylko futures): pozycje grup w kontraktach na surowce — złoto, srebro, miedź (COMEX), ropa WTI (NYMEX).
# Dane rządu USA (domena publiczna). Plik tygodniowy bez nagłówka: pierwsze 23 kolumny w kolejności pliku rocznego (sprawdzone 25.09.2026:
# 274 wiersze tygodnia identyczne z plikiem rocznym). Stan na wtorek, publikacja w piątek. Wynik: data/surowce.json.
CFTCD_WEEK_URL = 'https://www.cftc.gov/dea/newcot/f_disagg.txt'
CFTCD_YEAR_URL = 'https://www.cftc.gov/files/dea/history/fut_disagg_txt_{}.zip'
CFTCD_MARKETS = {'gold': '088691', 'silver': '084691', 'copper': '085692', 'wti': '067651'}
CFTCD_COLS = ('Market_and_Exchange_Names', 'As_of_Date_In_Form_YYMMDD', 'Report_Date_as_YYYY-MM-DD', 'CFTC_Contract_Market_Code',
              'CFTC_Market_Code', 'CFTC_Region_Code', 'CFTC_Commodity_Code', 'Open_Interest_All', 'Prod_Merc_Positions_Long_All',
              'Prod_Merc_Positions_Short_All', 'Swap_Positions_Long_All', 'Swap__Positions_Short_All', 'Swap__Positions_Spread_All',
              'M_Money_Positions_Long_All', 'M_Money_Positions_Short_All', 'M_Money_Positions_Spread_All', 'Other_Rept_Positions_Long_All',
              'Other_Rept_Positions_Short_All', 'Other_Rept_Positions_Spread_All', 'Tot_Rept_Positions_Long_All', 'Tot_Rept_Positions_Short_All',
              'NonRept_Positions_Long_All', 'NonRept_Positions_Short_All')
CFTCD_GROUPS = (('prod', 'Prod_Merc_Positions_Long_All', 'Prod_Merc_Positions_Short_All', None),
                ('swap', 'Swap_Positions_Long_All', 'Swap__Positions_Short_All', 'Swap__Positions_Spread_All'),
                ('mm', 'M_Money_Positions_Long_All', 'M_Money_Positions_Short_All', 'M_Money_Positions_Spread_All'),
                ('other', 'Other_Rept_Positions_Long_All', 'Other_Rept_Positions_Short_All', 'Other_Rept_Positions_Spread_All'),
                ('nonrept', 'NonRept_Positions_Long_All', 'NonRept_Positions_Short_All', None))


def parse_cftcd(text, header=None):
    """Plik disaggregated (CSV) → {kod rynku: {data: rekord}} dla rynków CFTCD_MARKETS; nagłówek z pliku (roczny) albo podany (tygodniowy).
    Rekord: {'name','date','oi','g': {grupa: {long, short, spread, net}}}; wiersz, w którym pozycje grup nie sumują się do open interest,
    trafia do 'bad' (pomijany — brak, nie zero)."""
    rows = list(csv.reader(io.StringIO(text)))
    if header is None:
        if not rows:
            return {}, []
        header, rows = rows[0], rows[1:]
    I = {n.strip(): i for i, n in enumerate(header)}
    want = set(CFTCD_MARKETS.values())
    out, bad = {}, []
    for r in rows:
        if len(r) < len(CFTCD_COLS) or r[I['CFTC_Contract_Market_Code']].strip() not in want:
            continue
        v = lambda n: _cftc_int(r[I[n]]) if n else 0
        day = r[I['Report_Date_as_YYYY-MM-DD']].strip()
        if not _cftc_iso(day):
            bad.append(f'{r[I["CFTC_Contract_Market_Code"]].strip()} {day} (data)'); continue   # v94: nieznany format daty — wiersz pominięty
        oi = v('Open_Interest_All')
        g = {}
        for gk, lo, sh, sp in CFTCD_GROUPS:
            L, S, P = v(lo), v(sh), v(sp) if sp else None
            g[gk] = {'long': L, 'short': S, 'spread': P, 'net': L - S if L is not None and S is not None else None}
        tl = [oi] + [x['long'] for x in g.values()] + [x['short'] for x in g.values()] + [g[k]['spread'] for k in ('swap', 'mm', 'other')]
        if None in tl or sum(x['long'] + (x['spread'] or 0) for x in g.values()) != oi or sum(x['short'] + (x['spread'] or 0) for x in g.values()) != oi:
            bad.append(f'{r[I["CFTC_Contract_Market_Code"]].strip()} {day}'); continue
        out.setdefault(r[I['CFTC_Contract_Market_Code']].strip(), {})[day] = {'name': r[I['Market_and_Exchange_Names']].strip(), 'date': day, 'oi': oi, 'g': g}
    return out, bad


def build_surowce(fetch=None, today=None, prev=None):
    """data/surowce.json — pozycje w kontraktach na złoto, srebro, miedź i ropę WTI: stan z ostatniego raportu + netto grup z 13 raportów."""
    fetch = fetch or (lambda u: get_bytes(u, timeout=120))
    today = today or _now_utc().date()
    errors, rows, bad = [], {}, []

    def add(parsed):
        p, b = parsed
        bad.extend(b)
        for code, days in p.items():
            for d, rec in days.items():
                rows.setdefault(code, {})[d] = rec          # plik tygodniowy wczytywany na końcu — wygrywa (liczby są identyczne)

    def year(y):
        try:
            with zipfile.ZipFile(io.BytesIO(fetch(CFTCD_YEAR_URL.format(y)))) as z:
                names = [n for n in z.namelist() if n.lower().endswith('.txt')]
                if not names:
                    raise RuntimeError('brak pliku .txt w archiwum')
                add(parse_cftcd(z.read(names[0]).decode('utf-8', 'replace')))
        except urllib.error.HTTPError as e:
            errors.append(f'CFTC surowce rok {y}: HTTP {e.code}' + (' (w pierwszych dniach stycznia to normalne)' if e.code == 404 else ''))
        except Exception as e:
            errors.append(f'CFTC surowce rok {y}: {e}')
    year(today.year)
    if any(len(rows.get(c, {})) < CFTC_WEEKS + 1 for c in CFTCD_MARKETS.values()):
        year(today.year - 1)
    try:
        add(parse_cftcd(fetch(CFTCD_WEEK_URL).decode('utf-8', 'replace'), header=CFTCD_COLS))
    except Exception as e:
        errors.append(f'CFTC surowce tydzień: {e}')
    markets = {}
    for key, code in CFTCD_MARKETS.items():
        recs = [rows[code][d] for d in sorted(rows.get(code, {}))]
        if not recs:
            errors.append(f'CFTC surowce: brak rynku {key} ({code})'); continue
        last = recs[-1]
        last_d = _cftc_iso(last['date'])
        recs = [x for x in recs if (last_d - _cftc_iso(x['date'])).days <= CFTC_SPAN_DAYS][-CFTC_WEEKS:]
        h = {'dates': [x['date'] for x in recs], 'oi': [x['oi'] for x in recs]}
        for gk, _, _, _ in CFTCD_GROUPS:
            h[gk] = [x['g'][gk]['net'] for x in recs]
        markets[key] = {'code': code, 'name': last['name'], 'asof': last['date'], 'oi': last['oi'], 'groups': last['g'], 'hist': h}
    if bad:
        META['notes'].append('CFTC surowce: pozycje ≠ open interest (wiersz pominięty): ' + ', '.join(bad[-5:]))
    for e in errors:
        META['errors'].append(mask(e))
    if not markets:
        raise RuntimeError('żaden rynek surowców nie ma danych')
    pm = prev.get('markets') if isinstance(prev, dict) and isinstance(prev.get('markets'), dict) else {}   # v94: bez pliku rocznego — nie skracamy historii
    short = [k for k, m in markets.items() if len(m['hist']['dates']) < CFTC_WEEKS // 2 <= len(((pm.get(k) or {}).get('hist') or {}).get('dates') or [])]
    if short:
        raise RuntimeError(f'krótka historia ({", ".join(short)}) — zostaje poprzedni plik')
    return {'at': NOW, 'src': 'CFTC — Commitments of Traders: Disaggregated (futures only)', 'url': CFTC_HOME, 'data_url': CFTCD_WEEK_URL,
            'unit': 'kontrakty', 'asof': max(m['asof'] for m in markets.values()), 'order': [g for g, _, _, _ in CFTCD_GROUPS], 'markets': markets}


# --- Coin Metrics Community (bez klucza): przepływy BTC i ETH na giełdy i z giełd, zapas na giełdach -------------------
# Licencja danych: CC BY-NC 4.0 (docs.coinmetrics.io/api/v4 → „Available to the community under the Creative Commons
# license” z linkiem do by-nc/4.0; github.com/coinmetrics/data/LICENSE). Limit Community: 10 zapytań / 6 s na IP.
# Jedno zapytanie na przebieg: 2 aktywa × 6 metryk × 36 dni (limit_per_asset + paging_from=end → rosnąco po dacie).
# 36, nie 35: limit liczy też najnowszy dzień, który Coin Metrics jeszcze publikuje (ok. 02–03 UTC). Gdy parser cofa się
# wtedy do ostatniego pełnego dnia, okno 35 dni nadal jest pełne — inaczej najstarszy dzień okna byłby fałszywą „luką”.
CM_ASSETS = ('btc', 'eth')
CM_DAYS = 35
CM_METRICS = (('in', 'FlowInExNtv'), ('out', 'FlowOutExNtv'), ('in_usd', 'FlowInExUSD'),
              ('out_usd', 'FlowOutExUSD'), ('sply', 'SplyExNtv'), ('sply_usd', 'SplyExUSD'))
CM_URL = ('https://community-api.coinmetrics.io/v4/timeseries/asset-metrics?assets=' + ','.join(CM_ASSETS)
          + '&metrics=' + ','.join(m for _, m in CM_METRICS)
          + f'&frequency=1d&limit_per_asset={CM_DAYS + 1}&paging_from=end&page_size=1000'
          # metryka przeniesiona do planu płatnego nie zwraca wtedy 400 dla CAŁEGO zapytania (sprawdzone 24.09.2026)
          + '&ignore_unsupported_errors=true')
CM_LICENSE_URL = 'https://creativecommons.org/licenses/by-nc/4.0/'
CM_ATTR = ('Source: Coin Metrics Community Network Data (https://coinmetrics.io), licensed under CC BY-NC 4.0 '
           '(https://creativecommons.org/licenses/by-nc/4.0/). Net flows and 7/30-day sums computed by CapitalFlowAI. '
           'Coin Metrics does not endorse this site.')
CM_COLS = ['date', 'in', 'out', 'net', 'in_usd', 'out_usd', 'net_usd', 'sply', 'sply_usd']


def _cm_val(v):
    """Liczba z tekstu Coin Metrics ('28280.09508818'); brak, nie-liczba, nan/inf albo wartość ujemna → None (nigdy 0).
    Przepływy i zapas na giełdach nie mogą być ujemne — ujemna liczba to błąd dostawcy, nie pomiar."""
    if v is None or isinstance(v, bool):
        return None
    x = _num(v)
    if x is None or x != x or x in (float('inf'), float('-inf')) or x < 0:
        return None
    return x


def _cm_round(key, v):
    if v is None:
        return None
    return int(round(v)) if key.endswith('_usd') else round(v, 2)


def _cm_sum(by_day, last, days, key):
    """Suma `key` z `days` kolejnych dni kalendarzowych kończących się na `last`; brak choćby jednego dnia → None."""
    d0 = datetime.date.fromisoformat(last)
    total = 0.0
    for i in range(days):
        v = by_day.get((d0 - datetime.timedelta(days=i)).isoformat(), {}).get(key)
        if v is None:
            return None
        total += v
    return total


def _cm_net(rec, a, b):
    return rec[a] - rec[b] if rec.get(a) is not None and rec.get(b) is not None else None


def parse_cm_asset(rows, asset):
    """Wiersze jednego aktywa → {'sym','asof','status','pending','d','missing','last','sum7','sum30','sply_ch7','sply_ch30'};
    None gdy brak dni. d: CM_DAYS dni kalendarzowych rosnąco kończących się na ostatnim dniu z danymi; dzień bez wiersza = same None.
    Netto liczone z liczb niezaokrąglonych (dopiero wynik jest zaokrąglany)."""
    by_day, status = {}, {}
    for r in rows:
        if not isinstance(r, dict) or r.get('asset') != asset:
            continue
        day = str(r.get('time', ''))[:10]
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', day):
            continue
        try:
            datetime.date.fromisoformat(day)      # np. '2026-02-30' przechodzi przez wzorzec, ale nie jest datą
        except ValueError:
            continue
        rec = {k: _cm_val(r.get(m)) for k, m in CM_METRICS}
        if all(v is None for v in rec.values()):
            continue
        rec['net'] = _cm_net(rec, 'in', 'out')
        rec['net_usd'] = _cm_net(rec, 'in_usd', 'out_usd')
        by_day[day] = rec
        status[day] = {str(r.get(m + '-status')) for _, m in CM_METRICS if r.get(m) is not None and r.get(m + '-status')}
    if not by_day:
        return None
    last = max(by_day)
    # Metryki nowego dnia pojawiają się po kolei (natywne od ok. 01:10 UTC, USD do ~70 min później). Gdy najnowszy dzień
    # jest jeszcze niepełny, a dzień wcześniej jest pełny — pokazujemy ten pełny (inaczej USD i sumy 7/30 dni = null
    # przez ~1 h dziennie, a BTC i ETH mogą mieć różne daty). Starsza luka niż 1 dzień = zmiana u dostawcy → bez cofania.
    full = [day for day, rec in by_day.items() if all(rec[k] is not None for k, _ in CM_METRICS)]
    pending = None
    if full and last not in full and (datetime.date.fromisoformat(last) - datetime.date.fromisoformat(max(full))).days == 1:
        pending, last = last, max(full)
    d0 = datetime.date.fromisoformat(last)
    cal = [(d0 - datetime.timedelta(days=i)).isoformat() for i in range(CM_DAYS - 1, -1, -1)]
    d = [[day] + [_cm_round(k, by_day.get(day, {}).get(k)) for k in CM_COLS[1:]] for day in cal]
    st = status.get(last, set())
    out = {'sym': asset.upper(), 'asof': last,
           # 'flash' = wstępne (Coin Metrics może je poprawić), 'reviewed' = po przeglądzie; mieszane → 'flash'
           'status': 'flash' if 'flash' in st else ('reviewed' if st == {'reviewed'} else None),
           'pending': pending,   # najnowszy dzień, który Coin Metrics jeszcze publikuje (pominięty), albo null
           'd': d, 'missing': sum(1 for day in cal if day not in by_day),
           'last': {k: _cm_round(k, by_day[last].get(k)) for k in CM_COLS[1:]}}
    flow_keys = ('in', 'out', 'net', 'in_usd', 'out_usd', 'net_usd')
    for n in (7, 30):
        out[f'sum{n}'] = {k: _cm_round(k, _cm_sum(by_day, last, n, k)) for k in flow_keys}
        now_s = by_day[last].get('sply')
        then_s = by_day.get((d0 - datetime.timedelta(days=n)).isoformat(), {}).get('sply')
        ch = (now_s - then_s) if now_s is not None and then_s is not None else None
        out[f'sply_ch{n}'] = {'ntv': _cm_round('sply', ch),
                              'pct': round(ch / then_s * 100, 2) if ch is not None and then_s else None}
    return out


def parse_cm(j):
    """Coin Metrics /timeseries/asset-metrics → data/cm.json. Każde aktywo osobno: brak jednego nie kasuje drugiego."""
    if isinstance(j, dict) and isinstance(j.get('error'), dict):
        raise RuntimeError('Coin Metrics: ' + str(j['error'].get('message', j['error']))[:200])
    rows = j.get('data') if isinstance(j, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError('Coin Metrics: brak pola data')
    if j.get('next_page_token'):
        META['errors'].append('Coin Metrics: odpowiedź podzielona na strony — użyto tylko pierwszej')
    seen = {m for r in rows if isinstance(r, dict) for _, m in CM_METRICS if r.get(m) is not None}
    for _, m in CM_METRICS:
        if m not in seen:
            META['errors'].append(f'Coin Metrics: brak metryki {m} w odpowiedzi')
    out = {'at': NOW, 'src': 'Coin Metrics Community Network Data — API v4 asset-metrics (exchange flows)',
           'url': 'https://docs.coinmetrics.io/api/v4/', 'home': 'https://coinmetrics.io',
           'license': 'CC BY-NC 4.0', 'license_url': CM_LICENSE_URL, 'attribution': CM_ATTR,
           'unit': {'ntv': 'native units (BTC, ETH)', 'usd': 'USD'},
           'sign': 'net = in - out; positive = more coins sent to exchanges than withdrawn (excl. exchange-to-exchange)',
           'cols': CM_COLS, 'asof': None, 'assets': {}}
    for a in CM_ASSETS:
        out['assets'][a] = parse_cm_asset(rows, a)
        if out['assets'][a] is None:
            META['errors'].append(f'Coin Metrics: brak dni dla {a}')
    dates = sorted({v['asof'] for v in out['assets'].values() if v})
    if not dates:
        raise RuntimeError('Coin Metrics: żadne aktywo nie ma danych')
    out['asof'] = dates[0] if len(dates) == 1 else f'{dates[0]} – {dates[-1]}'
    return out


def build_cm():
    """data/cm.json — jedno zapytanie bez klucza do Coin Metrics Community (limit 10 zapytań / 6 s na IP)."""
    return parse_cm(get_json(CM_URL))


# --- v50: Fed H.4.1 — papiery w depozycie Fed dla zagranicznych instytucji oficjalnych i międzynarodowych (serie w FRED_SERIES) ---
FRED_CUSTODY = ('WSEFINTL1', 'WMTSECL1', 'WFASECL1', 'WSEFINOL')   # razem; w tym Skarb USA; agencje i MBS; pozostałe
CUSTODY_TOL = 5     # mln USD: części H.4.1 są zaokrąglane („Components may not sum to totals because of rounding”)


def _fed_day_back(date_str, days):
    return (datetime.date.fromisoformat(date_str) - datetime.timedelta(days=days)).isoformat()


def custody_summary(series):
    """Z out['series'] build_fred: stan środowy (H.4.1, Table 1A, Wednesday level) papierów w depozycie Fed dla zagranicznych
    instytucji oficjalnych. Zmiany tylko względem DOKŁADNIE tej samej środy 1/4/52 tygodnie wcześniej (brak takiej środy = None,
    nigdy 0 i nigdy starszy tydzień); części (Skarb USA, agencje/MBS, pozostałe) tylko z tej samej daty co suma."""
    fin = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v not in (float('inf'), float('-inf')) else None
    tot = (series or {}).get('WSEFINTL1') or {}
    rows = sorted([[d, v] for d, v in (tot.get('d') or []) if fin(v) is not None], key=lambda r: r[0])
    if not rows:
        return None
    idx = {sid: {d: fin(v) for d, v in (((series or {}).get(sid) or {}).get('d') or [])} for sid in FRED_CUSTODY}
    asof, total = rows[-1]
    at = lambda sid, day: idx[sid].get(day)
    out = {'asof': asof, 'unit': 'mln USD', 'total': total, 'ust': at('WMTSECL1', asof),
           'agency': at('WFASECL1', asof), 'other': at('WSEFINOL', asof)}
    for key, days in (('d1w', 7), ('d4w', 28), ('d52w', 364)):
        day = _fed_day_back(asof, days)
        p = at('WSEFINTL1', day)
        out[key] = None if p is None else round(total - p, 1)
        u0, u1 = at('WMTSECL1', day), out['ust']
        out['ust_' + key] = None if u0 is None or u1 is None else round(u1 - u0, 1)
    out['ust_share_pct'] = round(100.0 * out['ust'] / total, 1) if out['ust'] is not None and total else None
    parts = [out['ust'], out['agency'], out['other']]
    out['parts_ok'] = None if any(p is None for p in parts) else abs(sum(parts) - total) <= CUSTODY_TOL
    year = [r for r in rows if r[0] >= _fed_day_back(asof, 364)]
    lo = min(year, key=lambda r: r[1]); hi = max(year, key=lambda r: r[1])
    out['lo52'] = [lo[0], lo[1]]; out['hi52'] = [hi[0], hi[1]]; out['n52'] = len(year)
    return out


# --- v50: rezerwy walutowe dużych posiadaczy — MFW (IMF), International Liquidity (IL), API SDMX 3.0 bez klucza ---
# Warunki MFW („The Use of IMF Data”): wolno pobierać i publikować z atrybucją „Source: International Monetary Fund, <baza>”;
# przekształcenie trzeba oznaczyć (strona: mld USD, złoto jako różnica, zmiany liczone przez stronę).
RES_COUNTRIES = ['CHN', 'JPN', 'CHE', 'IND', 'TWN', 'SAU', 'KOR', 'BRA']
RES_NAMES = {'CHN': ('Chiny', 'China'), 'JPN': ('Japonia', 'Japan'), 'CHE': ('Szwajcaria', 'Switzerland'),
             'IND': ('Indie', 'India'), 'TWN': ('Tajwan', 'Taiwan'), 'SAU': ('Arabia Saudyjska', 'Saudi Arabia'),
             'KOR': ('Korea Płd.', 'Korea'), 'BRA': ('Brazylia', 'Brazil')}
RES_IND = {'TRGMV_REVS': 'total', 'RXF11_REVS': 'ex_gold', 'RXF11FX_REVS': 'fx'}   # razem ze złotem rynkowo; bez złota; waluty obce
IMF_IL_URL = ('https://api.imf.org/external/sdmx/3.0/data/dataflow/IMF.STA/IL/+/'
              + '+'.join(RES_COUNTRIES) + '.' + '+'.join(RES_IND) + '.USD.M?lastNObservations=13')
IMF_IL_PAGE = 'https://data.imf.org/en/datasets/IMF.STA:IL'
IMF_IL_SRC = 'International Monetary Fund, International Liquidity (IL)'
_IMF_PERIOD = re.compile(r'^(\d{4})-M?(\d{2})$')


def _imf_month(p):
    """'2026-M06' albo '2026-06' → '2026-06'; inny zapis → None."""
    m = _IMF_PERIOD.match(str(p or '').strip())
    return f'{m.group(1)}-{m.group(2)}' if m and 1 <= int(m.group(2)) <= 12 else None


def _imf_month_add(ym, k):
    t = int(ym[:4]) * 12 + int(ym[5:7]) - 1 + k
    return f'{t // 12:04d}-{t % 12 + 1:02d}'


def parse_imf_sdmx(j, norm=None):
    """SDMX-JSON 2.0 z API MFW 3.0: {(kod wymiaru 1, 2, …): [[YYYY-MM, liczba]]} rosnąco. Klucz serii '0:2:0:0' = indeksy
    wartości wymiarów wg keyPosition; obserwacja [OBS_VALUE, atrybuty…]; wartość pusta / nie-liczba / NaN pominięta (nigdy 0).
    Brak jakiejkolwiek serii = błąd (API odpowiada 200 bez 'series')."""
    try:
        st = j['data']['structures'][0]; ds = j['data']['dataSets'][0]
        sdims = sorted(st['dimensions']['series'], key=lambda d: d.get('keyPosition', 0))
        periods = [v.get('value') or v.get('id') for v in st['dimensions']['observation'][0]['values']]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f'nieznany kształt odpowiedzi ({e})')
    out = {}
    for key, s in (ds.get('series') or {}).items():
        try:
            lab = tuple(sdims[i]['values'][int(n)]['id'] for i, n in enumerate(key.split(':')))
        except (IndexError, ValueError, KeyError):
            continue
        rows = []
        for oi, ov in ((s or {}).get('observations') or {}).items():
            try:
                per = (norm or _imf_month)(periods[int(oi)])   # v59: norm — np. kwartały COFER
            except (IndexError, ValueError):
                continue
            v = _num(ov[0]) if isinstance(ov, list) and ov and ov[0] is not None else None
            if v is not None and (v != v or v in (float('inf'), float('-inf'))):
                v = None      # 'NaN' / 'inf' = brak — nigdy NaN w JSON (JSON.parse na stronie by padł)
            if per is not None and v is not None:
                rows.append([per, v])
        if rows:
            out[lab] = sorted(rows)
    if not out:
        raise RuntimeError('brak serii z wartościami')
    return out


def parse_rezerwy(j):
    """IL → rezerwy 8 gospodarek w mld USD. total = rezerwy razem ze złotem po cenie rynkowej (TRGMV), ex_gold = bez złota
    (RXF11), fx = waluty obce (RXF11FX), gold = total − ex_gold (wyliczenie strony). Każdy kraj ma własny miesiąc 'asof'
    (MFW publikuje z różnym opóźnieniem); części i porównania tylko z dokładnie tego miesiąca (i 1/12 mies. wcześniej).
    Kraj bez sumy → 'missing' (nie zero)."""
    ser = parse_imf_sdmx(j)
    r1 = lambda v: None if v is None else round(v / 1e9, 1)
    countries, missing = {}, []
    for c in RES_COUNTRIES:
        by = {k: dict(ser.get((c, code, 'USD', 'M')) or []) for code, k in RES_IND.items()}
        if not by['total']:
            missing.append(c); continue
        tot_rows = sorted(by['total'].items())
        asof, total = tot_rows[-1]
        ex_gold, fx = by['ex_gold'].get(asof), by['fx'].get(asof)
        p1, p12 = by['total'].get(_imf_month_add(asof, -1)), by['total'].get(_imf_month_add(asof, -12))
        countries[c] = {'pl': RES_NAMES[c][0], 'en': RES_NAMES[c][1], 'asof': asof, 'total': r1(total), 'ex_gold': r1(ex_gold),
                        'fx': r1(fx), 'gold': None if ex_gold is None else r1(total - ex_gold),
                        'd1m': None if p1 is None else r1(total - p1), 'd12m': None if p12 is None else r1(total - p12),
                        'p12m': None if not p12 else round(100.0 * (total - p12) / p12, 1),
                        'd': [[m, r1(v)] for m, v in tot_rows]}
    if not countries:
        raise RuntimeError('żaden kraj bez sumy rezerw')
    tots = sorted(v['total'] for v in countries.values())
    if tots[len(tots) // 2] < 1:      # mediana < 1 mld USD: wartości nie są w dolarach (zmiana SCALE?) — nie pokazujemy źle
        raise RuntimeError('wartości wyglądają na przeskalowane (atrybut SCALE) — sprawdź jednostkę')
    order = sorted(countries, key=lambda c: -countries[c]['total'])
    months = [countries[c]['asof'] for c in countries]
    return {'src': IMF_IL_SRC, 'url': IMF_IL_PAGE, 'unit': 'mld USD', 'asof_min': min(months), 'asof_max': max(months),
            'order': order, 'countries': countries, 'missing': missing,
            'note': 'gold = total − ex_gold (wyliczenie strony z danych MFW); zmiana zawiera wycenę walut i złota'}


def build_rezerwy():
    """data/rezerwy.json — jedno zapytanie do API MFW (bez klucza); dane miesięczne, w main() najwyżej raz na dobę."""
    out = parse_rezerwy(get_json(IMF_IL_URL, {'Accept': 'application/json'}))
    out['at'] = NOW
    return out


# BIS — stopy procentowe banków centralnych (WS_CBPOL), bez klucza; dzienne (stan) + miesięczne (historia zmian)
CBPOL_AREAS = ['US', 'XM', 'GB', 'CH', 'SE', 'NO', 'PL', 'JP', 'KR', 'CN', 'IN', 'ID', 'AU', 'CA', 'BR', 'MX', 'ZA', 'TR', 'SA', 'RU']
CBPOL_BASE = 'https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/'


def parse_cbpol_csv(raw):
    """CSV BIS (pola w cudzysłowach, długie opisy) → {kraj: [[okres, stopa %], ...]} rosnąco; NaN / status braku = pominięte."""
    text = raw.decode('utf-8', errors='replace') if isinstance(raw, (bytes, bytearray)) else str(raw)
    out = {}
    for r in csv.DictReader(io.StringIO(text)):
        area = (r.get('REF_AREA') or '').strip(); per = (r.get('TIME_PERIOD') or '').strip()
        if not area or not re.match(r'^\d{4}-\d{2}(-\d{2})?$', per):
            continue
        if (r.get('OBS_STATUS') or '').strip() in ('H', 'K', 'L', 'M', 'Q'):
            continue
        v = _num(r.get('OBS_VALUE'))
        if v is None or v != v or v in (float('inf'), float('-inf')):
            continue
        out.setdefault(area, []).append([per, v])
    for a in out:
        out[a].sort(key=lambda x: x[0])
    if not out:
        raise RuntimeError('brak obserwacji w odpowiedzi')
    return out


def cbpol_summary(daily, monthly):
    """Stan (ostatnia wartość dzienna), zmiana od 12 miesięcy, ostatnia zmiana (miesiąc, o ile pkt proc.), różnica wobec Fed."""
    rows = {}
    for a in CBPOL_AREAS:
        d = daily.get(a) or []; m = monthly.get(a) or []
        if not d and not m:
            continue
        rate, date = (d[-1][1], d[-1][0]) if d else (m[-1][1], m[-1][0])
        cur_m = date[:7]
        y, mo = int(cur_m[:4]), int(cur_m[5:7])
        ago = f'{y - 1:04d}-{mo:02d}'
        base = [v for p, v in m if p <= ago]
        d12 = round(rate - base[-1], 4) if base else None
        last = None   # (miesiąc, zmiana) — przeszukanie od najnowszego: wartość dzienna vs ostatni miesiąc, potem miesiąc do miesiąca
        seq = [v for p, v in m if p < cur_m] + [rate]
        per = [p for p, v in m if p < cur_m] + [cur_m]
        for i in range(len(seq) - 1, 0, -1):
            if abs(seq[i] - seq[i - 1]) > 1e-9:
                last = [per[i], round(seq[i] - seq[i - 1], 4)]; break
        rows[a] = {'rate': rate, 'date': date, 'd12': d12, 'last': last, 'm_n': len(m)}   # v63: ile miesięcy historii (bez historii nie ma „bez zmian”)
    us = rows.get('US', {}).get('rate')
    for a, r in rows.items():
        r['vs_us'] = round(r['rate'] - us, 4) if us is not None else None
    return rows


def build_stopy():
    """data/stopy.json — stopy banków centralnych (BIS WS_CBPOL): dzienne 15 obserwacji (ostatnia ważna), miesięczne 25."""
    keys = '+'.join(CBPOL_AREAS)
    daily = parse_cbpol_csv(get_bytes(CBPOL_BASE + f'D.{keys}?lastNObservations=15&format=csv', timeout=90))
    try:
        monthly = parse_cbpol_csv(get_bytes(CBPOL_BASE + f'M.{keys}?lastNObservations=25&format=csv', timeout=90))
    except Exception as e:
        META['errors'].append(mask(f'BIS stopy (miesięczne): {e}')); monthly = {}
    rows = cbpol_summary(daily, monthly)
    if not rows:
        raise RuntimeError('BIS stopy: żadna gospodarka')
    return {'at': NOW, 'src': 'BIS — Central bank policy rates (WS_CBPOL)', 'url': 'https://data.bis.org/topics/CBPOL', 'unit': '% rocznie',
            'asof': max(r['date'] for r in rows.values()), 'order': [a for a in CBPOL_AREAS if a in rows], 'rows': rows}


# EBC — średnie miesięczne kursy referencyjne (EXR), bez klucza: kurs z tych samych miesięcy co średni indeks OECD na mapie
EXR_CUR = ['USD', 'CAD', 'BRL', 'MXN', 'GBP', 'CHF', 'SEK', 'PLN', 'TRY', 'ILS', 'ZAR', 'INR', 'CNY', 'HKD', 'JPY', 'KRW',
           'IDR', 'SGD', 'THB', 'MYR', 'PHP', 'AUD', 'NZD']
EXR_URL = ('https://data-api.ecb.europa.eu/service/data/EXR/M.' + '+'.join(EXR_CUR)
           + '.EUR.SP00.A?lastNObservations=15&format=csvdata')


def parse_exr_csv(raw):
    """CSV EBC (EXR, miesięczne średnie) → {waluta: [[RRRR-MM, jednostek waluty za 1 EUR], ...]} rosnąco; brak/0 = pominięte."""
    text = raw.decode('utf-8', errors='replace') if isinstance(raw, (bytes, bytearray)) else str(raw)
    out = {}
    for r in csv.DictReader(io.StringIO(text)):
        cur = (r.get('CURRENCY') or '').strip(); per = (r.get('TIME_PERIOD') or '').strip()
        if not cur or not re.match(r'^\d{4}-\d{2}$', per):
            continue
        v = _num(r.get('OBS_VALUE'))
        if v is None or v != v or v <= 0 or v == float('inf'):
            continue
        out.setdefault(cur, []).append([per, round(v, 6)])
    for c in out:
        out[c].sort(key=lambda x: x[0])
    if not out.get('USD'):
        raise RuntimeError('brak kursu USD w odpowiedzi')
    return out


def build_kursy():
    """data/kursy.json — średnie miesięczne kursów EBC (15 miesięcy) dla walut regionów mapy."""
    m = parse_exr_csv(get_bytes(EXR_URL, timeout=90))
    return {'at': NOW, 'src': 'ECB — euro foreign exchange reference rates, monthly averages (EXR)',
            'url': 'https://data.ecb.europa.eu/data/datasets/EXR', 'unit': 'jednostek waluty za 1 EUR, średnia miesiąca',
            'asof': m['USD'][-1][0], 'm': m}


# v54: ZMIERZONE dzienne przepływy inwestorów zagranicznych — Indie (NSDL, FPI) i Tajwan (TWSE), bez klucza
import html as _html   # biblioteka standardowa: encje w tabeli HTML NSDL
NSDL_URL = 'https://www.fpi.nsdl.co.in/web/Reports/Monthly.aspx'
TWSE_URL = 'https://www.twse.com.tw/rwd/en/fund/BFI82U?type=day&dayDate={d}&response=json'
TWSE_SLEEP = 2.0          # TWSE blokuje szybkie serie zapytań (ok. 3 na 5 s)
OBCE_EMPTY_DAYS = 400    # v89: tyle dni pamiętamy dni bez sesji Tajwanu i Hongkongu (kalendarz sesji dla TRENDÓW)
TWSE_MAX = 30             # najwyżej tyle dni na jeden przebieg (pierwszy przebieg: ok. 25 dni sesyjnych)
TW_BACK_DAYS = 182        # v95: Tajwan i Hongkong — brakujące starsze dni uzupełniane wstecz do ok. 26 tygodni (HKEX trzyma pliki dzienne od 03.2026)
TW_FX_OBS = 200           # v95: tyle ostatnich kursów FRED (ok. 9 miesięcy) — przeliczenie na USD także dni uzupełnionych wstecz
NSDL_ARCH = 'https://www.fpi.nsdl.co.in/web/Reports/Archive.aspx'   # v95: archiwum NSDL — pełne miesiące wstecz (formularz, bez klucza)
NSDL_ARCH_MONTHS = 12     # v95: tyle pełnych miesięcy wstecz
NSDL_ARCH_MAX = 3         # v95: najwyżej tyle miesięcy archiwum na jeden przebieg
TW_BACK_MAX = 12          # v95.1: najwyżej tyle starszych dni na źródło na przebieg (przebieg co godzinę ma już ok. 11 z 15 min limitu)
BACK_BUDGET = 40          # v95.1: sekund na uzupełnianie wstecz jednego źródła w przebiegu — potem przerwa do następnego przebiegu
TW_BACK_TIMEOUT = 10      # v95.2: limit jednego zapytania wstecz (TWSE, HKEX); budżet liczony razem z nim — twardy limit czasu
NSDL_BACK_BUDGET = 60     # v95.2: archiwum NSDL — sekund na przebieg; jeden miesiąc = formularz (15 s) + wynik (30 s)
BACK_LATE = 600           # v96: przebieg dłuższy niż 10 min przed krokiem krajów — bez historii wstecz (kraje liczone po ok. 9 min; zapas do 15 min)
_BACK_LATE_NOTE = [False]
TW_PEND_H = 12            # v95.2: starszy dzień „No Data!” (TWSE) = święto dopiero przy drugiej takiej odpowiedzi po ≥ 12 h
HK_NF_DAYS = 7            # v95.2: brak pliku HKEX za starszy dzień — ponowne pytanie po tygodniu (bez stałej granicy)
_RUN_T0 = [None]          # v95.2: początek przebiegu (main)
NSDL_MONTHS = ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December')
OBCE_KEEP = 300           # tyle ostatnich dni trzyma plik (historia narasta z przebiegu na przebieg; v89: 300 — tło dla TRENDÓW)
NSDL_CATS = {'equity': 'eq', 'debt-general limit': 'debt', 'debt-vrr': 'debt', 'debt-far': 'debt', 'hybrid': 'hyb',
             'mutual funds': 'mf', 'aifs': 'aif'}


def _nsdl_num(tok):
    """'1,705.34' → 1705.34; '(126.43)' → -126.43 (nawias = minus); brak → None."""
    t = str(tok).replace(',', '').strip()
    neg = t.startswith('(') and t.endswith(')')
    try:
        v = float(t.strip('()').strip())
    except ValueError:
        return None
    return -v if neg else v


def parse_nsdl_html(text, totals=None):
    """Tabela NSDL „Daily Trends in FPI Investments” → [[data, akcje, dług, hybrydy, razem, INR za USD], ...] w mln USD.
    Dług = Debt-General Limit + Debt-VRR + Debt-FAR; tabela instrumentów pochodnych (dalej na stronie) pominięta.
    v95: bloki „Total for <miesiąc>” i „Total for <rok>” (archiwum) kończą ostatni dzień — ich „Total” trafia do `totals`, nie do dnia."""
    t = re.sub(r'<script.*?</script>|<style.*?</style>', '', text, flags=re.S | re.I)
    days, date, cat, blk = {}, None, None, None
    for r in re.findall(r'<tr[^>]*>(.*?)</tr>', t, flags=re.S | re.I):
        cells = [re.sub(r'\s+', ' ', _html.unescape(re.sub(r'<[^>]+>', '', c))).strip()
                 for c in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', r, flags=re.S | re.I)]
        if not any(cells):
            continue
        if any('derivative' in c.lower() for c in cells):
            break
        if cells[0].lower().startswith('total for'):
            date, cat, blk = None, None, cells[0][9:].strip().lower()   # v95: suma miesiąca albo roku — nie dopisujemy jej do ostatniego dnia
            continue
        if date is None and blk and cells[0].lower() == 'total' and len(cells) >= 5:
            if totals is not None:
                totals.setdefault(blk, _nsdl_num(cells[4]))
            continue
        if re.match(r'^\d{2}-[A-Za-z]{3}-\d{4}$', cells[0]):
            date = datetime.datetime.strptime(cells[0], '%d-%b-%Y').date().isoformat(); cells = cells[1:]; cat = None; blk = None
            d = days.setdefault(date, {})
            if cells and cells[-1].lower().startswith('rs'):
                m = re.search(r'(\d+(?:\.\d+)?)', cells[-1])
                if m:
                    d['inr'] = float(m.group(1))
                cells = cells[:-1]
        if date is None or not cells:
            continue
        d = days[date]
        if cells[0].lower() == 'total' and len(cells) >= 5:
            d['tot'] = _nsdl_num(cells[4]); continue
        if cells[0].lower() in NSDL_CATS:
            cat = NSDL_CATS[cells[0].lower()]; cells = cells[1:]
        if cells and cells[0].lower() == 'sub-total' and cat and len(cells) >= 5:
            v = _nsdl_num(cells[4])
            if v is not None:
                d[cat] = round(d.get(cat, 0.0) + v, 2)
    out = [[k, days[k].get('eq'), days[k].get('debt'), days[k].get('hyb'), days[k].get('tot'), days[k].get('inr')]
           for k in sorted(days) if days[k].get('eq') is not None and days[k].get('tot') is not None]
    if not out:
        raise RuntimeError('brak dni w tabeli')
    return out


def parse_twse(j):
    """TWSE BFI82U (jeden dzień) → [data, zagraniczni, fundusze krajowe (SITC), dealerzy, razem] w mln TWD; brak sesji → None."""
    if not isinstance(j, dict) or j.get('stat') != 'OK' or not isinstance(j.get('data'), list):
        return None
    ds = str(j.get('date') or '')
    if not re.match(r'^\d{8}$', ds):
        return None
    fx = it = dl = tot = None
    for row in j['data']:
        if not isinstance(row, list) or len(row) < 4:
            continue
        name, v = str(row[0]).strip().lower(), _num(row[3])
        if v is None:
            continue
        if name.startswith('foreign'):
            fx = (fx or 0.0) + v          # inwestorzy zagraniczni (z Chin kontynentalnych) + zagraniczni dealerzy
        elif name.startswith('securities investment trust'):
            it = v
        elif name.startswith('dealers'):
            dl = (dl or 0.0) + v
        elif name.startswith('total'):
            tot = v
    if fx is None:
        return None
    m = lambda v: None if v is None else round(v / 1e6, 1)
    return [f'{ds[:4]}-{ds[4:6]}-{ds[6:]}', m(fx), m(it), m(dl), m(tot)]


def tw_dates(have, empty, now_tpe, first):
    """Dni robocze do pobrania z TWSE: brakujące w pliku, bez znanych dni bez sesji; dziś dopiero po 16:00 czasu Tajpej."""
    today = now_tpe.date(); out = []
    for i in range(35 if first else 10, -1, -1):
        d = today - datetime.timedelta(days=i)
        iso = d.isoformat()
        if d.weekday() >= 5 or iso in have or iso in empty or (d == today and now_tpe.hour < 16):
            continue
        out.append(iso)
    return out


def tw_back(have, empty, now_loc, skip=()):
    """v95: starsze dni robocze do uzupełnienia wstecz — od najnowszego, do TW_BACK_DAYS dni; bez dni znanych, bez dni bez sesji
    i bez dni z `skip` (już w kolejce albo odłożone na później)."""
    today = now_loc.date(); out = []
    for i in range(1, TW_BACK_DAYS + 1):
        d = today - datetime.timedelta(days=i)
        iso = d.isoformat()
        if d.weekday() >= 5 or iso in have or iso in empty or iso in skip:
            continue
        out.append(iso)
    return out


def _back_ok(t0, budget, cost):
    """v95.2: czy zmieści się jeszcze jedno zapytanie wstecz: czas od t0 + najdłuższy możliwy czas zapytania ≤ budżet,
    a cały przebieg nie trwa już dłużej niż BACK_LATE."""
    now = time.monotonic()
    if _RUN_T0[0] is not None and now - _RUN_T0[0] >= BACK_LATE:
        if not _BACK_LATE_NOTE[0]:
            _BACK_LATE_NOTE[0] = True
            META['notes'].append(f'historia wstecz pominięta w tym przebiegu: przebieg trwa już {now - _RUN_T0[0]:.0f} s (granica {BACK_LATE} s)')
        return False
    return now - t0 + cost <= budget


def _age_h(ts):
    """Godziny od chwili `ts` (ISO) do początku tego przebiegu (NOW); zły zapis → None."""
    try:
        return (datetime.datetime.fromisoformat(NOW) - datetime.datetime.fromisoformat(ts)).total_seconds() / 3600
    except (TypeError, ValueError):
        return None


def _twse_nodata(j):
    """v95: TWSE „No Data!” = dzień bez sesji; inna odpowiedź bez danych to błąd (ponowimy), nie święto."""
    return isinstance(j, dict) and str(j.get('stat') or '').strip().lower().startswith('no data')


def twd_rates(key):
    """FRED DEXTAUS (TWD za 1 USD, Fed H.10) → {data: kurs}; '.' = brak."""
    j = get_json(f'{FRED}?series_id=DEXTAUS&api_key={key}&file_type=json&sort_order=desc&limit={TW_FX_OBS}')
    out = {}
    for o in j.get('observations', []) if isinstance(j, dict) else []:
        v = _num(o.get('value'))
        if v and v > 0 and re.match(r'^\d{4}-\d{2}-\d{2}$', str(o.get('date', ''))):
            out[o['date']] = v
    return out


def _now_utc():
    return datetime.datetime.now(datetime.timezone.utc)   # osobno, żeby testy mogły ustawić czas


def _rate_for(rates, day):
    ks = [k for k in rates if k <= day] or []
    return (max(ks), rates[max(ks)]) if ks else (None, None)


def _rows(prev_part):
    return {r[0]: r for r in (prev_part or {}).get('d', []) if isinstance(r, list) and r and isinstance(r[0], str)}


def _ym_add(ym, k):
    y, m = int(ym[:4]), int(ym[5:7]) + k
    return f'{y + (m - 1) // 12:04d}-{(m - 1) % 12 + 1:02d}'


def nsdl_http(url, form=None, cookie='', timeout=30):
    """v95.2: archiwum NSDL — formularz i wynik w jednej sesji: ciasteczka z pobrania formularza (sesja ASP.NET, równoważenie
    obciążenia) idą razem z wysłaniem formularza. → (treść, ciasteczka)."""
    data = None if form is None else urllib.parse.urlencode(form).encode()
    h = {'User-Agent': 'CapitalFlowAI-collector/1.0'}
    if cookie:
        h['Cookie'] = cookie
    if data is not None:
        h.update({'Content-Type': 'application/x-www-form-urlencoded', 'Referer': url})
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=timeout) as r:
        got = '; '.join(c.split(';', 1)[0].strip() for c in (r.headers.get_all('Set-Cookie') or []) if '=' in c.split(';', 1)[0])
        return r.read(), got or cookie


def nsdl_month(ym):
    """v95: archiwum NSDL — wszystkie dni raportu jednego miesiąca (ta sama tabela co bieżący miesiąc). Suma dni musi się zgadzać
    z sumą miesiąca podaną przez NSDL pod tabelą (±1 mln USD) — inaczej miesiąc odrzucony (ponowimy jutro)."""
    body, ck = nsdl_http(NSDL_ARCH, timeout=15)
    page = body.decode('utf-8', 'replace')
    form = {}
    for n in ('__VIEWSTATE', '__VIEWSTATEGENERATOR', '__EVENTVALIDATION'):
        m = re.search(r'name="%s"[^>]*value="([^"]*)"' % n, page)
        if m:
            form[n] = _html.unescape(m.group(1))
    if '__VIEWSTATE' not in form or '__EVENTVALIDATION' not in form:
        raise RuntimeError('brak pól formularza archiwum')
    y, mo = int(ym[:4]), int(ym[5:7])
    last = datetime.date(y + mo // 12, mo % 12 + 1, 1) - datetime.timedelta(days=1)
    form.update({'__EVENTTARGET': 'btnSubmit1', '__EVENTARGUMENT': '', 'hdnDate': f'{last.day:02d}-{NSDL_MONTHS[mo - 1][:3]}-{y}',
                 'HdnValexceldata': '', 'hdnFlag': ''})
    tot = {}
    rows = [r for r in parse_nsdl_html(nsdl_http(NSDL_ARCH, form, ck, timeout=30)[0].decode('utf-8', 'replace'), tot)
            if r[0][:7] == ym]
    if not rows:
        raise RuntimeError('brak dni tego miesiąca')
    mt = tot.get(NSDL_MONTHS[mo - 1].lower())
    if mt is None:
        raise RuntimeError('brak sumy miesiąca pod tabelą')
    s = sum(r[4] for r in rows)
    if abs(s - mt) > 1 + 0.001 * abs(mt):
        raise RuntimeError(f'suma dni {s:.2f} ≠ suma miesiąca {mt:.2f} mln USD')
    return rows


def nsdl_part(prev_in):
    rows = parse_nsdl_html(get_bytes(NSDL_URL, timeout=60).decode('utf-8', 'replace'))
    m = _rows(prev_in)
    want = [_ym_add(rows[-1][0][:7], -i) for i in range(1, NSDL_ARCH_MONTHS + 1)]   # v95: pełne miesiące przed bieżącym
    arch = {x for x in ((prev_in or {}).get('arch') or []) if isinstance(x, str) and x in want}
    afail = {k: v for k, v in ((prev_in or {}).get('afail') or {}).items()          # v95.2: miesiąc odrzucony dziś — ponowimy jutro
             if k in want and isinstance(v, str) and v[:10] == NOW[:10]}
    t0 = time.monotonic(); tried = 0
    for ym in [x for x in want if x not in arch and x not in afail]:
        if tried >= NSDL_ARCH_MAX or not _back_ok(t0, NSDL_BACK_BUDGET, 45):
            break                                        # reszta w następnym przebiegu
        tried += 1
        try:
            got = nsdl_month(ym)
        except (RuntimeError, ValueError, KeyError, IndexError) as e:   # zła treść: ten miesiąc jutro, starsze dalej (v95.2)
            afail[ym] = NOW; META['notes'].append(mask(f'NSDL archiwum {ym}: {e} — ponowimy jutro')); continue
        except Exception as e:                                          # sieć: przerwa do następnego przebiegu
            META['notes'].append(mask(f'NSDL archiwum {ym}: {e} — ponowimy w następnym przebiegu')); break
        m.update({r[0]: r for r in got}); arch.add(ym)      # archiwum = pełny, ostateczny miesiąc (zastępuje dni zebrane wcześniej)
    m.update({r[0]: r for r in rows})
    d = [m[k] for k in sorted(m)][-OBCE_KEEP:]
    return {'at': NOW, 'src': 'NSDL — Daily Trends in FPI Investments', 'url': NSDL_URL, 'unit': 'mln USD (przeliczenie NSDL)',
            'cols': ['data raportu', 'akcje', 'dług', 'hybrydy', 'razem', 'INR za USD'], 'asof': d[-1][0], 'arch': sorted(arch), **({'afail': afail} if afail else {}), 'd': d}


def twse_part(prev_tw, key):
    prev_tw = prev_tw if isinstance(prev_tw, dict) else {}
    have = {k: list(v) for k, v in _rows(prev_tw).items()}   # kopie — poprzedni plik nie jest zmieniany w miejscu
    now_tpe = _now_utc() + datetime.timedelta(hours=8)
    lim = (now_tpe.date() - datetime.timedelta(days=OBCE_EMPTY_DAYS)).isoformat()
    empty = {x for x in prev_tw.get('empty', []) if isinstance(x, str) and x >= lim}
    fails, bfails = [], []
    pend = {k: v for k, v in (prev_tw.get('pend') or {}).items() if isinstance(k, str) and k >= lim and _age_h(v) is not None}
    recent = tw_dates(set(have), empty, now_tpe, first=not have)[-TWSE_MAX:]
    wait = {k for k, v in pend.items() if _age_h(v) < TW_PEND_H}
    back = tw_back(set(have), empty, now_tpe, skip=set(recent) | wait)[:min(TW_BACK_MAX, max(0, TWSE_MAX - len(recent)))]   # v95: historia wstecz
    t0 = time.monotonic()
    for iso in recent + back:
        old = iso in back
        bad = bfails if old else fails
        if old and not _back_ok(t0, BACK_BUDGET, TWSE_SLEEP + TW_BACK_TIMEOUT):
            break                                        # ostatnie dni zawsze, starsze — tylko w budżecie czasu (v95.1–v95.2)
        time.sleep(TWSE_SLEEP)
        try:
            u = TWSE_URL.format(d=iso.replace('-', ''))
            j = get_json(u, timeout=TW_BACK_TIMEOUT) if old else get_json(u)
            r = parse_twse(j)
        except Exception as e:
            bad.append(f'{iso}: {e}'); continue
        if r is None:
            if iso < now_tpe.date().isoformat():
                if _twse_nodata(j) and (not old or iso in pend):
                    empty.add(iso); pend.pop(iso, None)   # dzień bez sesji (święto) — nie pytamy ponownie
                elif _twse_nodata(j):
                    pend[iso] = NOW     # v95.2: starszy dzień — święto dopiero po drugiej takiej odpowiedzi (≥ 12 h), nie po jednej awarii
                else:
                    bad.append(f"{iso}: {str(j.get('stat'))[:60] if isinstance(j, dict) else 'nieczytelna odpowiedź'}")   # v95: nie święto — ponowimy
            continue
        have[r[0]] = r[:5]; pend.pop(iso, None)
    if not have:
        raise RuntimeError('brak dni' + (f' ({fails[0]})' if fails else ''))
    if fails:
        META['errors'].append(mask(f'TWSE: {len(fails)} dni bez odpowiedzi, np. {fails[0]}'))
    if bfails:
        META['notes'].append(mask(f'TWSE historia wstecz: {len(bfails)} dni bez odpowiedzi, np. {bfails[0]} — ponowimy'))
    d = [have[k] for k in sorted(have)][-OBCE_KEEP:]
    rates = {}
    if key:
        try:
            rates = twd_rates(key)
        except Exception as e:
            META['errors'].append(mask(f'TWSE kurs FRED DEXTAUS: {e}'))
    old = _rows(prev_tw)
    for r in d:
        rd, rt = _rate_for(rates, r[0])
        if rt and r[1] is not None:
            r[5:] = [round(r[1] / rt, 1), rd]
        elif r[0] in old and len(old[r[0]]) >= 7:
            r[5:] = old[r[0]][5:7]          # bez nowego kursu zostaje poprzednie przeliczenie
        else:
            r[5:] = [None, None]
    return {'at': NOW, 'src': 'TWSE — Trading Value of Foreign & Other Investors (BFI82U)',
            'url': 'https://www.twse.com.tw/en/trading/foreign/bfi82u.html', 'unit': 'mln TWD; ≈ mln USD kursem Fed H.10 (FRED DEXTAUS)',
            'cols': ['data', 'zagraniczni', 'fundusze krajowe', 'dealerzy', 'razem', '≈ mln USD (zagraniczni)', 'data kursu'],
            'asof': d[-1][0], 'empty': sorted(empty), **({'pend': pend} if pend else {}), 'd': d}


# v67: HKEX Stock Connect — dzienne kupno i sprzedaż akcji w Hongkongu przez inwestorów z Chin kontynentalnych (southbound)
HKEX_URL = 'https://www.hkex.com.hk/eng/csm/DailyStat/data_tab_daily_{d}e.js'
HKEX_SLEEP = 1.0


def parse_hkex(text):
    """Plik dzienny HKEX (JS `tabData = [...]`) → [data, netto, kupno, sprzedaż, liczba rynków] w mln HKD (Szanghaj + Shenzhen
    → Hongkong); dzień bez sesji albo bez kupna/sprzedaży → None. Northbound od 2024 bez podziału na kupno i sprzedaż — pomijamy."""
    t = str(text)
    try:
        j = json.loads(t[t.index('['):t.rindex(']') + 1])
    except (ValueError, json.JSONDecodeError):
        return None
    date, buy, sell, n, sb, bad = None, 0.0, 0.0, 0, 0, 0
    for m in j if isinstance(j, list) else []:
        if not isinstance(m, dict) or 'southbound' not in str(m.get('market', '')).lower():
            continue
        sb += 1
        if not m.get('tradingDay'):
            continue
        try:
            tb = m['content'][0]['table']; d = dict(zip(tb['schema'][0], [x['td'][0][0] for x in tb['tr']]))
        except (KeyError, IndexError, TypeError):
            bad += 1; continue
        b, s = _num(d.get('Buy Turnover')), _num(d.get('Sell Turnover'))
        if b is None or s is None or not re.match(r'^\d{4}-\d{2}-\d{2}$', str(m.get('date', ''))):
            bad += 1; continue
        if b == 0 and s == 0:
            continue      # v73: rynek bez handlu (święto w Chinach kontynentalnych przy otwartym Hongkongu: tradingDay 1, obrót 0,00)
        date = m['date']; buy += b; sell += s; n += 1
    if bad:
        return None       # v73: nieczytelna tabela rynku w dniu sesji = nieczytelny plik (ponowimy), nie dzień z częścią rynków
    if not n:
        return False if sb else None   # v69: False = dzień bez sesji (plik z tradingDay 0), None = nieczytelny plik
    return [date, round(buy - sell, 2), round(buy, 2), round(sell, 2), n]


def fred_rates(key, sid, limit=60):
    """FRED — kurs (dzienny albo średnia miesięczna, zależnie od serii; jednostek waluty za 1 USD, Fed H.10) → {data: kurs}; '.' = brak."""
    j = get_json(f'{FRED}?series_id={sid}&api_key={key}&file_type=json&sort_order=desc&limit={limit}')
    out = {}
    for o in j.get('observations', []) if isinstance(j, dict) else []:
        v = _num(o.get('value'))
        if v and v > 0 and re.match(r'^\d{4}-\d{2}-\d{2}$', str(o.get('date', ''))):
            out[o['date']] = v
    return out


def hkex_part(prev_hk, key):
    prev_hk = prev_hk if isinstance(prev_hk, dict) else {}
    have = {k: list(v) for k, v in _rows(prev_hk).items()}
    now_hk = _now_utc() + datetime.timedelta(hours=8)
    lim = (now_hk.date() - datetime.timedelta(days=OBCE_EMPTY_DAYS)).isoformat()
    empty = {x for x in prev_hk.get('empty', []) if isinstance(x, str) and x >= lim}
    fails, bfails, nf_new = [], [], []
    nf = {k: v for k, v in (prev_hk.get('nf') or {}).items()                  # v95.2: brak pliku za starszy dzień — pytamy znowu po tygodniu
          if isinstance(k, str) and _age_h(v) is not None and _age_h(v) < 24 * HK_NF_DAYS}
    for k in [k for k, r in have.items() if len(r) > 3 and r[2] == 0 and r[3] == 0]:
        del have[k]; empty.add(k)   # v73: dawny zapis „sesja z zerem” w dniu bez handlu → dzień bez sesji
    recent = tw_dates(set(have), empty, now_hk, first=not have)[-TWSE_MAX:]   # te same zasady dni co dla Tajwanu (UTC+8)
    back = tw_back(set(have), empty, now_hk, set(recent) | set(nf))[:min(TW_BACK_MAX, max(0, TWSE_MAX - len(recent)))]   # v95: historia wstecz, od najnowszego
    t0 = time.monotonic(); miss = 0
    for iso in recent + back:
        old = iso in back
        if old and (miss >= 3 or not _back_ok(t0, BACK_BUDGET, HKEX_SLEEP + TW_BACK_TIMEOUT)):
            break                                        # 3 braki pliku z rzędu albo koniec budżetu czasu — reszta w następnym przebiegu
        time.sleep(HKEX_SLEEP)
        try:
            r = parse_hkex(get_bytes(HKEX_URL.format(d=iso.replace('-', '')), timeout=TW_BACK_TIMEOUT if old else 30).decode('utf-8', 'replace'))
        except urllib.error.HTTPError as e:
            if old and e.code == 404:
                nf[iso] = NOW; nf_new.append(iso); miss += 1; continue   # v95.2: brak pliku za starszy dzień — bez stałej granicy
            if e.code != 404 or iso < now_hk.date().isoformat():
                fails.append(f'{iso}: HTTP {e.code}')   # v69: 404 za dzień roboczy z przeszłości = brak pliku (święta mają plik) — ponowimy
            continue
        except Exception as e:
            (bfails if old else fails).append(f'{iso}: {e}'); continue
        if old:
            miss = 0
        if r is False:
            empty.add(iso); continue      # v69: święto w Hongkongu — plik z tradingDay 0
        if r is None:
            (bfails if old else fails).append(f'{iso}: nieczytelny plik'); continue
        have[r[0]] = r[:5]
    if not have:
        raise RuntimeError('brak dni' + (f' ({fails[0]})' if fails else ''))
    if fails:
        META['errors'].append(mask(f'HKEX: {len(fails)} dni bez odpowiedzi, np. {fails[0]}'))
    if bfails:
        META['notes'].append(mask(f'HKEX historia wstecz: {len(bfails)} dni bez odpowiedzi, np. {bfails[0]} — ponowimy'))
    if nf_new:
        META['notes'].append(mask(f'HKEX historia wstecz: brak pliku za {len(nf_new)} dni, np. {nf_new[0]} — ponowimy za tydzień'))
    d = [have[k] for k in sorted(have)][-OBCE_KEEP:]
    rates = {}
    if key:
        try:
            rates = fred_rates(key, 'DEXHKUS', limit=TW_FX_OBS)
        except Exception as e:
            META['errors'].append(mask(f'HKEX kurs FRED DEXHKUS: {e}'))
    old = _rows(prev_hk)
    for r in d:
        rd, rt = _rate_for(rates, r[0])
        if rt and r[1] is not None:
            r[5:] = [round(r[1] / rt, 1), rd]
        elif r[0] in old and len(old[r[0]]) >= 7:
            r[5:] = old[r[0]][5:7]
        else:
            r[5:] = [None, None]
    return {'at': NOW, 'src': 'HKEX — Stock Connect daily statistics (Southbound: Shanghai + Shenzhen → Hong Kong)',
            'url': 'https://www.hkex.com.hk/Mutual-Market/Stock-Connect/Statistics/Historical-Daily',
            'unit': 'mln HKD; ≈ mln USD kursem Fed H.10 (FRED DEXHKUS)',
            'cols': ['data', 'netto', 'kupno', 'sprzedaż', 'rynki', '≈ mln USD (netto)', 'data kursu'],
            'asof': d[-1][0], 'empty': sorted(empty), **({'nf': nf} if nf else {}), 'd': d}


# v71: Banco Central do Brasil (SGS, bez klucza) — „câmbio contratado”: dzienne przepływy dolarów przez rynek walutowy Brazylii
BCB_URL = 'https://api.bcb.gov.br/dados/serie/bcdata.sgs.{id}/dados?formato=json&dataInicial={a}&dataFinal={b}'   # „ultimos/N” ma limit 20
BCB_SERIES = (('fin', 13970), ('fin_buy', 13968), ('fin_sell', 13969), ('com', 13967), ('tot', 13961))
BCB_DAYS = 400    # dni kalendarzowe wstecz (v95: ok. 275 dni roboczych — pełna historia dla TRENDÓW od pierwszego przebiegu)


def _bcb_date(s):
    m = re.match(r'^(\d{2})/(\d{2})/(\d{4})$', str(s or '').strip())
    return f'{m.group(3)}-{m.group(2)}-{m.group(1)}' if m else None


# v79: Brazylia — miesięczny bilans płatniczy (bank centralny, SGS): kapitał zagraniczny napływający do Brazylii (pasywa), mln USD
BCB_BOP = (('fdi', 22885), ('pi', 22924), ('pi_eq', 22927), ('pi_fund', 22936), ('pi_debt', 22939), ('oi', 22971),
           ('cb_dep', 22986), ('cb_loan', 23001), ('sdr', 23042))   # v81: pozycje banku centralnego i SDR — strona odejmuje je od pozostałych


def bcb_bop(prev_m):
    """SGS (miesięcznie) → [[YYYY-MM, bezpośrednie, portfelowe, akcje, fundusze, obligacje, pozostałe]] w mln USD, 25 miesięcy.
    Seria bez odpowiedzi zostawia stare wartości swojej kolumny (nowe miesiące: None, nigdy 0); bez żadnego miesiąca = błąd."""
    now_br = _now_utc() - datetime.timedelta(hours=3)
    a, b = f'01/{now_br.month:02d}/{now_br.year - 2}', now_br.strftime('%d/%m/%Y')
    got, fails = {}, []
    for name, sid in BCB_BOP:
        try:
            j = get_json(BCB_URL.format(id=sid, a=a, b=b))
            if not isinstance(j, list):
                raise RuntimeError('nieznany kształt odpowiedzi')
            for r in j:
                d = _bcb_date(r.get('data')) if isinstance(r, dict) else None
                v = _num(r.get('valor')) if d else None
                if d and v is not None and v == v and abs(v) != float('inf'):
                    got.setdefault(d[:7], {})[name] = round(v, 1)
        except Exception as e:
            fails.append(f'{sid}: {e}')
    if not got:
        raise RuntimeError('brak miesięcy' + (f' ({fails[0]})' if fails else ''))
    have = {r[0]: list(r) for r in (prev_m or []) if isinstance(r, list) and r and isinstance(r[0], str)}
    cols = [n for n, _ in BCB_BOP]
    for m, vals in got.items():
        old = have.get(m) or []
        have[m] = [m] + [vals[c] if c in vals else (old[i + 1] if len(old) > i + 1 else None) for i, c in enumerate(cols)]
    if fails:
        META['errors'].append(mask(f'BCB bilans płatniczy: {len(fails)} serie bez odpowiedzi, np. {fails[0]}'))
    rows = [have[k] for k in sorted(have)][-25:]
    bad = [r[0] for r in rows if None not in r[2:6] and abs(r[2] - r[3] - r[4] - r[5]) > 1]
    if bad:
        META['notes'].append('BCB: portfelowe ≠ akcje + fundusze + obligacje w miesiącach: ' + ', '.join(bad))
    return rows


def bcb_part(prev_br):
    """BCB SGS → [data, finansowy saldo, kupno, sprzedaż, handlowy saldo, razem] w mln USD. Każda seria osobno: seria bez odpowiedzi
    zostawia stare wartości swojej kolumny, a nowe dni mają tam None (nigdy 0). Bez żadnego nowego dnia = błąd (zostaje poprzednia część)."""
    have = {k: list(v) for k, v in _rows(prev_br).items()}
    got, fails = {}, []
    now_br = _now_utc() - datetime.timedelta(hours=3)
    a, b = (now_br - datetime.timedelta(days=BCB_DAYS)).strftime('%d/%m/%Y'), now_br.strftime('%d/%m/%Y')
    for name, sid in BCB_SERIES:
        try:
            j = get_json(BCB_URL.format(id=sid, a=a, b=b))
            if not isinstance(j, list):
                raise RuntimeError('nieznany kształt odpowiedzi')
            for r in j:
                d = _bcb_date(r.get('data')) if isinstance(r, dict) else None
                v = _num(r.get('valor')) if d else None
                if d and v is not None and v == v and abs(v) != float('inf'):
                    got.setdefault(d, {})[name] = round(v, 2)
        except Exception as e:
            fails.append(f'{sid}: {e}')
    if not got:
        raise RuntimeError('brak dni' + (f' ({fails[0]})' if fails else ''))
    cols = [n for n, _ in BCB_SERIES]
    for d, vals in got.items():
        old = have.get(d) or []
        have[d] = [d] + [vals[c] if c in vals else (old[i + 1] if len(old) > i + 1 else None) for i, c in enumerate(cols)]
    if fails:
        META['errors'].append(mask(f'BCB: {len(fails)} serie bez odpowiedzi, np. {fails[0]}'))
    d = [have[k] for k in sorted(have)][-OBCE_KEEP:]
    try:   # v79: miesięczny bilans płatniczy — jego awaria nie zatrzymuje części dziennej
        mrows = bcb_bop((prev_br or {}).get('m'))
    except Exception as e:
        mrows = (prev_br or {}).get('m'); META['errors'].append(mask(f'BCB bilans płatniczy: {e}'))
    extra = {'m': mrows, 'm_cols': ['miesiąc', 'bezpośrednie', 'portfelowe', 'akcje', 'fundusze', 'obligacje', 'pozostałe',
                                    'bank centralny: waluty i depozyty', 'bank centralny: kredyty', 'SDR'],
             'm_src': 'Banco Central do Brasil — SGS, balanço de pagamentos (22885, 22924, 22927, 22936, 22939, 22971, 22986, 23001, 23042)'} if mrows else {}
    return {**extra, 'at': NOW, 'src': 'Banco Central do Brasil — SGS, câmbio contratado (13961, 13967–13970)',
            'url': 'https://www.bcb.gov.br/estatisticas/tabelaespecial', 'unit': 'mln USD',
            'cols': ['data', 'finansowy saldo', 'finansowy kupno', 'finansowy sprzedaż', 'handlowy saldo', 'razem saldo'],
            'asof': d[-1][0], 'd': d}


# v74: Turcja — bank centralny (CBRT), tygodniowe transakcje netto nierezydentów w papierach (oczyszczone z cen i kursów), bez klucza
TCMB_PAGE = 'https://www.tcmb.gov.tr/wps/wcm/connect/EN/TCMB+EN/Main+Menu/Statistics/Monetary+and+Financial+Statistics/Securities+Statistics/'
TCMB_KEYS = (('tot', 'net transactions total'), ('eq', 'equity'), ('gdds', 'gdds (outright purchase)'),
             ('corp', 'debt securities issued by other than general'), ('intl', 'b.2. international market total'),
             ('eurob', 'general government issuances'))


def _tcmb_day(v):
    s = str(v or '').strip()
    m = re.match(r'^(\d{2})\.(\d{2})\.(\d{4})$', s)
    if m:
        return f'{m.group(3)}-{m.group(2)}-{m.group(1)}'
    x = _num(s)
    if x is not None and 20000 < x < 80000:
        return (datetime.date(1899, 12, 30) + datetime.timedelta(days=int(x))).isoformat()
    return None


def parse_tcmb(rows):
    """Arkusz T1_En → [[tydzień do (piątek), razem, akcje, obligacje skarbowe (zakup bezwarunkowy), obligacje firm i banków,
    rynek zagraniczny razem, euroobligacje rządu]] w mln USD — tylko część „B. NET TRANSACTIONS”, wiersze po nazwie."""
    head, got = None, {}
    for _, r in sorted(rows.items()):
        lab = re.sub(r'\s+', ' ', r.get(2, '')).strip().lower()
        if lab.startswith('b. net transactions'):
            head = {c: _tcmb_day(v) for c, v in r.items() if c > 2}
            head = {c: d for c, d in head.items() if d}
            continue
        if head is None:
            continue
        if not lab or lab.startswith('('):
            break          # koniec części B (przypisy)
        for key, pre in TCMB_KEYS:
            if key not in got and lab.startswith(pre):
                got[key] = {head[c]: _num(v) for c, v in r.items() if c in head and _num(v) is not None and _num(v) == _num(v)}
                break
    if not head or 'tot' not in got:
        raise RuntimeError('brak części „B. NET TRANSACTIONS”')
    days = sorted({d for x in got.values() for d in x})
    return [[d] + [(got.get(k) or {}).get(d) for k, _ in TCMB_KEYS] for d in days]


def tcmb_part(prev_tr):
    import html as _h
    import zipfile
    page = get_bytes(TCMB_PAGE, timeout=60).decode('utf-8', 'replace')
    m = re.search(r'href="([^"]*Securities\+Statistics\.zip[^"]*)"', page)
    if not m:
        raise RuntimeError('brak odnośnika do pliku')
    href = _h.unescape(m.group(1))
    z = zipfile.ZipFile(io.BytesIO(get_bytes(href if href.startswith('http') else 'https://www.tcmb.gov.tr' + href, timeout=90)))
    name = next((n for n in z.namelist() if n.lower().endswith('.xlsx')), None)
    if not name:
        raise RuntimeError('brak pliku .xlsx w archiwum')
    new = parse_tcmb(_xlsx_rows(z.read(name), 'T1_En'))
    big = [r[0] for r in new if any(v is not None and abs(v) > 1e5 for v in r[1:])]   # v77: tydzień > 100 mld USD = zła skala
    if big:
        raise RuntimeError(f'skala niezgodna ({big[-1]})')
    gap = [r[0] for r in new if None not in r[1:6] and abs(r[1] - r[2] - r[3] - r[4] - r[5]) > 1]
    if gap:
        META['notes'].append('CBRT: razem ≠ suma składników w tygodniach: ' + ', '.join(gap))
    have = {k: list(v) for k, v in _rows(prev_tr).items()}
    have.update({r[0]: r for r in new})      # nowszy plik poprawia poprzedni tydzień (dane wstępne)
    d = [have[k] for k in sorted(have)][-OBCE_KEEP:]
    return {'at': NOW, 'src': 'Central Bank of the Republic of Türkiye (CBRT) — Securities Statistics, Table 1, B. Net Transactions',
            'url': TCMB_PAGE, 'unit': 'mln USD; transakcje netto nierezydentów oczyszczone ze zmian cen i kursów; bez repo, zabezpieczeń i pożyczek papierów',
            'cols': ['tydzień do', 'razem', 'akcje', 'obligacje skarbowe (zakup bezwarunkowy)', 'obligacje firm i banków', 'rynek zagraniczny razem', 'euroobligacje rządu'],
            'asof': d[-1][0], 'd': d}


# v86: Tajlandia — ThaiBMA „Non-resident Flows”: dzienne transakcje nierezydentów w tajskich obligacjach (mln THB), bez klucza;
# ≈ USD kursem Fed H.10 (FRED DEXTHUS). Źródło oddaje całą historię (od 2016) w jednym pliku JSON.
THBMA_URL = 'https://www.thaibma.or.th/nrdaily/GetNR/'
THBMA_PAGE = 'https://www.thaibma.or.th/EN/Market/NR/NRDaily.aspx'
TH_KEEP = 270      # ok. 13 miesięcy sesji — wystarcza na sumę 12 miesięcy (250 sesji)
TH_COLS = ('NetFlow', 'TotalNetTrade', 'ShortTermTrade', 'LongTermTrade', 'ExpireToday', 'NetHolding')
TH_STALE = 8       # dni kalendarzowych bez nowego pełnego dnia = błąd (najdłuższa przerwa w 10 latach: 6 dni — Songkran, Nowy Rok)


def parse_thbma(j):
    """ThaiBMA /nrdaily/GetNR/ → [[data, przepływ netto (transakcje netto − wykupy), transakcje netto, z wykupem w ciągu roku, później,
    wykupy, stan posiadania (wartość nominalna)]] mln THB, rosnąco. Dzień w toku (najnowsza data bez części popołudniowej P3 — ThaiBMA
    publikuje ją o 16:30 w Bangkoku) pominięty; zakończony dzień bez P3 albo bez sum — wiersz z brakami (None), żeby sumy okien pokazały
    „—”, a nie sięgnęły dzień dalej; powtórzona data — pierwszy wiersz (różne wiersze = uwaga)."""
    if not isinstance(j, list):
        raise RuntimeError('odpowiedź nie jest listą')
    rows = [r for r in j if isinstance(r, dict) and re.match(r'^\d{4}-\d{2}-\d{2}$', str(r.get('Asof') or '')[:10])]
    if not rows:
        raise RuntimeError('brak dni')
    newest = max(str(r['Asof'])[:10] for r in rows)
    out, seen, dup, hole = {}, {}, [], []
    for r in rows:
        day = str(r['Asof'])[:10]
        v = [_num(r.get(k)) for k in TH_COLS]
        if day in seen:
            if seen[day] != v:
                dup.append(day)
            continue
        seen[day] = v
        if r.get('P3Net') is None and day == newest:
            continue                      # dzień w toku
        if r.get('P3Net') is None or v[0] is None or v[1] is None:
            v = [None] * len(TH_COLS); hole.append(day)
        out[day] = [day] + v
    if not out:
        raise RuntimeError('brak pełnych dni')
    keep = set(sorted(out)[-TH_KEEP:])
    if [x for x in dup if x in keep]:
        META['notes'].append('ThaiBMA: różne wiersze dla tej samej daty (wzięty pierwszy): ' + ', '.join(sorted(x for x in dup if x in keep)))
    if [x for x in hole if x in keep]:
        META['notes'].append('ThaiBMA: zakończony dzień bez części popołudniowej albo bez sum (pokazany jako brak): ' + ', '.join(sorted(x for x in hole if x in keep)))
    return [out[k] for k in sorted(out)]


def _th_scale_ok(r):
    return r[1] is None or (abs(r[1]) <= 2e5 and (r[6] is None or 1e5 < r[6] < 1e7))   # dzień ≤ 200 mld THB; stan 0,1–10 bln THB


def thbma_part(prev_th, key):
    prev_th = prev_th if isinstance(prev_th, dict) else {}
    try:
        j = json.loads(get_bytes(THBMA_URL, timeout=120).decode('utf-8-sig', 'replace'))
    except ValueError:
        raise RuntimeError('odpowiedź nie jest JSON (np. przerwa techniczna)')
    d = [list(r) for r in parse_thbma(j)[-TH_KEEP:]]      # źródło oddaje całą historię — wiersze tylko ze źródła (bez starych dni z pliku)
    if len(d) < 30:
        raise RuntimeError(f'za mało dni ({len(d)})')
    if not _th_scale_ok(d[-1]):
        raise RuntimeError(f'skala niezgodna ({d[-1][0]})')    # najnowszy dzień — zostaje poprzednia część
    bad = [r[0] for r in d if not _th_scale_ok(r)]
    for r in d:
        if r[0] in bad:
            r[1:7] = [None] * 6                                # pojedynczy zły dzień = brak, nie blokada całej części
    if bad:
        META['errors'].append('ThaiBMA: wartości poza skalą (pokazane jako brak) w dniach: ' + ', '.join(bad[-5:]))
    gap = [r[0] for r in d[-30:] if None not in r[1:6] and (abs(r[2] - r[3] - r[4]) > 1 or abs(r[2] - r[5] - r[1]) > 1)]
    if gap:
        META['notes'].append('ThaiBMA: sumy niezgodne w dniach: ' + ', '.join(gap))
    rates = {}
    if key:
        try:
            rates = fred_rates(key, 'DEXTHUS', limit=400)
        except Exception as e:
            META['errors'].append(mask(f'ThaiBMA kurs FRED DEXTHUS: {e}'))
    old = _rows(prev_th)
    for r in d:
        rd, rt = _rate_for(rates, r[0])
        if rt and r[1] is not None:
            r[7:] = [round(r[1] / rt, 1), rt, rd]
        elif r[1] is not None and r[0] in old and len(old[r[0]]) >= 10 and old[r[0]][1] == r[1]:
            r[7:] = old[r[0]][7:10]
        else:
            r[7:] = [None, None, None]
    last = d[-1][0]
    if ((_now_utc() + datetime.timedelta(hours=7)).date() - datetime.date.fromisoformat(last)).days > TH_STALE:
        META['errors'].append(f'ThaiBMA: brak nowego pełnego dnia po {last}')
    return {'at': NOW, 'src': 'The Thai Bond Market Association (ThaiBMA) — Non-resident Flows (daily)', 'url': THBMA_PAGE,
            'unit': 'mln THB; ≈ mln USD kursem Fed H.10 (FRED DEXTHUS); stan posiadania w wartości nominalnej',
            'cols': ['data', 'przepływ netto', 'transakcje netto', 'z wykupem w ciągu roku', 'z wykupem później', 'wykupy', 'stan posiadania',
                     '≈ mln USD', 'kurs THB/USD', 'dzień kursu'],
            'asof': last, 'd': d}

OBCE_SLOW = {'br': 180, 'tr': 180, 'th': 180}   # v91: min — Brazylia i Turcja publikują raz w tygodniu, ThaiBMA raz dziennie (regulamin: umiar)


def build_obce(key, prev=None):
    """data/obce.json — każda część osobno: awaria jednej zostawia jej poprzednią wersję (brak nie jest zerem)."""
    prev = prev if isinstance(prev, dict) else {}
    out = {'at': NOW, 'ok': {}, 'errs': {}}   # v77: stan i błędy części — widoczne także przy przebiegach z pamięci
    for part, fn in (('in', lambda: nsdl_part(prev.get('in'))), ('tw', lambda: twse_part(prev.get('tw'), key)),
                     ('hk', lambda: hkex_part(prev.get('hk'), key)),   # v67: Stock Connect southbound
                     ('br', lambda: bcb_part(prev.get('br'))),   # v71: Brazylia — rynek walutowy (BCB)
                     ('tr', lambda: tcmb_part(prev.get('tr'))),   # v74: Turcja — nierezydenci w papierach (CBRT)
                     ('th', lambda: thbma_part(prev.get('th'), key))):   # v86: Tajlandia — nierezydenci w obligacjach (ThaiBMA)
        n0 = len(META['errors'])
        pp = prev.get(part)
        if part in OBCE_SLOW and isinstance(pp, dict) and (prev.get('ok') or {}).get(part) is True and fresh(pp, OBCE_SLOW[part]):
            out[part] = pp; META['ok']['obce_' + part] = 'cached'; out['ok'][part] = True   # v91: źródło publikujące rzadziej — bez zapytania
            continue
        try:
            out[part] = fn(); META['ok']['obce_' + part] = True
        except Exception as e:
            META['errors'].append(mask(f"{ {'in': 'NSDL', 'tw': 'TWSE', 'hk': 'HKEX', 'br': 'BCB', 'tr': 'CBRT', 'th': 'ThaiBMA'}[part] }: {e}")); META['ok']['obce_' + part] = False
            if isinstance(prev.get(part), dict):
                out[part] = prev[part]
        out['ok'][part] = META['ok']['obce_' + part]
        if META['errors'][n0:]:
            out['errs'][part] = META['errors'][n0:]
    if not any(p in out for p in ('in', 'tw', 'hk', 'br', 'tr', 'th')):
        raise RuntimeError('żadna część nie odpowiedziała')
    return out


# v56: BIS — nominalne efektywne kursy walut (WS_EER, szeroki koszyk 64 gospodarek), bez klucza
EER_AREAS = ['US', 'XM', 'GB', 'CH', 'SE', 'NO', 'PL', 'JP', 'KR', 'CN', 'HK', 'IN', 'ID', 'SG', 'TH', 'MY', 'PH', 'AU', 'NZ',
             'CA', 'BR', 'MX', 'TR', 'IL', 'ZA', 'SA', 'RU']
EER_BASE = 'https://stats.bis.org/api/v2/data/dataflow/BIS/WS_EER/1.0/'


def eer_summary(daily, monthly):
    """Poziom (ostatni dzienny), zmiana 30 dni (dzienne) i 12 miesięcy (średnie miesięczne, ten sam miesiąc rok wcześniej), w %."""
    rows = {}
    for a in EER_AREAS:
        d = daily.get(a) or []; m = monthly.get(a) or []
        if not d and not m:
            continue
        r = {'v': None, 'd': None, 'c30': None, 'm': None, 'c12': None}
        if d:
            p, v = d[-1]
            lim = (datetime.date.fromisoformat(p) - datetime.timedelta(days=30)).isoformat()
            base = [x for q, x in d if q <= lim]
            r.update({'v': v, 'd': p, 'c30': round((v / base[-1] - 1) * 100, 2) if base and base[-1] else None})
        if m:
            p, v = m[-1]
            ago = f'{int(p[:4]) - 1:04d}-{p[5:7]}'
            prev = [x for q, x in m if q == ago]
            r.update({'m': p, 'c12': round((v / prev[0] - 1) * 100, 2) if prev and prev[0] else None})
        rows[a] = r
    return rows


def build_eer():
    """data/eer.json — kursy efektywne BIS: dzienne 45 obserwacji (do zmiany 30 dni), miesięczne 14 (do zmiany 12 mies.)."""
    keys = '+'.join(EER_AREAS)
    daily = parse_cbpol_csv(get_bytes(EER_BASE + f'D.N.B.{keys}?lastNObservations=45&format=csv&detail=dataonly', timeout=90))
    try:
        monthly = parse_cbpol_csv(get_bytes(EER_BASE + f'M.N.B.{keys}?lastNObservations=14&format=csv&detail=dataonly', timeout=90))
    except Exception as e:
        META['errors'].append(mask(f'BIS kursy efektywne (miesięczne): {e}')); monthly = {}
    rows = eer_summary(daily, monthly)
    if not rows:
        raise RuntimeError('żadna waluta')
    return {'at': NOW, 'src': 'BIS — Effective exchange rates (WS_EER), nominal, broad basket', 'url': 'https://data.bis.org/topics/EER',
            'unit': 'indeks 2020=100; zmiany w %', 'asof': max((r['d'] or '') for r in rows.values()), 'rows': rows}


# v59: MFW COFER — skład walutowy światowych rezerw walutowych (kwartalnie), bez klucza
COFER_CUR = ['CI_USD', 'CI_EUR', 'CI_JPY', 'CI_GBP', 'CI_CNY', 'CI_CAD', 'CI_AUD', 'CI_CHF', 'CI_OTHC']
COFER_URL = ('https://api.imf.org/external/sdmx/3.0/data/dataflow/IMF.STA/COFER/+/G001.AFXRA+TFXRA+TFXRA_IMP.'
             + '+'.join(COFER_CUR + ['CI_T']) + '.SHRO_PT+NV_USD.Q?lastNObservations=9')


def _imf_quarter(p):
    s = str(p or '').strip()
    return s if re.match(r'^\d{4}-Q[1-4]$', s) else None


def _q_add(q, n):
    i = int(q[:4]) * 4 + int(q[-1]) - 1 + n
    return f'{i // 4:04d}-Q{i % 4 + 1}'


def parse_cofer(j):
    """COFER → udział walut w rezerwach przypisanych do walut (%), zmiana 1 kw. i 1 roku (pkt proc.), wartość (mld USD).
    Brak wartości = brak (nie zero); zmiana tylko z dokładnie tego kwartału rok / kwartał wcześniej."""
    ser = parse_imf_sdmx(j, _imf_quarter)
    alloc = dict(ser.get(('G001', 'AFXRA', 'CI_T', 'NV_USD', 'Q')) or [])
    total = dict(ser.get(('G001', 'TFXRA', 'CI_T', 'NV_USD', 'Q')) or [])
    if not alloc:
        raise RuntimeError('COFER: brak sumy rezerw przypisanych do walut')
    q = max(alloc)
    bn = lambda v: None if v is None else round(v / 1e9, 1)
    dif = lambda a, b: None if a is None or b is None else round(a - b, 2)
    rows = {}
    for c in COFER_CUR:
        sh = dict(ser.get(('G001', 'AFXRA', c, 'SHRO_PT', 'Q')) or []); v = dict(ser.get(('G001', 'AFXRA', c, 'NV_USD', 'Q')) or [])
        if sh.get(q) is None and v.get(q) is None:
            continue
        s0, v0, v4 = sh.get(q), v.get(q), v.get(_q_add(q, -4))
        rows[c[3:]] = {'sh': None if s0 is None else round(s0, 2), 'd1': dif(s0, sh.get(_q_add(q, -1))), 'd4': dif(s0, sh.get(_q_add(q, -4))),
                       'v': bn(v0), 'dv4': bn(None if v0 is None or v4 is None else v0 - v4)}
    if not rows:
        raise RuntimeError('COFER: żadna waluta')
    tq = total.get(q)
    imp = dict(ser.get(('G001', 'TFXRA_IMP', 'CI_T', 'SHRO_PT', 'Q')) or []).get(q)   # od 2026: część składu szacuje MFW
    return {'src': 'International Monetary Fund, Currency Composition of Official Foreign Exchange Reserves (COFER)',
            'url': 'https://data.imf.org/en/datasets/IMF.STA:COFER', 'unit': '% rezerw przypisanych do walut; mld USD',
            'asof': q, 'alloc': bn(alloc[q]), 'total': bn(tq), 'alloc_pct': round(alloc[q] / tq * 100, 1) if tq else None,
            'imp_pct': None if imp is None else round(imp, 2),
            'order': [c[3:] for c in COFER_CUR if c[3:] in rows], 'rows': rows}


def build_cofer():
    out = parse_cofer(get_json(COFER_URL, {'Accept': 'application/json'}))
    out['at'] = NOW
    return out


# v70: MFW — bilans płatniczy (BOP, BPM6), kwartalnie, bez klucza: ZMIERZONE przepływy kapitału 37 gospodarek.
# L_NIL_T = kapitał zagraniczny napływający do kraju; A_NFA_T = kapitał mieszkańców wysyłany za granicę; NETCD_T CAB = rachunek bieżący.
BIL_CTY = ['USA', 'CAN', 'BRA', 'MEX', 'CHL', 'COL', 'ARG', 'DEU', 'FRA', 'GBR', 'ITA', 'ESP', 'NLD', 'CHE', 'SWE', 'POL', 'IRL',
           'RUS', 'SAU', 'TUR', 'ISR', 'ZAF', 'EGY', 'NGA', 'IND', 'CHN', 'HKG', 'JPN', 'KOR', 'IDN', 'SGP', 'THA', 'MYS', 'PHL',
           'VNM', 'AUS', 'NZL']
BIL_KEYS = {('L_NIL_T', 'D_F'): 'in_d', ('L_NIL_T', 'P_F'): 'in_p', ('L_NIL_T', 'P_F5'): 'in_pe', ('L_NIL_T', 'P_F3'): 'in_pd',
            ('L_NIL_T', 'O_F'): 'in_o', ('A_NFA_T', 'D_F'): 'out_d', ('A_NFA_T', 'P_F'): 'out_p', ('A_NFA_T', 'O_F'): 'out_o',
            ('NETCD_T', 'CAB'): 'ca'}
BIL_URL = ('https://api.imf.org/external/sdmx/3.0/data/dataflow/IMF.STA/BOP/+/' + '+'.join(BIL_CTY)
           + '.L_NIL_T+A_NFA_T+NETCD_T.D_F+P_F+P_F5+P_F3+O_F+CAB.USD.Q?lastNObservations=8&dimensionAtObservation=TIME_PERIOD')


def parse_bilans(j):
    """BOP MFW → {kraj: {'q': ostatni kwartał z napływem, 's': {klucz: [[kwartał, mln USD]] rosnąco, najwyżej 8}}}.
    Wartość pusta / NaN = brak (nigdy 0). Straż skali: napływ portfelowy USA musi mieć rząd mld–bln USD na kwartał."""
    ser = parse_imf_sdmx(j, _imf_quarter)
    rows = {}
    for lab, d in ser.items():
        if len(lab) != 5:
            continue
        c, e, i, u, f = lab
        k = BIL_KEYS.get((e, i))
        if not k or u != 'USD' or f != 'Q' or c not in BIL_CTY:
            continue
        rows.setdefault(c, {})[k] = [[q, round(v / 1e6, 1)] for q, v in d][-8:]
    usa = (rows.get('USA') or {}).get('in_p')
    if usa and not 1e3 <= abs(usa[-1][1]) <= 1e7:
        raise RuntimeError(f'skala niezgodna (USA napływ portfelowy {usa[-1][1]} mln USD)')
    out = {}
    for c in BIL_CTY:
        s = rows.get(c)
        qs = [x[-1][0] for k, x in (s or {}).items() if k in ('in_d', 'in_p', 'in_o') and x]
        if qs:
            q = max(qs); lo = _q_add(q, -4)
            out[c] = {'q': q, 's': {k: x for k, x in s.items() if x and x[-1][0] >= lo}}   # v73: seria urwana dawno (np. Wietnam 2014) — poza plikiem
    if not out:
        raise RuntimeError('żaden kraj z napływem kapitału')
    return {'src': 'International Monetary Fund, Balance of Payments (BOP)', 'url': 'https://data.imf.org/en/datasets/IMF.STA:BOP',
            'unit': 'mln USD, transakcje w kwartale; in_* = kapitał zagraniczny napływający do kraju (net incurrence of liabilities), '
                    'out_* = kapitał mieszkańców wysyłany za granicę (net acquisition of financial assets); d bezpośrednie, '
                    'p portfelowe (pe akcje, pd obligacje), o pozostałe (kredyty, depozyty); ca = saldo rachunku bieżącego',
            'asof_max': max(r['q'] for r in out.values()), 'order': [c for c in BIL_CTY if c in out], 'rows': out}


def build_bilans():
    out = parse_bilans(get_json(BIL_URL, {'Accept': 'application/json'}))
    out['at'] = NOW
    return out


# v72: SAFE (Chiny) — kupno i sprzedaż walut przez banki w imieniu klientów, miesięcznie, w USD; bez klucza
SAFE_PAGE = 'https://www.safe.gov.cn/en/2023/0215/2048.html'
SAFE_LINK = 'Time-series Data of Foreign Exchange Settlement and Sales by Banks'
SAFE_SHEET = 'in USD (Monthly)'
SAFE_KEEP = 25     # miesięcy (12-miesięczna suma i porównanie rok do roku)


def _xlsx_rows(data, sheet):
    """Czytnik .xlsx na bibliotece standardowej (zip + ElementTree): {nr wiersza: {nr kolumny: tekst}} arkusza o podanej nazwie.
    v77: puste komórki i wiersze pomijane bez przesuwania sąsiednich; tekst sformatowany (kilka <r><t>) sklejany; komórka bez
    adresu = następna kolumna; system dat 1904 = jawny błąd (daty byłyby przesunięte o 4 lata)."""
    import html as _h
    import zipfile
    import xml.etree.ElementTree as ET
    z = zipfile.ZipFile(io.BytesIO(data))
    names = set(z.namelist())
    loc = lambda tag: str(tag).rsplit('}', 1)[-1]
    def text(el):   # v80: tekst elementu bez podpowiedzi fonetycznych (<rPh>)
        if loc(el.tag) == 'rPh':
            return ''
        own = (el.text or '') if loc(el.tag) == 't' else ''
        return own + ''.join(text(c) for c in el)
    strs = []
    if 'xl/sharedStrings.xml' in names:
        strs = [text(si) for si in ET.fromstring(z.read('xl/sharedStrings.xml')) if loc(si.tag) == 'si']
    wb = z.read('xl/workbook.xml').decode('utf-8')
    if re.search(r'date1904="(1|true)"', wb):
        raise RuntimeError('system dat 1904 — nieobsługiwany')
    attr = lambda tag, a: (re.search(r'\b' + a + r'="([^"]*)"', tag) or [None, None])[1]
    rid = None
    for tag in re.findall(r'<sheet\b[^>]*>', wb):
        if _h.unescape(attr(tag, 'name') or '') == sheet:
            rid = attr(tag, 'r:id')
    if not rid:
        raise RuntimeError(f'brak arkusza „{sheet}”')
    target = None
    for tag in re.findall(r'<Relationship\b[^>]*>', z.read('xl/_rels/workbook.xml.rels').decode('utf-8')):
        if attr(tag, 'Id') == rid:
            target = attr(tag, 'Target')
    path = 'xl/' + target.lstrip('/').replace('xl/', '', 1) if target else None
    if path not in names:
        raise RuntimeError('brak pliku arkusza')
    col = lambda c: sum((ord(ch) - 64) * 26 ** i for i, ch in enumerate(reversed(c)))
    rows, rn = {}, 0
    for row in ET.fromstring(z.read(path)).iter():
        if loc(row.tag) != 'row':
            continue
        r = row.get('r')
        rn = int(r) if r and r.isdigit() else rn + 1
        cn = 0
        for c in row:
            if loc(c.tag) != 'c':
                continue
            m = re.match(r'^([A-Z]+)\d*$', c.get('r') or '')
            cn = col(m.group(1)) if m else cn + 1
            t = c.get('t')
            if t == 'inlineStr':
                v = text(c)
            else:
                ve = next((x for x in c if loc(x.tag) == 'v'), None)
                if ve is None or ve.text is None:
                    continue          # pusta komórka — bez wartości, sąsiedzi na swoich miejscach
                v = ve.text
                if t == 's':
                    i = int(v); v = strs[i] if 0 <= i < len(strs) else ''
            rows.setdefault(rn, {})[cn] = v.strip()
    return rows


def _xlsx_month(v):
    """Nagłówek miesiąca: liczba seryjna Excela (46235 = 2026-08-01) albo tekst '2026-08' / '2026.08' → 'YYYY-MM'."""
    s = str(v or '').strip()
    m = re.match(r'^(\d{4})[.\-/](\d{1,2})$', s)
    if m and 1 <= int(m.group(2)) <= 12:
        return f'{m.group(1)}-{int(m.group(2)):02d}'
    x = _num(s)
    if x is not None and 20000 < x < 80000:
        d = datetime.date(1899, 12, 30) + datetime.timedelta(days=int(x))
        return f'{d.year:04d}-{d.month:02d}'
    return None


def parse_safe(rows):
    """Arkusz „in USD (Monthly)” → [[YYYY-MM, klienci saldo, bieżący saldo, kapitałowy saldo, bezpośrednie saldo, portfelowe saldo,
    kapitałowy rozliczenie, kapitałowy sprzedaż]] w mld USD (plik: 100 mln USD). Wiersze po nazwie w sekcjach I/II/III, nie po numerze."""
    head = next((r for _, r in sorted(rows.items()) if r.get(1, '').strip().lower() == 'item'), None)
    if not head:
        raise RuntimeError('brak wiersza nagłówka')
    months = {c: _xlsx_month(v) for c, v in head.items() if c > 1}
    months = {c: m for c, m in months.items() if m}
    if not months:
        raise RuntimeError('brak miesięcy w nagłówku')
    sec, cust, got = None, False, {}
    for _, r in sorted(rows.items()):
        lab = re.sub(r'\s+', ' ', r.get(1, '')).strip()
        m = re.match(r'^(I|II|III|IV|V|VI|VII|VIII)\.\s', lab)
        if m:
            sec, cust = m.group(1), False
            continue
        if sec not in ('I', 'II', 'III'):
            continue
        low = lab.lower()
        if low.startswith('(ii) by banks for customers'):
            cust = True; key = 'cust'
        elif low.startswith('(i) by banks for themselves'):
            cust = False; continue
        elif cust and low.startswith('1. current account'):
            key = 'ca'
        elif cust and low.startswith('2. capital and financial account'):
            key = 'cfa'
        elif cust and 'direct investment' in low:
            key = 'fdi'
        elif cust and low.startswith('portfolio investment'):
            key = 'port'
        else:
            continue
        got[(sec, key)] = {months[c]: _num(v) for c, v in r.items() if c in months and _num(v) is not None and _num(v) == _num(v)}
    need = [('III', 'cust'), ('III', 'ca'), ('III', 'cfa')]
    if any(k not in got for k in need):
        raise RuntimeError('brak wierszy salda (klienci / bieżący / kapitałowy)')
    keys = [('III', 'cust'), ('III', 'ca'), ('III', 'cfa'), ('III', 'fdi'), ('III', 'port'), ('I', 'cfa'), ('II', 'cfa')]
    allm = sorted({mm for k in keys for mm in (got.get(k) or {})})
    bn = lambda v: None if v is None else round(v / 10, 2)
    out = [[mm] + [bn((got.get(k) or {}).get(mm)) for k in keys] for mm in allm]
    out = [r for r in out if any(x is not None for x in r[1:])][-SAFE_KEEP:]
    g = out[-1][6]   # straż skali: rozliczenia kapitałowe klientów w miesiącu to dziesiątki mld USD
    if g is not None and not 5 <= g <= 1000:
        raise RuntimeError(f'skala niezgodna (rozliczenia kapitałowe {g} mld USD)')
    return out


def build_safe():
    page = get_bytes(SAFE_PAGE, timeout=60).decode('utf-8', 'replace')
    href = next((h for h, t in re.findall(r'<a[^>]+href="([^"]+\.xlsx)"[^>]*>(.*?)</a>', page, re.S)
                 if SAFE_LINK.lower() in re.sub(r'<[^>]+>', '', t).lower()), None)
    if not href:
        raise RuntimeError('brak odnośnika do pliku')
    url = href if href.startswith('http') else 'https://www.safe.gov.cn' + href
    m = parse_safe(_xlsx_rows(get_bytes(url, timeout=90), SAFE_SHEET))
    ids = [r for r in m if None not in r[1:4] and abs(r[1] - r[2] - r[3]) > 0.2]
    if ids:
        META['notes'].append(f'SAFE: saldo klientów ≠ bieżący + kapitałowy w {len(ids)} mies. (np. {ids[-1][0]})')
    return {'at': NOW, 'src': 'State Administration of Foreign Exchange (SAFE), Data on Foreign Exchange Settlement and Sales by Banks',
            'url': SAFE_PAGE, 'file': url, 'unit': 'mld USD; saldo = rozliczenie (klienci sprzedają waluty bankom) minus sprzedaż (kupują od banków)',
            'cols': ['miesiąc', 'klienci saldo', 'rachunek bieżący saldo', 'kapitałowy i finansowy saldo', 'w tym bezpośrednie saldo',
                     'w tym portfelowe saldo', 'kapitałowy rozliczenie', 'kapitałowy sprzedaż'],
            'asof': m[-1][0], 'm': m}


# v76: Eurostat — miesięczny bilans płatniczy krajów UE (bop_c6_m), bez klucza
UE_GEO = ['AT', 'BE', 'BG', 'CY', 'CZ', 'DE', 'DK', 'EE', 'EL', 'ES', 'FI', 'FR', 'HR', 'HU', 'IE', 'IT', 'LT', 'LU', 'LV', 'MT',
          'NL', 'PL', 'PT', 'RO', 'SE', 'SI', 'SK']
UE_KEYS = {('FA__P__F', 'LIAB'): 'in_p', ('FA__D__F', 'LIAB'): 'in_d', ('FA__O__F', 'LIAB'): 'in_o',
           ('FA__P__F', 'ASS'): 'out_p', ('FA__D__F', 'ASS'): 'out_d', ('FA__O__F', 'ASS'): 'out_o'}
UE_URL = ('https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/bop_c6_m?format=JSON&lang=EN&currency=MIO_EUR'
          '&partner=WRL_REST&sector10=S1&sector10=S121&sectpart=S1&lastTimePeriod=24&' + '&'.join('geo=' + g for g in UE_GEO)
          + '&bop_item=FA__P__F&bop_item=FA__D__F&bop_item=FA__O__F&stk_flow=LIAB&stk_flow=ASS')


def parse_jsonstat(j):
    """JSON-stat 2.0 (Eurostat) → ({(kod wymiaru 1, 2, …): wartość}, id wymiarów, {(…): litery statusu}); indeks płaski = wiersz po
    wymiarach w kolejności 'id'; indeks kategorii jako obiekt albo lista (v80); status np. 'e' szacunek, 'p' wstępne (poufne bez wartości)."""
    try:
        ids, size = j['id'], j['size']
        keys = []
        for d in ids:
            idx = j['dimension'][d]['category']['index']
            if isinstance(idx, list):
                idx = {k: i for i, k in enumerate(idx)}
            keys.append([k for k, _ in sorted(idx.items(), key=lambda kv: kv[1])])
        vals = j.get('value') or {}
        stat = j.get('status') or {}
    except (KeyError, TypeError, AttributeError) as e:
        raise RuntimeError(f'nieznany kształt odpowiedzi ({e})')
    if isinstance(vals, list):
        vals = {str(i): v for i, v in enumerate(vals) if v is not None}
    if isinstance(stat, list):
        stat = {str(i): v for i, v in enumerate(stat) if v}

    def lab(flat):
        i, out = int(flat), []
        for s, ks in zip(reversed(size), reversed(keys)):
            out.append(ks[i % s]); i //= s
        return tuple(reversed(out))
    out, flags = {}, {}
    for flat, v in vals.items():
        try:
            k = lab(flat)
        except (ValueError, IndexError):
            continue
        x = _num(v)
        if x is not None and x == x:
            out[k] = x
            f = ''.join(ch for ch in str(stat.get(flat, '')).split('|')[0] if ch in 'ep')
            if f:
                flags[k] = f
    if not out:
        raise RuntimeError('brak wartości')
    return out, ids, flags


def parse_ue(j):
    """bop_c6_m → {kraj: {'m': ostatni miesiąc z kompletem składników, 's': {klucz: [[YYYY-MM, mln EUR]]}, 'f': {YYYY-MM: 'e'|'p'}}}.
    v80: „pozostałe” bez banku centralnego (S1 minus S121, tylko gdy są obie wartości; bez S121 = brak, nigdy suma z TARGET2)."""
    vals, ids, flags = parse_jsonstat(j)
    pos = {d: i for i, d in enumerate(ids)}
    rows, cb, fl = {}, {}, {}
    for lab, v in vals.items():
        item, flow, g, m = lab[pos['bop_item']], lab[pos['stk_flow']], lab[pos['geo']], lab[pos['time']]
        sec = lab[pos['sector10']] if 'sector10' in pos else 'S1'
        if g not in UE_GEO or not re.match(r'^\d{4}-\d{2}$', m):
            continue
        k = UE_KEYS.get((item, flow))
        if not k:
            continue
        if sec == 'S121':
            if item == 'FA__O__F':
                cb.setdefault(g, {}).setdefault(k, {})[m] = v
            continue
        if sec != 'S1':
            continue
        rows.setdefault(g, {}).setdefault(k, {})[m] = v
        if lab in flags and k.startswith('in_'):
            fl.setdefault(g, {})[m] = ''.join(sorted(set(fl.get(g, {}).get(m, '') + flags[lab])))
    out = {}
    for g in UE_GEO:
        s0 = rows.get(g) or {}
        s = {}
        for k, series in s0.items():
            if k.endswith('_o'):   # pozostałe bez banku centralnego
                c = (cb.get(g) or {}).get(k) or {}
                series = {m: v - c[m] for m, v in series.items() if m in c}
            if series:
                s[k] = [[m, round(v, 1)] for m, v in sorted(series.items())]
        have = lambda k: {m for m, _ in s.get(k, [])}
        full = have('in_p') & have('in_d') & have('in_o')
        anym = have('in_p') | have('in_d') | have('in_o')
        if anym:
            out[g] = {'m': max(full) if full else max(anym), 's': s}
            if fl.get(g):
                out[g]['f'] = fl[g]
    if not out:
        raise RuntimeError('żaden kraj z napływem kapitału')
    de = [abs(v) for _, v in ((out.get('DE') or {}).get('s', {}).get('in_p') or [])]
    if de and not 100 <= max(de) <= 1e6:
        raise RuntimeError(f'skala niezgodna (Niemcy, napływ portfelowy do {max(de)} mln EUR)')
    return {'src': 'Eurostat, Balance of payments by country — monthly data (BPM6), bop_c6_m', 'url': 'https://ec.europa.eu/eurostat/databrowser/view/bop_c6_m/default/table',
            'unit': 'mln EUR, transakcje w miesiącu; in_* = napływ kapitału z zagranicy (pasywa), out_* = kapitał mieszkańców za granicę (aktywa); '
                    'p portfelowe, d bezpośrednie, o pozostałe bez banku centralnego (S1 − S121); f = status Eurostatu (e szacunek, p wstępne)',
            'asof_max': max(r['m'] for r in out.values()), 'order': [g for g in UE_GEO if g in out], 'rows': out}


def build_ue():
    out = parse_ue(get_json(UE_URL))
    out['at'] = NOW
    return out


# v78: Kanada — Statistics Canada, tabela 36-10-0028-01 (International transactions in securities), miesięcznie, bez klucza
KAN_VEC = (('tot', 61915649), ('debt', 61915652), ('bond', 61915682), ('mm', 61915655), ('eq', 61915712))   # stałe numery wektorów „Net flows”
KAN_URL = 'https://www150.statcan.gc.ca/t1/wds/rest/getDataFromVectorByReferencePeriodRange?vectorIds={v}&startRefPeriod={a}&endReferencePeriod={b}'


def parse_kanada(j):
    """WDS → [[YYYY-MM, razem, dłużne, obligacje, rynek pieniężny, akcje]] w mln CAD; brak = None (nigdy 0); skala ≠ miliony = błąd."""
    names = {vid: k for k, vid in KAN_VEC}
    got = {}
    for x in j if isinstance(j, list) else []:
        o = (x or {}).get('object') or {}
        name = names.get(o.get('vectorId'))
        if not name:
            continue
        for p in o.get('vectorDataPoint') or []:
            m = re.match(r'^(\d{4}-\d{2})-01$', str(p.get('refPer', '')))
            v = p.get('value')
            if not m or not isinstance(v, (int, float)) or v != v:
                continue
            if p.get('scalarFactorCode') != 6:
                raise RuntimeError(f'skala niezgodna (scalarFactorCode {p.get("scalarFactorCode")})')
            got.setdefault(name, {})[m.group(1)] = round(float(v), 1)
    if 'tot' not in got:
        raise RuntimeError('brak serii „razem”')
    months = sorted({mm for d in got.values() for mm in d})[-25:]
    rows = [[mm] + [got.get(k, {}).get(mm) for k, _ in KAN_VEC] for mm in months]
    bad = [r[0] for r in rows if None not in (r[1], r[2], r[5]) and abs(r[1] - r[2] - r[5]) > 2]
    if bad:
        META['notes'].append('Statistics Canada: razem ≠ dłużne + akcje w miesiącach: ' + ', '.join(bad))
    return rows


def build_kanada():
    now = _now_utc().date()
    v = ','.join(f'%22{vid}%22' for _, vid in KAN_VEC)
    m = parse_kanada(get_json(KAN_URL.format(v=v, a=f'{now.year - 2}-{now.month:02d}-01', b=now.isoformat())))
    return {'at': NOW, 'src': 'Statistics Canada, Table 36-10-0028-01 International transactions in securities, portfolio transactions in Canadian and foreign securities, monthly',
            'url': 'https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=3610002801',
            'unit': 'mln CAD; transakcje nierezydentów w kanadyjskich papierach, netto (plus = zagranica kupiła netto)',
            'cols': ['miesiąc', 'razem', 'dłużne', 'obligacje', 'rynek pieniężny', 'akcje i jednostki funduszy'], 'asof': m[-1][0], 'm': m}


# v87: Polska — Ministerstwo Finansów: nierezydenci w krajowych skarbowych papierach wartościowych (SPW), miesięcznie, bez klucza.
# Odnośniki do plików zmieniają się co miesiąc — szukamy ich po stałych tytułach na stronie „Struktura inwestorów”.
SPW_PAGE = 'https://www.gov.pl/web/finanse/struktura-inwestorow'
SPW_BASE = 'https://www.gov.pl'
SPW_FILES = {'st': 'Struktura podmiotowa zadłużenia wobec nierezydentów w krajowych SPW',
             'kr': 'Zadłużenie wobec nierezydentów w krajowych SPW po krajach'}
SPW_A = re.compile(r'<a\b[^>]*href="(/attachment/[0-9a-f-]{36})"[^>]*>((?:(?!</a>).)*?)</a>', re.S)
SPW_FILE_RE = {'st': re.compile(r'Struktura_nierezydentow\d{2}\.xlsm'), 'kr': re.compile(r'Nierezydenci_kraje\d{2}\.xlsx')}   # v87.1: zapas — nazwa pliku
SPW_KEEP = 25      # miesięcy (zmiana 12 miesięcy i rok wcześniej)
SPW_GRP = 13       # miesięcy dla typów i regionów (zmiana 12 miesięcy)
SPW_STALE = 70     # dni po końcu najnowszego miesiąca bez nowego pliku = błąd (zwykle ok. miesiąca)
SPW_TYPES = (('bank', 'banki'), ('cb', 'banki centralne'), ('pub', 'instytucje publiczne'), ('ins', 'zakłady ubezpieczeniowe'),
             ('pen', 'fundusze emerytalne'), ('inv', 'fundusze inwestycyjne'), ('hf', 'fundusze hedgingowe'), ('hh', 'gospodarstwa domowe'),
             ('corp', 'przedsiębiorstwa niefinansowe'), ('oth', 'inne podmioty'), ('omni', 'rachunki zbiorcze'), ('tot', 'razem'))
SPW_REGS = (('ea', 'europa - kraje strefy euro'), ('eun', 'europa - kraje ue spoza strefy euro'), ('eur', 'europa - kraje spoza ue'),
            ('afr', 'afryka'), ('sam', 'ameryka południowa'), ('nam', 'ameryka północna'), ('oce', 'australia i oceania'),
            ('asia', 'azja'), ('me', 'bliski wschód'), ('omni', 'rachunki zbiorcze'), ('tot', 'razem'))


def _spw_month(v):
    x = _num(v)
    if x is None or not 30000 < x < 80000:
        return None
    d = datetime.date(1899, 12, 30) + datetime.timedelta(days=int(x))
    return f'{d.year:04d}-{d.month:02d}'


def spw_sheet(rows, keys):
    """Arkusz MF → {miesiąc: {klucz: mln zł}}; kolumny po nazwach z wiersza nagłówka „Data” (najpierw nazwa dokładna, potem początek:
    „Banki” ≠ „Banki centralne”); brak kolumny „Razem” = błąd; pusta komórka = brak, nie zero."""
    col, out = None, {}
    for _, r in sorted(rows.items()):
        lab = re.sub(r'\s+', ' ', str(r.get(1, ''))).strip().lower()
        if col is None:
            if lab == 'data':
                heads = {c: re.sub(r'\s+', ' ', str(v)).strip().lower() for c, v in r.items() if c > 1}
                col = {}
                for k, pre in keys:
                    hit = [c for c, h in heads.items() if h == pre] or [c for c, h in heads.items() if h.startswith((pre + ' ', pre + '('))]
                    if len(hit) == 1:
                        col[k] = hit[0]
                if 'tot' not in col:
                    raise RuntimeError('brak kolumny „Razem”')
            continue
        m = _spw_month(r.get(1))
        if m:
            out[m] = {k: _num(r.get(c)) for k, c in col.items()}
    if not out:
        raise RuntimeError('brak miesięcy')
    return out


def spw_links(page):
    out, alt = {}, {}
    for href, txt in SPW_A.findall(page):
        t = re.sub(r'\s+', ' ', _html.unescape(re.sub(r'<[^>]+>', ' ', txt))).strip()
        for k, name in SPW_FILES.items():
            if t == name or t.startswith(name + ' '):     # „…w krajowych SPW” ≠ „…w krajowych SPW po krajach”
                out.setdefault(k, SPW_BASE + href)
            if SPW_FILE_RE[k].search(t.replace('\u200b', '').replace(' ', '')):
                alt.setdefault(k, SPW_BASE + href)      # v87.1: tytuł zmieniony — plik rozpoznany po nazwie
    for k, u in alt.items():
        out.setdefault(k, u)
    return out


def spw_countries(data):
    """Plik „po krajach” → [{'m': YYYY-MM, 'c': [[kraj PL, kraj EN, mln zł, udział %]]}] dla dwóch najnowszych arkuszy (pierwsze w pliku);
    bez wierszy sum, rachunków zbiorczych i banków centralnych (tak jak w źródle)."""
    import zipfile
    names = [_html.unescape(n) for n in re.findall(r'<sheet\b[^>]*\bname="([^"]+)"', zipfile.ZipFile(io.BytesIO(data)).read('xl/workbook.xml').decode('utf-8', 'replace'))]
    dated = []
    for nm in names:     # v87.1: arkusze według odczytanego miesiąca, od najnowszego (nie według kolejności w pliku)
        mm = re.search(r'\(\s*([A-Za-z]+)\s*(\d{4})\s*\)', nm)
        mo = MONTHS_EN.get(mm.group(1).capitalize()) if mm else None
        if mo:
            dated.append((f'{mm.group(2)}-{mo:02d}', nm))
    if not dated:
        raise RuntimeError('brak arkuszy z miesiącem')
    out = []
    for ym, nm in sorted(dated, reverse=True)[:2]:
        cs = []
        for _, r in sorted(_xlsx_rows(data, nm).items()):
            lab = re.sub(r'\s+', ' ', str(r.get(1, ''))).strip()
            v, sh = _num(r.get(2)), _num(r.get(3))
            if '/' not in lab or v is None or lab.lower().startswith(('suma', 'rachunki zbiorcze', 'banki centralne', 'razem', 'kraje')):
                continue
            pl, en = [x.strip() for x in lab.split('/', 1)]
            en = re.sub(r'\s*\(the\)', '', en).replace('(the ', '(')      # v87.1: „Netherlands (the)” → „Netherlands”
            cs.append([pl, en, v, None if sh is None else round(sh * 100, 2)])
        if not cs:
            raise RuntimeError(f'brak krajów w arkuszu „{nm}”')
        tot = sum(c[3] for c in cs if c[3] is not None)
        if not 99 <= tot <= 101:
            raise RuntimeError(f'udziały krajów w arkuszu „{nm}” sumują się do {tot:.1f}%, nie do 100%')
        out.append({'m': ym, 'c': cs})
    return out


def build_spw():
    """data/spw.json — m: [[miesiąc, razem, obligacje, bony]] mln zł (wartość nominalna, koniec miesiąca), 25 miesięcy;
    t / r: {typ|region: [[miesiąc, mln zł]]} 13 miesięcy; kr: kraje (2 najnowsze miesiące, opcjonalnie)."""
    L = spw_links(get_bytes(SPW_PAGE, timeout=60).decode('utf-8', 'replace'))
    if 'st' not in L:
        raise RuntimeError('brak odnośnika do pliku struktury nierezydentów')
    data = get_bytes(L['st'], timeout=90)
    T = spw_sheet(_xlsx_rows(data, 'Razem_podmiot'), SPW_TYPES)
    R = spw_sheet(_xlsx_rows(data, 'Razem_region'), SPW_REGS)
    B = spw_sheet(_xlsx_rows(data, 'Obligacje skarbowe_podmiot'), (('tot', 'razem'),))
    S = spw_sheet(_xlsx_rows(data, 'Bony skarbowe_podmiot'), (('tot', 'razem'),))
    ms = sorted(m for m in T if T[m].get('tot') is not None)[-SPW_KEEP:]
    bad = [m for m in ms if not 1e4 < T[m]['tot'] < 1e7]     # razem poza 10 mld – 10 bln zł = zła skala
    if bad:
        raise RuntimeError(f'skala niezgodna ({bad[-1]})')
    lost = [k for G, keys in ((T, SPW_TYPES), (R, SPW_REGS)) for k, _ in keys if k not in G.get(ms[-1], {})]
    if lost:
        META['errors'].append('MF SPW: brak kolumn: ' + ', '.join(lost))     # v87.1: nowa albo zmieniona kolumna = błąd, nie cisza
    gap = [m for m in ms[-SPW_GRP:] if any(abs(sum(v for k, v in G.get(m, {}).items() if k != 'tot' and v is not None) - T[m]['tot']) > 1 for G in (T, R))
           or abs(((B.get(m) or {}).get('tot') or 0) + ((S.get(m) or {}).get('tot') or 0) - T[m]['tot']) > 1]
    if gap:
        META['errors'].append('MF SPW: sumy niezgodne w miesiącach: ' + ', '.join(gap))
    grp = ms[-SPW_GRP:]
    out = {'at': NOW, 'src': 'Ministerstwo Finansów — Struktura podmiotowa zadłużenia wobec nierezydentów w krajowych SPW (miesięcznie)',
           'url': SPW_PAGE, 'unit': 'mln zł, wartość nominalna, stan na koniec miesiąca', 'asof': ms[-1],
           'cols': ['miesiąc', 'razem', 'obligacje skarbowe', 'bony skarbowe'],
           'm': [[m, T[m]['tot'], (B.get(m) or {}).get('tot'), (S.get(m) or {}).get('tot')] for m in ms],
           't': {k: [[m, (T.get(m) or {}).get(k)] for m in grp] for k, _ in SPW_TYPES if k != 'tot'},
           'r': {k: [[m, (R.get(m) or {}).get(k)] for m in grp] for k, _ in SPW_REGS if k != 'tot'}}
    try:
        if 'kr' not in L:
            raise RuntimeError('brak odnośnika do pliku „po krajach”')
        out['kr'] = spw_countries(get_bytes(L['kr'], timeout=90))
        if out['kr'][0]['m'] != ms[-1]:
            META['notes'].append(f"MF SPW: kraje za {out['kr'][0]['m']}, stan za {ms[-1]} (tabela krajów ma własny miesiąc)")
    except Exception as e:
        META['errors'].append(mask(f'MF SPW kraje: {e}'))
    y, mo = int(ms[-1][:4]), int(ms[-1][5:7])
    end = datetime.date(y + (mo == 12), mo % 12 + 1, 1) - datetime.timedelta(days=1)
    if (_now_utc().date() - end).days > SPW_STALE:
        META['errors'].append(f'MF SPW: brak nowego miesiąca po {ms[-1]}')
    return out


# v88: Meksyk — Banco de México (SIE, tabela CA138): stan meksykańskich papierów rządowych u nierezydentów, dziennie, mln MXN
# w wartości nominalnej; eksport CSV bez tokenu (formularz strony); ≈ USD kursem Fed H.10 (FRED DEXMXUS).
import urllib.parse
BMX_URL = 'https://www.banxico.org.mx/SieInternet/consultarDirectorioInternetAction.do?accion=consultarSeries'
BMX_PAGE = 'https://www.banxico.org.mx/SieInternet/consultarDirectorioInternetAction.do?accion=consultarCuadroAnalitico&idCuadro=CA138&sector=7&locale=es'
BMX_SER = (('ext', 'SF65218'), ('tot', 'SF65219'),    # Residentes en el Extranjero (II); Total en Circulación (I + II)
           ('bon', 'SF65137'), ('cet', 'SF65046'), ('udi', 'SF65107'), ('udv', 'SP68257'))   # v88.1: Bonos M, Cetes, Udibonos (mln UDI), wartość UDI
MX_KEEP = 270      # ok. 13 miesięcy sesji
MX_STALE = 21      # dni bez nowego dnia (zwykłe opóźnienie ok. 1,5 tygodnia) = błąd


def post_bytes(url, form, timeout=90):
    data = urllib.parse.urlencode(form, doseq=True).encode()
    req = urllib.request.Request(url, data=data, headers={'User-Agent': 'CapitalFlowAI-collector/1.0', 'Content-Type': 'application/x-www-form-urlencoded'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_bmx(text):
    """CSV z SIE → [[data, nierezydenci, razem w obiegu, Bonos M, Cetes, Udibonos w pesos]] mln MXN, rosnąco; kolumny po kodach serii
    z wiersza „Fecha”; puste albo „N/E” = brak (nie zero); dzień bez nierezydentów pominięty (np. dni z samą wartością UDI);
    v88.1: Udibonos (mln UDI) × wartość UDI z tego dnia = mln MXN."""
    rows = list(csv.reader(io.StringIO(text)))
    head = next((r for r in rows if r and r[0].strip() == 'Fecha'), None)
    if not head:
        raise RuntimeError('brak wiersza z kodami serii')
    col = {k: head.index(c) for k, c in BMX_SER if c in head}
    if 'ext' not in col:
        raise RuntimeError('brak serii SF65218 (nierezydenci)')
    out = {}
    for r in rows:
        m = re.match(r'^(\d{2})/(\d{2})/(\d{4})$', r[0].strip()) if r else None
        if not m:
            continue
        v = {k: (_num(r[i]) if i < len(r) and r[i].strip() not in ('', 'N/E') else None) for k, i in col.items()}
        if v.get('ext') is None:
            continue
        udi = round(v['udi'] * v['udv'], 2) if v.get('udi') is not None and v.get('udv') is not None else None
        out[f'{m.group(3)}-{m.group(2)}-{m.group(1)}'] = [v.get('ext'), v.get('tot'), v.get('bon'), v.get('cet'), udi]
    if not out:
        raise RuntimeError('brak dni')
    return [[k] + out[k] for k in sorted(out)]


def build_meksyk(key):
    """data/meksyk.json — d: [[data, nierezydenci, razem w obiegu, Bonos M, Cetes, Udibonos]] mln MXN nominalnie (270 sesji);
    fx: [kurs MXN/USD, dzień kursu] (Fed, z dnia danych albo najbliższego wcześniejszego)."""
    now = _now_utc().date()
    form = {'locale': 'es', 'idCuadro': 'CA138', 'sector': '7', 'version': '3', 'series': [c for _, c in BMX_SER],
            'anoInicial': str(now.year - 2), 'anoFinal': str(now.year),    # v88.1: w styczniu też jest koniec poprzedniego roku 'tipoInformacion': '4,1', 'formatoHorizontal': 'false',
            'metadatosWeb': 'true', 'formatoCSV.x': '10', 'formatoCSV.y': '10'}
    d = parse_bmx(post_bytes(BMX_URL, form).decode('latin-1'))[-MX_KEEP:]
    bad = [r[0] for r in d if not 1e5 < r[1] < 1e8 or (r[2] is not None and not r[1] < r[2] < 1e9)]   # 0,1–100 bln MXN; mniej niż całość
    if bad:
        raise RuntimeError(f'skala niezgodna ({bad[-1]})')
    over = [r[0] for r in d[-30:] if None not in r[3:6] and r[3] + r[4] + r[5] > r[1] + 1]
    if over:
        META['notes'].append('Banxico: Bonos M + Cetes + Udibonos większe niż całość u nierezydentów w dniach: ' + ', '.join(over))
    out = {'at': NOW, 'src': 'Banco de México — SIE, Valores gubernamentales: tenencia de Residentes en el Extranjero (SF65218), Total en circulación (SF65219)',
           'url': BMX_PAGE, 'unit': 'mln MXN, wartość nominalna (rejestry INDEVAL, podlegają poprawkom)', 'cols': ['data', 'nierezydenci', 'razem w obiegu', 'Bonos M', 'Cetes', 'Udibonos (w pesos)'],
           'asof': d[-1][0], 'd': d}
    if key:
        try:
            rates = fred_rates(key, 'DEXMXUS')
            rd, rt = _rate_for(rates, d[-1][0])        # v88.1: kurs z dnia danych albo najbliższego wcześniejszego (jak w przypisie przeglądu)
            if rt:
                out['fx'] = [rt, rd]
        except Exception as e:
            META['errors'].append(mask(f'Banxico kurs FRED DEXMXUS: {e}'))
    if (now - datetime.date.fromisoformat(d[-1][0])).days > MX_STALE:
        META['errors'].append(f'Banxico: brak nowego dnia po {d[-1][0]}')
    return out


# v82: Korea Płd. — FSS, miesięczny komunikat „Foreign Investors' Stock and Bond Investment” (po angielsku), bez klucza
FSS_LIST = 'https://www.fss.or.kr/eng/bbs/B0000211/list.do?menuNo=400010&pageIndex={p}'
FSS_BASE = 'https://www.fss.or.kr'
FSS_PAGES = 12
FSS_TITLE = re.compile(r'<a href="(/eng/bbs/B0000211/view\.do\?[^"]+)"[^>]*>\s*Foreign Investors(?:&#39;|\'|’) Stock and Bond Investment, ([A-Z][a-z]+) (\d{4})\s*</a>')
FSS_SENT = re.compile(r'Foreign investors (bought|sold) a net KRW\s?([\d.,]+) (billion|trillion) of listed stocks? and (?:(bought|sold) )?a net KRW\s?([\d.,]+) '
                      r'(billion|trillion) of listed bonds in ([A-Z][a-z]+) (\d{4})')
MONTHS_EN = {m: i for i, m in enumerate(['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October',
                                         'November', 'December'], 1)}


def parse_fss(text):
    """Komunikat FSS → [YYYY-MM, akcje, obligacje] w mld KRW (plus = zakupy netto zagranicy); inne zdanie = None (nigdy zgadywanie)."""
    t = re.sub(r'\s+', ' ', _html.unescape(re.sub(r'<[^>]+>', ' ', str(text))))
    m = FSS_SENT.search(t)
    mo = MONTHS_EN.get(m.group(7)) if m else None
    if not mo:
        return None

    def val(side, num, unit):
        v = _num(num)
        if v is None:
            return None
        v *= 1000 if unit == 'trillion' else 1
        return round(v if side == 'bought' else -v, 1)
    return [f'{m.group(8)}-{mo:02d}', val(m.group(1), m.group(2), m.group(3)), val(m.group(4) or m.group(1), m.group(5), m.group(6))]   # bez drugiego czasownika = ten sam kierunek


def fss_months(a, b):
    """Lista miesięcy 'YYYY-MM' od a do b włącznie."""
    out, (y, m) = [], (int(a[:4]), int(a[5:7]))
    while f'{y:04d}-{m:02d}' <= b:
        out.append(f'{y:04d}-{m:02d}'); m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def build_korea(key, prev=None):
    """data/korea.json — [[YYYY-MM, akcje mld KRW, obligacje mld KRW, akcje ≈ mln USD, obligacje ≈ mln USD, kurs]]; 25 miesięcy.
    v83: komunikaty ze strony 1 czytane zawsze (poprawki); brakujące miesiące szukane na kolejnych stronach i zgłaszane przy
    każdym przebiegu; brak nowego komunikatu zbyt długo = błąd; wartość ponad 100 bln KRW = nieczytelna."""
    have = {r[0]: list(r) for r in ((prev or {}).get('m') or []) if isinstance(r, list) and r and isinstance(r[0], str)}
    had, newest, fails, missing = bool(have), None, [], []
    for p in range(1, FSS_PAGES + 1):
        page = get_bytes(FSS_LIST.format(p=p), timeout=60).decode('utf-8', 'replace')
        for href, mon, yr in FSS_TITLE.findall(page):
            mo = MONTHS_EN.get(mon)
            if not mo:
                continue
            ym = f'{yr}-{mo:02d}'
            newest = max(newest or ym, ym)
            if p > 1 and ym in have and have[ym][1] is not None and have[ym][2] is not None:
                continue
            try:
                r = parse_fss(get_bytes(FSS_BASE + _html.unescape(href), timeout=60).decode('utf-8', 'replace'))
                if not r or r[0] != ym or r[1] is None or r[2] is None or max(abs(r[1]), abs(r[2])) > 1e5:
                    raise RuntimeError('nieznany układ komunikatu')
                old = have.get(ym) or []
                have[ym] = r + (old[3:6] if len(old) >= 6 and old[1:3] == r[1:3] else [None, None, None])
            except Exception as e:
                fails.append(f'{ym}: {e}')
        if newest:
            lo = max(min(have) if had and have else _q_ym_add(newest, -12), _q_ym_add(newest, -24))
            missing = [m for m in fss_months(lo, newest) if m not in have]
            if not missing:
                break
    if not have:
        raise RuntimeError('brak komunikatów' + (f' ({fails[0]})' if fails else ''))
    if fails:
        META['errors'].append(mask(f'FSS: {len(fails)} komunikaty nieczytelne, np. {fails[0]}'))
    if missing:
        META['errors'].append(f'FSS: brak komunikatów za miesiące: {", ".join(missing)}')
    last = max(have)
    due = datetime.date(int(_q_ym_add(last, 2)[:4]), int(_q_ym_add(last, 2)[5:7]), 1) + datetime.timedelta(days=34)
    if _now_utc().date() > due:
        META['errors'].append(f'FSS: brak nowego komunikatu po {last} (ostatni termin {due.isoformat()})')
    rates = {}
    if key:
        try:
            rates = fred_rates(key, 'EXKOUS')   # Fed H.10, średnia miesięczna KRW za 1 USD
        except Exception as e:
            META['errors'].append(mask(f'FSS kurs FRED EXKOUS: {e}'))
    rows = [have[k] for k in sorted(have)][-25:]
    for r in rows:
        rt = rates.get(r[0] + '-01')
        if rt:
            r[3:6] = [round(r[1] * 1000 / rt, 1) if r[1] is not None else None, round(r[2] * 1000 / rt, 1) if r[2] is not None else None, rt]
        elif len(r) < 6 or r[5] is None:
            r[3:6] = [None, None, None]
    return {'at': NOW, 'src': 'Financial Supervisory Service (Korea) — monthly press release „Foreign Investors’ Stock and Bond Investment”',
            'url': FSS_LIST.format(p=1),
            'unit': 'mld KRW; akcje = zakupy netto (KOSPI, KOSDAQ, rozliczenie), obligacje = inwestycje netto (zakupy netto minus wykupy); '
                    '≈ mln USD po średnim kursie miesiąca (Fed H.10, FRED EXKOUS)',
            'cols': ['miesiąc', 'akcje mld KRW', 'obligacje mld KRW', 'akcje ≈ mln USD', 'obligacje ≈ mln USD', 'KRW za 1 USD'], 'asof': rows[-1][0], 'm': rows}


def _q_ym_add(ym, k):
    t = int(ym[:4]) * 12 + int(ym[5:7]) - 1 + k
    return f'{t // 12:04d}-{t % 12 + 1:02d}'


def build_krypto(cg_key):
    """data/krypto.json — każda część osobno (awaria jednej nie kasuje pozostałych); CoinGecko z kluczem w nagłówku."""
    out = {'at': NOW, 'src': 'krypto', 'attribution': 'Data by CoinGecko'}
    hdr = {'x-cg-demo-api-key': cg_key} if cg_key else None
    jobs = [('deriv', lambda: parse_deriv(get_json(CG + '/derivatives/exchanges?per_page=20', hdr))),
            ('defi', lambda: parse_defi(get_json(CG + '/global/decentralized_finance_defi', hdr))),
            ('fng', lambda: parse_fng(get_json(FNG_URL))),
            ('mk', lambda: parse_mk([get_json(CG + f'/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page={p}'
                                             '&price_change_percentage=24h,7d,30d,1y', hdr) for p in (1, 2)])),
            ('stabh', lambda: parse_stabh(get_json('https://stablecoins.llama.fi/stablecoincharts/all'))),
            ('stabc', lambda: parse_stabc(get_json('https://stablecoins.llama.fi/stablecoins?includePrices=true')))]   # v58: per sieć
    for name, job in jobs:
        try:
            out[name] = job(); META['ok']['krypto.' + name] = True
        except Exception as e:
            META['errors'].append(mask(f'krypto {name}: {e}')); META['ok']['krypto.' + name] = False
    if not any(k in out for k, _ in jobs):
        raise RuntimeError('żadne źródło rynku krypto nie odpowiedziało')
    return out


ETF_KEEP_DAYS = 300  # v55: tyle dni trzyma etf.json (SoSoValue oddaje tylko ok. 21 ostatnich — reszta z poprzedniego pliku); v89: 300 (TRENDY)


def etf_merge_days(prev_day, new_day):
    """v55: [[ts, mln USD], ...] — nowe okno SoSoValue + starsze dni z poprzedniego pliku, TYLKO gdy okna się nakładają
    (bez cichej luki w sumie 22 sesji); dla tego samego dnia wygrywa nowa wartość; ostatnie ETF_KEEP_DAYS dni."""
    new = [r for r in (new_day or []) if isinstance(r, list) and len(r) == 2 and isinstance(r[0], int)]
    old = [r for r in (prev_day or []) if isinstance(r, list) and len(r) == 2 and isinstance(r[0], int) and not isinstance(r[0], bool)
           and isinstance(r[1], (int, float)) and not isinstance(r[1], bool)]
    if not new or not old or max(r[0] for r in old) < min(r[0] for r in new):
        return new
    m = {r[0]: r[1] for r in old}; m.update({r[0]: r[1] for r in new})
    return [[k, m[k]] for k in sorted(m)][-ETF_KEEP_DAYS:]


def build_etf(key, cg_key, prev=None):
    out = {'at': NOW, 'asof': '', 'src': 'SoSoValue', 'live': True, 'mcap': {}, 'assets': {}}
    prev_assets = prev.get('assets') if isinstance(prev, dict) and isinstance(prev.get('assets'), dict) else {}
    # kapitalizacje (CoinGecko) — do udziału ETF w rynku
    try:
        u = ('https://api.coingecko.com/api/v3/simple/price?ids=' + ','.join(CG_IDS.values())
             + '&vs_currencies=usd&include_market_cap=true')
        j = get_json(u, {'x-cg-demo-api-key': cg_key} if cg_key else None)   # klucz w nagłówku, nie w adresie
        out['mcap'] = {s: j[CG_IDS[s]]['usd_market_cap'] for s in ETF_SYMS}
        META['ok']['coingecko'] = True
    except Exception as e:
        META['errors'].append(mask(f'CoinGecko: {e}'))
        META['ok']['coingecko'] = False
    for s in ETF_SYMS:
        try:
            pa = prev_assets.get(s) if isinstance(prev_assets.get(s), dict) else {}
            _etf_coin(out, s, key, pa.get('day'))
        except Exception as e:   # v49: brak jednej monety nie kasuje pozostałych
            META['errors'].append(mask(f'SoSoValue {s.upper()}: {e}'))
    if not out['assets']:
        raise RuntimeError('SoSoValue: brak danych dla wszystkich monet')
    _etf_hk(out, key, prev.get('hk') if isinstance(prev, dict) else None)   # v61: Hongkong (próba, nie psuje części USA)
    # fundusze publikują dane w różnych godzinach — jeśli daty różnią się między monetami, pokazujemy zakres, nie najnowszą
    dates = sorted({a['asof'] for a in out['assets'].values()})
    out['asof'] = dates[0] if len(dates) == 1 else f'{dates[0]} – {dates[-1]}'
    return out


def _etf_hk(out, key, prev_assets_hk):
    """v61: ETF-y spot w Hongkongu (SoSoValue country_code=HK), BTC i ETH — dane dzienne jak dla USA (bez listy funduszy).
    Brak/awaria = notatka w meta (nie błąd strony); pole 'fields' mówi, co zwraca API (nazwy pól, bez wartości)."""
    hk = {}
    for s in ('btc', 'eth'):
        if _DEADLINE[0] is not None and time.monotonic() > _DEADLINE[0]:
            META['notes'].append('SoSoValue HK: pominięte — limit czasu przebiegu'); break
        try:
            rows = soso(f'/etfs/summary-history?symbol={s.upper()}&country_code=HK&limit=60', key)
            rows = sorted([r for r in (rows or []) if isinstance(r, dict) and r.get('date') and r.get('total_net_inflow') is not None],
                          key=lambda r: r['date'])
            if not rows:
                META['notes'].append(f'SoSoValue HK {s.upper()}: brak danych'); continue
            last = rows[-1]
            pd = (prev_assets_hk or {}).get(s) if isinstance(prev_assets_hk, dict) else None
            day = etf_merge_days(pd.get('day') if isinstance(pd, dict) else None, [[ts(r['date']), r['total_net_inflow'] / 1e6] for r in rows])
            num = lambda k: last[k] / 1e6 if isinstance(last.get(k), (int, float)) and not isinstance(last.get(k), bool) else None
            hk[s] = {'sym': s.upper(), 'asof': last['date'], 'day': day, 'd1': day[-1][1],
                     'w': sum(v for _, v in day[-5:]) if len(day) >= 5 else None,
                     'm': sum(v for _, v in day[-22:]) if len(day) >= 22 else None, 'm_n': min(len(day), 22),
                     'cum': num('cum_net_inflow'), 'aum': num('total_net_assets'), 'fields': sorted(str(k) for k in last)[:20]}
            META['notes'].append(f'SoSoValue HK {s.upper()}: {len(rows)} dni do {last["date"]}; pola: {", ".join(hk[s]["fields"])}')
        except Exception as e:
            META['notes'].append(mask(f'SoSoValue HK {s.upper()}: {e}'))
    if hk:
        out['hk'] = hk


def _etf_coin(out, s, key, prev_day=None):
        """v49: dane jednej monety (wydzielone z build_etf, żeby błąd jednej nie kasował pozostałych)."""
        rows = soso(f'/etfs/summary-history?symbol={s.upper()}&country_code=US&limit=60', key)
        rows = [r for r in rows if r.get('date') and r.get('total_net_inflow') is not None]
        rows.sort(key=lambda r: r['date'])
        if not rows:
            raise RuntimeError(f'SoSoValue: brak danych dla {s}')
        day = etf_merge_days(prev_day, [[ts(r['date']), r['total_net_inflow'] / 1e6] for r in rows])   # v55: historia dłuższa niż okno API
        last = rows[-1]
        out['asof'] = max(out['asof'], last['date'])
        aum = last['total_net_assets'] / 1e6 if last.get('total_net_assets') else None
        mc = out['mcap'].get(s)
        a = {'sym': s.upper(), 'asof': last['date'], 'day': day, 'd1': day[-1][1],
             'w': sum(v for _, v in day[-5:]), 'm': sum(v for _, v in day[-22:]) if len(day) >= 22 else None, 'm_n': min(len(day), 22),   # v51: 22 sesje albo brak (nie cicha niepełna suma)
            
             'cum': last['cum_net_inflow'] / 1e6, 'aum': aum,
             'share': (aum * 1e6 / mc * 100) if (aum and mc) else None, 'funds': []}
        lst = soso(f'/etfs?symbol={s.upper()}&country_code=US', key)
        for it in (lst or [])[:12]:
            if _DEADLINE[0] is not None and time.monotonic() > _DEADLINE[0]:   # v49: limit czasu przebiegu
                META['errors'].append(f'SoSoValue {s.upper()}: lista funduszy pominięta — limit czasu przebiegu'); break
            if not TICKER.match(str(it.get('ticker', ''))):
                # ticker spoza wzorca nie trafia na stronę — i nie znika po cichu
                META['errors'].append(f'SoSoValue {s.upper()}: odrzucony ticker {str(it.get("ticker"))[:20]!r}')
                continue
            try:
                d = soso(f'/etfs/{it["ticker"]}/market-snapshot', key)
                a['funds'].append({'t': it['ticker'], 'n': it.get('name'), 'cty': 'us',
                                   'aum': d['net_assets'] / 1e6 if d.get('net_assets') is not None else None,
                                   'cum': d['cum_inflow'] / 1e6 if d.get('cum_inflow') is not None else None,
                                   'd1': d['net_inflow'] / 1e6 if d.get('net_inflow') is not None else None,
                                   'fee': d['sponsor_fee'] * 100 if d.get('sponsor_fee') is not None else None,
                                   'prem': d.get('prem_dsc')})
            except Exception as e:
                META['errors'].append(mask(f'SoSoValue {it.get("ticker")}: {e}'))
        a['funds'].sort(key=lambda f: -(f['aum'] or 0))
        if not a['aum'] and a['funds']:
            # suma aktywów tylko wtedy, gdy KAŻDY fundusz ma aktywa — brak nie jest zerem
            a['aum'] = (sum(f['aum'] for f in a['funds']) or None) if all(f['aum'] is not None for f in a['funds']) else None
            if a['aum'] and mc:
                a['share'] = a['aum'] * 1e6 / mc * 100
        out['assets'][s] = a
        print(f'{s.upper()}: dzień {last["date"]} {a["d1"]:+.1f} mln, AUM {a["aum"]}, funduszy {len(a["funds"])}')


# v90: FUNDUSZE ETF notowane w USA — dzienny przepływ = zmiana liczby jednostek × cena jednostki (NAV) z plików samych wydawców:
# State Street (SPDR: pełna historia NAV i liczby jednostek w pliku xlsx) i iShares (BlackRock: zestawienie 525 funduszy z NAV
# i aktywami z datą — aktywa / NAV = liczba jednostek; pełna historia funduszu tylko do uzupełnienia wstecz). Bez klucza.
# Wynik: data/fundusze.json (historia do obliczeń; strona go nie czyta — liczby trafiają do TRENDÓW).
FUND_SSGA_URL = 'https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/navhist-us-en-{t}.xlsx'
FUND_ISH_SCR = ('https://www.ishares.com/us/product-screener/product-screener-v3.1.jsn?dcrPath=/templatedata/config/product-screener-v3/'
                'data/en/us-ishares/ishares-product-screener-backend-config&siteEntryPassthrough=true')
FUND_ISH_DOC = ('https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document?appType=PRODUCT_PAGE'
                '&appSubType=ISHARES&targetSite=us-ishares&locale=en_US&portfolioId={pid}&component=fundDownload&userType=individual')
FUND_SSGA = ('SPY', 'XLK', 'XLF', 'XLE', 'XLV', 'XLI', 'XLY', 'XLP', 'XLU', 'SPDW', 'SPEM', 'BIL', 'JNK', 'GLD', 'GLDM')
FUND_ISH = ('IVV', 'EFA', 'IEFA', 'EZU', 'EWJ', 'EEM', 'IEMG', 'MCHI', 'FXI', 'INDA', 'EWZ', 'EWY', 'EWT',
            'TLT', 'IEF', 'SHY', 'AGG', 'LQD', 'HYG', 'EMB', 'IAU', 'SLV')
FUND_KEEP = 300          # dni historii na fundusz (tło dla TRENDÓW: 9 tygodni porównania + historia „czy tydzień zapowiadał następny”)
FUND_MIN = 250           # fundusz iShares z krótszą historią — uzupełnienie pełnym plikiem (najwyżej FUND_BACKFILL na przebieg)
FUND_BACKFILL = 3
FUND_SSGA_EVERY = 360    # min — pliki State Street (cała historia) najwyżej co 6 h
FUND_SCR_EVERY = 120     # min — zestawienie iShares najwyżej co 2 h
FUND_SLEEP = 1.0
FUND_BUDGET = 240       # v94: s — cały krok funduszy; po nim reszta w kolejnym przebiegu (automat strony ma limit 15 min)
FUND_RETRY = 60         # v94: min — przerwa po błędzie pliku State Street
FUND_BF_RETRY = 360     # v94: min — przerwa po błędzie pełnego pliku iShares


def _fund_num(x):
    try:
        v = float(str(x).replace(',', '').strip())
    except ValueError:
        return None
    return v if _isnum(v) and v > 0 else None


def parse_ssga_navhist(data, ticker):
    """State Street navhist-us-en-{ticker}.xlsx → [[data, NAV, liczba jednostek]] rosnąco. Symbol w pliku musi się zgadzać."""
    rows = _xlsx_rows(data, 'navhist')
    tick, head, out = None, None, []
    for k in sorted(rows):
        r = rows[k]
        c1 = str(r.get(1, '')).strip()
        if c1.lower().startswith('ticker'):
            tick = re.sub(r'[^A-Z]', '', str(r.get(2, '')).upper())
        elif c1 == 'Date' and 'share' in str(r.get(3, '')).lower():
            head = k
        elif head is not None:
            try:
                d = datetime.datetime.strptime(c1, '%d-%b-%Y').date().isoformat()
            except ValueError:
                break                      # koniec tabeli (dalej są zastrzeżenia prawne)
            nav, sh = _fund_num(r.get(2)), _fund_num(r.get(3))
            if nav and sh:
                out.append([d, round(nav, 6), round(sh)])
    if tick != ticker:
        raise RuntimeError(f'{ticker}: w pliku inny symbol ({tick})')
    if head is None or len(out) < 20:
        raise RuntimeError(f'{ticker}: brak tabeli NAV')
    return sorted({r[0]: r for r in out}.values())


def parse_ishares_hist(data):
    """iShares (BlackRock) — plik funduszu (Excel 2003 XML), arkusz „Historical”: As Of, NAV per Share, Shares Outstanding →
    [[data, NAV, liczba jednostek]] rosnąco."""
    import xml.etree.ElementTree as ET
    ns = '{urn:schemas-microsoft-com:office:spreadsheet}'
    txt = data.decode('utf-8-sig', 'replace')
    txt = re.sub(r'&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)', '&amp;', txt)   # gołe „&” w nazwach spółek
    root = ET.fromstring(txt)
    ws = next((w for w in root.iter(ns + 'Worksheet') if w.get(ns + 'Name') == 'Historical'), None)
    if ws is None:
        raise RuntimeError('brak arkusza Historical')
    head, out = None, []
    for row in ws.iter(ns + 'Row'):
        cells = [(c.find(ns + 'Data').text if c.find(ns + 'Data') is not None else '') or '' for c in row.iter(ns + 'Cell')]
        if head is None:
            if 'As Of' in cells and 'Shares Outstanding' in cells:
                head = {n: i for i, n in enumerate(cells)}
            continue
        try:
            d = datetime.datetime.strptime(cells[head['As Of']].strip(), '%b %d, %Y').date().isoformat()
        except (ValueError, IndexError, KeyError):
            continue
        nav = _fund_num(cells[head['NAV per Share']]) if 'NAV per Share' in head and len(cells) > head['NAV per Share'] else None
        sh = _fund_num(cells[head['Shares Outstanding']]) if len(cells) > head['Shares Outstanding'] else None
        if nav and sh:
            out.append([d, round(nav, 6), round(sh)])
    if len(out) < 20:
        raise RuntimeError('za krótka historia')
    return sorted({r[0]: r for r in out}.values())


def parse_ishares_screener(j):
    """Zestawienie iShares → {symbol: (id funduszu, data, NAV, liczba jednostek = aktywa funduszu / NAV)}; tylko gdy obie daty są te same."""
    out = {}
    for pid, f in (j.items() if isinstance(j, dict) else []):
        if not isinstance(f, dict):
            continue
        t = str(f.get('localExchangeTicker') or '').upper()
        if t not in FUND_ISH:
            continue
        g = lambda k: (f.get(k) or {}).get('r') if isinstance(f.get(k), dict) else None
        nav, tna, d1, d2 = g('navAmount'), g('totalNetAssetsFund'), g('navAmountAsOf'), g('totalNetAssetsFundAsOf')
        if _isnum(nav) and nav > 0 and _isnum(tna) and tna > 0 and d1 and d1 == d2 and re.match(r'^\d{8}$', str(d1)):
            s = str(d1)
            sh = tna / nav; r3 = round(sh, -3)                  # v94: aktywa / NAV różni się od prawdziwej liczby o kilka jednostek (NAV do 6 miejsc)
            out[t] = (str(f.get('portfolioId') or pid), f'{s[:4]}-{s[4:6]}-{s[6:]}', round(nav, 6), int(r3) if abs(sh - r3) <= 10 else round(sh))
    return out


def _fund_merge(old, new):
    """Historia funduszu: nowe wiersze wygrywają dla tych samych dni; ostatnie FUND_KEEP dni rosnąco."""
    m = {r[0]: r for r in old or [] if isinstance(r, list) and len(r) == 3}
    m.update({r[0]: r for r in new or []})
    return [m[k] for k in sorted(m)][-FUND_KEEP:]


def build_fundusze(prev=None):
    """v90/v94: historia NAV i liczby jednostek 37 funduszy. v94: limit czasu całego kroku, przerwa po błędzie źródła, dzień z zestawienia
    iShares dopisywany tylko bez luki w sesjach (inaczej fundusz czeka na uzupełnienie pełnym plikiem — brak zostaje brakiem)."""
    prev = prev if isinstance(prev, dict) else {}
    pf = prev.get('f') if isinstance(prev.get('f'), dict) else {}
    out = {'at': NOW, 'src': 'State Street Global Advisors (SPDR) — NAV history; iShares by BlackRock — product screener and fund data download',
           'unit': 'NAV w USD; liczba jednostek; przepływ = zmiana liczby jednostek × NAV (mln USD, liczony w TRENDACH)',
           'scr_at': prev.get('scr_at'), 'f': {}}
    errs = []
    now = _now_utc(); t0 = time.monotonic()
    due = lambda at, minutes: not at or (now - datetime.datetime.fromisoformat(at)).total_seconds() >= minutes * 60
    left = lambda: time.monotonic() - t0 < FUND_BUDGET
    for t in FUND_SSGA:
        p = pf.get(t) if isinstance(pf.get(t), dict) else {}
        if p.get('h') and not due(p.get('at'), FUND_SSGA_EVERY) or not due(p.get('err_at'), FUND_RETRY) or not left():
            if p.get('h') or p.get('err_at'):
                out['f'][t] = p
            continue
        time.sleep(FUND_SLEEP)
        try:
            h = parse_ssga_navhist(get_bytes(FUND_SSGA_URL.format(t=t.lower()), timeout=30), t)
            out['f'][t] = {'iss': 'ssga', 'at': NOW, 'h': _fund_merge(p.get('h'), h)}
        except Exception as e:
            errs.append(f'{t}: {e}')
            out['f'][t] = dict(p, iss='ssga', err_at=NOW)
    spy = [r[0] for r in ((out['f'].get('SPY') or {}).get('h') or [])]            # sesje giełdy w Nowym Jorku według SPY
    scr = {}
    if due(prev.get('scr_at'), FUND_SCR_EVERY) and left():
        try:
            scr = parse_ishares_screener(json.loads(get_bytes(FUND_ISH_SCR, timeout=45).decode('utf-8-sig', 'replace')))
            out['scr_at'] = NOW
            if len(scr) < len(FUND_ISH) // 2:
                errs.append(f'zestawienie iShares: tylko {len(scr)} z {len(FUND_ISH)} funduszy')
        except Exception as e:
            errs.append(f'zestawienie iShares: {e}')

    def gap(h, d):
        """Czy między ostatnim dniem historii a dniem d brakuje sesji (według SPY, a bez niego — według dni roboczych)."""
        if not h:
            return False
        L = h[-1][0]
        if spy and spy[-1] >= d:
            return any(L < c < d for c in spy)
        return _bdays(_d(L), _d(d)) > 1

    backfills = 0
    for t in FUND_ISH:
        p = dict(pf.get(t)) if isinstance(pf.get(t), dict) else {'iss': 'ishares'}
        h = p.get('h') or []
        pid = (scr.get(t) or (p.get('pid'),))[0]
        if pid:
            p['pid'] = pid
        if t in scr and gap(h, scr[t][1]):
            p['bf_need'] = True                                          # luka w sesjach — do uzupełnienia pełnym plikiem
        need = (len(h) < FUND_MIN and not p.get('bf_done')) or p.get('bf_need')
        if need and pid and backfills < FUND_BACKFILL and due(p.get('bf_err_at'), FUND_BF_RETRY) and left():
            backfills += 1
            time.sleep(FUND_SLEEP)
            try:
                full = parse_ishares_hist(get_bytes(FUND_ISH_DOC.format(pid=pid), timeout=90))
                h = _fund_merge(h, full)
                p['at'] = NOW; p.pop('bf_need', None); p.pop('bf_err_at', None)
                if len(full) < FUND_MIN:
                    p['bf_done'] = True                                  # młody fundusz — cała historia już jest
            except Exception as e:
                errs.append(f'{t} (historia): {e}'); p['bf_err_at'] = NOW
        if t in scr:
            _, d, nav, sh = scr[t]
            last = h[-1] if h else None
            if last and last[0] == d and abs(last[2] - sh) > max(1, 1e-4 * sh):
                META['notes'].append(f'fundusze {t}: liczba jednostek z zestawienia ({sh}) ≠ z historii ({last[2]}) dla {d} — zostaje historia')
            elif (not last or d > last[0]) and not gap(h, d):
                h = _fund_merge(h, [[d, nav, sh]])
                p.pop('bf_need', None)
        p['iss'] = 'ishares'
        if h:
            p['h'] = h
            out['f'][t] = p
    if not any(f.get('h') for f in out['f'].values()):
        raise RuntimeError('żaden fundusz nie odpowiedział' + (f' ({errs[0]})' if errs else ''))
    if not left():
        errs.append(f'limit czasu kroku funduszy ({FUND_BUDGET} s) — reszta w kolejnym przebiegu')
    if errs:
        (META['errors'] if len(errs) > 5 else META['notes']).append(mask(f'fundusze ETF: {len(errs)} problemów, np. {errs[0]}'))
    return out


# v89: TRENDY — dokąd płynął kapitał w ostatnim tygodniu. Każde źródło osobno, w swoich jednostkach, według jednej jawnej reguły:
# suma ostatniego tygodnia (5 sesji giełdowych, 7 dni kalendarzowych albo 1 tydzień raportu) porównana ze średnią 4 poprzednich
# tygodni, w jednostkach rozrzutu 4–8 poprzednich tygodni. Opis tego, co się stało — nie prognoza i nie rekomendacja.
# Liczone z plików zapisanych w tym przebiegu (bez zapytań do sieci). Brak w oknie = brak wyniku, nigdy zero. Kwoty w USD: mln USD.
TR_BASE_MIN = 4       # najmniej tylu poprzednich tygodni do porównania (mniej = „za krótka historia”)
TR_BASE_MAX = 8       # najwyżej tylu; mniej niż 8 = pokazujemy tylko kierunek, bez oceny siły
TR_DIR = 0.5          # tydzień ma wyraźny kierunek, gdy |suma| ≥ połowy typowego tygodnia …
TR_ALL = 1.0          # … a gdy suma jest mniejsza niż typowy tydzień, także większość dni ma ten sam znak (dane dzienne)
TR_FLOOR = 0.25       # rozrzut nie mniejszy niż 1/4 typowego tygodnia — małe liczby nie dają ogromnych wyników
TR_STRONG = 1.0       # |d| ≥ 1 w stronę kierunku: „większy/słabszy niż zwykle”
TR_EXC = 3.0          # |d| ≥ 3 (tylko przy 8 tygodniach historii): „wyjątkowo daleko od zwykłego poziomu”
TR_DAY_N = 40         # dzień nietypowy: najmniej tylu poprzednich sesji …
TR_DAY_Z = 3.0        # … i |ostatni dzień − średnia| ≥ 3 odchylenia (z najwyżej 60 poprzednich sesji)
TR_SPAN = 11          # 5 sesji mieści się w 11 dniach kalendarzowych (święta do 4 dni roboczych); dłużej = brak dnia w tygodniu
TR_PX_MIN = 0.5       # ceny: ruch tygodnia wyraźny, gdy |zmiana| ≥ 0,5 typowego tygodniowego ruchu tego rynku
TR_PX_WEEKS = 50      # typowy tygodniowy ruch: z najwyżej 50 poprzednich tygodni (co najmniej 20)
TR_CR_TYP = 9.0       # krypto (brak historii cen w plikach): umowny typowy tydzień 9% — ruch tygodnia wyraźny od 4,5%, 23 dni przed nim od 9%
TR_CR_SYMS = ('BTC', 'ETH', 'XRP', 'BNB', 'SOL', 'DOGE', 'ADA', 'TRX', 'LINK', 'AVAX')
TR_PX_SYMS = ('SPY', 'EWC', 'ILF', 'VGK', 'KSA', 'TUR', 'EIS', 'EZA', 'INDA', 'MCHI', 'EWJ', 'EWY', 'ASEA', 'EWA')
TR_CFTC = ('usd', 'eur', 'jpy', 'spx', 'msciem', 'btc', 'eth')   # fundusze lewarowane; bez obligacji 10L (transakcja na bazie)
TR_OB = (('in', 1), ('in', 2), ('tw', 1), ('hk', 1), ('br', 1))   # v95: Indie (akcje, dług), Tajwan, Hongkong, Brazylia
TR_FE = (('fe_us', ('SPY', 'IVV')), ('fe_tech', ('XLK',)), ('fe_fin', ('XLF',)), ('fe_energy', ('XLE',)), ('fe_health', ('XLV',)),
         ('fe_indu', ('XLI',)), ('fe_cdisc', ('XLY',)), ('fe_cstap', ('XLP',)), ('fe_util', ('XLU',)),
         ('fe_dev', ('EFA', 'IEFA', 'SPDW')), ('fe_eur', ('EZU',)), ('fe_jpn', ('EWJ',)), ('fe_em', ('EEM', 'IEMG', 'SPEM')),
         ('fe_chn', ('MCHI', 'FXI')), ('fe_india', ('INDA',)), ('fe_bra', ('EWZ',)), ('fe_kor', ('EWY',)), ('fe_twn', ('EWT',)),
         ('fe_ustl', ('TLT',)), ('fe_ustm', ('IEF',)), ('fe_usts', ('SHY', 'BIL')), ('fe_agg', ('AGG',)), ('fe_ig', ('LQD',)),
         ('fe_hy', ('HYG', 'JNK')), ('fe_emb', ('EMB',)), ('fe_gold', ('GLD', 'IAU', 'GLDM')), ('fe_silver', ('SLV',)))   # v90: grupy funduszy ETF
TR_FP = ('fe_tech', 'fe_fin', 'fe_energy', 'fe_health', 'fe_indu', 'fe_cdisc', 'fe_cstap', 'fe_util', 'fe_dev', 'fe_eur', 'fe_em', 'fe_twn',
         'fe_bra', 'fe_gold', 'fe_silver')   # v93: ceny NAV (bez rynków z listy cen krajów); v94: bez obligacji — comiesięczna wypłata odsetek obniża NAV


# v120: TRENDY — sygnały dzienne („TD” = trend dzienny; to nie są stałe Twelve Data TD_BATCH/TD_SLEEP/TD_OUTPUT z początku pliku).
# Jedna reguła i jedne progi dla wszystkich rynków, ustalone przed policzeniem wyników; nic nie jest dostrajane do rynku.
# Zmiana którejkolwiek stałej = TD_V + 1 i nowa data TD_SINCE (licznik „od wdrożenia” zaczyna się od nowa).
TD_V = 1                      # wersja reguły (strona pokazuje ją w opisie metody)
TD_SINCE = '2026-09-28'       # dzień wdrożenia: pary z datą sygnału ≥ tej daty liczymy osobno („od wdrożenia” — poza próbą)
TD_LB = 60                    # dzień porównywany z najwyżej 60 poprzednimi dniami …
TD_MIN = 40                   # … i najmniej 40 (mniej = „za mało historii”)
TD_Z1 = 1.0                   # wejście „strzela”, gdy |z| ≥ 1 rozrzut …
TD_Z2 = 2.0                   # … mocno: |z| ≥ 2 (dodatkowy punkt siły dnia)
TD_FLOOR = 0.25               # rozrzut przepływów nie mniejszy niż 1/4 typowego dnia (małe liczby nie dają ogromnych z)
TD_GAP = 4                    # następna sesja najwyżej 4 dni kalendarzowych po sesji sygnału (pt → pn = 3 dni; dłuższa przerwa = para pominięta)
TD_NEFF = 100                 # przewaga wymaga co najmniej 100 dni z sygnałem (niepewność liczona na dni, nie na pary rynek–dzień)
TD_PUB = {'ishares': 0, 'ssga': 1}   # opóźnienie publikacji w sesjach: sygnał z dnia t jest znany dopiero w sesji t + pub
TD_PUB_SYM = {'GLD': 0, 'GLDM': 0}   # wyjątek według pliku (dostępność danych, nie próg): trusty złota SPDR publikują NAV sesję wcześniej niż
                                     # pozostałe pliki tego wydawcy (26.09, jedno pobranie: GLD/GLDM do 25.09, reszta do 24.09) — przed TD_PUB
TD_RULES = (('eq', 'f'), ('eq', 'p'), ('eq', 'fp'), ('bd', 'f'), ('pm', 'f'), ('pm', 'p'), ('pm', 'fp'))   # 7 linii testu, zawsze publikowane
TD_FAM = {'bd': ('BIL', 'JNK', 'TLT', 'IEF', 'SHY', 'AGG', 'LQD', 'HYG', 'EMB'), 'pm': ('GLD', 'GLDM', 'IAU', 'SLV')}   # reszta funduszy = eq (akcje)
TD_OB = (('in_eq', 'in', 1, None, 'INDA', 'USD'), ('tw', 'tw', 1, 5, 'EWT', 'TWD'), ('hk', 'hk', 1, 5, 'FXI', 'HKD'))
#        (id wiersza, część pliku obce, kolumna przepływu, kolumna ≈ mln USD [None = przepływ już w USD], fundusz z ceną, waluta)
TD_PX = ('EWC', 'ILF', 'VGK', 'KSA', 'TUR', 'EIS', 'EZA', 'ASEA', 'EWA')   # rynki tylko z ceną (zamknięcia z pliku cen, bez przepływów)
TD_GRP = {sym: gid for gid, ms in TR_FE for sym in ms}                      # fundusz → grupa (nazwa i ikona na stronie); lista funduszy = TR_FE
TD_EXCLUDED = ('br', 'mx', 'th', 'in_bd', 'jp', 'tr', 'cf', 'cs', 'cr', 'ix')   # poza sygnałami dziennymi — powody w opisie metody na stronie
# v123: rodzina krypto „cr” — 10 par z TR_CR_SYMS, doba UTC, na razie tylko ruch ceny (reguła „p”). Własna wersja i własny licznik
# „od wdrożenia” (TD_VC, TD_SINCE_CR) — 7 linii świata (TD_V, TD_SINCE) bez zmian. TD_EXCLUDED nadal zawiera 'cr': opisuje zestaw
# reguł wersji TD_V, do którego krypto nie należy. Przepływy (ETF, giełdy, stablecoiny) = faza 2 → TD_VC + 1 i nowa data TD_SINCE_CR.
TD_VC = 1                     # wersja reguły krypto (strona: tytuł opisu metody w widoku krypto)
TD_SINCE_CR = '2026-09-28'    # pierwsza doba UTC po wdrożeniu (wdrożenie później → następna doba po wdrożeniu)
TD_RULES_CR = (('cr', 'p'),)  # 1 linia testu krypto, zawsze publikowana


def _isnum(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) != float('inf')


def _d(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _tr_sums(vals, size, k, dates=None, span=None):
    """Sumy k kolejnych pełnych bloków po `size` wartości, od końca: [ostatni, poprzedni, …]. Blok z brakiem (None) albo — gdy podano
    daty i `span` — rozciągnięty na więcej niż `span` dni kalendarzowych (brakujący dzień w pliku) kończy listę."""
    out = []
    for i in range(k):
        a, b = len(vals) - (i + 1) * size, len(vals) - i * size
        blk = vals[a:b] if a >= 0 else []
        if len(blk) < size or not all(_isnum(x) for x in blk):
            break
        if dates is not None and span:
            d0, d1 = _d(dates[a]), _d(dates[b - 1])
            if not d0 or not d1 or (d1 - d0).days > span:
                break
        out.append(sum(blk))
    return out


def _mean(xs):
    return sum(xs) / len(xs)


def _sd(xs):
    m = _mean(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5 if len(xs) > 1 else 0.0


def _tr_clear(w, typ, days):
    """Wyraźny kierunek tygodnia: |suma| ≥ 0,5 typowego tygodnia i (gdy suma < 1 typowego tygodnia) większość dni ze zmianą ma znak sumy
    (v94: dni bez zmiany się nie liczą — fundusz, który tworzy jednostki raz w tygodniu, nie ma „dni w różne strony”)."""
    if not w or not typ or abs(w) < TR_DIR * typ:
        return False
    nz = [x for x in days or [] if x != 0]
    return days is None or abs(w) >= TR_ALL * typ or sum(1 for x in nz if x * w > 0) >= len(nz) // 2 + 1


def trend_state(vals, size, dates=None, span=None):
    """Stan trendu jednej serii (wartości w kolejności dat, None = brak). size = 5 (sesje), 7 (dni) albo 1 (tygodnie raportu).
    → {st, w, base, d, n, x, lc}. st: in_up / in_flat / in_down (napływ jak zwykle, ale większy / taki sam / słabszy),
    in_rev (napływ po tygodniach odpływu), in_new (napływ po okresie bez wyraźnego kierunku), in_dir (napływ, historia za krótka
    do oceny siły), in_stop (zwykle napływ, w tym tygodniu prawie nic); to samo dla out_*; mixed (duża suma, dni w różne strony);
    none; short; gap."""
    sums = _tr_sums(vals, size, 1 + TR_BASE_MAX, dates, span)
    if not sums:
        return {'st': 'gap'}
    w, prev = sums[0], sums[1:]
    if len(prev) < TR_BASE_MIN:
        return {'st': 'short', 'w': w, 'n': len(prev)}
    n = len(prev); lc = n < TR_BASE_MAX
    base = _mean(prev[:TR_BASE_MIN])
    typ = _mean([abs(x) for x in prev])
    sd = max(_sd(prev), TR_FLOOR * typ)
    if sd <= 0:
        return {'st': 'none', 'w': w, 'base': base, 'n': n, 'x': False, 'lc': lc}
    d = (w - base) / sd
    clear = _tr_clear(w, typ, None if size == 1 else vals[-size:])
    bpos, bneg = base > 0 and base >= TR_DIR * typ, base < 0 and -base >= TR_DIR * typ
    if clear:
        s = 'in' if w > 0 else 'out'
        same, opp = (bpos, bneg) if w > 0 else (bneg, bpos)
        dd = d if w > 0 else -d                        # wynik w stronę kierunku tygodnia
        if lc:
            st = s + '_dir'
        elif same:
            st = s + ('_up' if dd >= TR_STRONG else '_down' if dd <= -TR_STRONG else '_flat')
        else:
            st = s + ('_rev' if opp else '_new')
    elif w and abs(w) >= TR_DIR * typ:
        st = 'mixed'                                   # duża suma z kilku dni, pozostałe dni w drugą stronę
    elif not lc and bpos and d <= -TR_STRONG:
        st = 'in_stop'                                 # v90: zwykle napływ, w tym tygodniu prawie nic (osobny stan — nie „słabszy napływ”)
    elif not lc and bneg and d >= TR_STRONG:
        st = 'out_stop'
    else:
        st = 'none'
    return {'st': st, 'w': w, 'base': base, 'd': d, 'n': n, 'x': not lc and abs(d) >= TR_EXC, 'lc': lc}


def trend_streak(vals):
    """Ile ostatnich wartości z rzędu ma ten sam znak (brak albo zero przerywa); znak: +1 / −1 / 0."""
    n, sg = 0, 0
    for x in reversed(vals):
        if not _isnum(x) or x == 0:
            break
        s = 1 if x > 0 else -1
        if sg and s != sg:
            break
        sg = s; n += 1
    return n, sg


def trend_day_z(vals):
    """Ostatnia wartość w odchyleniach od średniej najwyżej 60 poprzednich (tylko liczby); None, gdy mniej niż TR_DAY_N."""
    if not vals or not _isnum(vals[-1]):
        return None
    prev = [x for x in vals[-61:-1] if _isnum(x)]
    if len(prev) < TR_DAY_N:
        return None
    sd = _sd(prev)
    return (vals[-1] - _mean(prev)) / sd if sd > 0 else None


def wilson(k, n, z=1.96, n_eff=None):
    """95% przedział Wilsona dla odsetka k/n (w %); n_eff — mniejsza liczba niezależnych obserwacji (np. tygodni przy wielu rynkach)."""
    if not n:
        return None, None
    p = k / n; m = n_eff or n; den = 1 + z * z / m
    c = (p + z * z / (2 * m)) / den; h = z * ((p * (1 - p) / m + z * z / (4 * m * m)) ** 0.5) / den
    return max(0.0, round(100 * (c - h), 1)) + 0.0, min(100.0, round(100 * (c + h), 1)) + 0.0


def _iso_weeks(dates, vals, today):
    """Wartości dzienne → pełne tygodnie kalendarzowe (pon–nd) od najstarszego: [(poniedziałek, suma, wartości dni)].
    Tydzień z brakiem (None), tydzień z dniem dzisiejszym (niezakończony) i niepełny pierwszy tydzień historii (mniej niż 4 dni) są pomijane."""
    wk = {}
    for d, v in zip(dates, vals):
        dd = _d(d)
        if dd is None:
            continue
        wk.setdefault(dd - datetime.timedelta(days=dd.weekday()), []).append(v)
    cur = today - datetime.timedelta(days=today.weekday())
    W = [(m, sum(v), v) for m, v in sorted(wk.items()) if m < cur and all(_isnum(x) for x in v)]
    return W[1:] if W and len(W[0][2]) < 4 else W


def trend_persist(dates, vals, today, weeks=None):
    """Jak często po tygodniu z wyraźnym kierunkiem następny tydzień kalendarzowy miał ten sam kierunek. Wyraźny tydzień — ta sama reguła
    co na kartach, z typowym tygodniem liczonym tylko z 4–8 wcześniejszych tygodni. → (k, n, pierwszy, ostatni poniedziałek par)."""
    W = _iso_weeks(dates, vals, today)
    k = n = 0; first = last = None
    for i in range(len(W) - 1):
        prev = [s for _, s, _ in W[max(0, i - TR_BASE_MAX):i]]
        if len(prev) < TR_BASE_MIN or (W[i + 1][0] - W[i][0]).days != 7:
            continue
        m, s, days = W[i]
        if _tr_clear(s, _mean([abs(x) for x in prev]), days):
            n += 1; k += 1 if s * W[i + 1][1] > 0 else 0
            first = first or m; last = W[i + 1][0]
            if weeks is not None:
                weeks.add(m)
    return k, n, first and first.isoformat(), last and last.isoformat()


def _bdays(a, b, skip=()):
    """Dni robocze (pon–pt) po dniu a do dnia b włącznie, bez dni z `skip` (znane dni bez sesji)."""
    n, d = 0, a
    while d < b:
        d += datetime.timedelta(days=1)
        n += d.weekday() < 5 and d.isoformat() not in skip
    return n


def _tr_row(sid, g, m, size, dates, vals, usd=None, hold=None, lag=1, weekly_days=None, cur='USD', span=None, skip=()):
    """Jeden wiersz TRENDÓW. lag = zwykłe opóźnienie publikacji w dniach roboczych (dane sesyjne); dane z każdego dnia kalendarza
    (size 7) — za stare po 3 dniach; weekly_days = po ilu dniach od końca tygodnia dane tygodniowe są za stare."""
    if not dates or not vals or len(dates) != len(vals):
        return None
    ld = _d(dates[-1])
    if ld is None:
        return None
    t = trend_state(vals, size, dates, span)
    today = _now_utc().date()
    age = (today - ld).days
    stale = age > weekly_days if weekly_days else (age > 3 if size == 7 else _bdays(ld, today, skip) > lag + 2)
    st = 'stale' if stale else t['st']
    r = {'id': sid, 'g': g, 'm': m, 'sz': size, 'cur': cur, 'date': ld.isoformat(), 'age': age, 'st': st}
    for k in ('w', 'base', 'd'):
        if _isnum(t.get(k)):
            r[k] = round(t[k], 2)
    for k in ('n', 'lc'):
        if k in t:
            r[k] = t[k]
    ev = st not in ('stale', 'gap', 'short')
    r['x'] = bool(ev and t.get('x'))                   # bez oceny — bez dopisków oceny
    ratio = 1.0 if cur == 'USD' else None
    if usd is not None and cur != 'USD':
        us = _tr_sums(usd, size, 1, dates, span)
        if us:
            r['wu'] = round(us[0], 1)
            if _isnum(t.get('w')) and t['w']:
                ratio = us[0] / t['w']
    if ratio is not None and _isnum(t.get('w')) and _isnum(t.get('base')):
        r['du'] = round((t['w'] - t['base']) * ratio, 1)   # odchylenie od zwykłego poziomu w mln USD (do kafli)
    if _isnum(hold) and hold and _isnum(t.get('w')):
        r['ph'] = round(100 * t['w'] / hold, 2)
    r['s'], r['sg'] = trend_streak(vals)
    dz = trend_day_z(vals) if size != 1 and ev else None
    if dz is not None:
        r['dz'] = round(dz, 2)
    if _isnum(vals[-1]):
        r['last'] = round(vals[-1], 2)
    return r


def _tr_weeks(ds, vals, n=30):
    """Dane tygodniowe → kolejne tygodnie co 7 dni od ostatniego wstecz, z tolerancją ±3 dni (np. raport CFTC przesunięty na poniedziałek
    po święcie). Brakujący tydzień = None (nie zero, nie sklejenie); etykieta = prawdziwa data raportu albo termin wyliczony."""
    pts = sorted((d, v) for d, v in ((_d(a), b) for a, b in zip(ds, vals)) if d)
    if not pts:
        return [], []
    last = pts[-1][0]; B = {}
    for d, v in pts:
        k = round((last - d).days / 7)
        if abs((last - d).days - 7 * k) <= 3:
            B[k] = (d, v)
    out_d, out_v = [], []
    for k in range(n - 1, -1, -1):
        d, v = B.get(k, (last - datetime.timedelta(days=7 * k), None))
        out_d.append(d.isoformat()); out_v.append(v)
    return out_d, out_v


def _jpy_per_usd(S):
    """JPY za 1 USD ze średnich miesięcznych EBC (kursy.json: jednostek za 1 EUR) → {miesiąc: kurs}; brak pliku = pusty słownik."""
    m = (S.get('kursy') or {}).get('m') if isinstance(S.get('kursy'), dict) else None
    m = m if isinstance(m, dict) else {}
    j = {r[0]: r[1] for r in m.get('JPY') or [] if isinstance(r, list) and len(r) == 2 and _isnum(r[1]) and r[1] > 0}
    u = {r[0]: r[1] for r in m.get('USD') or [] if isinstance(r, list) and len(r) == 2 and _isnum(r[1]) and r[1] > 0}
    return {mo: j[mo] / u[mo] for mo in j if mo in u}


def _tr_cols(part, *idx):
    """Kolumny części pliku: (daty, kolumna idx[0], kolumna idx[1], …); nie-liczba = None."""
    d = [r for r in (part or {}).get('d') or [] if isinstance(r, list) and r and isinstance(r[0], str) and _d(r[0])]
    return ([r[0] for r in d],) + tuple([r[i] if len(r) > i and _isnum(r[i]) else None for r in d] for i in idx)


def _tr_sessions(part, *idx):
    """Tajwan i Hongkong: kolumny na pełnym kalendarzu dni roboczych od pierwszego do ostatniego wiersza, bez znanych dni bez sesji
    (part['empty']); dzień bez wiersza = None (brak, nie zero) — pomijany dzień nie jest cicho sklejany z sąsiednimi."""
    cols = _tr_cols(part, *idx)
    if not cols[0]:
        return cols
    empty = {x for x in (part or {}).get('empty') or [] if isinstance(x, str)}
    by = [dict(zip(cols[0], c)) for c in cols[1:]]
    have = set(cols[0])
    d0, d1 = _d(cols[0][0]), _d(cols[0][-1])
    cal = [(d0 + datetime.timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]
    cal = [c for c in cal if c in have or (_d(c).weekday() < 5 and c not in empty)]
    return (cal,) + tuple([b.get(c) for c in cal] for b in by)


def _cftc_roll(ds):
    """Raport CFTC w tygodniu wygasania kontraktów kwartalnych (±7 dni od 3. środy marca, czerwca, września, grudnia)."""
    d = _d(ds)
    if not d or d.month not in (3, 6, 9, 12):
        return False
    first = datetime.date(d.year, d.month, 1)
    wed3 = first + datetime.timedelta(days=(2 - first.weekday()) % 7 + 14)
    return abs((d - wed3).days) <= 7


def fund_split(a, b):
    """v93/v94: podział jednostek między dniami a i b ([data, NAV, liczba jednostek]): 1 = zwykły dzień; k (2, 3, 3/2, 1/2 …) = podział,
    gdy liczba jednostek zmienia się o prosty ułamek, a wartość funduszu (NAV × liczba) prawie nie; 0 = skok bez wyjaśnienia (dzień pomijany)."""
    nr, sr = b[1] / a[1], b[2] / a[2]
    if abs(sr - 1) > 0.15 and abs(nr * sr - 1) < 0.1:
        for q in (2, 3, 4, 5, 10, 1.5, 4 / 3, 1.25):
            for k in (q, 1 / q):
                if abs(sr / k - 1) < 0.05:
                    return k
        return 0
    return 1 if 0.6 < nr < 1.6 and 1 / 3 < sr < 3 else 0


def _fund_rows(h):
    """Poprawne wiersze historii bez wierszy powtórzonych (ten sam NAV i ta sama liczba jednostek — np. dzień wolny w USA w pliku złota)."""
    h = [r for r in h or [] if isinstance(r, list) and len(r) == 3 and _d(r[0]) and _isnum(r[1]) and _isnum(r[2]) and r[1] > 0 and r[2] > 0]
    return h[:1] + [b for a, b in zip(h, h[1:]) if not (b[1] == a[1] and b[2] == a[2])]


def fund_flows(h):
    """Historia funduszu [[data, NAV, liczba jednostek]] → {data: przepływ w mln USD} = zmiana liczby jednostek × NAV z tego dnia;
    w dniu podziału jednostek poprzednia liczba mnożona przez współczynnik podziału (v93); skok bez wyjaśnienia — dzień pominięty."""
    out = {}
    h = _fund_rows(h)
    for a, b in zip(h, h[1:]):
        k = fund_split(a, b)
        if k:
            out[b[0]] = (b[2] - a[2] * k) * b[1] / 1e6
    return out


def fund_group(fu, members):
    """Grupa funduszy → (daty, przepływy, aktywa w mln USD) do ostatniego wspólnego dnia. v94: dzień, którego nie ma któryś fundusz
    (np. inny kalendarz), nie przerywa serii — przepływy pozostałych funduszy z tego dnia dodajemy do najbliższego wspólnego dnia (sumy dokładne)."""
    F, H = [], []
    for t in members:
        h = _fund_rows(((fu or {}).get(t) or {}).get('h') or [])
        fl = fund_flows(h)
        if not fl:
            return [], [], None
        F.append(fl); H.append(h[-1])
    end = min(max(f) for f in F)
    days, vals, pend = [], [], 0.0
    for d in sorted({d for f in F for d in f if d <= end}):
        if all(d in f for f in F):
            days.append(d); vals.append(sum(f[d] for f in F) + pend); pend = 0.0
        else:
            pend += sum(f[d] for f in F if d in f)
    aum = sum(r[1] * r[2] for r in H) / 1e6
    return days, vals, aum


# v120: TRENDY — sygnały dzienne. Dla każdego rynku ostatni dzień jego danych: czy przepływ i ruch ceny odbiegają od zwykłego poziomu
# (z = odchylenie w rozrzutach z najwyżej 60 poprzednich dni), klasa dnia (f / p / fp / x / none), kierunek z założenia = kontynuacja,
# oraz test na całej historii pliku: jak często po takim dniu następna sesja miała ten sam znak. To opis danych i ich historii —
# nie prognoza i nie rekomendacja. Brak w danych = None, nigdy zero. Wszystko z plików tego przebiegu, bez zapytań do sieci.

def _td_z(vals, i, demean=True):
    """Wartość o indeksie i w rozrzutach najwyżej TD_LB poprzednich liczb (None pomijany — nigdy nie zastępowany zerem); mniej niż TD_MIN → None.
    Przepływy (demean=True): (v − średnia) / max(rozrzut, TD_FLOOR × typowy |dzień|); tło z samych zer: 0 albo ±TD_Z2 (niżej). Zwroty
    (demean=False): r / pierwiastek średniego r² (średni dzienny zwrot ≈ 0 — odejmowanie go dodałoby tylko szum); same zerowe zwroty w tle =
    cena stoi (plik bez aktualizacji) → brak porównania, None. Uogólnienie trend_day_z na dowolny indeks; tamta bez zmian."""
    if not vals or i < 0 or i >= len(vals) or not _isnum(vals[i]):
        return None
    prev = [x for x in vals[max(0, i - TD_LB):i] if _isnum(x)]
    if len(prev) < TD_MIN:
        return None
    if demean:
        m = _mean(prev)
        sd = max(_sd(prev), TD_FLOOR * _mean([abs(x) for x in prev]))
        if sd > 0:
            return (vals[i] - m) / sd
        # Rozrzut 0 zdarza się tylko wtedy, gdy wszystkie poprzednie dni to dokładnie 0 (liczba jednostek funduszu bez zmian przez tygodnie).
        # Prawdziwa liczba dnia nie może wtedy wyjść jako brak (strona napisałaby „brak danych”): dzień taki jak zwykle → 0; każdy inny jest
        # „dużo większy niż zwykle” → ±TD_Z2 (znak = kierunek). Tło nadal bez dnia t; jedna reguła dla wszystkich rynków.
        return 0.0 if vals[i] == m else (TD_Z2 if vals[i] > m else -TD_Z2)
    rms = (sum(x * x for x in prev) / len(prev)) ** 0.5
    return vals[i] / rms if rms > 0 else None


def _td_class(zf, zp, bd=False):
    """Klasa dnia z odchyleń przepływu (zf) i ceny (zp) → (reguła, kierunek, siła). Reguły rozłączne: fp (oba w tę samą stronę),
    f (tylko przepływ), p (tylko cena), x (sprzeczne — bez kierunku), none. Kierunek z założenia = kontynuacja (napływ / wzrost → +1).
    Obligacje (bd): cena nie wchodzi do reguły (wypłata odsetek obniża NAV — sp zawsze 0). Siła 0–3 = [wejście strzela] + [drugie zgodne,
    |z| ≥ 1] + [największe |z| wchodzące do reguły ≥ 2] — opisuje dzień, nigdy nie jest testowana. Jedne progi dla wszystkich rynków."""
    sf = 0 if zf is None or abs(zf) < TD_Z1 else (1 if zf > 0 else -1)
    sp = 0 if bd or zp is None or abs(zp) < TD_Z1 else (1 if zp > 0 else -1)
    if sf and sp == sf:
        rule, d = 'fp', sf
    elif sf and not sp:
        rule, d = 'f', sf
    elif sp and not sf:
        rule, d = 'p', sp
    elif sf and sp == -sf:
        rule, d = 'x', 0
    else:
        rule, d = 'none', 0
    if not d:
        return rule, 0, 0
    zs = [abs(zf) if zf is not None else 0.0] + ([] if bd else [abs(zp) if zp is not None else 0.0])
    return rule, d, 1 + int(rule == 'fp') + int(max(zs) >= TD_Z2)


def _td_next(date):
    """Następna sesja: pierwszy dzień pon–pt po dniu `date` (świąt w USA nie modelujemy — karta może stać się „nieaktualna” o sesję za wcześnie)."""
    d = _d(date) + datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d


def _td_live(date, now_ny):
    """Karta aktualna do 16:15 czasu Nowego Jorku w dniu następnej sesji (ten sam zegar co _drop_open_session); później „nieaktualna”."""
    nx = _td_next(date)
    now = now_ny.replace(tzinfo=None) if getattr(now_ny, 'tzinfo', None) else now_ny
    return now < datetime.datetime(nx.year, nx.month, nx.day, 16, 15)


def _td_live_cr(date, now_utc):
    """v123: krypto — następny dzień = kolejna doba UTC (7 dni w tygodniu, bez weekendów i świąt); karta aktualna do końca tej doby
    (północ UTC po niej), potem „nieaktualna”. Zegar bez strefy traktowany jak UTC."""
    end = _d(date) + datetime.timedelta(days=2)
    now = now_utc.astimezone(datetime.timezone.utc).replace(tzinfo=None) if getattr(now_utc, 'tzinfo', None) else now_utc
    return now < datetime.datetime(end.year, end.month, end.day)


def _td_month_first(dates, i):
    """Czy wiersz i jest pierwszym wierszem swojego miesiąca kalendarzowego (obligacje: sesja wypłaty odsetek obniża NAV — nie jest wynikiem)."""
    return i <= 0 or str(dates[i])[:7] != str(dates[i - 1])[:7]


def _td_pairs(dates, flow, ret, pub=0, bd=False):
    """Test wstecz jednego rynku → [(data sygnału, reguła, kierunek, trafienie)]. Dla każdego dnia t: z liczone tylko z danych ≤ t;
    sygnał jest znany w sesji s = t + pub; wynik = zwrot sesji s+1 (ret[s+1], w %), wymagany: nie więcej niż TD_GAP dni po sesji s (i sesja s
    nie dalej niż TD_GAP dni od dnia t — przy pub > 0 luka w pliku nie może przesunąć sygnału o tydzień), bez
    podziału jednostek (zwrot None), różny od zera (dzień bez zmiany ceny to ani trafienie, ani pudło), u obligacji nie pierwszy wiersz
    miesiąca. Trafienie = ten sam znak co kierunek. Dni sprzeczne (x) zwracane z trafieniem None — nigdy nie są parą."""
    out = []
    n = len(dates)
    for t in range(n):
        zf = _td_z(flow, t) if flow else None
        zp = _td_z(ret, t, demean=False) if ret else None
        if zf is None and zp is None:
            continue
        rule, d, _ = _td_class(zf, zp, bd)
        if rule == 'none':
            continue
        if rule == 'x':
            out.append((dates[t], 'x', 0, None))
            continue
        s = t + pub
        if not ret or s + 1 >= n:
            continue
        r = ret[s + 1]
        if r is None or r == 0:
            continue
        a, b = _d(dates[s]), _d(dates[s + 1])
        if not a or not b or (b - a).days > TD_GAP or (a - _d(dates[t])).days > TD_GAP:
            continue
        if bd and _td_month_first(dates, s + 1):
            continue
        out.append((dates[t], rule, d, 1 if (r > 0) == (d > 0) else 0))
    return out


def _td_pool(lines, rules=None, since=None):   # v123: rules/since — linie krypto; None = TD_RULES / TD_SINCE w chwili wywołania
    """Pary wszystkich rynków rodziny razem → 7 linii (fam, reguła) z TD_RULES, zawsze wszystkie. lines: {(fam, reguła): [(data, trafienie, id)]}.
    Zakres 95% (Wilson) liczony na dni z sygnałem (rynki poruszają się razem), nie na pary. Połowy historii dzielone po datach sygnału.
    lk/ln/ldays = pary z datą sygnału ≥ TD_SINCE (poza próbą). Ocena: short (< TD_NEFF dni), edge (dolna granica > 50 i obie połowy > 50),
    anti (górna granica < 50 i obie połowy < 50 — informacja, nigdy odwrócenie strony), inaczej none. Brak liczby = None, nigdy zero."""
    def pct(hs):
        return round(100 * sum(hs) / len(hs), 1) if hs else None
    out = []
    for fam, rule in (TD_RULES if rules is None else rules):
        pr = sorted((lines or {}).get((fam, rule)) or [], key=lambda x: x[0])
        n = len(pr); k = sum(h for _, h, _ in pr)
        ds = sorted({d for d, _, _ in pr}); days = len(ds)
        lo, hi = wilson(k, n, n_eff=days) if n else (None, None)
        mid = ds[days // 2] if days > 1 else None
        p1 = pct([h for d, h, _ in pr if mid and d < mid]); p2 = pct([h for d, h, _ in pr if mid and d >= mid])
        late = [(d, h) for d, h, _ in pr if d >= (TD_SINCE if since is None else since)]
        if days < TD_NEFF:
            vd = 'short'
        elif lo is not None and lo > 50 and p1 is not None and p1 > 50 and p2 is not None and p2 > 50:
            vd = 'edge'
        elif hi is not None and hi < 50 and p1 is not None and p1 < 50 and p2 is not None and p2 < 50:
            vd = 'anti'
        else:
            vd = 'none'
        out.append({'fam': fam, 'rule': rule, 'k': k, 'n': n, 'days': days, 'from': ds[0] if ds else None, 'to': ds[-1] if ds else None,
                    'p': pct([h for _, h, _ in pr]), 'ci': [lo, hi], 'h1': p1, 'h2': p2, 'lk': sum(h for _, h in late), 'ln': len(late),
                    'ldays': len({d for d, _ in late}), 'm': len({i for _, _, i in pr}), 'need': max(0, TD_NEFF - days), 'vd': vd})
    return out


def _td_rules():
    """Słownik `dr` w trendy.json: stałe reguły i lista rynków poza sygnałami dziennymi (strona opisuje powody w metodzie)."""
    return {'lb': TD_LB, 'min': TD_MIN, 'z1': TD_Z1, 'z2': TD_Z2, 'floor': TD_FLOOR, 'gap': TD_GAP, 'neff': TD_NEFF, 'pub': dict(TD_PUB),
            'pubsym': dict(TD_PUB_SYM), 'excluded': list(TD_EXCLUDED)}      # pubsym: wyjątki według pliku, sprawdzane przed pub (wydawca)


def _td_fund_px(h):
    """Poprawne wiersze historii funduszu → (daty, NAV, zwroty w %): zwrot tylko między kolejnymi wierszami bez podziału jednostek
    (fund_split ≠ 1 → None; poziomu nie trzeba korygować, bo zwroty liczymy wyłącznie dzień do dnia)."""
    dates = [r[0] for r in h]; px = [r[1] for r in h]
    ret = [None] + [(b[1] / a[1] - 1) * 100 if fund_split(a, b) == 1 else None for a, b in zip(h, h[1:])]
    return dates, px, ret


def _td_series_fund(fu, sym):
    """Fundusz ETF → seria dzienna: daty = wiersze NAV, przepływ dnia z fund_flows (mln USD; skok bez wyjaśnienia = brak), zwrot NAV.
    Rodzina z TD_FAM (reszta = eq), opóźnienie publikacji: najpierw wyjątek pliku z TD_PUB_SYM, potem TD_PUB według wydawcy.
    Mniej niż 2 wiersze — bez serii."""
    p = fu.get(sym) if isinstance(fu, dict) else None
    h = _fund_rows(p.get('h') if isinstance(p, dict) else None)
    if len(h) < 2:
        return None
    dates, px, ret = _td_fund_px(h)
    fl = fund_flows(h)
    flow = [fl.get(d) for d in dates]
    iss = p.get('iss') if isinstance(p.get('iss'), str) else None
    return {'id': sym, 'fam': next((f for f, ms in TD_FAM.items() if sym in ms), 'eq'), 'grp': TD_GRP.get(sym), 'iss': iss,
            'pub': TD_PUB_SYM.get(sym, TD_PUB.get(iss, 0)), 'sym': sym, 'cur': 'USD', 'dates': dates, 'flow': flow, 'fu': flow, 'ret': ret}


def _td_series_ob(ob, spec, fu):
    """Wiersz kraju (Indie akcje, Tajwan, Hongkong) → seria: daty = dni przepływu (tw/hk: pełny kalendarz sesji, dzień bez wiersza = brak),
    przepływ w walucie kraju (fu = kolumna ≈ mln USD; Indie już w USD), cena = NAV funduszu USA z tego samego dnia ISO (brak NAV = brak,
    nigdy zero); zwrot tylko między kolejnymi dniami serii z NAV i bez podziału jednostek między nimi. Bez pliku funduszu — bez wiersza.
    nopool: pary reguły „p” (sama cena) nie idą do linii zbiorczych — wejście ceny to NAV tego samego funduszu, który ma własny wiersz
    (ten sam dzień, ta sama sesja wyniku liczyłyby się dwa razy); własne k/n karty liczone jak zwykle."""
    iid, part, col, ucol, sym, cur = spec
    if not isinstance(ob, dict) or not isinstance(ob.get(part), dict):
        return None
    p = fu.get(sym) if isinstance(fu, dict) else None
    h = _fund_rows(p.get('h') if isinstance(p, dict) else None)
    if len(h) < 2:
        return None
    cols = (_tr_sessions if part in ('tw', 'hk') else _tr_cols)(ob[part], *([col, ucol] if ucol else [col]))
    dates, flow = cols[0], cols[1]
    if not dates:
        return None
    fuv = cols[2] if ucol else flow
    nav = {str(r[0])[:10]: r[1] for r in h}
    splits = [b[0] for a, b in zip(h, h[1:]) if fund_split(a, b) != 1]
    px = [nav.get(str(d)[:10]) for d in dates]
    ret = [None]
    for i in range(1, len(dates)):
        ok = px[i] is not None and px[i - 1] is not None and not any(dates[i - 1] < s <= dates[i] for s in splits)
        ret.append((px[i] / px[i - 1] - 1) * 100 if ok else None)
    return {'id': iid, 'fam': 'eq', 'grp': None, 'iss': None, 'pub': 0, 'sym': sym, 'cur': cur, 'dates': dates, 'flow': flow, 'fu': fuv, 'ret': ret,
            'nopool': ('p',)}


def _td_series_px(ce, sym):
    """Rynek tylko z ceną (zamknięcia z pliku cen) → seria bez przepływów: f / zf / fu = brak (None), zwrot między kolejnymi wierszami."""
    q = (ce.get('q') or {}).get(sym) if isinstance(ce, dict) else None
    d = [r for r in ((q or {}).get('d') or []) if isinstance(r, list) and len(r) > 1 and _isnum(r[1]) and r[1] > 0 and _d(r[0])]
    if len(d) < 2:
        return None
    dates = [str(r[0])[:10] for r in d]; px = [r[1] for r in d]
    ret = [None] + [(px[i] / px[i - 1] - 1) * 100 for i in range(1, len(px))]
    return {'id': sym, 'fam': 'eq', 'grp': None, 'iss': None, 'pub': 0, 'sym': sym, 'cur': None, 'dates': dates, 'flow': None, 'fu': None, 'ret': ret}


def _td_row(s, vd_by, own, now_ny, today, now_utc=None):   # v123: now_utc — zegar kart krypto (doba UTC)
    """Karta jednego rynku z ostatniego dnia jego serii. Stany: buy/sell (kierunek, aktualna, linia z przewagą), obs (kierunek, aktualna,
    bez przewagi — szara karta po stronie kierunku; ocena „anti” nigdy nie odwraca strony), x (sprzeczne), quiet (spokojny dzień),
    stale (po 16:15 NY następnej sesji — reguła i siła nadal policzone do opisu), short (żadne wejście reguły nie ma 40 dni historii),
    nodata (brak liczb z tego dnia). Wszystkie brakujące liczby = None, nigdy zero; z i zwrot na 2 miejsca, kwoty na 1."""
    i = len(s['dates']) - 1
    date = s['dates'][i]
    bd = s['fam'] == 'bd'
    flow, ret = s['flow'], s['ret']
    zf = _td_z(flow, i) if flow else None
    zp = _td_z(ret, i, demean=False) if ret else None
    rule, d, sg = _td_class(zf, zp, bd)
    inputs = [v for v in ([flow] if bd else [flow, ret]) if v]              # wejścia reguły (u obligacji cena nie wchodzi)
    has_now = any(_isnum(v[i]) for v in inputs)
    has_hist = any(sum(1 for x in v[max(0, i - TD_LB):i] if _isnum(x)) >= TD_MIN for v in inputs)
    live = _td_live_cr(date, now_utc) if s['fam'] == 'cr' else _td_live(date, now_ny)
    signal = rule in ('f', 'p', 'fp')
    vd = vd_by.get((s['fam'], rule)) if signal else None
    if not has_now:
        st = 'nodata'
    elif not has_hist:
        st = 'short'
    elif not live:
        st = 'stale'
    elif rule == 'x':
        st = 'x'
    elif rule == 'none':
        st = 'quiet'
    else:
        st = ('buy' if d > 0 else 'sell') if vd == 'edge' else 'obs'
    side = ('buy' if d > 0 else 'sell') if st in ('buy', 'sell', 'obs') else 'none'
    o = own.get((s['id'], rule), (0, 0)) if signal else (None, None)

    def rnd(x, k):
        return round(x, k) if _isnum(x) else None
    return {'id': s['id'], 'fam': s['fam'], 'grp': s['grp'], 'iss': s['iss'], 'pub': s['pub'], 'sym': s['sym'], 'date': date,
            'nx': (_d(date) + datetime.timedelta(days=1) if s['fam'] == 'cr' else _td_next(date)).isoformat(), 'live': live, 'age': (today - _d(date)).days,
            'f': rnd(flow[i] if flow else None, 1), 'cur': s['cur'], 'fu': rnd(s['fu'][i] if s['fu'] else None, 1), 'zf': rnd(zf, 2),
            'r': rnd(ret[i] if ret else None, 2), 'zp': rnd(zp, 2), 'rule': rule, 'dir': d, 'side': side, 'str': sg, 'st': st, 'vd': vd,
            'ik': o[0], 'in': o[1]}


def build_daily(S):
    """Sygnały dzienne → (d, bd): d = karty rynków (fundusze z TR_FE, wiersze krajów z TD_OB, rynki tylko z ceną z TD_PX — lista zamknięta,
    wyniki niczego nie dodają ani nie usuwają; rynek bez serii cen jest pomijany, nie pokazywany jako karta z samymi brakami),
    bd = 7 linii testu wstecz (zawsze wszystkie, także z n = 0). Zepsuta seria jednego rynku (budowa, test wstecz albo karta) → uwaga
    w meta, bez jego karty; reszta zostaje. Gdy nie powstała żadna karta (brak serii wejściowych albo wszystkie zepsute) → (None, None):
    blok niepoliczony, strona go ukrywa — pusta lista znaczyłaby „dziś zero rynków”, a to nieprawda. Nie zmienia S.
    Zegary: _ny_now (aktualność karty), _now_utc (wiek danych) — w testach podmieniane."""
    S = S if isinstance(S, dict) else {}
    fu = (S.get('fundusze') or {}).get('f') if isinstance(S.get('fundusze'), dict) else None
    ob = S.get('obce') if isinstance(S.get('obce'), dict) else {}
    ce = S.get('ceny') if isinstance(S.get('ceny'), dict) else {}
    todo = [(sym, lambda sym=sym: _td_series_fund(fu, sym)) for sym in TD_GRP] + \
           [(sp[0], lambda sp=sp: _td_series_ob(ob, sp, fu)) for sp in TD_OB] + \
           [(sym, lambda sym=sym: _td_series_px(ce, sym)) for sym in TD_PX]
    series = []
    for name, fn in todo:
        try:
            s = fn()
            if s:
                series.append(s)
        except Exception as e:
            META['notes'].append(mask(f'trendy dziennie {name}: {e}'))
    if not series:
        return None, None                                   # żadnej serii wejściowej — blok niepoliczony (None), nie „zero rynków” ([])
    lines = {key: [] for key in TD_RULES}
    own = {}
    good = []
    for s in series:
        try:
            pr = _td_pairs(s['dates'], s['flow'], s['ret'], s['pub'], s['fam'] == 'bd')
        except Exception as e:                              # zepsuta seria jednego rynku: uwaga, bez jego par i karty; reszta zostaje
            META['notes'].append(mask(f"trendy dziennie {s['id']}: {e}"))
            continue
        good.append(s)
        for dt, rule, d, hit in pr:
            if hit is None:
                continue
            if (s['fam'], rule) in lines and rule not in s.get('nopool', ()):
                lines[(s['fam'], rule)].append((dt, hit, s['id']))
            o = own.setdefault((s['id'], rule), [0, 0]); o[0] += hit; o[1] += 1
    bd = _td_pool(lines)
    vd_by = {(b['fam'], b['rule']): b['vd'] for b in bd}
    now_ny, today = _ny_now(), _now_utc().date()
    rows = []
    for s in good:
        try:
            rows.append(_td_row(s, vd_by, own, now_ny, today))
        except Exception as e:                              # zepsuty ostatni dzień jednego rynku: uwaga, bez jego karty
            META['notes'].append(mask(f"trendy dziennie {s['id']}: {e}"))
    return (rows, bd) if rows else (None, None)


def _td_series_cr(kc, sym, today):
    """v123: para <SYM>USDT z pliku cen krypto → seria dzienna (doba UTC). Tylko zamknięte doby: wiersz z datą ≥ dziś (UTC) odpada —
    budowniczy pliku już pomija trwającą dobę, to druga straż. Zamknięcie musi być liczbą > 0; inne wiersze odpadają (brak, nigdy zero).
    Daty muszą ściśle rosnąć — inaczej ValueError (uwaga w meta, bez karty tej pary). Zwrot w % tylko między kolejnymi dobami
    kalendarzowymi: luka w danych = brak (None), nigdy zero ani zwrot przez dwie doby. Bez przepływów: f / zf / fu / waluta = brak.
    Mniej niż 2 wiersze — bez serii (bez karty)."""
    q = (kc.get('q') or {}).get(sym) if isinstance(kc, dict) else None
    d = [r for r in ((q or {}).get('d') or []) if isinstance(r, list) and len(r) > 1 and _isnum(r[1]) and r[1] > 0 and _d(r[0]) and _d(r[0]) < today]
    if len(d) < 2:
        return None
    dates = [str(r[0])[:10] for r in d]; px = [r[1] for r in d]
    if any(a >= b for a, b in zip(dates, dates[1:])):
        raise ValueError('daty nie rosną')
    ret = [None] + [(px[i] / px[i - 1] - 1) * 100 if (_d(dates[i]) - _d(dates[i - 1])).days == 1 else None for i in range(1, len(px))]
    return {'id': sym, 'fam': 'cr', 'grp': None, 'iss': None, 'pub': 0, 'sym': sym, 'cur': None, 'dates': dates, 'flow': None, 'fu': None, 'ret': ret}


def build_daily_cr(S):
    """v123: sygnały dzienne krypto → (wiersze, linie): pary z TR_CR_SYMS, reguła „p” (sam ruch ceny), test na następnej dobie UTC,
    linie z TD_RULES_CR z własną wersją (v) i datą wdrożenia (since). Brak pliku cen krypto albo żadnej serii → (None, None), nie „zero
    monet”. Zepsuta seria jednej pary → uwaga w meta, bez jej karty; reszta zostaje. Nie zmienia S. Zegar: _now_utc (w testach podmieniany)."""
    S = S if isinstance(S, dict) else {}
    kc = S.get('ceny-krypto') if isinstance(S.get('ceny-krypto'), dict) else None
    if not kc:
        return None, None
    now_utc = _now_utc(); today = now_utc.date()
    series = []
    for sym in TR_CR_SYMS:
        try:
            s = _td_series_cr(kc, sym, today)
            if s:
                series.append(s)
        except Exception as e:
            META['notes'].append(mask(f'trendy dziennie krypto {sym}: {e}'))
    if not series:
        return None, None
    lines = {key: [] for key in TD_RULES_CR}; own = {}; good = []
    for s in series:
        try:
            pr = _td_pairs(s['dates'], None, s['ret'], 0, False)
        except Exception as e:                              # zepsuta seria jednej pary: uwaga, bez jej par i karty
            META['notes'].append(mask(f"trendy dziennie krypto {s['id']}: {e}")); continue
        good.append(s)
        for dt, rule, d, hit in pr:
            if hit is None:
                continue
            if ('cr', rule) in lines:
                lines[('cr', rule)].append((dt, hit, s['id']))
            o = own.setdefault((s['id'], rule), [0, 0]); o[0] += hit; o[1] += 1
    bd = _td_pool(lines, TD_RULES_CR, TD_SINCE_CR)
    for b in bd:
        b['v'], b['since'] = TD_VC, TD_SINCE_CR               # linie krypto niosą własną wersję i datę wdrożenia
    vd_by = {(b['fam'], b['rule']): b['vd'] for b in bd}
    rows = []
    for s in good:
        try:
            rows.append(_td_row(s, vd_by, own, None, today, now_utc))
        except Exception as e:
            META['notes'].append(mask(f"trendy dziennie krypto {s['id']}: {e}"))
    return (rows, bd) if rows else (None, None)


def _tr_try(name, fn, out):
    """Jedno źródło TRENDÓW — błąd jednego źródła nie usuwa pozostałych (uwaga w meta zamiast pustej zakładki)."""
    try:
        for r in fn() or []:
            if r:
                out.append(r)
    except Exception as e:
        META['notes'].append(mask(f'trendy {name}: {e}'))


def _tr_flows(S):
    """Serie przepływów z obiektów zapisanych w tym przebiegu (brak pliku albo części = brak wiersza)."""
    rows = []
    today = _now_utc().date()
    ob = S.get('obce') if isinstance(S.get('obce'), dict) else {}

    def india():
        if not ob.get('in'):
            return []
        ds, eq, db = _tr_cols(ob['in'], 1, 2)
        return [_tr_row('in_eq', 'eq', 'flow', 5, ds, eq, span=TR_SPAN), _tr_row('in_bd', 'bd', 'flow', 5, ds, db, span=TR_SPAN)]

    def twhk():
        out = []
        for k, cur in (('tw', 'TWD'), ('hk', 'HKD')):
            if ob.get(k):
                ds, v, u = _tr_sessions(ob[k], 1, 5)
                out.append(_tr_row(k, 'eq', 'flow', 5, ds, v, usd=u, cur=cur, skip=set(ob[k].get('empty') or [])))
        return out

    def thai():
        if not ob.get('th'):
            return []
        ds, v, u, h = _tr_cols(ob['th'], 1, 7, 6)
        return [_tr_row('th', 'bd', 'flow', 5, ds, v, usd=u, hold=next((x for x in reversed(h) if _isnum(x)), None), cur='THB', span=TR_SPAN)]

    def brazil():
        if not ob.get('br'):
            return []
        ds, v = _tr_cols(ob['br'], 1)
        return [_tr_row('br', 'fx', 'flow', 5, ds, v, lag=7, span=TR_SPAN)]   # publikacja w środy za tydzień do piątku

    def turkey():
        if not ob.get('tr'):
            return []
        ds, eq, gb = _tr_cols(ob['tr'], 2, 3)
        return [_tr_row('tr_eq', 'eq', 'flow', 1, *_tr_weeks(ds, eq), weekly_days=16),
                _tr_row('tr_bd', 'bd', 'flow', 1, *_tr_weeks(ds, gb), weekly_days=16)]

    def japan():
        inst = S.get('instytucje') if isinstance(S.get('instytucje'), dict) else {}
        mof = [w for w in ((inst.get('mof') or {}).get('d') or []) if isinstance(w, dict) and w.get('to')]
        if not mof:
            return []
        ds = [w['to'] for w in mof]
        rates = _jpy_per_usd(S); out = []
        for key, sid, g in (('equity_net', 'jp_eq', 'eq'), ('ltdebt_net', 'jp_bd', 'bd')):
            v = [(w.get('liabilities') or {}).get(key) for w in mof]
            wd, wv = _tr_weeks(ds, [x / 10 if _isnum(x) else None for x in v])      # mld JPY
            fx = [_rate_for(rates, d[:7]) for d in wd]                                # średni kurs miesiąca EBC (ostatni znany ≤ miesiąc tygodnia)
            usd = [x * 1000 / rt if _isnum(x) and rt else None for x, (_, rt) in zip(wv, fx)]
            r = _tr_row(sid, g, 'flow', 1, wd, wv, usd=usd, weekly_days=20, cur='JPY')
            if r and fx and fx[-1][0]:
                r['fxm'] = fx[-1][0]
            out.append(r)
        return out

    def mexico():
        mx = S.get('meksyk') if isinstance(S.get('meksyk'), dict) else {}
        md = [r for r in mx.get('d') or [] if isinstance(r, list) and len(r) > 1 and isinstance(r[0], str) and _d(r[0])]
        if len(md) < 2:
            return []
        ch = [md[i][1] - md[i - 1][1] if _isnum(md[i][1]) and _isnum(md[i - 1][1]) else None for i in range(1, len(md))]
        fx = (mx.get('fx') or [None])[0]
        usd = [x / fx if _isnum(x) and _isnum(fx) and fx > 0 else None for x in ch]
        return [_tr_row('mx', 'bd', 'stock', 5, [r[0] for r in md[1:]], ch, usd=usd, hold=md[-1][1], lag=10, cur='MXN', span=TR_SPAN)]

    def etfs():
        etf = S.get('etf') if isinstance(S.get('etf'), dict) else {}; out = []
        for k in ('btc', 'eth', 'sol', 'xrp'):
            a = (etf.get('assets') or {}).get(k) or {}
            day = [r for r in a.get('day') or [] if isinstance(r, list) and len(r) == 2 and _isnum(r[0])]
            if day:
                ds = [datetime.datetime.fromtimestamp(r[0], datetime.timezone.utc).date().isoformat() for r in day]
                out.append(_tr_row('etf_' + k, 'cr', 'flow', 5, ds, [r[1] if _isnum(r[1]) else None for r in day], hold=a.get('aum'), span=TR_SPAN))
        return out

    def coinmetrics():
        cm = S.get('cm') if isinstance(S.get('cm'), dict) else {}; out = []
        for k in ('btc', 'eth'):
            a = (cm.get('assets') or {}).get(k) or {}
            ds, v, u, sp = _tr_cols(a, 3, 6, 7)
            if ds:
                u = [x / 1e6 if _isnum(x) else None for x in u]      # Coin Metrics podaje USD — tu mln USD jak w pozostałych wierszach
                out.append(_tr_row('cm_' + k, 'cr', 'exch', 7, ds, v, usd=u, hold=next((x for x in reversed(sp) if _isnum(x)), None), cur=k.upper()))
        return out

    def stable():
        kr = S.get('krypto') if isinstance(S.get('krypto'), dict) else {}
        dd = {r[0]: r[1] for r in (kr.get('stabh') or {}).get('dd') or []
              if isinstance(r, list) and len(r) == 2 and _isnum(r[1]) and _d(r[0]) and _d(r[0]) < today}   # bez dzisiejszego, niezamkniętego dnia
        if len(dd) < 2:
            return []
        d0, d1 = _d(min(dd)), _d(max(dd))
        cal = [(d0 + datetime.timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]
        ch = [(dd[b] - dd[a]) / 1e6 if a in dd and b in dd else None for a, b in zip(cal, cal[1:])]   # mln USD; brak dnia = brak
        return [_tr_row('stab', 'cr', 'supply', 7, cal[1:], ch)]

    def cftc():
        cf = S.get('cftc') if isinstance(S.get('cftc'), dict) else {}; out = []
        for k in TR_CFTC:
            h = ((cf.get('markets') or {}).get(k) or {}).get('hist') or {}
            ds, lv = h.get('dates') or [], h.get('lev_funds') or []
            if len(ds) == len(lv) and len(ds) > 1:
                wd, wv = _tr_weeks(ds, lv, n=len(ds) + 4)
                ch = [wv[i] - wv[i - 1] if _isnum(wv[i]) and _isnum(wv[i - 1]) else None for i in range(1, len(wv))]
                r = _tr_row('cf_' + k, 'pos', 'pos', 1, wd[1:], ch, weekly_days=14, cur='CT')
                if r and _cftc_roll(r['date']):
                    r['roll'] = True; r['x'] = False       # rolowanie kontraktów kwartalnych zawyża zmiany — bez „wyjątkowo”
                out.append(r)
        su = S.get('surowce') if isinstance(S.get('surowce'), dict) else {}   # v92: surowce — fundusze zarządzające (managed money)
        for k in CFTCD_MARKETS:
            h = ((su.get('markets') or {}).get(k) or {}).get('hist') or {}
            ds, lv = h.get('dates') or [], h.get('mm') or []
            if len(ds) == len(lv) and len(ds) > 1:
                wd, wv = _tr_weeks(ds, lv, n=len(ds) + 4)
                ch = [wv[i] - wv[i - 1] if _isnum(wv[i]) and _isnum(wv[i - 1]) else None for i in range(1, len(wv))]
                out.append(_tr_row('cs_' + k, 'pos', 'pos', 1, wd[1:], ch, weekly_days=14, cur='CT'))
        return out

    def funds():
        fu = (S.get('fundusze') or {}).get('f') if isinstance(S.get('fundusze'), dict) else None
        if not isinstance(fu, dict):
            return []
        out = []
        for gid, members in TR_FE:
            ds, v, aum = fund_group(fu, members)
            if ds:
                r = _tr_row(gid, 'fe', 'flow', 5, ds, v, hold=aum, span=TR_SPAN)
                if r:
                    iss = {(fu.get(t) or {}).get('iss') for t in members}   # v94: źródło grupy — wydawcy jej funduszy
                    r['iss'] = 'both' if len(iss) > 1 else (next(iter(iss)) or '')
                    lasts = {t: ((fu.get(t) or {}).get('h') or [[None]])[-1][0] for t in members}
                    top = max(x for x in lasts.values() if x)
                    for t, x in lasts.items():
                        if x and _bdays(_d(x), _d(top)) >= 5:
                            META['notes'].append(f'trendy {gid}: {t} ma dane tylko do {x} — grupa liczona do tego dnia')
                out.append(r)
        return out

    for name, fn in (('fundusze', funds), ('in', india), ('tw/hk', twhk), ('th', thai), ('br', brazil), ('tr', turkey), ('jp', japan), ('mx', mexico),
                     ('etf', etfs), ('cm', coinmetrics), ('stab', stable), ('cftc', cftc)):
        _tr_try(name, fn, rows)
    return rows


def _px_state(w, prev, typ):
    """Ceny: stan z ruchu tygodnia (w) i ruchu 4 tygodni przed nim (prev), progi względem typowego tygodniowego ruchu (typ, w %)."""
    if abs(w) < TR_PX_MIN * typ:
        return 'flat'
    pc = abs(prev) >= TR_PX_MIN * 2 * typ       # 4 tygodnie ≈ dwa razy tyle co tydzień (pierwiastek z 4)
    if w > 0:
        return 'up_cont' if pc and prev > 0 else 'dn_fade' if pc else 'up_new'
    return 'dn_cont' if pc and prev < 0 else 'up_fade' if pc else 'dn_new'


def _tr_prices(S):
    """Ceny ETF-ów krajów (Twelve Data, dzienne zamknięcia w USD) i największych kryptowalut (CoinGecko, zmiany 7 i 30 dni)
    oraz dane do „czy tydzień zapowiada następny”: pary tygodni kalendarzowych (ta sama reguła wyraźnego ruchu, bez patrzenia w przód)."""
    out, pairs = [], {'k': 0, 'n': 0, 'weeks': set(), 'from': None, 'to': None}
    today = _now_utc().date()
    cur = today - datetime.timedelta(days=today.weekday())
    ce = S.get('ceny') if isinstance(S.get('ceny'), dict) else {}
    for sym in TR_PX_SYMS:
        d = [r for r in ((ce.get('q') or {}).get(sym) or {}).get('d') or []
             if isinstance(r, list) and len(r) > 1 and _isnum(r[1]) and r[1] > 0 and _d(r[0])]
        if len(d) < 26:
            continue
        c = [r[1] for r in d]
        wk = [(c[-1 - 5 * i] / c[-1 - 5 * (i + 1)] - 1) * 100 for i in range(min(TR_PX_WEEKS + 1, (len(c) - 1) // 5))]   # [ten tydzień, poprzedni, …]
        if len(wk) < 21:
            continue
        typ = _sd(wk[1:])
        prev = (c[-6] / c[-26] - 1) * 100
        z = wk[0] / typ if typ > 0 else None
        out.append({'id': sym, 'g': 'eq', 'date': d[-1][0], 'w': round(wk[0], 2), 'pr': round(prev, 2), 'typ': round(typ, 2),
                    'z': round(z, 2) if z is not None else None, 'st': _px_state(wk[0], prev, typ) if typ > 0 else 'flat'})
        lastc = {}                                   # tygodnie kalendarzowe: zamknięcie ostatniej sesji tygodnia, bez bieżącego tygodnia
        for r in d:
            m = _d(r[0]) - datetime.timedelta(days=_d(r[0]).weekday())
            if m < cur:
                lastc[m] = r[1]
        ms = sorted(lastc)
        ret = [(ms[i], (lastc[ms[i]] / lastc[ms[i - 1]] - 1) * 100) for i in range(1, len(ms)) if (ms[i] - ms[i - 1]).days == 7]
        for i in range(len(ret) - 1):
            past = [x for _, x in ret[max(0, i - TR_PX_WEEKS):i]]
            if len(past) < 20 or (ret[i + 1][0] - ret[i][0]).days != 7:
                continue
            (m0, a), (m1, b) = ret[i], ret[i + 1]
            if abs(a) >= TR_PX_MIN * _sd(past):
                pairs['n'] += 1; pairs['k'] += 1 if a * b > 0 else 0; pairs['weeks'].add(m0)
                pairs['from'] = min(pairs['from'] or m0, m0); pairs['to'] = max(pairs['to'] or m1, m1)
    fu = (S.get('fundusze') or {}).get('f') if isinstance(S.get('fundusze'), dict) else None   # v93: cena jednostki (NAV) największego funduszu grupy
    if isinstance(fu, dict):
        for gid, members in TR_FE:
            if gid not in TR_FP:
                continue
            cand = [(t, _fund_rows((fu.get(t) or {}).get('h'))) for t in members]
            cand = [(t, h) for t, h in cand if len(h) >= 5 * 21 + 1]        # v94: tylko fundusz z historią na cały rachunek i z bieżącą datą
            newest = max((h[-1][0] for _, h in cand), default=None)
            cand = [(t, h) for t, h in cand if _bdays(_d(h[-1][0]), _d(newest)) <= 3]
            best = max(cand, key=lambda c: c[1][-1][1] * c[1][-1][2], default=None)
            if not best:
                continue
            h = best[1][-(5 * (TR_PX_WEEKS + 1) + 1):]
            c, f = [h[-1][1]], 1.0                         # NAV w jednostkach po ostatnim podziale; skok bez wyjaśnienia ucina starszą część
            for a, b in zip(reversed(h[:-1]), reversed(h[1:])):
                k = fund_split(a, b)
                if not k:
                    META['notes'].append(f'trendy {best[0]}: skok NAV bez podziału jednostek ({b[0]}) — cena liczona od tego dnia'); break
                f *= k; c.append(a[1] / f)
            c.reverse()
            if len(c) < 26:
                continue
            wk = [(c[-1 - 5 * i] / c[-1 - 5 * (i + 1)] - 1) * 100 for i in range(min(TR_PX_WEEKS + 1, (len(c) - 1) // 5))]
            if len(wk) < 21:
                continue
            typ = _sd(wk[1:]); prev = (c[-6] / c[-26] - 1) * 100; z = wk[0] / typ if typ > 0 else None
            out.append({'id': 'fp_' + gid[3:], 'g': 'fp', 'sym': best[0], 'date': best[1][-1][0], 'w': round(wk[0], 2), 'pr': round(prev, 2),
                        'typ': round(typ, 2), 'z': round(z, 2) if z is not None else None, 'st': _px_state(wk[0], prev, typ) if typ > 0 else 'flat'})
    kr = S.get('krypto') if isinstance(S.get('krypto'), dict) else {}
    mk = kr.get('mk') or {}
    cols = mk.get('cols') or []
    if all(c in cols for c in ('sym', 'p7d', 'p30d')):
        i_s, i7, i30 = cols.index('sym'), cols.index('p7d'), cols.index('p30d')
        by = {}
        for r in mk.get('rows') or []:      # wiersze od największej kapitalizacji — przy powtórzonym symbolu zostaje pierwszy
            if isinstance(r, list) and len(r) == len(cols):
                by.setdefault(str(r[i_s]).upper(), r)
        for sym in TR_CR_SYMS:
            r = by.get(sym)
            if not r or not _isnum(r[i7]) or not _isnum(r[i30]) or r[i7] <= -100:
                continue
            prev = ((1 + r[i30] / 100) / (1 + r[i7] / 100) - 1) * 100     # 23 dni przed ostatnim tygodniem
            out.append({'id': sym, 'g': 'cr', 'date': str(kr.get('at') or '')[:10], 'w': round(r[i7], 2), 'pr': round(prev, 2), 'z': None,
                        'st': _px_state(r[i7], prev, TR_CR_TYP)})
    return out, pairs


def build_trendy(S):
    """data/trendy.json — przepływy (f), ceny (p) i „czy tydzień zapowiada następny” (b) z obiektów zapisanych w tym przebiegu."""
    S = S if isinstance(S, dict) else {}
    today = _now_utc().date()
    flows = _tr_flows(S)
    prices, pp = [], {'k': 0, 'n': 0, 'weeks': set(), 'from': None, 'to': None}
    try:
        prices, pp = _tr_prices(S)
    except Exception as e:
        META['notes'].append(mask(f'trendy ceny: {e}'))
    base = []
    ob = S.get('obce') if isinstance(S.get('obce'), dict) else {}
    try:
        if ob.get('th'):
            ds, v = _tr_cols(ob['th'], 1)
            k, n, a, b = trend_persist(ds, v, today)
            if n:
                base.append({'id': 'th', 'kind': 'flow', 'k': k, 'n': n, 'weeks': n, 'from': a, 'to': b, 'ci': list(wilson(k, n))})
        md = [r for r in ((S.get('meksyk') or {}).get('d') or []) if isinstance(r, list) and len(r) > 1 and isinstance(r[0], str)]
        if len(md) > 1:
            ch = [md[i][1] - md[i - 1][1] if _isnum(md[i][1]) and _isnum(md[i - 1][1]) else None for i in range(1, len(md))]
            k, n, a, b = trend_persist([r[0] for r in md[1:]], ch, today)
            if n:
                base.append({'id': 'mx', 'kind': 'flow', 'k': k, 'n': n, 'weeks': n, 'from': a, 'to': b, 'ci': list(wilson(k, n))})
    except Exception as e:
        META['notes'].append(mask(f'trendy historia: {e}'))
    try:                                  # v95: dzienne przepływy krajów razem — niepewność liczona na tygodnie
        K = N = 0; wk = set(); a0 = b0 = None
        for part, col in TR_OB:
            if not isinstance(ob.get(part), dict):
                continue
            ds, v = _tr_sessions(ob[part], col) if part in ('tw', 'hk') else _tr_cols(ob[part], col)
            k, n, a, b = trend_persist(ds, v, today, wk)
            K += k; N += n
            if n:
                a0 = min(a0 or a, a); b0 = max(b0 or b, b)
        if N:
            base.append({'id': 'ob', 'kind': 'flow', 'k': K, 'n': N, 'weeks': len(wk), 'from': a0, 'to': b0, 'ci': list(wilson(K, N, n_eff=len(wk)))})
    except Exception as e:
        META['notes'].append(mask(f'trendy historia krajów: {e}'))
    try:                                  # v90: fundusze ETF — wszystkie grupy razem, niepewność liczona na tygodnie
        fu = (S.get('fundusze') or {}).get('f') if isinstance(S.get('fundusze'), dict) else None
        if isinstance(fu, dict):
            K = N = 0; wk = set(); a0 = b0 = None
            for gid, members in TR_FE:
                ds, v, _ = fund_group(fu, members)
                k, n, a, b = trend_persist(ds, v, today, wk)
                K += k; N += n
                if n:
                    a0 = min(a0 or a, a); b0 = max(b0 or b, b)
            if N:
                base.append({'id': 'fe', 'kind': 'flow', 'k': K, 'n': N, 'weeks': len(wk), 'from': a0, 'to': b0, 'ci': list(wilson(K, N, n_eff=len(wk)))})
    except Exception as e:
        META['notes'].append(mask(f'trendy historia funduszy: {e}'))
    if pp['n']:
        nw = len(pp['weeks'])            # rynki są ze sobą powiązane: niepewność liczona na tygodnie, nie na pary rynek-tydzień
        base.append({'id': 'px', 'kind': 'price', 'k': pp['k'], 'n': pp['n'], 'weeks': nw, 'from': pp['from'].isoformat(),
                     'to': pp['to'].isoformat(), 'ci': list(wilson(pp['k'], pp['n'], n_eff=nw))})
    daily = {'dv': TD_V, 'dsince': TD_SINCE, 'dr': _td_rules(), 'd': None, 'bd': None}   # v120: sygnały dzienne (ten sam przebieg, ten sam `at`)
    try:
        daily['d'], daily['bd'] = build_daily(S)
    except Exception as e:
        daily['d'] = daily['bd'] = None     # awaria to nie „zero rynków”: null w pliku (strona ukrywa blok), nigdy pusta lista
        META['notes'].append(mask(f'trendy dziennie: {e}'))
    try:                                  # v123: rodzina krypto — dopisana na końcu d i bd; awaria nie rusza wierszy i linii świata
        cd, cb = build_daily_cr(S)
        if cd:
            daily['d'] = (daily['d'] or []) + cd
            daily['bd'] = (daily['bd'] or []) + cb
    except Exception as e:
        META['notes'].append(mask(f'trendy dziennie krypto: {e}'))
    return {'at': NOW, 'v': 1, 'src': 'CapitalFlowAI — obliczenia z plików tej strony (fundusze, obce, meksyk, instytucje, kursy, etf, cm, krypto, cftc, surowce, ceny)',
            'rules': {'base_min': TR_BASE_MIN, 'base_max': TR_BASE_MAX, 'dir': TR_DIR, 'all': TR_ALL, 'floor': TR_FLOOR, 'strong': TR_STRONG,
                      'exc': TR_EXC, 'day_z': TR_DAY_Z, 'span': TR_SPAN, 'px_min': TR_PX_MIN, 'cr_typ': TR_CR_TYP},
            'f': flows, 'p': prices, 'b': base, **daily}


# ===================== v97: DANE RZĄDU USA (domena publiczna) — EIA, BLS, BEA =====================
# Klucze tylko z GitHub Secrets (EIA_KEY, BLS_KEY, BEA_KEY); nigdy w plikach wynikowych (maskowanie komunikatów: SECRETS).
EIA_API = 'https://api.eia.gov/v2/'
EIA_SERIES = (('wti', 'petroleum/pri/spt', 'daily', 'RWTC', 90),        # ropa WTI, Cushing — cena spot, USD za baryłkę
              ('brent', 'petroleum/pri/spt', 'daily', 'RBRTE', 90),     # ropa Brent — cena spot, USD za baryłkę
              ('gas', 'natural-gas/pri/fut', 'daily', 'RNGWHHD', 90),   # gaz Henry Hub — cena spot, USD za mln BTU
              ('crude', 'petroleum/stoc/wstk', 'weekly', 'WCESTUS1', 60),  # zapasy ropy w USA bez rezerwy strategicznej, tys. baryłek
              ('spr', 'petroleum/stoc/wstk', 'weekly', 'WCSSTUS1', 60))    # rezerwa strategiczna (SPR), tys. baryłek


def eia_series(key, route, freq, sid, n):
    """EIA API v2 → ([[data, wartość], …] rosnąco, jednostka). Kody tras i serii sprawdzone 25.09.2026 (DEMO_KEY);
    nie-liczba = brak (nigdy 0); odpowiedź z polem error = błąd."""
    q = urllib.parse.urlencode([('api_key', key), ('frequency', freq), ('data[0]', 'value'), ('facets[series][]', sid),
                                ('sort[0][column]', 'period'), ('sort[0][direction]', 'desc'), ('offset', '0'), ('length', str(n))])
    j = get_json(f'{EIA_API}{route}/data/?{q}')
    if not isinstance(j, dict) or j.get('error'):
        raise RuntimeError(str((j or {}).get('error') if isinstance(j, dict) else 'nieznany kształt odpowiedzi')[:140])
    rows = (j.get('response') or {}).get('data')
    if not isinstance(rows, list):
        raise RuntimeError('nieznany kształt odpowiedzi')
    out, unit = {}, None
    for r in rows:
        if not isinstance(r, dict) or r.get('series') != sid:
            continue
        d, v = str(r.get('period') or ''), _num(r.get('value'))
        if re.match(r'^\d{4}-\d{2}-\d{2}$', d) and v is not None and v == v and abs(v) != float('inf'):
            out[d] = v; unit = unit or r.get('units')
    if not out:
        raise RuntimeError('brak wartości')
    return [[d, out[d]] for d in sorted(out)], unit


def build_energia(key, prev=None):
    """data/energia.json — ceny ropy (WTI, Brent), gazu (Henry Hub) i zapasy ropy w USA (EIA). Seria bez odpowiedzi zostawia
    poprzednie wartości (z datą); bez żadnej nowej serii = błąd (zostaje poprzedni plik)."""
    old = (prev or {}).get('s') if isinstance(prev, dict) else None
    old = old if isinstance(old, dict) else {}
    out = {'at': NOW, 'src': 'U.S. Energy Information Administration (EIA) — Open Data API v2', 'url': 'https://www.eia.gov/opendata/', 's': {}}
    fails, got = [], 0
    for name, route, freq, sid, n in EIA_SERIES:
        try:
            d, unit = eia_series(key, route, freq, sid, n)
            out['s'][name] = {'id': sid, 'freq': freq, 'unit': unit, 'd': d}; got += 1
        except Exception as e:
            fails.append(f'{sid}: {e}')
            if isinstance(old.get(name), dict):
                out['s'][name] = old[name]
    if not got:
        raise RuntimeError('brak serii' + (f' ({fails[0]})' if fails else ''))
    if fails:
        META['errors'].append(mask(f'EIA: {len(fails)} serie bez odpowiedzi, np. {fails[0]}'))
    return out


BLS_URL = 'https://api.bls.gov/publicAPI/v2/timeseries/data/'
BLS_SERIES = (('cpi', 'CUUR0000SA0'),      # inflacja CPI-U (indeks, bez korekty sezonowej — r/r z 12 miesięcy)
              ('core', 'CUUR0000SA0L1E'),  # inflacja bazowa (bez żywności i energii)
              ('unemp', 'LNS14000000'),    # stopa bezrobocia, %, z korektą sezonową
              ('nfp', 'CES0000000001'),    # zatrudnienie poza rolnictwem, tys. osób, z korektą sezonową
              ('ahe', 'CES0500000003'))    # średnia płaca godzinowa w sektorze prywatnym, USD


def post_json(url, obj, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(), headers={'User-Agent': 'CapitalFlowAI-collector/1.0', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8', 'replace'))


def _yoy(rows, k=12):
    """Zmiana % wobec wartości sprzed k miesięcy — tylko gdy obie wartości istnieją (brak nie jest zerem)."""
    m = {d: v for d, v in rows}
    out = []
    for d, v in rows:
        y, mo = int(d[:4]), int(d[5:7]) - k
        while mo < 1:
            y, mo = y - 1, mo + 12
        p = m.get(f'{y:04d}-{mo:02d}')
        if v is not None and p:
            out.append([d, round((v / p - 1) * 100, 1)])
    return out


def build_usa_makro(key, prev=None, today=None):
    """data/usa-makro.json — BLS (miesięcznie): inflacja r/r (CPI i bazowa), bezrobocie, zatrudnienie (zmiana m/m), płace r/r.
    „-” w danych BLS (np. październik 2025 — przerwa w pracy rządu USA) = brak, nigdy 0."""
    y = (today or _now_utc().date()).year
    body = {'seriesid': [s for _, s in BLS_SERIES], 'startyear': str(y - 2), 'endyear': str(y)}
    if key:
        body['registrationkey'] = key
    j = post_json(BLS_URL, body)
    if not isinstance(j, dict) or j.get('status') != 'REQUEST_SUCCEEDED':
        raise RuntimeError(f"{(j or {}).get('status') if isinstance(j, dict) else '?'}: {'; '.join(map(str, (j or {}).get('message') or []))[:160] if isinstance(j, dict) else ''}")
    series = {s.get('seriesID'): s for s in ((j.get('Results') or {}).get('series') or []) if isinstance(s, dict)}
    out = {'at': NOW, 'src': 'U.S. Bureau of Labor Statistics (BLS) — Public Data API v2', 'url': 'https://www.bls.gov/developers/', 's': {}}
    for name, sid in BLS_SERIES:
        rows = {}
        for r in (series.get(sid) or {}).get('data') or []:
            p, yr = str(r.get('period') or ''), str(r.get('year') or '')
            if re.match(r'^M(0[1-9]|1[0-2])$', p) and re.match(r'^\d{4}$', yr):   # M13 = średnia roczna — pomijamy
                rows[f'{yr}-{p[1:]}'] = _num(r.get('value'))
        if rows:
            out['s'][name] = {'id': sid, 'd': [[d, rows[d]] for d in sorted(rows)]}
    if not out['s']:
        raise RuntimeError('brak serii w odpowiedzi')
    for name in ('cpi', 'core', 'ahe'):
        if name in out['s']:
            out['s'][name]['yoy'] = _yoy(out['s'][name]['d'])
    if 'nfp' in out['s']:
        d = out['s']['nfp']['d']
        out['s']['nfp']['chg'] = [[b[0], round(b[1] - a[1], 1)] for a, b in zip(d, d[1:])   # zmiana m/m tylko dla dwóch kolejnych miesięcy z danymi
                                  if a[1] is not None and b[1] is not None and _next_month(a[0]) == b[0]]
    miss = [s for _, s in BLS_SERIES if not (series.get(s) or {}).get('data')]
    if miss:
        META['errors'].append(f'BLS: brak serii {", ".join(miss)}')
    return out


def _next_month(ym):
    y, m = int(ym[:4]), int(ym[5:7]) + 1
    return f'{y + (m - 1) // 12:04d}-{(m - 1) % 12 + 1:02d}'


BEA_URL = 'https://apps.bea.gov/api/data'
BEA_IND = ('BalCurrAcct', 'FinAssetsExclFinDeriv', 'FinLiabsExclFinDeriv', 'NetLendBorrFinAcct')
BEA_AREA_IND = ('FinAssetsExclFinDeriv', 'FinLiabsExclFinDeriv')


def bea_get(key, **params):
    """BEA API → Results; błąd BEA (także „Invalid API UserId”) = wyjątek. Klucz w adresie — komunikaty maskowane (SECRETS)."""
    q = urllib.parse.urlencode({'UserID': key, **params, 'ResultFormat': 'JSON'})
    j = get_json(f'{BEA_URL}?{q}')
    api = (j or {}).get('BEAAPI') if isinstance(j, dict) else None
    if not isinstance(api, dict):
        raise RuntimeError('nieznany kształt odpowiedzi')
    res = api.get('Results')
    err = api.get('Error') or (res.get('Error') if isinstance(res, dict) else None)
    if err:
        raise RuntimeError(str(err.get('APIErrorDescription') if isinstance(err, dict) else err)[:160])
    if isinstance(res, list):
        res = res[0] if res and isinstance(res[0], dict) else {}
    return res if isinstance(res, dict) else {}


def _bea_q(p):
    m = re.match(r'^(\d{4})Q([1-4])$', str(p or ''))
    return f'{m.group(1)}-Q{m.group(2)}' if m else None


def _bea_mln(r):
    """Wartość BEA w mln USD (DataValue z przecinkami × 10^(UNIT_MULT−6)); „(D)”, puste = brak."""
    v = _num(r.get('DataValue'))
    if v is None or v != v or abs(v) == float('inf'):
        return None
    m = _num(r.get('UNIT_MULT'))
    return round(v * 10 ** ((6 if m is None else m) - 6), 1)


def build_bilans_usa(key, prev=None, today=None):
    """data/bilans-usa.json — BEA: bilans płatniczy USA kwartalnie (rachunek bieżący, kapitał z USA za granicę i z zagranicy do USA,
    także według obszarów) oraz realny PKB (zmiana % kw/kw, w skali roku). Uzupełnia TIC (miesięczne transakcje w papierach)."""
    y = (today or _now_utc().date()).year
    years = ','.join(str(x) for x in range(y - 3, y + 1))
    out = {'at': NOW, 'src': 'U.S. Bureau of Economic Analysis (BEA) — Data API (ITA, NIPA T10101)', 'url': 'https://apps.bea.gov/api/',
           'unit': 'mln USD; PKB: % kw/kw w skali roku', 'ita': {}, 'areas': {}, 'names': {}, 'ind': {}, 'gdp': []}
    fails = []
    try:
        have = {v.get('Key'): v.get('Desc') for v in (bea_get(key, method='GetParameterValues', datasetname='ITA', ParameterName='Indicator').get('ParamValue') or [])
                if isinstance(v, dict) and v.get('Key')}
        out['ind'] = {k: have[k] for k in BEA_IND if k in have}
        missing = [k for k in BEA_IND if k not in have]
        if missing and have:
            META['notes'].append('BEA: brak wskaźników ' + ', '.join(missing))
    except Exception as e:
        fails.append(f'lista wskaźników: {e}')
    try:
        out['names'] = {v.get('Key'): v.get('Desc') for v in (bea_get(key, method='GetParameterValues', datasetname='ITA', ParameterName='AreaOrCountry').get('ParamValue') or [])
                        if isinstance(v, dict) and v.get('Key')}
    except Exception as e:
        fails.append(f'lista obszarów: {e}')
    for ind in BEA_IND:
        try:
            d = {}
            for r in bea_get(key, method='GetData', datasetname='ITA', Indicator=ind, AreaOrCountry='AllCountries', Frequency='QSA', Year=years).get('Data') or []:
                q, v = _bea_q(r.get('TimePeriod')), _bea_mln(r) if isinstance(r, dict) else None
                if q and v is not None:
                    d[q] = v
            if d:
                out['ita'][ind] = [[q, d[q]] for q in sorted(d)]
            else:
                fails.append(f'{ind}: brak wartości')
        except Exception as e:
            fails.append(f'{ind}: {e}')
    for ind in BEA_AREA_IND:
        try:
            A = {}
            for r in bea_get(key, method='GetData', datasetname='ITA', Indicator=ind, AreaOrCountry='All', Frequency='QNSA', Year=years).get('Data') or []:
                if not isinstance(r, dict):
                    continue
                a, q, v = str(r.get('AreaOrCountry') or ''), _bea_q(r.get('TimePeriod')), _bea_mln(r)
                if a and q and v is not None:
                    A.setdefault(a, {})[q] = v
            if A:
                out['areas'][ind] = {a: [[q, x[q]] for q in sorted(x)][-12:] for a, x in sorted(A.items())}
            else:
                fails.append(f'{ind} (obszary): brak wartości')
        except Exception as e:
            fails.append(f'{ind} (obszary): {e}')
    try:
        g = {}
        for r in bea_get(key, method='GetData', datasetname='NIPA', TableName='T10101', Frequency='Q', Year=years).get('Data') or []:
            if isinstance(r, dict) and str(r.get('LineNumber')) == '1':
                q, v = _bea_q(r.get('TimePeriod')), _num(r.get('DataValue'))
                if q and v is not None:
                    g[q] = v
        out['gdp'] = [[q, g[q]] for q in sorted(g)]
        if not g:
            fails.append('PKB: brak wartości')
    except Exception as e:
        fails.append(f'PKB: {e}')
    if not out['ita'] and not out['areas'] and not out['gdp']:
        raise RuntimeError('brak danych' + (f' ({fails[0]})' if fails else ''))
    if fails:
        META['errors'].append(mask(f'BEA: {len(fails)} zapytań bez danych, np. {fails[0]}'))
    return out


# ===================== v99: OECD na serwerze =====================
# Indeksy giełdowe (SHARE), rentowności 10-letnie (IRLT) i wskaźnik wyprzedzający (CLI) dla 27 krajów mapy GLOBAL.
# Strona pobierała je dotąd prosto z OECD; OECD ogranicza liczbę zapytań z jednego adresu (HTTP 429) i wtedy mapa
# pokazywała „dane przykładowe”. Teraz: zbieracz co 6 h → data/oecd.json; strona pyta OECD tylko, gdy pliku brak.
# Lista krajów = kody ISO z GREG na stronie (test pilnuje zgodności).
OECD_ISO = ['USA', 'CAN', 'BRA', 'MEX', 'CHL', 'COL', 'DEU', 'FRA', 'GBR', 'ITA', 'ESP', 'NLD', 'CHE', 'SWE', 'POL', 'RUS',
            'SAU', 'TUR', 'ISR', 'ZAF', 'IND', 'CHN', 'JPN', 'KOR', 'IDN', 'AUS', 'NZL']
OECD_BASE = 'https://sdmx.oecd.org/public/rest/data/'
OECD_Q = {   # te same zapytania co na stronie (GSRC.oecd / rate / cli)
    'share': 'OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0/{iso}.M.SHARE.IX._Z._Z._Z._Z.N',
    'irlt': 'OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0/{iso}.M.IRLT.PA._Z._Z._Z._Z.N',
    'cli': 'OECD.SDD.STES,DSD_STES@DF_CLI,4.1/{iso}.M.LI...AA...H',
}
OECD_SLEEP = 4       # odstęp między trzema zapytaniami (limit OECD na adres)
OECD_EVERY = 6 * 60  # dane miesięczne — co 6 h wystarczy
OECD_RETRY = 60      # część z błędem — ponów po godzinie, nie co 20 min


def oecd_start(today=None, months=15):
    """Pierwszy miesiąc zapytania — jak gStart() na stronie: bieżący miesiąc minus 15."""
    d = today or datetime.datetime.now(datetime.timezone.utc).date()
    m = d.year * 12 + (d.month - 1) - months
    return f'{m // 12:04d}-{m % 12 + 1:02d}'


def oecd_parse(j):
    """SDMX-JSON (obserwacje płaskie) → {kraj: [[miesiąc, wartość], …]} rosnąco — ten sam kształt co gOecd() na stronie.
    Brak obserwacji albo wartość nieliczbowa = brak wpisu (nigdy zero)."""
    D = j['data']['structure']['dimensions']['observation']
    obs = j['data']['dataSets'][0].get('observations') or {}
    iA = next(i for i, d in enumerate(D) if d.get('id') == 'REF_AREA')
    iT = next(i for i, d in enumerate(D) if d.get('id') == 'TIME_PERIOD')
    A = [v.get('id') for v in D[iA]['values']]
    T = [v.get('id') for v in D[iT]['values']]
    out = {}
    for k, v in obs.items():
        p = k.split(':')
        x = v[0] if isinstance(v, list) and v else None
        if isinstance(x, bool) or not isinstance(x, (int, float)) or x != x:
            continue
        a, tm = A[int(p[iA])], T[int(p[iT])]
        if a and tm:
            out.setdefault(a, {})[tm] = x
    return {a: [[tm, v] for tm, v in sorted(m.items())] for a, m in sorted(out.items())}


def oecd_get(path, start, _retry=True):
    url = f'{OECD_BASE}{path.format(iso="+".join(OECD_ISO))}?startPeriod={start}&format=jsondata&dimensionAtObservation=AllDimensions'
    try:
        return get_json(url, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 429 and _retry:   # limit zapytań OECD — jedna ponowna próba po przerwie
            time.sleep(20)
            return oecd_get(path, start, _retry=False)
        raise


def build_oecd(prev=None, today=None):
    start = oecd_start(today)
    prev = prev if isinstance(prev, dict) else {}
    pat = prev.get('part_at') if isinstance(prev.get('part_at'), dict) else {}
    out = {'at': NOW, 'start': start, 'iso': OECD_ISO, 'ok': {}, 'part_at': {}}
    fails = []
    for i, (k, path) in enumerate(OECD_Q.items()):
        if i:
            time.sleep(OECD_SLEEP)
        try:
            ser = oecd_parse(oecd_get(path, start))
            if not ser:
                raise ValueError('pusta odpowiedź')
            out[k] = ser; out['ok'][k] = True; out['part_at'][k] = NOW
        except Exception as e:  # noqa — część bez danych: poprzednia wersja tej części (z własnym czasem), nigdy zera
            fails.append(f'{k}: {e}'); out['ok'][k] = False
            if isinstance(prev.get(k), dict) and prev[k]:
                out[k] = prev[k]; out['part_at'][k] = pat.get(k) or prev.get('at')
    if 'share' not in out:
        raise RuntimeError('brak indeksów giełdowych' + (f' ({fails[0]})' if fails else ''))
    if fails:
        META['errors'].append(mask(f'OECD: {"; ".join(fails)}'))
    return out


# ===================== v101: kursy walut i rentowności 10L na serwerze =====================
# Strona pobierała je z przeglądarki przy każdym wejściu (6 zapytań kursów, 2 pliki XML Skarbu USA po ~0,5 MB, Bundesbank).
# Teraz: zbieracz co godzinę → data/rynki.json; strona pyta źródła sama tylko, gdy części pliku brak albo jest za stara.
FX_URL = 'https://api.frankfurter.dev/v1/{d}?from=USD'
UST_URL = 'https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value={y}'
BUBA_URL = 'https://api.statistiken.bundesbank.de/rest/data/BBSIS/D.I.ZAR.ZI.EUR.S1311.B.A604.R10XX.R.A.A._Z._Z.A?startPeriod={f}&format=json'
RYNKI_EVERY = 60     # kursy EBC i rentowności zmieniają się raz dziennie — co godzinę wystarczy
RYNKI_PX = {'fx': 'Frankfurter', 'ust': 'Skarb USA 10L', 'buba': 'Bundesbank 10L'}   # początek komunikatu błędu (strona Źródła)


def months_back(d, n):
    """Ten sam dzień n miesięcy wcześniej (koniec miesiąca przycięty) — jak back(n) na stronie."""
    m = d.year * 12 + (d.month - 1) - n
    y, mo = m // 12, m % 12 + 1
    last = (datetime.date(y + (mo == 12), mo % 12 + 1, 1) - datetime.timedelta(days=1)).day
    return datetime.date(y, mo, min(d.day, last))


def fx_dates(today):
    return {'now': 'latest', '1M': months_back(today, 1).isoformat(), '1Q': months_back(today, 3).isoformat(), '1R': months_back(today, 12).isoformat(),
            '1D': (today - datetime.timedelta(days=1)).isoformat(), '1T': (today - datetime.timedelta(days=7)).isoformat()}


def ust_parse(xml_text):
    """Plik XML Skarbu USA (krzywa rentowności) → [[dzień, rentowność 10L], …] rosnąco — jak gUst() na stronie."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_text)
    loc = lambda e: e.tag.rsplit('}', 1)[-1]
    out = {}
    for ent in root.iter():
        if loc(ent) != 'entry':
            continue
        d = y = None
        for e in ent.iter():
            if loc(e) == 'NEW_DATE':
                d = (e.text or '')[:10]
            elif loc(e) == 'BC_10YEAR':
                y = e.text
        try:
            v = float(y)
        except (TypeError, ValueError):
            continue
        if d and v == v:
            out[d] = v
    return [[k, out[k]] for k in sorted(out)]


def buba_parse(j):
    """SDMX-JSON Bundesbanku → [[dzień, rentowność], …] rosnąco — jak gBuba() na stronie."""
    o = j.get('data') or j
    ser = list(o['dataSets'][0]['series'].values())[0]
    T = [v['id'] for v in o['structure']['dimensions']['observation'][0]['values']]
    out = []
    for k, v in ser['observations'].items():
        try:
            x = float(v[0])
        except (TypeError, ValueError, IndexError):
            continue
        if x == x:
            out.append([T[int(k)], x])
    return sorted(out)


def build_rynki(prev=None, today=None):
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    prev = prev if isinstance(prev, dict) else {}
    pat = prev.get('part_at') if isinstance(prev.get('part_at'), dict) else {}
    out = {'at': NOW, 'ok': {}, 'part_at': {}}

    def part(k, fn):
        try:
            v = fn()
            if not v:
                raise ValueError('pusta odpowiedź')
            out[k] = v; out['ok'][k] = True; out['part_at'][k] = NOW
        except Exception as e:  # noqa — część z błędem: poprzednia wersja z własnym czasem, nigdy zera
            META['errors'].append(mask(f'{RYNKI_PX[k]}: {e}')); out['ok'][k] = False
            if prev.get(k):
                out[k] = prev[k]; out['part_at'][k] = pat.get(k) or prev.get('at')

    def fx():
        r = {}
        for k, d in fx_dates(today).items():
            j = get_json(FX_URL.format(d=d))
            if not isinstance(j, dict) or not isinstance(j.get('rates'), dict) or not j['rates']:
                raise ValueError(f'kursy {k}: brak')
            r[k] = {'amount': j.get('amount'), 'base': j.get('base'), 'date': j.get('date'), 'rates': j['rates']}
        return r

    def ust():
        y = today.year
        old = [r for r in (prev.get('ust') or []) if isinstance(r, list) and str(r[0]).startswith(str(y - 1))]
        if len(old) < 200:   # poprzedni rok zmienia się rzadko — pobierany tylko, gdy w pliku go brak
            old = ust_parse(get(UST_URL.format(y=y - 1), timeout=60)[1])
        cur = ust_parse(get(UST_URL.format(y=y), timeout=60)[1])
        return old + [r for r in cur if not old or r[0] > old[-1][0]]

    def buba():
        return buba_parse(get_json(BUBA_URL.format(f=months_back(today, 13).isoformat()), timeout=60))

    part('fx', fx); part('ust', ust); part('buba', buba)
    if not any(out['ok'].values()):
        raise RuntimeError('żadna część nie odpowiedziała')
    return out


# ===================== v104: dźwignia i pozycje w krypto (Hyperliquid, pliki dzienne Binance, Deribit, OKX — bez klucza) =====================
# v109 (dzwignia2): trzy dalsze giełdy — Kraken Futures (kr), Coinbase International (cb), dYdX (dy) — na tych samych zasadach; suma giełd w historii.
# Plik data/dzwignia.json: siedem części (hl, bn, dr, okx, kr, cb, dy) z własnym ok/part_at; część z błędem = poprzednia wersja tej części
# z jej własnym czasem, nigdy zera. Z serwera GitHub (USA) API Binance i Bybit odpowiadają 451/403 — dlatego Binance tylko
# z plików dziennych (data.binance.vision, poprzedni dzień UTC). Historia dzienna (hist) daje stronie zmianę 1 dn. / 7 dni.
HL_URL = 'https://api.hyperliquid.xyz/info'
BN_URL = 'https://data.binance.vision/data/futures/um/daily/metrics/{s}/{s}-metrics-{d}.zip'
DR_URL = 'https://www.deribit.com/api/v2/public/'
OKX_URL = 'https://www.okx.com/api/v5/'
KR_URL = 'https://futures.kraken.com/derivatives/api/v3/tickers'                # v109: Kraken Futures — jedno zapytanie, wszystkie rynki
CB_URL = 'https://api.international.coinbase.com/api/v1/instruments/{c}-PERP'   # v109: Coinbase International — szczegóły instrumentu (z notowaniem)
DY_URL = 'https://indexer.dydx.trade/v4/perpetualMarkets?ticker={c}-USD'        # v109: dYdX v4 — indexer
LEV_EVERY = 55            # minut — co godzinę (przebieg co 20 min); część z błędem ponawiana przy następnym przebiegu
LEV_TIMEOUT = 20          # s na zapytanie (budżet czasu przebiegu)
LEV_KEEP = ['BTC', 'ETH', 'SOL', 'XRP']
LEV_TOP = 12              # + największe rynki wg otwartych pozycji w USD
LEV_HIST = 90             # dni historii dziennej
LEV_PX = {'hl': 'Hyperliquid', 'bn': 'Binance', 'dr': 'Deribit', 'okx': 'OKX', 'kr': 'Kraken', 'cb': 'Coinbase', 'dy': 'dYdX'}   # początek komunikatu błędu (strona Źródła); v109: +kr, cb, dy
LEV_HOURS = {'kr': 1, 'cb': 1, 'dy': 1}    # v109: okres finansowania wg dokumentacji giełd (godziny): Kraken, Coinbase i dYdX rozliczają co godzinę
LEV_FMAX = 0.005                          # v109: |stawka godzinowa| ponad 0,5 % (4 380 % rocznie) = pomyłka jednostki → brak, nie liczba
LEV_SUMAGE = 6 * 3600                     # v109: do sumy „wszystkie giełdy” wchodzą tylko wiersze młodsze niż 6 h
LEV_LIVE = ['hl', 'okx', 'kr', 'cb', 'dy']
LEV_LIMIT = 45                            # v109.1: sekund na cały przebieg budowniczego — milcząca giełda nie blokuje przebiegu (limit BRIEF < 60 s)
_LEV_TERMIN = [None]                      # koniec budżetu (time.monotonic) ustawiany w build_dzwignia


def lev_tmo():
    """Limit jednego zapytania: nie dłużej niż zostało z budżetu przebiegu; brak czasu = wyjątek (część zostaje z poprzedniego przebiegu)."""
    if _LEV_TERMIN[0] is None:
        return LEV_TIMEOUT
    left = _LEV_TERMIN[0] - time.monotonic()
    if left < 1:
        raise RuntimeError(f'limit czasu przebiegu ({LEV_LIMIT} s)')
    return max(1, min(LEV_TIMEOUT, left))
   # v109: giełdy z bieżącym stanem (Binance = plik z poprzedniego dnia — poza sumą)
KR_SYM = {'BTC': 'PF_XBTUSD', 'ETH': 'PF_ETHUSD'}   # v109: kontrakty wieczyste multi-collateral Kraken (1 kontrakt = 1 moneta)
BN_COLS = {'sum_open_interest': 'oi', 'sum_open_interest_value': 'oi_usd', 'count_long_short_ratio': 'ls',
           'count_toptrader_long_short_ratio': 'top_ls', 'sum_toptrader_long_short_ratio': 'top_pos', 'sum_taker_long_short_vol_ratio': 'taker'}
_LEV_MON = {'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6, 'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12}


def lev_num(v):
    """Liczba z tekstu albo liczby; bool, brak, NaN, nieskończoność → None (brak nigdy nie staje się zerem)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float('inf') else None


def lev_r(v, n):
    return None if v is None else round(v, n)


def _dig(o, path):
    for p in path:
        o = o.get(p) if isinstance(o, dict) else None
    return o if isinstance(o, (int, float)) and not isinstance(o, bool) else None


def hl_post(body, timeout=None):
    """Hyperliquid: jedno wejście POST /info z treścią JSON (w testach podmieniane)."""
    req = urllib.request.Request(HL_URL, data=json.dumps(body).encode('utf-8'),
                                 headers={'User-Agent': 'CapitalFlowAI-collector/1.0', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=(lev_tmo() if timeout is None else timeout)) as r:
        return json.loads(r.read().decode('utf-8', 'replace'))


def hl_parse(j, keep=LEV_KEEP, top=LEV_TOP):
    """[meta, ctxs] → wiersze monet: zawsze `keep` + `top` największych wg otwartych pozycji w USD (kolejność malejąca).
    Wiersz bez liczbowego openInterest albo ceny — odrzucony; brak stawki finansowania → None."""
    if not (isinstance(j, list) and len(j) >= 2 and isinstance(j[0], dict) and isinstance(j[1], list)):
        raise ValueError('nieoczekiwany kształt odpowiedzi')
    uni = j[0].get('universe')
    if not isinstance(uni, list) or len(uni) != len(j[1]):
        raise ValueError('universe i ctxs różnej długości')
    rows = {}
    for u, c in zip(uni, j[1]):
        if not (isinstance(u, dict) and isinstance(c, dict)) or u.get('isDelisted'):
            continue
        name, oi, px = u.get('name'), lev_num(c.get('openInterest')), lev_num(c.get('markPx'))
        if not isinstance(name, str) or not name or oi is None or px is None or px <= 0:
            continue
        fh, prev = lev_num(c.get('funding')), lev_num(c.get('prevDayPx'))
        rows[name] = {'f_h': fh, 'f_y': None if fh is None else lev_r(fh * 24 * 365 * 100, 3), 'oi': lev_r(oi, 4), 'oi_usd': lev_r(oi * px, 0),
                      'px': px, 'd1': None if not prev or prev <= 0 else lev_r((px / prev - 1) * 100, 3), 'vol_usd': lev_r(lev_num(c.get('dayNtlVlm')), 0)}
    if not rows:
        raise ValueError('brak wierszy z liczbami')
    order = sorted(rows, key=lambda k: -rows[k]['oi_usd'])
    sel = [k for k in order if k in order[:top] or k in keep]
    return {'rows': {k: rows[k] for k in sel}, 'top': sel, 'n': len(rows)}


def hl_fund7(coin, now_ms=None):
    """Średnia godzinowa stawka finansowania z 7 dni → % w skali roku; za mało wierszy (< 24) → None."""
    now_ms = now_ms or int(time.time() * 1000)
    j = hl_post({'type': 'fundingHistory', 'coin': coin, 'startTime': now_ms - 7 * 86400 * 1000})
    vals = [lev_num(r.get('fundingRate')) for r in j if isinstance(r, dict)] if isinstance(j, list) else []
    vals = [v for v in vals if v is not None]
    return lev_r(sum(vals) / len(vals) * 24 * 365 * 100, 3) if len(vals) >= 24 else None


def lev_hl():
    out = hl_parse(hl_post({'type': 'metaAndAssetCtxs'}))
    f7 = {}
    for c in ('BTC', 'ETH'):
        if c in out['rows']:
            try:
                f7[c] = hl_fund7(c)
            except Exception as e:  # noqa — średnia 7 dni to dodatek; jej brak nie psuje części (None, nie zero)
                f7[c] = None; META['notes'].append(mask(f'Dźwignia: Hyperliquid, historia finansowania {c}: {e}'))
    out['f7_y'] = f7
    return out


def bn_parse(csv_text):
    """CSV pliku dziennego (wiersze 5-minutowe) → ostatni wiersz dnia i średnia dnia; kolumna bez liczby → None."""
    rows = [r for r in csv.DictReader(io.StringIO(csv_text)) if isinstance(r, dict) and r.get('create_time')]
    if not rows:
        raise ValueError('pusty plik')
    rows.sort(key=lambda r: str(r['create_time']))
    last = rows[-1]
    out = {'t': str(last['create_time']), 'n': len(rows), 'last': {}, 'mean': {}}
    for col, k in BN_COLS.items():
        out['last'][k] = lev_r(lev_num(last.get(col)), 8)
        vals = [v for v in (lev_num(r.get(col)) for r in rows) if v is not None]
        out['mean'][k] = lev_r(sum(vals) / len(vals), 8) if vals else None
    if out['last']['oi'] is None:
        raise ValueError('brak otwartych pozycji w ostatnim wierszu')
    return out


def bn_fetch(sym, day):
    """Plik dzienny (zip z jednym CSV) → wiersze; 404 = pliku jeszcze nie ma (HTTPError do decyzji wyżej)."""
    import zipfile
    data = get_bytes(BN_URL.format(s=sym, d=day), timeout=lev_tmo())
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if n.lower().endswith('.csv')]
        if not names:
            raise ValueError('archiwum bez CSV')
        return bn_parse(z.read(names[0]).decode('utf-8', 'replace'))


def bn_retry_eth(prev, day):
    """Ten sam dzień pliku, ale ETH wtedy zawiodło (None): ponawiamy tylko ETHUSDT; dalej brak → None (zostaje poprzednia część, nie zero)."""
    try:
        eth = bn_fetch('ETHUSDT', day)
    except Exception as e:  # noqa — wciąż brak: poprzednia część, kolejna próba przy następnym przebiegu
        META['errors'].append(mask(f'Dźwignia: Binance ETHUSDT {day}: {e}')); return None
    return dict(prev, ETH=eth)


def lev_bn(prev, today):
    """Poprzedni dzień UTC; gdy pliku jeszcze nie ma (404) — dzień wcześniej (najwyżej 2 próby).
    Dzień już w poprzednim pliku → None (zostaje poprzednia część, bez pobierania); sam brak ETH → ponowienie tylko ETH."""
    prev = prev if isinstance(prev, dict) else {}
    days = [(today - datetime.timedelta(days=i)).isoformat() for i in (1, 2)]
    for day in days:
        if prev.get('day') == day and isinstance(prev.get('BTC'), dict):
            if isinstance(prev.get('ETH'), dict):
                return None
            return bn_retry_eth(prev, day)   # BTC z tego dnia już jest, ETH wtedy zawiodło — ponawiamy tylko ETH
        try:
            btc = bn_fetch('BTCUSDT', day)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise
        out = {'day': day, 'BTC': btc, 'ETH': None}
        try:
            out['ETH'] = bn_fetch('ETHUSDT', day)
        except Exception as e:  # noqa — ETH bez pliku: None, nie zero
            META['errors'].append(mask(f'Dźwignia: Binance ETHUSDT {day}: {e}'))
        return out
    raise ValueError(f'brak plików dziennych ({", ".join(days)}: 404)')


def dr_dvol(cur, now_ms=None):
    """Indeks zmienności DVOL: świece godzinowe z 3 dni → ostatnie zamknięcie i zamknięcie ~24 h wcześniej (±2 h), zmiana w %."""
    now_ms = now_ms or int(time.time() * 1000)
    j = get_json(DR_URL + f'get_volatility_index_data?currency={cur}&start_timestamp={now_ms - 3 * 86400 * 1000}&end_timestamp={now_ms}&resolution=3600', timeout=lev_tmo())
    data = (j.get('result') or {}).get('data') if isinstance(j, dict) else None
    rows = [(int(r[0]), lev_num(r[4])) for r in (data or []) if isinstance(r, list) and len(r) >= 5 and isinstance(r[0], (int, float)) and lev_num(r[4]) is not None]
    if not rows:
        raise ValueError('brak wierszy')
    rows.sort()
    ts, v = rows[-1]
    back = [r for r in rows if abs(r[0] - (ts - 86400000)) <= 2 * 3600000]
    v24 = min(back, key=lambda r: abs(r[0] - (ts - 86400000)))[1] if back else None
    return {'v': v, 'v24': v24, 'd1': lev_r((v / v24 - 1) * 100, 3) if v24 else None,
            't': datetime.datetime.fromtimestamp(ts / 1000, datetime.timezone.utc).replace(microsecond=0).isoformat()}


def dr_exp(name):
    """'BTC-27MAR26-90000-P' → ('2026-03-27', 'P'); inny kształt → (None, None)."""
    p = str(name or '').split('-')
    if len(p) != 4:
        return None, None
    m = re.match(r'^(\d{1,2})([A-Z]{3})(\d{2})$', p[1])
    if not m or m.group(2) not in _LEV_MON:
        return None, None
    try:
        d = datetime.date(2000 + int(m.group(3)), _LEV_MON[m.group(2)], int(m.group(1))).isoformat()
    except ValueError:
        return None, None
    return d, p[3]


def dr_book(cur, top=4):
    """Opcje: suma otwartych pozycji putów i calli (w monetach), put/call, terminy z największymi pozycjami, cena indeksu (mediana)."""
    j = get_json(DR_URL + f'get_book_summary_by_currency?currency={cur}&kind=option', timeout=lev_tmo())
    R = j.get('result') if isinstance(j, dict) else None
    if not isinstance(R, list) or not R:
        raise ValueError('brak instrumentów')
    puts = calls = 0.0; byexp = {}; n = 0; px = []
    for r in R:
        if not isinstance(r, dict):
            continue
        d, kind = dr_exp(r.get('instrument_name')); oi = lev_num(r.get('open_interest'))
        if not d or kind not in ('P', 'C') or oi is None:
            continue
        n += 1
        if kind == 'P':
            puts += oi
        else:
            calls += oi
        byexp[d] = byexp.get(d, 0.0) + oi
        p = lev_num(r.get('estimated_delivery_price'))
        if p:
            px.append(p)
    if not n:
        raise ValueError('brak wierszy z liczbami')
    exp = sorted(byexp.items(), key=lambda x: -x[1])[:top]
    return {'oi': lev_r(puts + calls, 2), 'oi_p': lev_r(puts, 2), 'oi_c': lev_r(calls, 2), 'pc': lev_r(puts / calls, 4) if calls > 0 else None,
            'n': n, 'px': sorted(px)[len(px) // 2] if px else None, 'exp': [[d, lev_r(v, 2)] for d, v in exp], 't': NOW}


def lev_dr(prev=None):
    prev = prev if isinstance(prev, dict) else {}
    now_ms = int(time.time() * 1000)
    out, fails, new = {}, [], 0
    for cur in ('BTC', 'ETH'):
        o = {}
        try:
            o['dvol'] = dr_dvol(cur, now_ms); new += 1
        except Exception as e:  # noqa
            fails.append(f'DVOL {cur}: {e}')
        try:
            o['opt'] = dr_book(cur); new += 1
        except Exception as e:  # noqa
            fails.append(f'opcje {cur}: {e}')
        old = prev.get(cur) if isinstance(prev.get(cur), dict) else {}
        for k in ('dvol', 'opt'):   # podczęść bez nowych danych → poprzednia (ma własny czas 't'), nigdy zera
            if k not in o and isinstance(old.get(k), dict):
                o[k] = old[k]
        if o:
            out[cur] = o
    if not new:   # nic nowego — cała część nieudana (zostaje poprzednia z jej czasem)
        raise ValueError('; '.join(fails) or 'brak danych')
    if fails:
        META['errors'].append(mask('Dźwignia: Deribit: ' + '; '.join(fails)))
    return out


def okx_get(path):
    j = get_json(OKX_URL + path, timeout=lev_tmo())
    if not isinstance(j, dict) or str(j.get('code')) != '0' or not isinstance(j.get('data'), list) or not j['data']:
        raise ValueError(f'{path.split("?")[0]}: {j.get("msg") or "pusta odpowiedź" if isinstance(j, dict) else "zły kształt"}')
    return j['data']


def lev_okx(prev=None):
    """OKX (kontrakty USDT): stawka finansowania (okres z pola nextFundingTime), otwarte pozycje w USD, dzienny stosunek kont długich do krótkich."""
    prev = prev if isinstance(prev, dict) else {}
    out, fails, new = {}, [], 0
    for cur in ('BTC', 'ETH'):
        o = {'t': NOW}
        try:
            d = okx_get(f'public/funding-rate?instId={cur}-USDT-SWAP')[0]
            f = lev_num(d.get('fundingRate'))
            iv = (lev_num(d.get('nextFundingTime')) or 0) - (lev_num(d.get('fundingTime')) or 0)
            hours = iv / 3600000 if 0 < iv <= 86400000 else 8
            o['f'] = f; o['f_hours'] = hours; o['f_y'] = None if f is None else lev_r(f * (24 / hours) * 365 * 100, 3)
        except Exception as e:  # noqa
            fails.append(f'finansowanie {cur}: {e}')
        try:
            d = okx_get(f'public/open-interest?instType=SWAP&instId={cur}-USDT-SWAP')[0]
            o['oi_usd'] = lev_r(lev_num(d.get('oiUsd')), 0); o['oi'] = lev_r(lev_num(d.get('oiCcy')), 4)
        except Exception as e:  # noqa
            fails.append(f'otwarte pozycje {cur}: {e}')
        try:
            d = okx_get(f'rubik/stat/contracts/long-short-account-ratio?ccy={cur}&period=1D')
            rows = [(int(r[0]), lev_num(r[1])) for r in d if isinstance(r, list) and len(r) >= 2 and lev_num(r[1]) is not None and str(r[0]).isdigit()]
            if not rows:
                raise ValueError('brak wierszy')
            ts, v = max(rows)   # najnowszy punkt dzienny; giełda stempluje go o 16:00 UTC (północ UTC+8) — pokazujemy pełny czas
            o['ls'] = v; o['ls_t'] = datetime.datetime.fromtimestamp(ts / 1000, datetime.timezone.utc).replace(microsecond=0).isoformat()
        except Exception as e:  # noqa
            fails.append(f'długie/krótkie {cur}: {e}')
        if len(o) > 1:
            out[cur] = o; new += 1
        elif isinstance(prev.get(cur), dict):
            out[cur] = prev[cur]
    if not new:   # nic nowego — cała część nieudana (zostaje poprzednia z jej czasem)
        raise ValueError('; '.join(fails) or 'brak danych')
    if fails:
        META['errors'].append(mask('Dźwignia: OKX: ' + '; '.join(fails)))
    return out


# --- v109 (dzwignia2): Kraken Futures, Coinbase International, dYdX — te same zasady co wyżej (część z błędem = poprzednia z własnym czasem, brak ≠ zero).
# Okresy finansowania wg dokumentacji giełd: Kraken co godzinę (stawka bezwzględna w USD za 1 kontrakt = 1 moneta; względna = bezwzględna / cena
# znacznikowa), Coinbase co godzinę (pole funding_interval w nanosekundach: 3 600 s), dYdX co godzinę (nextFundingRate za godzinę). Wszystko
# sprowadzone do stawki godzinowej f_h i rocznej f_y (× 24 × 365 × 100), jak Hyperliquid.
def lev_iso(s):
    """Czas ISO z odpowiedzi giełdy ('2026-09-26T07:13:30.453Z') → ISO UTC bez ułamków sekund; inny kształt → None."""
    m = re.match(r'^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:Z|\+00:00)?$', str(s or ''))
    return f'{m.group(1)}T{m.group(2)}+00:00' if m else None


def lev_row(f, hours, oi, px, vol=None, vol_usd=None, t=None):
    """Wspólny wiersz giełdy: stawka `f` za okres `hours` godzin → f_h (godzinowa) i f_y (% w skali roku); oi (monety) × px → oi_usd.
    Brak liczby → None, nigdy zero; zero pozycji albo obrotu = nie do odróżnienia od braku → None; stawka nieprawdopodobna (pomyłka jednostki,
    |f_h| > LEV_FMAX) → None; wiersz bez pozycji i bez stawki → None (moneta pominięta)."""
    fh = None if f is None or not hours or hours <= 0 else f / hours
    if fh is not None and abs(fh) > LEV_FMAX:
        fh = None
    px = px if px is not None and px > 0 else None
    oi = oi if oi is not None and oi > 0 else None
    row = {'f_h': None if fh is None else round(fh, 12), 'f_y': None if fh is None else lev_r(fh * 24 * 365 * 100, 3), 'f_hours': hours,
           'oi': lev_r(oi, 4), 'oi_usd': lev_r(oi * px, 0) if oi is not None and px else None, 'px': px,
           'vol': lev_r(vol, 4) if vol is not None and vol > 0 else None, 'vol_usd': lev_r(vol_usd, 0) if vol_usd is not None and vol_usd > 0 else None, 't': t or NOW}
    return row if row['oi'] is not None or row['f_h'] is not None else None


def kr_parse(j, syms=KR_SYM):
    """Kraken Futures: tickers → wiersze BTC/ETH z kontraktów PF_ (multi-collateral: 1 kontrakt = 1 moneta, otwarte pozycje w monetach).
    fundingRate to stawka bezwzględna (USD za kontrakt za godzinę); godzinowa względna = relativeFundingRate, a gdy giełda go nie podaje — fundingRate / markPrice.
    Rynek zawieszony, wiersz bez symbolu albo bez liczb — pominięty; brak obu monet → błąd części."""
    tick = j.get('tickers') if isinstance(j, dict) else None
    if not isinstance(tick, list) or not tick:
        raise ValueError('brak wierszy tickers')
    by = {r.get('symbol'): r for r in tick if isinstance(r, dict) and isinstance(r.get('symbol'), str)}
    t = lev_iso(j.get('serverTime')) or NOW
    out = {'t': t, 'f_hours': LEV_HOURS['kr']}
    for c, sym in syms.items():
        r = by.get(sym)
        if not isinstance(r, dict) or r.get('suspended') is True:
            continue
        px = lev_num(r.get('markPrice'))
        f = lev_num(r.get('relativeFundingRate'))
        if f is None:
            ab = lev_num(r.get('fundingRate'))
            f = ab / px if ab is not None and px else None
        row = lev_row(f, LEV_HOURS['kr'], lev_num(r.get('openInterest')), px, lev_num(r.get('vol24h')), lev_num(r.get('volumeQuote')), t)
        if row:
            row['sym'] = sym; out[c] = row
    if len(out) <= 2:
        raise ValueError('brak wierszy z liczbami (' + ', '.join(syms.values()) + ')')
    return out


def lev_kr(prev=None):
    """Kraken Futures: jedno zapytanie dla wszystkich rynków; moneta bez wiersza → poprzednia wersja tej monety (z własnym czasem t)."""
    prev = prev if isinstance(prev, dict) else {}
    out = kr_parse(get_json(KR_URL, timeout=lev_tmo()))
    for c, sym in KR_SYM.items():
        if c not in out and isinstance(prev.get(c), dict):
            out[c] = prev[c]; META['errors'].append(mask(f'Dźwignia: Kraken: brak wiersza {sym} — zostaje poprzedni'))
    return out


def cb_parse(j, c):
    """Coinbase International: szczegóły instrumentu {c}-PERP z notowaniem `quote`: otwarte pozycje w monetach, przewidywane finansowanie za okres
    funding_interval (nanosekundy; w dokumentacji i w odpowiedzi 3 600 s = 1 h — wartość poza 0–24 h albo jej brak → 1 h z dokumentacji)."""
    if not isinstance(j, dict):
        raise ValueError('zły kształt')
    q = j.get('quote') if isinstance(j.get('quote'), dict) else {}
    iv = lev_num(j.get('funding_interval'))
    hours = iv / 3.6e12 if iv and 0 < iv / 3.6e12 <= 24 else LEV_HOURS['cb']
    row = lev_row(lev_num(q.get('predicted_funding')), hours, lev_num(j.get('open_interest')), lev_num(q.get('mark_price')),
                  lev_num(j.get('qty_24hr')), lev_num(j.get('notional_24hr')), lev_iso(q.get('timestamp')))
    if not row:
        raise ValueError('brak liczb')
    return row


def lev_cb(prev=None):
    """Coinbase International: po jednym zapytaniu na monetę (szczegóły zawierają notowanie; gdyby nie — osobne zapytanie o notowanie).
    Moneta z błędem → poprzednia wersja tej monety z własnym czasem; nic nowego → część nieudana."""
    prev = prev if isinstance(prev, dict) else {}
    out, fails, new = {'t': NOW, 'f_hours': LEV_HOURS['cb']}, [], 0
    for c in ('BTC', 'ETH'):
        try:
            j = get_json(CB_URL.format(c=c), timeout=lev_tmo())
            if isinstance(j, dict) and not isinstance(j.get('quote'), dict):
                j = dict(j, quote=get_json(CB_URL.format(c=c) + '/quote', timeout=lev_tmo()))
            out[c] = cb_parse(j, c); new += 1
        except Exception as e:  # noqa
            fails.append(f'{c}: {e}')
            if isinstance(prev.get(c), dict):
                out[c] = prev[c]
    if not new:
        raise ValueError('; '.join(fails) or 'brak danych')
    if fails:
        META['errors'].append(mask('Dźwignia: Coinbase: ' + '; '.join(fails)))
    return out


def dy_parse(j, c):
    """dYdX v4 (indexer): perpetualMarkets?ticker={c}-USD → nextFundingRate za godzinę, openInterest w monetach, oraclePrice (USD), volume24H (USD)."""
    m = j.get('markets') if isinstance(j, dict) else None
    r = m.get(f'{c}-USD') if isinstance(m, dict) else None
    if not isinstance(r, dict):
        raise ValueError('brak rynku')
    if str(r.get('status') or 'ACTIVE') != 'ACTIVE':
        raise ValueError(f'rynek {r.get("status")}')
    row = lev_row(lev_num(r.get('nextFundingRate')), LEV_HOURS['dy'], lev_num(r.get('openInterest')), lev_num(r.get('oraclePrice')), None, lev_num(r.get('volume24H')), None)
    if not row:
        raise ValueError('brak liczb')
    return row


def lev_dy(prev=None):
    prev = prev if isinstance(prev, dict) else {}
    out, fails, new = {'t': NOW, 'f_hours': LEV_HOURS['dy']}, [], 0
    for c in ('BTC', 'ETH'):
        try:
            out[c] = dy_parse(get_json(DY_URL.format(c=c), timeout=lev_tmo()), c); new += 1
        except Exception as e:  # noqa
            fails.append(f'{c}: {e}')
            if isinstance(prev.get(c), dict):
                out[c] = prev[c]
    if not new:
        raise ValueError('; '.join(fails) or 'brak danych')
    if fails:
        META['errors'].append(mask('Dźwignia: dYdX: ' + '; '.join(fails)))
    return out


def lev_all(out, now=None):
    """Suma otwartych pozycji w USD z giełd z bieżącym stanem (wiersz młodszy niż LEV_SUMAGE wg własnego czasu `t`, a bez niego — czasu części);
    Binance poza sumą (plik z poprzedniego dnia). Na monetę: usd i posortowana lista giełd — historia porównuje dzień do dnia tylko ten sam zestaw.
    Brak giełdy z bieżącym stanem → moneta pominięta (nie zero)."""
    now = now or datetime.datetime.fromisoformat(NOW)
    pat = out.get('part_at') if isinstance(out.get('part_at'), dict) else {}
    res = {}
    for c in ('BTC', 'ETH'):
        tot, ven = 0.0, []
        for k in LEV_LIVE:
            P = out.get(k)
            if not isinstance(P, dict):
                continue
            r = (P.get('rows') or {}).get(c) if k == 'hl' else P.get(c)
            if not isinstance(r, dict):
                continue
            v = r.get('oi_usd')
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
                continue
            t = r.get('t') if k != 'hl' and isinstance(r.get('t'), str) else pat.get(k)
            try:
                age = (now - datetime.datetime.fromisoformat(str(t))).total_seconds()
            except Exception:
                continue
            if age > LEV_SUMAGE:
                continue
            tot += v; ven.append(k)
        if ven:
            res[c] = {'usd': round(tot), 'v': ','.join(sorted(ven))}
    return res



def lev_hist(hist, out, today):
    """Jeden wpis na dzień UTC (dzisiejszy nadpisywany w ciągu dnia). Do wpisu trafiają tylko części pobrane w tym przebiegu;
    liczby Binance — do dnia pliku, nie do dnia przebiegu. Brak liczby → None; najwyżej LEV_HIST dni."""
    H = {h['d']: h for h in (hist if isinstance(hist, list) else []) if isinstance(h, dict) and isinstance(h.get('d'), str)}
    d = today.isoformat()
    fresh_part = lambda k: out['ok'].get(k) is True and out['part_at'].get(k) == NOW
    row = dict(H.get(d) or {'d': d})
    if fresh_part('hl'):
        for c in ('btc', 'eth'):
            row['hl_' + c] = _dig(out, ('hl', 'rows', c.upper(), 'oi_usd')); row['f_' + c] = _dig(out, ('hl', 'rows', c.upper(), 'f_y'))
    if fresh_part('dr'):
        for c in ('btc', 'eth'):   # tylko świeca z dnia przebiegu (podczęść zachowana z wcześniej ma starszy czas 't')
            dvo = out['dr'].get(c.upper()) if isinstance(out.get('dr'), dict) else None
            dvo = dvo.get('dvol') if isinstance(dvo, dict) else None
            if isinstance(dvo, dict) and _dig(dvo, ('v',)) is not None and str(dvo.get('t') or '')[:10] == d:
                row['dvol_' + c] = dvo['v']
    tot = lev_all(out)   # v109: suma giełd z bieżącym stanem (do 6 h) — do zmiany dziennej; zestaw giełd zapisany, żeby porównywać to samo
    for c in ('btc', 'eth'):
        if c.upper() in tot:
            row['all_' + c] = tot[c.upper()]['usd']; row['all_' + c + '_v'] = tot[c.upper()]['v']
    H[d] = row
    bn = out.get('bn') if out['ok'].get('bn') else None
    if isinstance(bn, dict) and isinstance(bn.get('day'), str):
        r = dict(H.get(bn['day']) or {'d': bn['day']})
        for c in ('btc', 'eth'):
            v = _dig(bn, (c.upper(), 'last', 'oi_usd'))
            if v is not None:
                r['bn_' + c] = v
        H[bn['day']] = r
    keys = sorted(k for k in H if k <= d)
    return [H[k] for k in keys[-LEV_HIST:]]


def build_dzwignia(prev=None, today=None, only=None):
    """only = zbiór części do pobrania (młody plik z częścią z błędem: zdrowe części zostają z własnym czasem); None = wszystkie.
    full_at = czas ostatniej pełnej budowy — harmonogram w main() liczy godzinę od niego, nie od `at` (które odświeża też dobranie części)."""
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    prev = prev if isinstance(prev, dict) else {}
    pat = prev.get('part_at') if isinstance(prev.get('part_at'), dict) else {}
    pok = prev.get('ok') if isinstance(prev.get('ok'), dict) else {}
    out = {'at': NOW, 'ok': {}, 'part_at': {}, 'full_at': (prev.get('full_at') or prev.get('at') or NOW) if only is not None else NOW}
    _LEV_TERMIN[0] = time.monotonic() + LEV_LIMIT   # v109.1: budżet czasu całego budowniczego

    def keep(k):
        if isinstance(prev.get(k), dict) and prev[k]:
            out[k] = prev[k]; out['part_at'][k] = pat.get(k) or prev.get('at')

    def part(k, fn):
        if only is not None and k not in only:   # zdrowa część z młodego pliku — bez pobierania, z własnym czasem
            keep(k); out['ok'][k] = bool(out.get(k)) and pok.get(k) is True; return
        try:
            if _LEV_TERMIN[0] is not None and time.monotonic() >= _LEV_TERMIN[0]:
                raise RuntimeError(f'limit czasu przebiegu ({LEV_LIMIT} s) — część pominięta')
            v = fn()
            if v is None:            # ta sama wersja co poprzednio (np. ten sam dzień pliku Binance) — bez pobierania
                keep(k); out['ok'][k] = bool(out.get(k)); return
            if not v:
                raise ValueError('pusta odpowiedź')
            out[k] = v; out['ok'][k] = True; out['part_at'][k] = NOW
        except Exception as e:  # noqa — część z błędem: poprzednia wersja z własnym czasem, nigdy zera
            META['errors'].append(mask(f'Dźwignia: {LEV_PX[k]}: {e}')); out['ok'][k] = False; keep(k)

    part('hl', lev_hl)
    part('bn', lambda: lev_bn(prev.get('bn'), today))
    part('dr', lambda: lev_dr(prev.get('dr')))
    part('okx', lambda: lev_okx(prev.get('okx')))
    part('kr', lambda: lev_kr(prev.get('kr')))     # v109
    part('cb', lambda: lev_cb(prev.get('cb')))
    part('dy', lambda: lev_dy(prev.get('dy')))
    if not any(out['ok'].values()):
        raise RuntimeError('żadna część nie odpowiedziała')
    out['hist'] = lev_hist(prev.get('hist'), out, today)
    return out


# ===================== v105: wieloryby — portfele giełd na Ethereum (publiczny łańcuch, bez klucza) =====================
# Sami czytamy publiczny węzeł Ethereum (JSON-RPC): salda ETH/USDT/USDC portfeli, które giełdy same ogłosiły (źródło przy
# każdym adresie), i zdarzenia Transfer USDT/USDC do tych portfeli i z nich. Brak odpowiedzi = poprzednia część z własnym
# czasem, nigdy zera. Węzeł główny sprawdzony z runnera USA 26.09.2026 (zakres logów ≤ ~900 bloków; losowe 403 „archive” —
# stąd ponowienia); węzły zapasowe sprawdzone tylko z Polski (ankr wymaga klucza, cloudflare odmawia, llamarpc 403/525).
WH_RPC = 'https://ethereum-rpc.publicnode.com'
WH_RPC_ZAPAS = {'wywolania': ('https://1rpc.io/eth', 25), 'logi': ('https://rpc.flashbots.net', 5)}   # (adres, limit paczki)
WH_BATCH = 40           # paczka żądań do węzła głównego (sprawdzone 62 w jednym żądaniu)
WH_PROBY = 3            # główny dwa razy (odstęp 0,5 s), potem zapas
WH_USDT = '0xdac17f958d2ee523a2206206994597c13d831ec7'
WH_USDC = '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48'
WH_TOKENY = {WH_USDT: 'USDT', WH_USDC: 'USDC'}   # oba po 6 miejsc
WH_TRANSFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
WH_CHAINLINK = '0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419'   # ETH/USD: wyrocznia cenowa na łańcuchu (latestRoundData, 8 miejsc)
WH_CG_PRICE = 'https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd'   # zapas kursu (bez klucza)
WH_PROG = 1_000_000     # transfer ≥ 1 mln USD (USDT/USDC ≈ USD)
WH_CHUNK, WH_CHUNKS, WH_START = 800, 6, 4800   # paczka logów, paczek na przebieg, pierwszy przebieg (≈ 16 h wstecz)
WH_OKNO = 7200          # okno tabeli: ≈ 24 h w blokach (12 s/blok)
WH_MAX = 60             # najwyżej tyle wierszy transferów
WH_HIST_DNI = 120       # historia sald: jeden zrzut na dobę UTC
WH_DOB_DNI = 3          # v117: sumy dobowe przelewów (UTC) trzymane w pliku — archiwum bierze pełną poprzednią dobę; tabela (60 wierszy) jest obcięta
WH_BUDZET = 12          # sekund na skanowanie logów w jednym przebiegu (cały przebieg < 20 s) — reszta w następnym
WH_LIMIT = 40           # sekund na cały przebieg budowniczego: gdy węzeł milczy, nie czekamy dłużej (limit BRIEF: < 60 s)
WH_PARA = 30            # ta sama kwota w obie strony tej samej giełdy w ≤ tylu blokach = najpewniej ruch wewnętrzny (oznaczenie)
WH_TOPICS = 200         # v108: najwyżej tyle portfeli w jednej tablicy tematów eth_getLogs (150 sprawdzone na obu węzłach 26.09.2026); więcej = grupy
# v112: transfery ETH natywne (zwykłe transakcje) — publiczne API eksploratora łańcucha z kluczem właściciela (sekret ETHERSCAN). Węzeł RPC nie ma
# filtra „przelewy ETH ≥ X w zakresie bloków”, więc pytamy portfel po portfelu (txlist) w rotacji: najdłużej niesprawdzane najpierw.
WH_ETH_URL = 'https://api.etherscan.io/v2/api?chainid=1&module=account&action=txlist&address={addr}&startblock={od}&endblock={do}&page=1&offset={n}&sort=asc&apikey={key}'
WH_ETH_PER_RUN = 40     # portfeli na przebieg (154 portfele z ETH ≈ co 4 przebiegi ≈ 80 min); dobowo ≤ 40 × 72 = 2 880 zapytań (limit dostawcy 100 000)
WH_ETH_BUDZET = 18      # sekund na część ETH w jednym przebiegu (salda + logi zajmują ~10 s, całość < WH_LIMIT 40 s); przy 0,4 s odstępu = ~40 portfeli
WH_ETH_OFFSET = 10000   # najwyżej tyle transakcji w jednej odpowiedzi (limit dostawcy); pełna odpowiedź = ciąg dalszy od jej ostatniego bloku
WH_ETH_TEMPO = 0.4      # sekund między zapytaniami (limit planu bezpłatnego dostawcy: 3 zapytania na sekundę — 0,21 s dawało „rate limit reached (3/sec)” 26.09.2026)
WH_ETH_LAG = 5          # bloków za głowicą węzła RPC — indeks eksploratora bywa o chwilę w tyle; skan portfela kończy się na head − WH_ETH_LAG
WH_ETH_BLEDY = 3        # tyle kolejnych błędów (limit, awaria) = koniec części ETH w tym przebiegu; pojedynczy błąd = portfel wraca na początek kolejki
WH_ETH_START = WH_START   # pierwszy skan portfela albo zaległość większa niż tyle bloków: od head − WH_ETH_START + 1 (≈ 16 h), bez udawania ciągłości
WH_GIELDY = {
    'Binance': {'src': 'Binance — wpis „Our Commitment To Transparency” (blog giełdy, listopad 2022): portfele gorące i zimne na Ethereum',
                'url': 'https://www.binance.com/en/blog/community/our-commitment-to-transparency-2895840147147652626', 'since': '2022-11',
                'tokeny': ['USDT', 'USDC', 'ETH'],
                'addr': ['0xbe0eb53f46cd790cd13851d5eff43d12404d33e8', '0xf977814e90da44bfa03b6295a0616a897441acec',
                         '0x5a52e96bacdabb82fd05763e25335261b270efcb', '0x28c6c06298d514db089934071355e5743bf21d60',
                         '0x9696f59e4d72e237be84ffd425dcad154bf96976', '0x21a31ee1afc51d94c2efccaa2092ad1028285549',
                         '0xdfd5293d8e347dfe59e90efd55b2956a1343963d', '0x56eddb7aa87536c09ccc2793473599fd21a8b17f',
                         '0x4976a4a02f38326660d17bf34b431dc6e2eb2327']},
    # OKX ogłasza ~24 000 adresów na Ethereum (plik CSV dowodu rezerw, adresy podpisane „I am an OKX address”); ETH giełdy leży
    # w tysiącach adresów stakingu — nie do odczytu co 20 min z publicznego węzła. USDC jest skupione: 10 największych = 98 %.
    'OKX': {'src': 'OKX — Proof of Reserves, plik adresów por_csv_2026090800_V1 (8.09.2026): 10 największych portfeli USDC w sieci Ethereum (98 % USDC z tej listy)',
            'url': 'https://www.okx.com/proof-of-reserves/download', 'since': '2026-09-08', 'tokeny': ['USDC'],
            'addr': ['0xa073345811e360e9b66f24bc11f3a4bfa924f236', '0x08f619716db7c6245401b543e59acac1d25cb483',
                     '0x6709383e211ab08a6e3270532e756220400424a6', '0xa09fe38187abecc2768b98b80ea30afbc2fbd774',
                     '0x445f16314284b43dfa1fd3cd77b9dea4a1bebd97', '0x87d0d6c8cdd71a658ecf1566f13e4cbc562beaf9',
                     '0x1dfc6bec8499fcb5e3151c7c6d27feb9d7eae1d4', '0xebf6c883a1d60ab38c8ed4780aadbfe4c805ed4f',
                     '0x3425519651e4c42833633fe5c0e24ed89c1023a3', '0x7fea5b4568751533039179116e372e26b6b41b13']},
    # v108: pełne listy portfeli w sieci Ethereum z raportów dowodu rezerw giełd (sprawdzone 26.09.2026): Bybit — tabela „Audited wallets”
    # z raportu audytu z 26.08.2026 (PDF na stronie giełdy; lista styczniowa CSV giełdy pokrywa się w 90 ze 108 adresów — portfele rotują),
    # KuCoin — tabela „Verified wallets” z raportu ze stanem na 31.08.2026 (PDF podlinkowany na stronie Proof of Reserves giełdy),
    # Bitfinex — publiczna lista portfeli giełdy (GitHub bitfinexcom/pub, wallets.txt, 11.2022). Salda wszystkich odczytane 26.09.2026:
    # Bybit 2,34 mld USD (108 portfeli), KuCoin 0,74 mld (33), Bitfinex 0,76 mld (4) — pełne listy, bez wyboru „największych N”.
    'Bybit': {'src': 'Bybit — miesięczny raport dowodu rezerw giełdy (audyt z 26.08.2026, PDF na stronie giełdy): wszystkie 108 portfeli w sieci Ethereum z tabeli „Audited wallets”',
              'url': 'https://www.bybit.com/common-static/cht-static/por/Bybit_PoR_Audit_2026_Aug_26.pdf', 'since': '2026-08-26',
              'tokeny': ['USDT', 'USDC', 'ETH'],
              'addr': ['0x01e2fb8f565d5e3cb9e0e8f0b607a96169b94393', '0x076d55c8998da29531ef7fcac2a01fa21582eed2',
                      '0x0ac92eb5716516a08e7760d314d42e1d5d3c03ae', '0x0e928b196f0ba723de312a7798027b014a7f1055',
                      '0x13f52026493dccf09065952d44101c3e42b41dda', '0x18673311fec54ac2244a602e6d91845553d24e62',
                      '0x187c9fbf5bd0f266883c03f320260c407c7b4100', '0x18e296053cbdf986196903e889b7dca7a73882f6',
                      '0x1c3944173abee256456b1498299fc501ad5bbd6f', '0x223fea5291d43a2fb372c5f332e9ade371ded0b5',
                      '0x25c76fa90e90f5a5a6914da07baed9a9647c3dfd', '0x25c7d768a7d53e6ebe5590c621437126c766e1ea',
                      '0x260b364fe0d3d37e6fd3cda0fa50926a06c54cea', '0x2adcd148c975fc5fac0b11c92ed79637d4f1016c',
                      '0x30ba21597f22aafa4b0e86c250c8a6eebaf0da54', '0x324316a23cd9dc84681e730225559015798567a7',
                      '0x33ae83071432116ae892693b45466949a38ac74c', '0x35696b0847ed8428a098cba726b6514582aa5fc7',
                      '0x35bf33df55472938ad314678962b7c69204f0a8a', '0x371c31f9221459e10565cfe78937cbda5db1791c',
                      '0x3bd0e57e2917d3d9a93f479b3a23b28c3f31a789', '0x3cef1f90be0f15f1573bda7a3e045cca9cff1d15',
                      '0x3db4cb6d753d9e0ba7cc84e576d17dcd01b6b67d', '0x3ef28b7e18c510b8fa031d7f8a1bb24a83ccceb0',
                      '0x412dd3f282b1fa20d3232d86ae060dec644249f6', '0x429b41e5eb73e2266affbc2d7a41553bd8f1ede1',
                      '0x448c642074d7be4c5fc25929bb5536f772cd9d5c', '0x4865d4bcf4ab92e1c9ba5011560e7d4c36f54106',
                      '0x495c70e181c45f80806dcbd733140361a0f75cc2', '0x495eb9345788ee6be50c9c36ed67ffa2beb3699f',
                      '0x4e19698c366f7dcd1cfad4d7f621b4d275bb1a6c', '0x4e5e17e8ef17c9a7ef9798ddf78f3a2c38367d16',
                      '0x4e7292e03703e38ad5dbee25fadbf9e61016b3fe', '0x554e61af48afb53bd28670c3a2aa14ce858e63dd',
                      '0x59800fc68c7039566ed7a04b0f735255093cac1d', '0x61b2aa17c1c1114e7583bb31f777ff4bdc7ab717',
                      '0x6206ae3781f9f1b6fbcf44c7240b1be14f3169ef', '0x62425cd6bdcb6bfe51558ea465b063486b70dc9f',
                      '0x63bee4a7e4aa5d76dc6ab9b9d1852aabb9a40936', '0x651641299c7ec0aa44ad7ed9b7e12702fed2022f',
                      '0x6522b7f9d481eceb96557f44753a4b893f837e90', '0x695f7dea85bf8c0aaafef0a9484e74834e28ce8b',
                      '0x6b9b774502e6afaafcac84f840ac8a0844a1abe3', '0x6bd869be16359f9e26f0608a50497f6ef122ee3e',
                      '0x6f4565c9d673dbdd379aba0b13f8088d1af3bb0c', '0x70167b76543c4a12b49b2f2b70cbf04d99345786',
                      '0x70f58622158d7e609ae5839c4ad0d477f468863f', '0x72187db55473b693ded367983212fe2db3768829',
                      '0x75df67943d35129dd22da5d14fda4983571f553a', '0x77439637cac35bedd63fc9e769d7819ca1d24a48',
                      '0x79ae8c1b31b1e61c4b9d1040217a051f954d4433', '0x7a84c1f1aa344d466b0f161f57b0321b98faf6ee',
                      '0x7b66a51bad1c2fb2b21b42e6ec98e70e891edf24', '0x7c41c7d883dbbe1edfcefca9d7a7592dd30c8b51',
                      '0x801bfd99636ec8961f7e2d2dd0a296d726f5f1ae', '0x80a9b4aab0ad3c73cce1c9223236b722db5d6628',
                      '0x80d45515a84e762c6497a089913f26b41235bf65', '0x8121cf7f01b32a5cc4dc079a5c3127849c85bd6e',
                      '0x855e9a74196764ab41adb3e3b76eaf9e3d6d6d48', '0x869bcee3a0bad2211a65c63ec47dbd3d85a84d68',
                      '0x88a1493366d48225fc3cefbdae9ebb23e323ade3', '0x8a2458f32e5ec9935f20e7c2e06e8d4820f726e5',
                      '0x8c28e696c89200d423f9a5eea7347747e78ed25f', '0x8fa129f87b8a11ee1ca35abd46674f8b66984d4a',
                      '0x922fa922da1b0b28d0af5aa274d7326eaa108c3d', '0x93228d328c9c74c2bfe9f97638bbb5ef322f2bd5',
                      '0x933646d78ede6f1ef5cf4a0a03e3a819c8057922', '0x9814ba501c7ad64bed5ac731d0a4f9a506e18f8a',
                      '0x9cdb59516b37f5c1bd166bc41c5b9f68a57225bd', '0xa0acdf9fa38b293f0bbdd01ca6bf3e7ed8291dd4',
                      '0xa1abfa21f80ecf401bd41365adbb6fef6fefdf09', '0xa4287bc8a021025974a642806cf2ab717b857380',
                      '0xa7a93fd0a276fc1c0197a5b5623ed117786eed06', '0xa9acc15d8b74a01e5605eb0f1e815ba98e3a16af',
                      '0xa9cf4aa55c675badb68519e3cfa8f4be942e6d11', '0xad85405cbb1476825b78a021fa9e543bf7937549',
                      '0xb24692d17babefd97ea2b4ca604a481a7cc2c8ea', '0xb3dc298398877e65ab5125ced5014438bd5c3c4c',
                      '0xb829e684df8e31b402a4d4aedf3bbc18a52e7589', '0xba0553154a8e0fc37d822f042f2cf7b6823c4377',
                      '0xbaed383ede0e5d9d72430661f3285daa77e9439f', '0xbce9aecd3985d4cbb9d273453159a26301fa02ef',
                      '0xc19bb2709321bd6ad6d8396a885b7c151b8d48c5', '0xc22166664e820cda6bf4cedbdbb4fa1e6a84c440',
                      '0xc273a2e3fc4c8f8610ebe51123dc32d233913da7', '0xc63fe58d36bef77a9a98df32a547537f45aac71d',
                      '0xc6c6a48ee8e9f593724161c72414d76e94cda93f', '0xc6ddab48e5966115810974de57a3b367ec893cc1',
                      '0xc93e48d89f2d6dbc1672908aa68ce7c24d0413b4', '0xcab3f132a11e5b723fc20ddab8bb1b858d00a8e8',
                      '0xcbf446565eddf074b2c99e8f1c15582a0bfe6eba', '0xd07e6ab4b75b3da5a96b5e81064a65fc04f98814',
                      '0xd4d2e6ebca6c94dd28a0935ae468012fdda5d35a', '0xd7c4d4b3f076bf9fe391190c42676b4dc269ee02',
                      '0xd860962a96cd471bbe60a83c33e65011d40eb65f', '0xdae4fdcb7fc93738ec6d5b1ea92b7c7f75e4f2f6',
                      '0xdba34cfc849738b2075fd28446902d3f1689c09d', '0xee5b5b923ffce93a870b3104b7ca09c3db80047a',
                      '0xefef30bd1cca520619306c95091ab18473febc5c', '0xf2f40c3bb444288f6f64d8336dcc14dbd929fd94',
                      '0xf42aac93ab142090db9fdc0bc86aab73cb36f173', '0xf440139a62b2b939699c5b3e09f88e40464ab9bc',
                      '0xf833685f98eba1b99947d418c9512d27c8193b1a', '0xf89d7b9c864f589bbf53a82105107622b35eaa40',
                      '0xf8f061cfc030928a4acb8c4980911b4f5afc4002', '0x3f07566d6b5febfbd0b813b857bb388c2bd15569',
                      '0xeb9af6505bdfd2b64848a21ed02f8ccab9144f5d', '0x3dedec283546862d78e7f0707215954e166907bd']},
    'KuCoin': {'src': 'KuCoin — miesięczny raport dowodu rezerw giełdy (stan z 31.08.2026, PDF podlinkowany na stronie Proof of Reserves giełdy): wszystkie 33 portfele w sieci Ethereum z tabeli „Verified wallets”',
               'url': 'https://assets.staticimg.com/cms/media/YKni0XbjiNeiu4woYgDkB3rZ2Uk6rBO4nmihgsvRI.pdf', 'since': '2026-08-31',
               'tokeny': ['USDT', 'USDC', 'ETH'],
               'addr': ['0x061f7937b7b2bc7596539959804f86538b6368dc', '0x0c32131b67a9306a42e5b66f869bc213d40e43f0',
                       '0x1692e170361cefd1eb7240ec13d048fd9af6d667', '0x175ce6204bfda2a509c7e9c786b74407f569c9cc',
                       '0x189b24f3eb15dc71b4fc57c5914e7e9b3246e449', '0x1b14376ee2d46ae5c27a43d902d96d4f3f264b83',
                       '0x2677c4c8757da1857cc7cc4071e0e0dd32ccb975', '0x2933782b5a8d72f2754103d1489614f29bfa4625',
                       '0x37e4d1cd3fe31edf473ebcf3b6a75f419c8839d1', '0x3b6d76719a4ea8c53a7a26b50175b8de23c8e956',
                       '0x41e29c02713929f800419abe5770faa8a5b4dadc', '0x446b86a33e2a438f569b15855189e3da28d027ba',
                       '0x44f1b02d78ed39962600df6440cf8eed3e02a96b', '0x5b234aaab0f61d346a3ef8faca474c1c19f80c1f',
                       '0x651f1d419c548125d7e5456fb61f3df47c29600d', '0x69be413d648ae00f0fbd9856f1355e22b36ee5e0',
                       '0x6d6cc65e2060d0a280fcd47b6c22ec5636797fec', '0x7491f26a0fcb459111b3a1db2fbfc4035d096933',
                       '0x7b915c27a0ed48e2ce726ee40f20b2bf8a88a1b3', '0x83c41363cbee0081dab75cb841fa24f3db46627e',
                       '0x8dac80ce96f69f9762bc450faa4d7fbd5891ae18', '0x9f4cf329f4cf376b7aded854d6054859dd102a2a',
                       '0xa152f8bb749c55e9943a3a0a3111d18ee2b3f94e', '0xaa10db8804d076601999c7cd769e02e44a99d5b2',
                       '0xaa99fc695eb1bbfb359fbad718c7c6dafc03a839', '0xb514c67824443868d3a70352398f524ef6af6207',
                       '0xb8e6d31e7b212b2b7250ee9c26c56cebbfbe6b23', '0xbee64116bd2b1b6373273d01664fbc5532dad06d',
                       '0xd6216fc19db775df9774a6e33526131da7d19a2c', '0xd91efec7e42f80156d1d9f660a69847188950747',
                       '0xdd276dc5223d0120f9bf1776f38957cc8da23cb0', '0xe8c15aad9d4cd3f59c9dfa18828b91a8b2c49596',
                       '0xf16e9b0d03470827a95cdfd0cb8a8a3b46969b91']},
    'Bitfinex': {'src': 'Bitfinex — publiczna lista portfeli giełdy (GitHub bitfinexcom/pub, plik wallets.txt, listopad 2022): portfele gorące i zimne ETH/ERC-20',
                 'url': 'https://github.com/bitfinexcom/pub/blob/main/wallets.txt', 'since': '2022-11',
                 'tokeny': ['USDT', 'USDC', 'ETH'],
                 'addr': ['0x77134cbc06cb00b66f4c7e623d5fdbf6777635ec', '0x742d35cc6634c0532925a3b844bc454e4438f44e',
                         '0xc61b9bb3a7a0767e3179713f3a5c7a9aedce193c', '0x876eabf441b2ee5b5b0554fd502a8e0600950cfa']},
}


def wh_portfele():
    """Lista portfeli z WH_GIELDY (małe litery) — do pliku (źródło przy każdym adresie) i do filtrów."""
    return [{'addr': a.lower(), 'exch': g, 'src': c['src'], 'since': c['since']} for g, c in WH_GIELDY.items() for a in c['addr']]


def wh_hex(v):
    """'0x1a' → 26. None dla braku, dla '0x' (kontrakt bez odpowiedzi) i dla śmieci — brak nie jest zerem."""
    if not isinstance(v, str) or not v.startswith('0x') or len(v) < 3:
        return None
    try:
        return int(v, 16)
    except ValueError:
        return None


def wh_topic(addr):
    """Adres jako 32-bajtowy temat zdarzenia."""
    return '0x' + '0' * 24 + addr[2:].lower()


def wh_iso(s):
    return datetime.datetime.fromtimestamp(int(s), datetime.timezone.utc).replace(microsecond=0).isoformat()


class WhTermin(RuntimeError):
    """Budżet czasu przebiegu wyczerpany — bez dalszych prób i bez zapasu (część zachowuje poprzednią wersję)."""


def wh_tmo(timeout, termin):
    """Limit czasu jednego żądania: nie dłużej niż do terminu (czas monotoniczny). Poniżej sekundy = koniec prób."""
    if termin is None:
        return timeout
    left = termin - time.monotonic()
    if left < 1:
        raise WhTermin(f'budżet czasu przebiegu ({WH_LIMIT} s) wyczerpany')
    return min(timeout, left)


def wh_rpc(calls, kind='wywolania', timeout=20, termin=None):
    """Lista (metoda, parametry) → lista wyników w tej samej kolejności. Jedno żądanie zbiorcze na paczkę; węzeł główny dwa
    razy (losowe 403 „archive”), potem zapas dla tego rodzaju żądań. Każdy element musi mieć wynik — inaczej próba nieudana.
    termin: czas monotoniczny, po którym nie próbujemy dalej — milczący węzeł nie może zatrzymać przebiegu na minuty."""
    zapas, zlimit = WH_RPC_ZAPAS[kind]
    last = None
    for proba in range(WH_PROBY):
        url, lim = (WH_RPC, WH_BATCH) if proba < WH_PROBY - 1 else (zapas, zlimit)
        try:
            out = []
            for i in range(0, len(calls), lim):
                part = calls[i:i + lim]
                body = [{'jsonrpc': '2.0', 'id': k, 'method': m, 'params': p} for k, (m, p) in enumerate(part)]
                r = post_json(url, body, timeout=wh_tmo(timeout, termin))
                if not isinstance(r, list) or len(r) != len(part):
                    raise ValueError(f'odpowiedź {type(r).__name__} ({len(r) if isinstance(r, list) else "?"} z {len(part)})')
                by = {x.get('id'): x for x in r if isinstance(x, dict)}
                for k in range(len(part)):
                    x = by.get(k)
                    if not isinstance(x, dict) or 'result' not in x:
                        err = x.get('error') if isinstance(x, dict) else None
                        raise ValueError(str(err.get('message') if isinstance(err, dict) else err or 'brak wyniku')[:120])
                    out.append(x['result'])
            return out
        except WhTermin as e:
            raise RuntimeError(f'węzeł RPC: {e}' + (f'; ostatni błąd: {last}' if last else ''))
        except Exception as e:  # noqa — następna próba (albo zapas)
            last = e
            if proba < WH_PROBY - 1:
                time.sleep(0.5)
    raise RuntimeError(f'węzeł RPC: {last}')


def wh_zakres(head, ostatni, chunk=WH_CHUNK, chunks=WH_CHUNKS, start=WH_START):
    """Paczki bloków do przeskanowania: od ostatniego zapisanego + 1 do głowicy, ≤ chunk bloków w paczce, ≤ chunks paczek.
    Pierwszy przebieg albo zaległość większa niż start bloków → od head − start + 1 (luka = True: bez udawania ciągłości)."""
    luka = not isinstance(ostatni, int) or head - ostatni > start
    od = head - start + 1 if luka else ostatni + 1
    out = []
    while od <= head and len(out) < chunks:
        do = min(od + chunk - 1, head)
        out.append((od, do)); od = do + 1
    return out, luka


def wh_dekoduj(logs, wmap, prog=WH_PROG):
    """Zdarzenia Transfer USDT/USDC → {klucz: wiersz}. Pomija inne kontrakty, kwoty < prog, zdarzenia bez kwoty i przelewy
    między dwoma portfelami tej samej giełdy (wewnętrzne). Druga strona przelewu nigdy nie jest opisywana."""
    out = {}
    for l in logs if isinstance(logs, list) else []:
        if not isinstance(l, dict):
            continue
        tok = WH_TOKENY.get(str(l.get('address', '')).lower())
        tp = l.get('topics') or []
        if not tok or not isinstance(tp, list) or len(tp) < 3:
            continue
        raw, blk = wh_hex(l.get('data')), wh_hex(l.get('blockNumber'))
        if raw is None or blk is None:
            continue
        amt = raw / 1e6
        if amt < prog:
            continue
        fr, to = '0x' + str(tp[1])[-40:].lower(), '0x' + str(tp[2])[-40:].lower()
        gf, gt = wmap.get(fr), wmap.get(to)
        if gf and gt and gf == gt:
            continue
        tx, li = str(l.get('transactionHash', '')), wh_hex(l.get('logIndex'))
        for d, g in (('out', gf), ('in', gt)):
            if g:
                out[(tx, li, g, d)] = {'t': None, 'token': tok, 'amt': round(amt, 2), 'usd': round(amt, 2), 'dir': d, 'exch': g, 'tx': tx, 'blk': blk, 'li': li}
    return out


def wh_hist(prev, salda, dzien, t, dni=WH_HIST_DNI):
    """Historia sald: jeden zrzut na dobę UTC dla giełdy (pierwszy z dnia zostaje), najwyżej dni dni.
    Wiersz: [dzień, czas bloku, eth, usdt, usdc]. Giełda bez świeżego salda zachowuje swoją historię bez nowego wiersza."""
    prev = prev if isinstance(prev, dict) else {}
    out = {}
    for g, rows in prev.items():
        if isinstance(rows, list):
            out[g] = [r for r in rows if isinstance(r, list) and len(r) >= 5 and isinstance(r[0], str)]
    for g, s in salda.items():
        rows = out.setdefault(g, [])
        if not any(r[0] == dzien for r in rows):
            rows.append([dzien, t, s['eth'], s['usdt'], s['usdc']])
        rows.sort(key=lambda r: r[0])
    return {g: rows[-dni:] for g, rows in out.items()}


def wh_cena(res, now_s):
    """Wynik latestRoundData (5 słów po 32 bajty) → kurs ETH/USD, gdy odczyt jest z ostatniej doby; inaczej None."""
    if not isinstance(res, str) or not res.startswith('0x') or len(res) < 2 + 64 * 5:
        return None
    try:
        ans, upd = int(res[2 + 64:2 + 128], 16), int(res[2 + 64 * 3:2 + 64 * 4], 16)
    except ValueError:
        return None
    if ans <= 0 or upd <= 0 or now_s - upd > 86400 or upd - now_s > 3600:
        return None
    return ans / 1e8


def wh_polacz(prev_rows, new_rows, od, maks=WH_MAX):
    """Wiersze poprzednie i nowe: tylko bloki ≥ od (okno), bez duplikatów, malejąco wg kwoty w USD (v112: ETH i stablecoiny w jednej
    tabeli; wiersz bez pola usd — ze starszego pliku — wg amt), najwyżej maks."""
    seen = {}
    for r in list(prev_rows or []) + list(new_rows or []):
        if not isinstance(r, dict) or not isinstance(r.get('blk'), int) or r['blk'] < od or not isinstance(r.get('amt'), (int, float)):
            continue
        seen[(r.get('tx'), r.get('li'), r.get('exch'), r.get('dir'))] = r
    return sorted(seen.values(), key=lambda r: (-wh_usd(r), -r['blk']))[:maks]


def wh_dobowe(prev, prev_klucze, rows):
    """v117: sumy dobowe (UTC) przelewów ≥ progu per giełda i aktywo, liczone z KAŻDEGO zdekodowanego wiersza zanim tabela zostanie obcięta
    do WH_MAX: {dzień: {giełda: {token: {'in': USD, 'out': USD, 'n': liczba}}}}. Klucze (tx|indeks zdarzenia|giełda|kierunek → dzień) pilnują, by ten sam
    przelew — znaleziony po raz drugi (druga strona przy rotacji portfeli ETH, ponownie zeskanowany blok) — nie był liczony dwa razy.
    Zwraca (sumy, klucze) z ostatnich WH_DOB_DNI dni; nic nie jest zerem z braku obserwacji — dzień bez wpisu = brak sum."""
    dob = {d: v for d, v in prev.items() if isinstance(d, str) and isinstance(v, dict)} if isinstance(prev, dict) else {}
    kl = {k: d for k, d in prev_klucze.items() if isinstance(k, str) and isinstance(d, str)} if isinstance(prev_klucze, dict) else {}
    for r in rows:
        if not isinstance(r, dict) or not isinstance(r.get('t'), str) or r.get('dir') not in ('in', 'out') or not isinstance(r.get('amt'), (int, float)):
            continue
        k = f"{r.get('tx')}|{r.get('li')}|{r.get('exch')}|{r['dir']}"   # v118.1: z indeksem zdarzenia — kilka przelewów w jednej transakcji to osobne wiersze
        if k in kl:
            continue
        d = r['t'][:10]; kl[k] = d
        s = dob.setdefault(d, {}).setdefault(str(r.get('exch')), {}).setdefault(str(r.get('token')), {'in': 0.0, 'out': 0.0, 'n': 0})
        s[r['dir']] = round(s[r['dir']] + wh_usd(r), 2); s['n'] += 1
    dni = sorted(dob)[-WH_DOB_DNI:]
    dob = {d: dob[d] for d in dni}
    if dni:
        kl = {k: d for k, d in kl.items() if d >= dni[0]}
    return dob, kl


def wh_pary(rows, bloki=WH_PARA):
    """Ta sama kwota tego samego tokena w obie strony tej samej giełdy w odstępie ≤ bloki bloków = najpewniej ruch wewnętrzny
    przez nieogłoszony portfel giełdy. Wiersze zostają (przejrzystość), ale dostają wew=True — strona pokazuje to przy kierunku.
    Raz nadane oznaczenie zostaje, gdy druga strona pary wypadnie z okna. Zwraca tę samą listę."""
    outs = [r for r in rows if isinstance(r, dict) and r.get('dir') == 'out']
    for r in rows:
        if not isinstance(r, dict) or r.get('dir') != 'in':
            continue
        for o in outs:
            if (o.get('exch') == r.get('exch') and o.get('token') == r.get('token') and o.get('amt') == r.get('amt')
                    and isinstance(o.get('blk'), int) and isinstance(r.get('blk'), int) and abs(o['blk'] - r['blk']) <= bloki):
                r['wew'] = o['wew'] = True
    return rows


def wh_logi(f, tw, maks=None):
    """v108: zapytania o zdarzenia Transfer dla paczki bloków f — po dwa (do portfeli, z portfeli) na każdą grupę ≤ maks tematów.
    Koszt skanu nie rośnie z liczbą portfeli (tablica tematów), ale bardzo długą tablicę węzeł mógłby odrzucić — stąd grupy."""
    maks = maks or WH_TOPICS   # stała czytana przy wywołaniu (testy ją podmieniają)
    out = []
    for i in range(0, len(tw), maks):
        g = tw[i:i + maks]
        out += [('eth_getLogs', [dict(f, topics=[WH_TRANSFER, None, g])]), ('eth_getLogs', [dict(f, topics=[WH_TRANSFER, g])])]
    return out


def wh_usd(r):
    """Kwota wiersza w USD: pole usd (v112), a bez niego amt (USDT/USDC ≈ USD w starszych plikach)."""
    u = r.get('usd') if isinstance(r, dict) else None
    return u if isinstance(u, (int, float)) and not isinstance(u, bool) else r['amt']


def wh_eth_portfele(W):
    """Portfele giełd, dla których lista giełdy obejmuje ETH (OKX — tylko USDC — pominięty)."""
    return [w for w in W if 'ETH' in WH_GIELDY.get(w['exch'], {}).get('tokeny', ())]


def wh_eth_kolejka(cands, scan, n=None):
    """Kolejka rotacji: portfele bez odczytu najpierw, potem od najdawniej sprawdzanych (najniższy blok); stabilnie wg adresu."""
    n = WH_ETH_PER_RUN if n is None else n
    return sorted(cands, key=lambda w: (scan.get(w['addr'], -1), w['addr']))[:n]


def wh_eth_dekoduj(txs, wmap, px, prog=WH_PROG):
    """Zwykłe transakcje portfela (txlist) → {klucz: wiersz} dla ETH wartego ≥ prog USD po kursie px. Pomija transakcje nieudane,
    bez kwoty i przelewy między dwoma portfelami tej samej giełdy; przelew między giełdami = dwa wiersze (out/in). Kwota w ETH
    (4 miejsca) i w USD po kursie z chwili odczytu; czas z bloku (timeStamp). Druga strona przelewu nigdy nie jest opisywana."""
    out = {}
    if not isinstance(px, (int, float)) or px <= 0:
        return out
    for x in txs if isinstance(txs, list) else []:
        if not isinstance(x, dict) or str(x.get('isError', '0')) != '0':
            continue
        try:
            wei, blk, ts = int(x.get('value')), int(x.get('blockNumber')), int(x.get('timeStamp'))
        except (TypeError, ValueError):
            continue
        eth = wei / 1e18
        usd = eth * px
        if usd < prog:
            continue
        fr, to = str(x.get('from', '')).lower(), str(x.get('to', '')).lower()
        gf, gt = wmap.get(fr), wmap.get(to)
        if gf and gt and gf == gt:
            continue
        tx = str(x.get('hash', ''))
        for d, g in (('out', gf), ('in', gt)):
            if g:
                out[(tx, None, g, d)] = {'t': wh_iso(ts), 'token': 'ETH', 'amt': round(eth, 4), 'usd': round(usd, 2), 'dir': d, 'exch': g, 'tx': tx, 'blk': blk, 'li': None}
    return out


def wh_eth(key, W, wmap, head, px, prev, termin=None, budzet=None):
    """Część ETH jednego przebiegu: ≤ WH_ETH_PER_RUN portfeli z kolejki rotacji, każdy od swojego ostatniego bloku + 1 (pierwszy raz albo
    zaległość > WH_ETH_START bloków: od head − WH_ETH_START + 1) do head − WH_ETH_LAG. Zwraca (wiersze, stan skanu, sprawdzone, błędy, przerwane).
    Pojedynczy błąd nie rusza stanu portfela (wraca na początek kolejki); WH_ETH_BLEDY kolejnych błędów = przerwane."""
    t0 = time.monotonic()
    ps = prev.get('eth_scan') if isinstance(prev, dict) else None
    scan = {a: b for a, b in (ps.items() if isinstance(ps, dict) else ()) if isinstance(a, str) and isinstance(b, int) and not isinstance(b, bool)}
    do = head - WH_ETH_LAG
    rows, errs, n, zle, przerwane = {}, [], 0, 0, False
    for w in wh_eth_kolejka(wh_eth_portfele(W), scan):
        if time.monotonic() - t0 > (WH_ETH_BUDZET if budzet is None else budzet) or (termin is not None and termin - time.monotonic() < 2):
            break
        a, last = w['addr'], scan.get(w['addr'])
        od = last + 1 if isinstance(last, int) and do - last <= WH_ETH_START else do - WH_ETH_START + 1
        if od > do:   # sprawdzony przed chwilą — nic nowego
            n += 1
            continue
        if n or errs:
            time.sleep(WH_ETH_TEMPO)
        try:
            j = get_json(WH_ETH_URL.format(addr=a, od=od, do=do, n=WH_ETH_OFFSET, key=key), timeout=wh_tmo(10, termin))
            st, res = (str(j.get('status')), j.get('result')) if isinstance(j, dict) else ('?', None)
            if st == '1' and isinstance(res, list):
                pass
            elif st == '0' and (res == [] or str(j.get('message', '')).lower().startswith('no transactions')):
                res = []
            else:
                raise ValueError(str(res if isinstance(res, str) else (j.get('message') if isinstance(j, dict) else None) or 'zła odpowiedź')[:100])
        except WhTermin as e:
            errs.append(f'{w["exch"]} {a[:10]}: {e}'); przerwane = True
            break
        except Exception as e:  # noqa — ten portfel bez zmiany stanu (najbliższy przebieg zaczyna od niego)
            zle += 1; errs.append(f'{w["exch"]} {a[:10]}: {e}')
            if zle >= WH_ETH_BLEDY:
                przerwane = True
                break
            continue
        zle = 0; n += 1
        rows.update(wh_eth_dekoduj(res, wmap, px))
        if len(res) >= WH_ETH_OFFSET:   # pełna odpowiedź = mogło być więcej: ciąg dalszy od ostatniego zwróconego bloku (ten blok raz jeszcze)
            bl = [int(x['blockNumber']) for x in res if isinstance(x, dict) and str(x.get('blockNumber', '')).isdigit()]
            scan[a] = max(bl) - 1 if bl else od - 1
        else:
            scan[a] = do
    return rows, scan, n, errs, przerwane


def build_wieloryby(prev=None, eth_key=None):
    """data/wieloryby.json — salda ETH/USDT/USDC ogłoszonych portfeli giełd (co przebieg, jedno żądanie zbiorcze), historia
    dobowa i duże transfery USDT/USDC (skan zdarzeń od ostatniego zapisanego bloku). Każda część osobno: błąd = poprzednia
    wersja tej części z własnym czasem; brak kursu ETH = suma w USD None, nigdy zero.
    v112: z kluczem eth_key także transfery ETH natywne (część 'eth'): zwykłe transakcje portfeli z publicznego API eksploratora,
    portfele w rotacji (stan eth_scan: adres → ostatni sprawdzony blok), w tej samej tabeli transfery (sortowanie wg USD)."""
    t0 = time.monotonic(); termin = t0 + WH_LIMIT   # milczący węzeł: łącznie nie dłużej niż WH_LIMIT s, potem poprzednie części
    prev = prev if isinstance(prev, dict) else {}
    pat = prev.get('part_at') if isinstance(prev.get('part_at'), dict) else {}
    W = wh_portfele(); wmap = {w['addr']: w['exch'] for w in W}
    out = {'at': NOW, 'src': 'Publiczny łańcuch Ethereum (JSON-RPC, węzeł publiczny) — odczyt własny sald i zdarzeń Transfer; kurs ETH/USD z wyroczni na łańcuchu'
                            + ('; transfery ETH natywne: publiczne API eksploratora łańcucha (klucz właściciela), portfele w rotacji' if eth_key else ''),
           'rpc': WH_RPC, 'ok': {}, 'part_at': {}, 'wallets': W,
           'gieldy': {g: {'n': len(c['addr']), 'src': c['src'], 'url': c['url'], 'since': c['since'], 'tokeny': c['tokeny']} for g, c in WH_GIELDY.items()},
           'prog': WH_PROG, 'eth_usd': None, 'eth_usd_at': None,
           'dobowe': prev.get('dobowe') if isinstance(prev.get('dobowe'), dict) else {}, 'dobowe_klucze': prev.get('dobowe_klucze') if isinstance(prev.get('dobowe_klucze'), dict) else {},
           'dobowe_od': prev.get('dobowe_od') if isinstance(prev.get('dobowe_od'), str) else NOW}   # v117: sumy dobowe + od kiedy są zbierane (pierwsza doba jest niepełna)
    errs = []

    def keep(k):   # część z błędem: poprzednia wersja z własnym czasem
        out['ok'][k] = False
        if prev.get(k) is not None:
            out[k] = prev[k]; out['part_at'][k] = pat.get(k) or prev.get('at')

    head = head_t = None
    # --- salda + blok „latest” + kurs ETH: jedno żądanie zbiorcze (2 + 3 × liczba portfeli wywołań)
    try:
        calls = [('eth_getBlockByNumber', ['latest', False]), ('eth_call', [{'to': WH_CHAINLINK, 'data': '0xfeaf968c'}, 'latest'])]
        for w in W:
            a = w['addr']
            calls += [('eth_getBalance', [a, 'latest']), ('eth_call', [{'to': WH_USDT, 'data': '0x70a08231' + '0' * 24 + a[2:]}, 'latest']),
                      ('eth_call', [{'to': WH_USDC, 'data': '0x70a08231' + '0' * 24 + a[2:]}, 'latest'])]
        res = wh_rpc(calls, termin=termin)
        blk = res[0] if isinstance(res[0], dict) else {}
        head, head_t = wh_hex(blk.get('number')), wh_hex(blk.get('timestamp'))
        if head is None or head_t is None:
            raise ValueError('brak bloku „latest”')
        out['blk'], out['blk_t'] = head, wh_iso(head_t)
        px = wh_cena(res[1], head_t)
        if px is None:   # zapas: kurs z publicznego API bez klucza; bez kursu suma w USD = None
            try:
                j = get_json(WH_CG_PRICE, timeout=min(10, max(1, termin - time.monotonic()))); px = float(j['ethereum']['usd'])
                if not px > 0:
                    raise ValueError('kurs ≤ 0')
            except Exception as e:  # noqa
                px = None; errs.append(f'kurs ETH/USD: {e}')
        out['eth_usd'], out['eth_usd_at'] = px, (out['blk_t'] if px else None); out['ok']['cena'] = px is not None
        salda, bad = {}, []
        for g, c in WH_GIELDY.items():
            s, ok = {'eth': 0.0, 'usdt': 0.0, 'usdc': 0.0}, True
            for i, w in enumerate(W):
                if w['exch'] != g:
                    continue
                e, u, d = wh_hex(res[2 + 3 * i]), wh_hex(res[3 + 3 * i]), wh_hex(res[4 + 3 * i])
                if e is None or u is None or d is None:   # portfel bez odpowiedzi = giełda bez nowego salda (suma częściowa byłaby fałszem)
                    ok = False; break
                s['eth'] += e / 1e18; s['usdt'] += u / 1e6; s['usdc'] += d / 1e6
            if not ok:
                bad.append(g); continue
            s = {'eth': round(s['eth'], 6), 'usdt': round(s['usdt'], 2), 'usdc': round(s['usdc'], 2)}
            s['usd'] = round(s['eth'] * px + s['usdt'] + s['usdc'], 2) if px else None
            s['blk'], s['t'], s['n'] = head, out['blk_t'], len(c['addr'])
            salda[g] = s
        if not salda:
            raise ValueError('żaden portfel nie odpowiedział')
        out['hist'] = wh_hist(prev.get('hist'), salda, out['blk_t'][:10], out['blk_t'])
        ps = prev.get('salda') if isinstance(prev.get('salda'), dict) else {}
        for g in bad:   # giełda bez odpowiedzi: poprzednie saldo z własnym czasem (pole t), nigdy zero
            errs.append(f'salda {g}: brak odpowiedzi węzła')
            if isinstance(ps.get(g), dict):
                salda[g] = ps[g]
        out['salda'] = salda; out['ok']['salda'] = not bad; out['part_at']['salda'] = NOW
    except Exception as e:  # noqa
        errs.append(f'salda: {e}'); keep('salda'); out['ok'].setdefault('cena', False)
        if prev.get('hist') is not None:
            out['hist'] = prev['hist']
        if prev.get('salda') is not None:   # poprzednie sumy w USD liczono poprzednim kursem — jego wartość i data zostają przy nich
            out['eth_usd'], out['eth_usd_at'] = prev.get('eth_usd'), prev.get('eth_usd_at')
    # --- transfery: skan zdarzeń Transfer USDT/USDC od ostatniego zapisanego bloku (paczki ≤ 800 bloków, ≤ 6 na przebieg)
    try:
        if head is None:   # salda bez głowicy (np. 403 na paczce): jedno małe żądanie — w ramach tego samego terminu
            b = wh_rpc([('eth_getBlockByNumber', ['latest', False])], termin=termin)[0]
            head, head_t = wh_hex((b or {}).get('number') if isinstance(b, dict) else None), wh_hex((b or {}).get('timestamp') if isinstance(b, dict) else None)
            if head is None or head_t is None:
                raise ValueError('brak bloku „latest”')
        ostatni = prev.get('ostatni_blok') if isinstance(prev.get('ostatni_blok'), int) else None
        paczki, luka = wh_zakres(head, ostatni)
        tw = [wh_topic(w['addr']) for w in W]
        rows, done, err, n = {}, (None if luka else ostatni), None, 0
        for a, b in paczki:
            if n and time.monotonic() - t0 > WH_BUDZET:   # reszta w następnym przebiegu
                break
            try:
                f = {'fromBlock': hex(a), 'toBlock': hex(b), 'address': [WH_USDT, WH_USDC]}
                r = wh_rpc(wh_logi(f, tw), kind='logi', termin=termin)   # v108: grupy tematów (przy ≤ WH_TOPICS portfeli — jedna)
            except Exception as e:  # noqa — postęp do tej paczki zostaje, reszta w następnym przebiegu
                err = str(e); break
            if not all(isinstance(x, list) for x in r):   # null zamiast listy = brak odpowiedzi, nie „brak zdarzeń” — paczka nieudana
                err = f'logi bloków {a}–{b}: wynik nie jest listą'; break
            rows.update(wh_dekoduj([l for x in r for l in x], wmap))
            done, n = b, n + 1
        if done is None or (err and not n):   # nic nie zeskanowano — poprzednia część w całości, z własnym czasem
            raise ValueError(err or 'brak zeskanowanych bloków')
        pocz = prev['okno_od'] if (not luka and isinstance(prev.get('okno_od'), int)) else (paczki[0][0] if paczki else done - WH_OKNO + 1)
        od = max(done - WH_OKNO + 1, pocz)
        prev_rows = [r for r in (prev.get('transfery') or []) if isinstance(r, dict)]
        znane = {r['blk']: r['t'] for r in prev_rows if isinstance(r.get('blk'), int) and isinstance(r.get('t'), str)}
        znane[head] = wh_iso(head_t)
        for k in ('ostatni', 'okno_od'):
            if isinstance(prev.get(k + '_blok' if k == 'ostatni' else k), int) and isinstance(prev.get(k + '_t'), str):
                znane[prev[k + '_blok' if k == 'ostatni' else k]] = prev[k + '_t']
        need = sorted(({r['blk'] for r in rows.values()} | {done, od}) - set(znane))
        if need:
            bl = wh_rpc([('eth_getBlockByNumber', [hex(b), False]) for b in need], termin=termin)
            for b, x in zip(need, bl):
                ts = wh_hex(x.get('timestamp')) if isinstance(x, dict) else None
                if ts is None:
                    raise ValueError(f'brak czasu bloku {b}')
                znane[b] = wh_iso(ts)
        for r in rows.values():
            r['t'] = znane[r['blk']]
        out['dobowe'], out['dobowe_klucze'] = wh_dobowe(out['dobowe'], out['dobowe_klucze'], rows.values())   # v117: sumy dobowe z pełnej listy
        out['transfery'] = wh_pary(wh_polacz(prev_rows, rows.values(), od))   # pary „ta sama kwota w obie strony” = oznaczenie wew
        out['ostatni_blok'], out['ostatni_t'] = done, znane[done]
        out['okno_od'], out['okno_od_t'], out['okno'] = od, znane[od], done - od + 1
        out['luka'] = bool(luka and ostatni is not None)   # przerwa w obserwacji (zaległość pominięta)
        out['part_at']['transfery'] = NOW; out['ok']['transfery'] = err is None
        if err:
            errs.append(f'transfery: {err} (zeskanowano do bloku {done})')
        elif done < head:
            META['notes'].append(f'Wieloryby: skan do bloku {done} z {head} (budżet czasu) — reszta w następnym przebiegu')
    except Exception as e:  # noqa
        errs.append(f'transfery: {e}'); keep('transfery')
        for k in ('ostatni_blok', 'ostatni_t', 'okno_od', 'okno_od_t', 'okno', 'luka'):
            if prev.get(k) is not None:
                out[k] = prev[k]
    # --- v112: transfery ETH natywne (zwykłe transakcje) z publicznego API eksploratora — klucz właściciela; portfele w rotacji; własna część
    if eth_key:
        cands = wh_eth_portfele(W)
        try:
            if head is None:
                raise ValueError('brak bloku „latest”')
            px = out.get('eth_usd')
            if not isinstance(px, (int, float)) or not px > 0:
                raise ValueError('brak kursu ETH — próg w USD nieprzeliczalny')
            ers, scan, n, eerrs, przerwane = wh_eth(eth_key, W, wmap, head, px, prev, termin=termin)
            if not n:   # nic nie sprawdzono w tym przebiegu — część bez nowego stanu, z własnym czasem
                raise ValueError(eerrs[0] if eerrs else 'brak czasu na część ETH')
            od = out['okno_od'] if isinstance(out.get('okno_od'), int) else head - WH_OKNO + 1
            out['dobowe'], out['dobowe_klucze'] = wh_dobowe(out['dobowe'], out['dobowe_klucze'], ers.values())   # v117
            out['transfery'] = wh_pary(wh_polacz([r for r in (out.get('transfery') or []) if isinstance(r, dict)], ers.values(), od))
            out['eth_scan'] = scan
            done = [scan[w['addr']] for w in cands if w['addr'] in scan]
            out['eth'] = {'n': len(done), 'total': len(cands), 'sprawdzono': n, 'wiersze': len(ers), 'per_run': WH_ETH_PER_RUN,
                          'lag_min': int((head - min(done)) * 12 // 60) if done else None}   # ≈ 12 s na blok — orientacyjnie
            out['part_at']['eth'] = NOW; out['ok']['eth'] = not przerwane
            if eerrs:
                (errs if przerwane else META['notes']).append(mask(('ETH: ' if przerwane else 'Wieloryby ETH: ') + '; '.join(eerrs)[:240]))   # v117: także notatka maskowana
        except Exception as e:  # noqa
            errs.append(f'ETH: {e}'); out['ok']['eth'] = False
            for k in ('eth_scan', 'eth'):
                if prev.get(k) is not None:
                    out[k] = prev[k]
            if prev.get('eth') is not None:
                out['part_at']['eth'] = pat.get('eth') or prev.get('at')
    if not any(out['ok'].get(k) for k in ('salda', 'transfery')):
        raise RuntimeError('żadna część nie odpowiedziała' + (f' ({errs[0]})' if errs else ''))
    if errs:
        META['errors'].append(mask('Wieloryby: ' + '; '.join(errs)))
    return out


# ===================== v106: INDEKSY GIEŁDOWE ŚWIATA I NOTOWANIA ETF (klucze właściciela: EODHD, Massive, Tiingo) =====================
# Decyzja właściciela 26.09.2026: wszystkie jego klucze pracują dla strony publicznej. Klucze wyłącznie z GitHub Secrets
# (EODHD_KEY, MASSIVE_KEY, TIINGO_KEY; FMP_KEY i ALPHAVANTAGE_KEY są odczytywane i maskowane, ale jeszcze nieużywane — RAPORT v106).
# Plan bezpłatny EODHD: 20 zapytań na dobę i rok historii — 23 indeksy odświeżane rotacyjnie (FTSE 100 i FTSE MIB poza planem, v118.2) (najdłużej czekające najpierw),
# każdy najwyżej raz po zamknięciu swojej sesji. Nazwy dostawców zostają w tym pliku i na stronie Źródła — nie w panelu.
IX_URL = 'https://eodhd.com/api/eod/{sym}.INDX?api_token={key}&fmt=json&period=d&from={frm}'
IX_SYMBOLS = (   # kod EODHD (giełda INDX), kraj (flaga), godzina UTC, po której zamknięcie sesji powinno już być u dostawcy
    ('GSPC', 'us', 22), ('IXIC', 'us', 22), ('DJI', 'us', 22), ('GSPTSE', 'ca', 22), ('BVSP', 'br', 22), ('MXX', 'mx', 22),
    ('GDAXI', 'de', 17), ('FCHI', 'fr', 17), ('IBEX', 'es', 17), ('AEX', 'nl', 17),   # v118.2: FTSE 100 i FTSE MIB (grupa LSE) poza planem — sonda 26.09: HTTP 200, pusta lista
    ('SSMI', 'ch', 17), ('OMXS30', 'se', 17), ('WIG20', 'pl', 17), ('TA125', 'il', 16), ('XU100', 'tr', 16), ('JTOPI', 'za', 16),
    ('N225', 'jp', 7), ('KS11', 'kr', 7), ('HSI', 'hk', 9), ('SSEC', 'cn', 8), ('BSESN', 'in', 11), ('AXJO', 'au', 7), ('JKSE', 'id', 10))
IX_KEYS = ('EODHD_KEY', 'MASSIVE_KEY', 'TIINGO_KEY', 'FMP_KEY', 'ALPHAVANTAGE_KEY')
IX_ACTIVE = ('EODHD_KEY', 'MASSIVE_KEY', 'TIINGO_KEY', 'FMP_KEY')   # klucze, które dziś coś pobierają (v119: FMP — FTSE 100)
IX_PARTS = ('ix', 'etf')
IX_DAILY = 20        # limit planu bezpłatnego EODHD: zapytań na dobę (liczone według daty UTC)
IX_PER_RUN = 4       # najwyżej tyle indeksów na przebieg — budżet dobowy rozłożony na cały dzień, nie zużyty o świcie
IX_KEEP = 265        # sesji w pliku: ponad rok (zmiana od początku roku wymaga ostatniego zamknięcia poprzedniego roku)
IX_EVERY = 55        # min — zbieracz zajmuje się indeksami najwyżej raz na godzinę
IX_BAD_DAYS = 7      # najdłuższa przerwa po odrzuconym kodzie (HTTP 401/403/404); przerwa rośnie: 1, 2, 4, 7 dni (bad_n)
IX_HIST_DAYS = 370   # pierwsze pobranie: rok wstecz; potem dopełnienie od ostatniej sesji (z zakładką IX_OVERLAP dni)
IX_OVERLAP = 10
IX_TIMEOUT = 12      # s na jedno zapytanie EODHD (kilkadziesiąt KB); Massive 15 s (ok. 1,5 MB), Tiingo 10 s
ETF_TIMEOUT = 15
TIINGO_TIMEOUT = 10
IX_BUDGET_S = 40     # s na cały budowniczy: po tym czasie żadnego nowego zapytania (jedno w toku ≤ 15 s → zawsze < 60 s); zbieracz ma 15 min na wszystko
IX_MISS_MAX = 2      # tyle braków odpowiedzi z rzędu (nie HTTP: przekroczony czas, zerwane połączenie, DNS) = dostawca nie odpowiada, koniec pętli
ETF_URL = 'https://api.massive.com/v2/aggs/grouped/locale/us/market/stocks/{d}?adjusted=true&apiKey={key}'   # Massive (dawniej Polygon)
TIINGO_URL = 'https://api.tiingo.com/tiingo/daily/{t}/prices?token={key}&startDate={frm}'
ETF_EVERY = 6 * 60   # min — notowania dzienne: 4 zapytania Massive na dobę (limit planu: 5 na minutę)
ETF_KEEP = 30        # sesji na fundusz
ETF_TIINGO_MAX = 40  # zapas Tiingo: najwyżej tyle funduszy na przebieg (limit 50 zapytań na godzinę)
ETF_TIINGO_SLEEP = 0.25
IX_ETF = tuple(dict.fromkeys(list(DAY_SYMS) + list(FUND_SSGA) + list(FUND_ISH)))   # fundusze używane przez stronę (mapa GLOBAL, TRENDY)


def _ix_dt(s):
    """Czas ISO → datetime UTC; brak albo zepsuty = None."""
    try:
        d = datetime.datetime.fromisoformat(str(s))
        return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def ix_ready(h, now):
    """Ostatni dzień roboczy o godzinie h (UTC) nie później niż teraz — po nim zamknięcie sesji powinno być u dostawcy."""
    m = now.replace(hour=h, minute=0, second=0, microsecond=0)
    if m > now:
        m -= datetime.timedelta(days=1)
    while m.weekday() >= 5:
        m -= datetime.timedelta(days=1)
    return m


def _ix_pause(rec):
    """Dni przerwy po odrzuconym kodzie: 1, 2, 4, potem IX_BAD_DAYS (według licznika bad_n) — poprawiony klucz wraca w dobę,
    a zły kod nie marnuje zapytania co dzień."""
    n = rec.get('bad_n')
    n = n if isinstance(n, int) and n > 0 else 1
    return min(IX_BAD_DAYS, 2 ** (n - 1))


def _ix_bad(part, sym, code):
    """Kod odrzucony przez dostawcę (HTTP 401/403/404): znacznik przerwy na wpisie symbolu, licznik odrzuceń rośnie.
    Stara seria (jeśli była) zostaje — strona pokazuje ją z własną datą i wiekiem."""
    rec = part.get(sym) if isinstance(part.get(sym), dict) else {}
    n = rec.get('bad_n')
    rec.update({'bad_at': NOW, 'bad_n': (n if isinstance(n, int) and n > 0 else 0) + 1, 'bad': code}); part[sym] = rec


def ix_plan(part, now, budget):
    """Indeksy do pobrania w tym przebiegu: „do odświeżenia” (nie pobrane od ostatniego zamknięcia swojej sesji), najdłużej
    czekające najpierw; najwyżej IX_PER_RUN i nie więcej, niż zostało z dobowego limitu. Kod odrzucony — przerwa _ix_pause."""
    due = []
    for i, (sym, cc, h) in enumerate(IX_SYMBOLS):
        rec = part.get(sym) if isinstance(part, dict) else None
        rec = rec if isinstance(rec, dict) else {}
        bad = _ix_dt(rec.get('bad_at'))
        if bad and (now - bad).days < _ix_pause(rec):
            continue
        at = _ix_dt(rec.get('at'))
        if at is None or at < ix_ready(h, now):
            due.append((at or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc), i, sym))   # nigdy nie pobrane: kolejność listy
    due.sort()
    return [s for _, _, s in due[:max(0, min(IX_PER_RUN, budget))]]


class IxPusto(ValueError):
    """v118.2: dostawca odpowiedział HTTP 200 pustą listą — kod istnieje, ale plan nie obejmuje tego indeksu (26.09: FTSE.INDX, UKX.INDX)."""


def eod_parse(j):
    """Odpowiedź EODHD (lista świec) → [[dzień, zamknięcie], …] rosnąco; świeca bez liczby = brak (nigdy 0);
    słownik zamiast listy = komunikat błędu dostawcy."""
    if isinstance(j, dict):
        raise RuntimeError(str(j.get('message') or j.get('error') or j)[:140])
    if not isinstance(j, list):
        raise RuntimeError('nieznany kształt odpowiedzi')
    out = {}
    for r in j:
        if not isinstance(r, dict):
            continue
        d, v = str(r.get('date') or ''), _num(r.get('close'))
        if re.match(r'^\d{4}-\d{2}-\d{2}$', d) and v is not None and v == v and 0 < v < float('inf'):
            out[d] = v
    if not out:
        raise IxPusto('pusta odpowiedź')
    return [[d, out[d]] for d in sorted(out)]


def _ix_merge(old, new, keep):
    """Stare i nowe pary [dzień, zamknięcie] → jedna seria rosnąco (nowsza wartość wygrywa), ostatnie `keep` sesji."""
    m = {r[0]: r[1] for r in (old or []) if isinstance(r, list) and len(r) == 2 and isinstance(r[0], str) and isinstance(r[1], (int, float))}
    m.update({r[0]: r[1] for r in new})
    return [[d, m[d]] for d in sorted(m)][-keep:]


def ix_fetch(sym, cc, key, rec, now):
    """Jeden indeks: dopełnienie od ostatniej sesji (z zakładką) albo rok wstecz; zwraca nowy wpis symbolu."""
    old = rec.get('d') if isinstance(rec, dict) and isinstance(rec.get('d'), list) else []
    try:
        frm = datetime.date.fromisoformat(old[-1][0]) - datetime.timedelta(days=IX_OVERLAP)
    except (TypeError, ValueError, IndexError):
        frm = now.date() - datetime.timedelta(days=IX_HIST_DAYS)
    j = get_json(IX_URL.format(sym=sym, key=key, frm=frm.isoformat()), timeout=IX_TIMEOUT)
    return {'cc': cc, 'at': NOW, 'd': _ix_merge(old, eod_parse(j), IX_KEEP)}


def _ix_late(deadline):
    """Budżet czasu budowniczego wyczerpany? (None = bez limitu, np. w testach jednostkowych)."""
    return deadline is not None and time.monotonic() > deadline


def ix_part(key, prev_part, prev_calls, prev_quota, now, errors, deadline=None):
    """Część ix: plan (rotacja), pobranie, dobowy licznik zapytań i blokada po przekroczeniu limitu.
    Kod odrzucony (401/403/404) dostaje przerwę i NIE zatrzymuje pozostałych — jeden zły kod nie może zablokować całej rotacji;
    dopiero gdy każda próba w przebiegu została odrzucona, zgłaszamy „klucz odrzucony”. Dwa braki odpowiedzi z rzędu albo
    przekroczony budżet czasu = koniec pętli (zbieracz ma 15 min na wszystkie źródła).
    Zwraca (część, licznik, dzień blokady, czy bez błędów, ile odświeżono)."""
    znane = {s for s, _, _ in IX_SYMBOLS} | {s for s, _, _, _ in IX_FMP}
    part = {s: dict(r) for s, r in prev_part.items() if isinstance(r, dict) and s in znane} if isinstance(prev_part, dict) else {}   # v118.2: kod usunięty z listy wypada z pliku
    today = now.date().isoformat()
    calls = dict(prev_calls) if isinstance(prev_calls, dict) and prev_calls.get('d') == today else {'d': today, 'n': 0}
    quota = today if prev_quota == today else None
    ok, got, tried, miss, rej = True, 0, 0, 0, []
    if quota and ix_plan(part, now, IX_PER_RUN):   # blokada po przekroczeniu limitu: informacja, nie błąd (dane zostają z własnymi datami)
        META['notes'].append(f'Indeksy: limit dobowy EODHD wyczerpany ({today}) — odświeżenie jutro')
    for sym in ix_plan(part, now, 0 if quota else IX_DAILY - int(calls.get('n') or 0)):
        if _ix_late(deadline):
            ok = False; errors.append('EODHD: przekroczony budżet czasu — reszta indeksów za godzinę'); break
        cc = next(c for s, c, _ in IX_SYMBOLS if s == sym)
        calls['n'] = int(calls.get('n') or 0) + 1; tried += 1
        try:
            part[sym] = ix_fetch(sym, cc, key, part.get(sym), now); got += 1; miss = 0
        except urllib.error.HTTPError as e:
            ok = False
            if e.code in (402, 429):      # dobowy limit planu wyczerpany — reszta jutro, poprzednie serie zostają
                quota = today; errors.append(f'EODHD HTTP {e.code} — limit zapytań wyczerpany ({sym})'); break
            if e.code in (401, 403):      # klucz albo plan bez tego indeksu — przerwa na tym kodzie, następny indeks
                _ix_bad(part, sym, e.code); rej.append(sym); errors.append(f'EODHD HTTP {e.code} — odrzucony kod {sym}.INDX'); continue
            if e.code == 404:             # nieznany kod indeksu — przerwa
                _ix_bad(part, sym, 404); errors.append(f'EODHD HTTP 404 — nieznany kod {sym}.INDX'); continue
            errors.append(f'EODHD HTTP {e.code} ({sym})')
        except IxPusto:               # v118.2: pusta lista = indeks poza planem — przerwa rosnąca (1, 2, 4, 7 dni), nie „brak odpowiedzi”
            _ix_bad(part, sym, 'pusto'); errors.append(f'EODHD: pusta lista dla {sym}.INDX (poza planem?) — przerwa'); continue
        except Exception as e:  # noqa — jeden indeks bez odpowiedzi nie zatrzymuje pozostałych; dwa z rzędu = dostawca nie odpowiada
            ok = False; miss += 1; errors.append(mask(f'{sym}: {e}'))
            if miss >= IX_MISS_MAX:
                errors.append('EODHD: brak odpowiedzi — koniec przebiegu'); break
    if tried and len(rej) == tried:   # każda próba odrzucona: najpewniej klucz (albo plan bez indeksów), nie pojedynczy kod —
        for sym in rej:               # przerwa zostaje jednodniowa, żeby poprawiony klucz wrócił do pracy w dobę
            part[sym]['bad_n'] = 1
        errors.append('EODHD — klucz odrzucony albo plan bez indeksów (każda próba: HTTP 401/403)')
    return part, calls, quota, ok, got


def massive_parse(j, tickers):
    """Odpowiedź Massive (notowania dzienne całego rynku USA) → {ticker: zamknięcie} tylko dla funduszy strony; brak liczby = brak wpisu."""
    if not isinstance(j, dict):
        raise RuntimeError('nieznany kształt odpowiedzi')
    if str(j.get('status') or '').upper() not in ('OK', 'DELAYED'):
        raise RuntimeError(str(j.get('error') or j.get('message') or j.get('status') or 'nieznany kształt odpowiedzi')[:140])
    want, out = set(tickers), {}
    for r in j.get('results') or []:
        if isinstance(r, dict) and r.get('T') in want:
            v = _num(r.get('c'))
            if v is not None and v == v and 0 < v < float('inf'):
                out[r['T']] = v
    return out


def tiingo_parse(j):
    """Odpowiedź Tiingo (lista dni) → [[dzień, zamknięcie], …] rosnąco; słownik = komunikat błędu (np. „detail”)."""
    if isinstance(j, dict):
        raise RuntimeError(str(j.get('detail') or j.get('message') or j)[:140])
    if not isinstance(j, list):
        raise RuntimeError('nieznany kształt odpowiedzi')
    out = {}
    for r in j:
        if not isinstance(r, dict):
            continue
        d, v = str(r.get('date') or '')[:10], _num(r.get('close'))
        if re.match(r'^\d{4}-\d{2}-\d{2}$', d) and v is not None and v == v and 0 < v < float('inf'):
            out[d] = v
    return [[d, out[d]] for d in sorted(out)]


def etf_prev_day(d):
    """Poprzedni dzień roboczy (dla notowań USA: sesja już zamknięta)."""
    d = d - datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d


def etf_part(keys, prev_etf, now, errors, deadline=None):
    """Część etf: jedno zapytanie Massive o cały rynek USA z poprzedniej sesji (święto → dzień wcześniej, do 4 prób);
    gdy Massive zawiedzie — Tiingo fundusz po funduszu (najwyżej ETF_TIINGO_MAX). Historia: ostatnie ETF_KEEP sesji.
    Budżet czasu (deadline) i dwa braki odpowiedzi z rzędu kończą pętlę — 40 funduszy × przekroczony czas nie może zjeść
    limitu zbieracza; to, co już pobrano, zostaje (każda seria z własnymi datami)."""
    q = {t: r for t, r in ((prev_etf or {}).get('q') or {}).items() if isinstance(r, list)} if isinstance(prev_etf, dict) else {}
    fails = []
    if keys.get('MASSIVE_KEY'):
        d = etf_prev_day(now.date())
        for _ in range(4):
            if _ix_late(deadline):
                fails.append('Massive: przekroczony budżet czasu'); break
            try:
                closes = massive_parse(get_json(ETF_URL.format(d=d.isoformat(), key=keys['MASSIVE_KEY']), timeout=ETF_TIMEOUT), IX_ETF)
            except urllib.error.HTTPError as e:
                fails.append(f'Massive HTTP {e.code}'); break
            except Exception as e:  # noqa
                fails.append(mask(f'Massive: {e}')); break
            if closes:
                for t, v in closes.items():
                    q[t] = _ix_merge(q.get(t), [[d.isoformat(), v]], ETF_KEEP)
                return {'date': d.isoformat(), 'src': 'massive', 'q': q}
            fails.append(f'Massive {d.isoformat()}: brak notowań (dzień bez sesji?)'); d = etf_prev_day(d)
    if keys.get('TIINGO_KEY'):
        frm = (now.date() - datetime.timedelta(days=45)).isoformat()
        got, last, miss = 0, '', 0
        for i, t in enumerate(IX_ETF[:ETF_TIINGO_MAX]):
            if _ix_late(deadline):
                fails.append(f'Tiingo: przekroczony budżet czasu po {i} funduszach'); break
            if i:
                time.sleep(ETF_TIINGO_SLEEP)
            try:
                rows = tiingo_parse(get_json(TIINGO_URL.format(t=t, key=keys['TIINGO_KEY'], frm=frm), timeout=TIINGO_TIMEOUT))
            except urllib.error.HTTPError as e:
                fails.append(f'Tiingo HTTP {e.code} ({t})')
                if e.code in (401, 403, 429):   # klucz odrzucony albo limit — dalsze fundusze bez sensu
                    break
                continue
            except Exception as e:  # noqa — brak odpowiedzi: dwa z rzędu = dostawca nie odpowiada, koniec
                fails.append(mask(f'Tiingo {t}: {e}')); miss += 1
                if miss >= IX_MISS_MAX:
                    fails.append('Tiingo: brak odpowiedzi — koniec'); break
                continue
            miss = 0
            if rows:
                q[t] = _ix_merge(q.get(t), rows, ETF_KEEP); got += 1; last = max(last, rows[-1][0])
        if got:
            errors.extend(fails[:3])   # Massive zawiódł, Tiingo zastąpił (może częściowo) — awaria widoczna na stronie Źródła
            return {'date': last, 'src': 'tiingo', 'q': q}
    raise RuntimeError('; '.join(fails[:4]) or 'brak klucza')


# v119: FTSE 100 z FMP — plan bezpłatny EODHD nie daje indeksów grupy LSE (pusta lista), a sonda 26.09 pokazała, że FMP (plan bezpłatny)
# oddaje dzienne zamknięcia ^FTSE (FTSE MIB i ^GDAXI: HTTP 402 — poza planem). Jedno zapytanie po sesji, jak indeksy EODHD; ten sam wpis w części ix.
IX_FMP = (('FTSE', 'gb', 17, '%5EFTSE'),)   # kod na stronie, flaga, godzina UTC po sesji, symbol FMP (zakodowany w adresie)
FMP_EOD_URL = 'https://financialmodelingprep.com/stable/historical-price-eod/light?symbol={sym}&from={frm}&apikey={key}'


def fmp_eod_parse(j):
    """Odpowiedź FMP (lista {date, price|close, volume}) → [[dzień, zamknięcie], …] rosnąco; bez liczby = brak; słownik = komunikat dostawcy."""
    if isinstance(j, dict):
        raise RuntimeError(str(j.get('message') or j.get('Error Message') or j.get('error') or j)[:140])
    if not isinstance(j, list):
        raise RuntimeError('nieznany kształt odpowiedzi')
    out = {}
    for r in j:
        if not isinstance(r, dict):
            continue
        d = str(r.get('date') or '')[:10]; v = _num(r.get('price') if r.get('price') is not None else r.get('close'))
        if re.match(r'^\d{4}-\d{2}-\d{2}$', d) and v is not None and v == v and 0 < v < float('inf'):
            out[d] = v
    if not out:
        raise IxPusto('pusta odpowiedź')
    return [[d, out[d]] for d in sorted(out)]


def ix_fmp(key, part, now, errors):
    """Indeksy z FMP (IX_FMP): dopełnienie od ostatniej sesji (z zakładką) albo rok wstecz, raz po zamknięciu sesji (ix_ready);
    kod odrzucony (401/402/403/404) i pusta lista dostają przerwę jak w EODHD (_ix_bad). Zwraca liczbę pobranych."""
    got = 0
    for sym, cc, h, fsym in IX_FMP:
        rec = part.get(sym) if isinstance(part.get(sym), dict) else {}
        bad = _ix_dt(rec.get('bad_at'))
        if bad and (now - bad).days < _ix_pause(rec):
            continue
        at = _ix_dt(rec.get('at'))
        if at is not None and at >= ix_ready(h, now):
            continue
        old = rec.get('d') if isinstance(rec.get('d'), list) else []
        try:
            frm = datetime.date.fromisoformat(old[-1][0]) - datetime.timedelta(days=IX_OVERLAP)
        except (TypeError, ValueError, IndexError):
            frm = now.date() - datetime.timedelta(days=IX_HIST_DAYS)
        try:
            j = get_json(FMP_EOD_URL.format(sym=fsym, frm=frm.isoformat(), key=key), timeout=IX_TIMEOUT)
            part[sym] = {'cc': cc, 'at': NOW, 'src': 'fmp', 'd': _ix_merge(old, fmp_eod_parse(j), IX_KEEP)}; got += 1
        except urllib.error.HTTPError as e:
            if e.code in (401, 402, 403, 404):
                _ix_bad(part, sym, e.code); errors.append(f'FMP HTTP {e.code} — {sym} (przerwa)')
            else:
                errors.append(f'FMP HTTP {e.code} ({sym})')
        except IxPusto:
            _ix_bad(part, sym, 'pusto'); errors.append(f'FMP: pusta lista dla {sym} — przerwa')
        except Exception as e:  # noqa
            errors.append(mask(f'FMP {sym}: {e}'))
    return got


def build_indeksy(keys, prev=None, now=None):
    """data/indeksy.json — część ix: dzienne zamknięcia 23 indeksów świata (EODHD, rotacja 20 zapytań na dobę);
    część etf: zamknięcia funduszy ETF używanych przez stronę (Massive; zapas Tiingo). Część bez klucza albo z błędem = poprzednia
    wersja z własnym czasem (part_at), nigdy zera. Z kluczem plik powstaje zawsze — także gdy nic się nie udało (licznik dobowy,
    blokada limitu i przerwy na kodach muszą przetrwać do następnej godziny); bez kluczy i bez poprzednich danych = wyjątek."""
    now = now or _now_utc()
    keys = keys if isinstance(keys, dict) else {}
    prev = prev if isinstance(prev, dict) else {}
    pat = prev.get('part_at') if isinstance(prev.get('part_at'), dict) else {}
    deadline = time.monotonic() + IX_BUDGET_S   # cały budowniczy < 60 s: po tym czasie żadnego nowego zapytania
    out, errors = {'at': NOW, 'ok': {}, 'part_at': {}}, []
    if keys.get('EODHD_KEY'):
        part, calls, quota, ok, got = ix_part(keys['EODHD_KEY'], prev.get('ix'), prev.get('ix_calls'), prev.get('ix_quota'), now, errors, deadline)
        has = any(isinstance(r.get('d'), list) and r['d'] for r in part.values())
        out['ix_calls'] = calls; out['ok']['ix'] = bool(ok and has)   # licznik zawsze w pliku — także po nieudanym pierwszym przebiegu
        if quota:
            out['ix_quota'] = quota
        if part:   # serie i/lub znaczniki przerw; strona ukrywa panel, dopóki żaden indeks nie ma serii
            out['ix'] = part
            at = NOW if (ok or got) else (pat.get('ix') or prev.get('at'))
            if at:
                out['part_at']['ix'] = at
    elif isinstance(prev.get('ix'), dict) and prev['ix']:   # klucz zniknął — stare serie zostają (strona pokazuje ich datę i wiek)
        out['ix'] = prev['ix']; out['part_at']['ix'] = pat.get('ix') or prev.get('at')
    if keys.get('FMP_KEY') and not _ix_late(deadline):   # v119: FTSE 100 z FMP dopisany do tej samej części ix
        src = out['ix'] if isinstance(out.get('ix'), dict) else (prev.get('ix') if isinstance(prev.get('ix'), dict) else {})
        part = {s: dict(r) for s, r in src.items() if isinstance(r, dict)}   # kopia — poprzedni plik nie jest modyfikowany w miejscu
        if ix_fmp(keys['FMP_KEY'], part, now, errors):
            out['part_at']['ix'] = NOW
        if part:
            out['ix'] = part
            out['part_at'].setdefault('ix', pat.get('ix') or prev.get('at') or NOW)
    pe = prev.get('etf') if isinstance(prev.get('etf'), dict) and isinstance(prev['etf'].get('q'), dict) and prev['etf']['q'] else None
    if keys.get('MASSIVE_KEY') or keys.get('TIINGO_KEY'):
        last = _ix_dt(pat.get('etf'))
        if pe and last and (now - last).total_seconds() < ETF_EVERY * 60:   # notowania dzienne — co 6 h wystarczy
            out['etf'] = pe; out['ok']['etf'] = True; out['part_at']['etf'] = pat['etf']
        else:
            try:
                out['etf'] = etf_part(keys, pe, now, errors, deadline); out['ok']['etf'] = True; out['part_at']['etf'] = NOW
            except Exception as e:  # noqa — część z błędem: poprzednia wersja z własnym czasem
                errors.append(mask(f'ETF: {e}')); out['ok']['etf'] = False
                if pe:
                    out['etf'] = pe; out['part_at']['etf'] = pat.get('etf') or prev.get('at')
    elif pe:
        out['etf'] = pe; out['part_at']['etf'] = pat.get('etf') or prev.get('at')
    if errors:
        META['errors'].append(mask('Indeksy: ' + '; '.join(errors)[:400]))
    if not any(k in out for k in IX_PARTS) and not any(keys.get(k) for k in IX_ACTIVE):
        raise RuntimeError('brak kluczy i poprzednich danych')
    return out


# ===================== v121: ceny krypto — dzienne zamknięcia 10 par USDT z publicznych plików giełdy (bez klucza) =====================
# Plik data/ceny-krypto.json: q[SYM] = {'d': [[dzień, zamknięcie]], 'vol': [[dzień, obrót doby w USDT]]} dla par <SYM>USDT z TR_CR_SYMS
# (BTC ETH XRP BNB SOL DOGE ADA TRX LINK AVAX), do KC_KEEP dni; ok / part_at / bledy / blok osobno dla każdej pary; full_at = czas
# ostatniej pełnej budowy (harmonogram w main() liczy godzinę od niego, nie od `at`, które odświeża też ponowienie jednej pary — wzór v104).
# Źródło: Binance Vision (data.binance.vision) — publiczne archiwum plików rynku spot giełdy (świece 1d, pliki miesięczne i dzienne,
# bez klucza; ten sam host, z którego zbieracz czyta już metryki kontraktów w v104).
# Licencja DANYCH: CC BY-NC-SA 4.0 — „Binance Vision Dataset Terms” v1.0 z 26.08.2026 (link na data.binance.vision/terms-of-use.html):
# pkt 3.1 licencja CC BY-NC-SA 4.0; pkt 4.5 każda rozpowszechniana praca pochodna podaje „Binance Vision” i zostaje na tej samej licencji;
# pkt 3.4 i 4.1 tylko użycie niekomercyjne (płatny dostęp do tych danych wymaga osobnej umowy z giełdą — decyzja właściciela);
# pkt 8.2 bez sugerowania, że giełda wspiera projekt. Licencja MIT repozytorium narzędzi tego archiwum dotyczy tylko kodu, nie danych.
# Podpis „Binance Vision — CC BY-NC-SA 4.0” jest na stronie Źródła (akapit wymaganych podpisów); panel opisuje dane słowami
# („publiczne dane rynkowe giełdy”), bez nazwy dostawcy.
# Pierwsze pobranie: pliki miesięczne za KC_MONTHS miesięcy + dzienne za bieżący miesiąc; potem tylko dni po ostatnim zapisanym zamknięciu
# (plik dzienny pojawia się ok. 2 h po północy UTC, miesięczny — kilka dni po końcu miesiąca; jego brak albo błąd = pliki dzienne).
# Dzień bez pliku = brak (nigdy zero); dzisiejsza, niepełna doba nie wchodzi. Każda para osobno: błąd = wiersze pobrane przed nim + wpis
# w błędach; plik dzienny z błędem dłużej niż KC_SKIP_H godzin zostaje luką, gdy późniejszy plik pary się pobierze (para nie staje na
# zawsze); budżet czasu przebiegu (także przekroczenie czasu zapytania skróconego budżetem) = ciąg dalszy w następnym przebiegu.
KC_MON_URL = 'https://data.binance.vision/data/spot/monthly/klines/{p}/1d/{p}-1d-{m}.zip'
KC_DAY_URL = 'https://data.binance.vision/data/spot/daily/klines/{p}/1d/{p}-1d-{d}.zip'
KC_QUOTE = 'USDT'
KC_KEEP = 420          # dni na parę w pliku
KC_MONTHS = 14         # pierwsze pobranie: tyle pełnych miesięcy wstecz (≈ 425 dni, po obcięciu KC_KEEP)
KC_EVERY = 60          # minut — pełna budowa najwyżej raz na godzinę (plik dzienny giełdy powstaje raz na dobę)
KC_BUDGET = 50         # s na cały przebieg budowniczego (pierwsze pobranie to ~390 małych plików — reszta w następnym przebiegu)
KC_TIMEOUT = 20        # s na jeden plik
KC_SKIP_H = 24         # h: tak długo ten sam plik dzienny pary musi zawodzić, zanim jego dzień zostanie luką
KC_LABEL = 'Ceny krypto'   # początek komunikatu błędu (strona Źródła)
KC_SRC = 'Publiczne dane rynkowe giełdy — dzienne zamknięcia rynku spot par z USDT (pliki miesięczne i dzienne świec 1d, bez klucza); zmiany 1/7/30 dni liczy strona'


class KcCut(Exception):
    """Zapytanie przerwane limitem czasu skróconym przez budżet przebiegu — ciąg dalszy w następnym przebiegu, nie błąd pary."""


def kc_num(v):
    """Liczba > 0 z tekstu albo liczby; brak, NaN, nieskończoność, ≤ 0 → None (brak nigdy nie staje się zerem)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and 0 < x < float('inf') else None


def kc_parse(csv_text):
    """CSV świec 1d (bez nagłówka; kolumny: open_time, open, high, low, close, volume, close_time, quote_volume, …) → {dzień UTC: (zamknięcie, obrót w USDT)}.
    open_time w milisekundach (pliki do 2024) albo mikrosekundach (od 2025) — rozpoznawane po wielkości liczby; wiersz nagłówka, wiersz bez liczb
    albo z zamknięciem ≤ 0 pominięty (nigdy zero zamiast ceny); obrót bez liczby → None."""
    out = {}
    for row in csv.reader(io.StringIO(csv_text)):
        if len(row) < 5 or not str(row[0]).strip().isdigit():
            continue
        ts_ = int(row[0])
        if ts_ > 10 ** 14:   # mikrosekundy
            ts_ //= 1000
        close = kc_num(row[4])
        if close is None:
            continue
        day = datetime.datetime.fromtimestamp(ts_ / 1000, datetime.timezone.utc).date().isoformat()
        qv = kc_num(row[7]) if len(row) > 7 else None
        out[day] = (close, None if qv is None else round(qv))
    return out


def kc_is_timeout(e):
    """Przekroczenie czasu zapytania: odczyt (TimeoutError / socket.timeout) albo połączenie (urllib opakowuje je w URLError)."""
    return isinstance(e, TimeoutError) or (isinstance(e, urllib.error.URLError) and isinstance(getattr(e, 'reason', None), TimeoutError))


def kc_get(url, termin=None):
    """Plik zip z jednym CSV → {dzień: (zamknięcie, obrót)}; 404 (pliku jeszcze nie ma) → None; inny błąd HTTP / zip → wyjątek.
    Limit czasu zapytania to KC_TIMEOUT albo mniej, gdy do końca budżetu zostało mniej; przekroczenie takiego SKRÓCONEGO limitu → KcCut
    (koniec budżetu, nie błąd pary). Przekroczenie pełnego KC_TIMEOUT to zwykły błąd."""
    tmo = KC_TIMEOUT if termin is None else max(1, min(KC_TIMEOUT, int(termin - time.monotonic())))
    try:
        data = get_bytes(url, timeout=tmo)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except Exception as e:  # noqa — rozróżnienie: koniec budżetu czy błąd
        if tmo < KC_TIMEOUT and kc_is_timeout(e):
            raise KcCut(str(e)) from e
        raise
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if n.lower().endswith('.csv')]
        if not names:
            raise ValueError('archiwum bez CSV')
        return kc_parse(z.read(names[0]).decode('utf-8', 'replace'))


def kc_prev_rows(prev):
    """Wiersze z poprzedniego rekordu pary → {dzień: (zamknięcie, obrót|None)}; wiersz bez daty albo bez liczby pominięty."""
    rows = {}
    if not isinstance(prev, dict):
        return rows
    vol = {r[0]: r[1] for r in (prev.get('vol') or []) if isinstance(r, list) and len(r) == 2 and isinstance(r[0], str)}
    for r in prev.get('d') or []:
        if isinstance(r, list) and len(r) == 2 and isinstance(r[0], str) and re.match(r'^\d{4}-\d{2}-\d{2}$', r[0]) and kc_num(r[1]) is not None:
            v = kc_num(vol.get(r[0]))
            rows[r[0]] = (float(r[1]), None if v is None else round(v))
    return rows


def kc_start(last, today):
    """Pierwszy dzień do pobrania: dzień po ostatnim zapisanym zamknięciu; bez historii — pierwszy dzień miesiąca sprzed KC_MONTHS miesięcy."""
    if last:
        return datetime.date.fromisoformat(last) + datetime.timedelta(days=1)
    m = today.year * 12 + today.month - 1 - KC_MONTHS
    return datetime.date(m // 12, m % 12 + 1, 1)


def kc_age_h(iso):
    """Wiek znacznika czasu ISO (ze strefą) w godzinach; brak albo zły zapis → -1 (nigdy „dość stary”)."""
    try:
        t0 = datetime.datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return -1
    if t0.tzinfo is None:
        return -1
    return (_now_utc() - t0).total_seconds() / 3600


def kc_pair(sym, prev, today, termin, skip=None):
    """Jedna para: dopełnienie od kc_start do wczoraj (dzisiejsza doba jest niepełna). Miesiąc zakończony — najpierw plik miesięczny
    (404 = jeszcze nieopublikowany, inny błąd = uwaga; w obu razach pliki dzienne tego miesiąca); bieżący miesiąc — pliki dzienne.
    Dzień bez pliku (404) = brak, dalsze dni idą dalej; brak wczorajszego pliku to nie luka (pojawia się ok. 2 h po północy UTC).
    Plik dzienny z błędem (inny HTTP / zły zip) kończy parę: wiersze pobrane wcześniej zostają, błąd = (dzień, wyjątek). `skip` = dzień,
    którego plik zawodzi od ponad KC_SKIP_H h: gdy późniejszy plik pary się pobierze, ten dzień zostaje luką (brak, nigdy zero) z uwagą;
    gdy nie — nadal błąd tego dnia. Koniec budżetu (także KcCut) = przerwane, bez błędu.
    Zwraca (rekord {'d', 'vol'}, uwagi, przerwane budżetem, błąd (dzień, wyjątek) | None)."""
    pair = sym + KC_QUOTE
    rows = kc_prev_rows(prev)
    end = today - datetime.timedelta(days=1)
    d = kc_start(max(rows) if rows else None, today)
    notes, cut, err, pending = [], False, None, None

    def merge(got):
        nonlocal pending
        for k, v in got.items():
            if k <= end.isoformat():   # bez dzisiejszej doby
                rows[k] = v
        if pending is not None:   # późniejszy plik się pobrał — dzień z uporczywym błędem zostaje luką
            notes.append(f'{pending[0]}: plik z błędem dłużej niż {KC_SKIP_H} h ({pending[1]}) — dzień pominięty (brak, nie zero)')
            pending = None

    while d <= end:
        if time.monotonic() > termin:
            cut = True
            break
        nxt = datetime.date(d.year + (d.month == 12), d.month % 12 + 1, 1)   # pierwszy dzień następnego miesiąca
        if nxt <= today:   # miesiąc zakończony — jeden plik miesięczny zamiast ≤ 31 dziennych
            key = d.strftime('%Y-%m')
            try:
                got = kc_get(KC_MON_URL.format(p=pair, m=key), termin)
            except KcCut:
                cut = True
                break
            except Exception as e:  # noqa — uszkodzony albo niedostępny plik miesięczny: ten miesiąc plikami dziennymi
                notes.append(f'{key}: plik miesięczny z błędem ({e}) — pliki dzienne')
                got = None
            if got is not None:
                merge(got)
                d = nxt
                continue
        m_end = min(end, nxt - datetime.timedelta(days=1))
        while d <= m_end:
            if time.monotonic() > termin:
                cut = True
                break
            try:
                got = kc_get(KC_DAY_URL.format(p=pair, d=d.isoformat()), termin)
            except KcCut:
                cut = True
                break
            except Exception as e:  # noqa — plik dzienny z błędem
                if pending is None and skip == d.isoformat():
                    pending = (d.isoformat(), e)   # próbujemy dni dalej; luka dopiero, gdy któryś się pobierze
                    d += datetime.timedelta(days=1)
                    continue
                err = pending or (d.isoformat(), e)
                break
            if got is None:
                if d != end:
                    notes.append(f'{d.isoformat()}: brak pliku')
            else:
                merge(got)
            d += datetime.timedelta(days=1)
        if cut or err:
            break
    if pending is not None and err is None and not cut:
        err = pending   # nic późniejszego się nie pobrało — dzień nadal blokuje parę (następny przebieg spróbuje znowu)
    days = sorted(rows)[-KC_KEEP:]
    rec = {'d': [[k, rows[k][0]] for k in days], 'vol': [[k, rows[k][1]] for k in days if rows[k][1] is not None]}
    return rec, notes, cut, err


def build_ceny_krypto(prev=None, only=None, today=None):
    """data/ceny-krypto.json — dzienne zamknięcia (i obrót doby w USDT) 10 par <SYM>USDT z TR_CR_SYMS z publicznych plików giełdy (bez klucza);
    do KC_KEEP dni na parę; pełna budowa najwyżej co KC_EVERY min (od full_at). Każda para osobno: błąd = wiersze pobrane przed nim (albo
    poprzednie dane tej pary) z własnym czasem + wpis w bledy, blok (dzień i początek błędu) i w błędach zbieracza; przerwane budżetem = ok False
    bez błędu (ciąg dalszy w następnym przebiegu). `only` = tylko te pary (reszta przepisana z poprzedniego pliku; full_at bez zmian).
    Żadnych danych = wyjątek (główny przebieg zostawia poprzedni plik). Brak nigdy nie jest zerem."""
    today = today or _now_utc().date()
    termin = time.monotonic() + KC_BUDGET
    prev = prev if isinstance(prev, dict) else {}
    pq = prev.get('q') if isinstance(prev.get('q'), dict) else {}
    pok = prev.get('ok') if isinstance(prev.get('ok'), dict) else {}
    pat = prev.get('part_at') if isinstance(prev.get('part_at'), dict) else {}
    pbl = prev.get('blok') if isinstance(prev.get('blok'), dict) else {}
    out = {'at': NOW, 'full_at': (prev.get('full_at') or prev.get('at') or NOW) if only is not None else NOW,
           'src': KC_SRC, 'quote': KC_QUOTE, 'keep': KC_KEEP, 'ok': {}, 'part_at': {}, 'bledy': {}, 'blok': {}, 'q': {}}
    errs = []
    for sym in TR_CR_SYMS:
        old = pq.get(sym) if isinstance(pq.get(sym), dict) else None
        ob = pbl.get(sym) if isinstance(pbl.get(sym), dict) else None
        if only is not None and sym not in only:   # para spoza ponowienia — bez zmian, z własnym czasem
            if old:
                out['q'][sym] = old; out['ok'][sym] = pok.get(sym) is True; out['part_at'][sym] = pat.get(sym) or prev.get('at')
            if ob:
                out['blok'][sym] = ob
            continue
        skip = ob.get('d') if ob and kc_age_h(ob.get('od')) >= KC_SKIP_H else None
        try:
            rec, notes, cut, err = kc_pair(sym, old, today, termin, skip)
        except Exception as e:  # noqa — nieprzewidziany błąd pary: poprzednie dane z własnym czasem, nigdy zero
            rec, notes, cut, err = None, [], False, (None, e)
        if rec is None:
            if old:
                out['q'][sym] = old
        elif rec['d']:
            out['q'][sym] = rec
        grew = rec is not None and bool(rec['d']) and (old is None or rec['d'] != old.get('d'))
        out['part_at'][sym] = NOW if rec is not None and (err is None or grew) else (pat.get(sym) or prev.get('at'))
        for n in notes:
            META['notes'].append(mask(f'{KC_LABEL}: {sym} {n}'))
        if err is not None:
            day, e = err
            msg = mask(f'{sym}: ' + (f'{day}: ' if day else '') + str(e))[:120]
            errs.append(msg); out['ok'][sym] = False; out['bledy'][sym] = msg
            if day:   # początek błędu tego samego dnia przechodzi z pliku do pliku — po KC_SKIP_H h dzień może zostać luką
                out['blok'][sym] = {'d': day, 'od': ob['od'] if ob and ob.get('d') == day and kc_age_h(ob.get('od')) >= 0 else NOW}
            continue
        out['ok'][sym] = bool(rec['d']) and not cut
        if cut:
            META['notes'].append(f'{KC_LABEL}: {sym} — budżet czasu przebiegu ({KC_BUDGET} s), ciąg dalszy w następnym przebiegu')
    if errs:
        META['errors'].append(mask(f'{KC_LABEL}: ' + '; '.join(errs)[:400]))
    if not out['q']:
        raise RuntimeError('żadna para nie odpowiedziała' + (': ' + '; '.join(errs)[:200] if errs else ''))
    return out


# ===================== v121: INSIDERZY SPÓŁEK USA — zgłoszenia Form 4 (SEC EDGAR, bez klucza) =====================
# Źródło z pierwszej ręki: indeks dzienny EDGAR według typu formularza (form.YYYYMMDD.idx) i pełne dokumenty zgłoszeń
# (…/Archives/edgar/data/CIK/ACCESSION.txt — w środku blok <XML> ownershipDocument).
# Licencja: informacja publiczna SEC — wolno kopiować i rozpowszechniać bez zgody urzędu; SEC prosi o wskazanie źródła
# (https://www.sec.gov/privacy#dissemination, „Website Dissemination”, sprawdzone 26.09.2026). Zgłoszenia piszą sami zgłaszający,
# więc to nie są utwory rządu USA. Źródło wskazane w polu „src” pliku data/insider.json; panel mówi „zgłoszenia do nadzoru
# giełdowego USA”, strona Źródła ma ogólne zdanie o legalnych źródłach publicznych (osobna linia podziękowań nie jest wymagana).
# Zasady dostępu (https://www.sec.gov/os/accessing-edgar-data): najwyżej 10 zapytań/s (tu ≤ 8), User-Agent z nazwą programu i adresem
# kontaktowym (bez adresu e-mail urząd odpowiada 403 „Undeclared Automated Tool” — sprawdzone 26.09.2026), pobierać tylko to, co potrzebne —
# jeden dzień zgłoszeń, raz, po jego zamknięciu. Adres kontaktowy WYŁĄCZNIE z sekretu SEC_CONTACT (nigdy w kodzie, testach ani plikach strony;
# maskowany w komunikatach, pilnowany przez straż kluczy); bez sekretu część jest wyłączona z notatką.
# Odpowiedzi 403 urzędu mają dwa znaczenia: brak pliku (weekend, święto; treść magazynu „<Code>AccessDenied</Code>”) albo blokada
# (strona „Undeclared Automated Tool” / „Request Rate Threshold Exceeded” — zły User-Agent albo przekroczony limit). Rozróżnia je _ins_err.
INS_IDX_URL = 'https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/form.{d}.idx'
INS_DOC_URL = 'https://www.sec.gov/Archives/{f}'
INS_UA = 'CapitalFlowAI/1.0 ({})'   # wymagany przez SEC: nazwa programu i kontakt — kontakt z sekretu SEC_CONTACT (ins_ua)
INS_TEMPO = 0.125       # s między zapytaniami — najwyżej 8 na sekundę
INS_BUDGET = 120        # s na przebieg (dzień w toku dokańczany w następnych przebiegach)
INS_BUDGET_LATE = 40    # s, gdy przebieg zbieracza trwa już dłużej niż INS_LATE (cały przebieg ma limit 15 min)
INS_LATE = 480
INS_MAX = 700           # najwyżej tyle zgłoszeń Form 4 na dzień (więcej = notatka, liczone pierwsze INS_MAX)
INS_HIST = 120          # dni w historii
INS_TOP = 10            # największe spółki dnia
INS_READY_H = 4         # dzień D gotowy od D+1 04:00 UTC: Form 4 przyjmowane do 22:00 ET tego dnia (02:00/03:00 UTC), indeks odświeżony ok. 02:16 UTC (sprawdzone 26.09.2026)
INS_BACK_DAYS = 30      # pierwszy przebieg: tyle dni wstecz (jeden dzień na przebieg, weekendy nic nie kosztują); potem: najdłuższa zaległość po przerwie zbieracza
INS_SKIP_KEEP = 60      # tyle dni bez indeksu (weekendy, święta) pamiętamy — bez ponownych zapytań
INS_BLEDY = 3           # tyle kolejnych błędów sieci/serwera = koniec części na ten przebieg (reszta zostaje w kolejce)
INS_PROBY = 3           # tyle przebiegów z błędem sieci/serwera dla jednego zgłoszenia = zgłoszenie pominięte (n_fail, notatka) — dzień nie utknie
INS_TIMEOUT = 15
INS_SRC = ('Zgłoszenia insiderów Form 4 do amerykańskiego nadzoru giełdowego (SEC EDGAR: indeks dzienny i dokumenty zgłoszeń) — odczyt własny; '
           'liczone tylko transakcje na rynku: kod P (zakup) i S (sprzedaż), akcje × cena ze zgłoszenia; dzień = data zgłoszenia; każde zgłoszenie raz; '
           'informacja publiczna SEC — wolno kopiować i rozpowszechniać bez zgody (sec.gov/privacy#dissemination); SEC prosi o wskazanie źródła')
_INS_ROW = re.compile(r'^4\s{2,}(.*?)\s{2,}(\d+)\s+(\d{8})\s+(edgar/data/\S+\.txt)\s*$')
_INS_TICK = re.compile(r'^[A-Z][A-Z0-9.\-]{0,9}$')
_INS_DAY = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_INS_MAIL = re.compile(r'^[^\s@()<>,;"]+@[^\s@()<>,;"]+\.[^\s@()<>,;"]+$')   # adres kontaktowy do User-Agent (tylko e-mail — tego wymaga urząd)
_INS_LAST = [0.0]       # czas ostatniego zapytania (monotonic) — odstęp INS_TEMPO


def ins_ua(contact):
    """User-Agent dla EDGAR: nazwa programu i adres kontaktowy z sekretu SEC_CONTACT; pusty albo nie-e-mail = None (część wyłączona)."""
    c = (contact or '').strip()
    return INS_UA.format(c) if len(c) <= 120 and _INS_MAIL.match(c) else None


def ins_target(now):
    """Dzień zgłoszeń gotowy do odczytu: D od D+1 INS_READY_H:00 UTC (Form 4 przyjmowane do 22:00 ET tego samego dnia)."""
    return (now - datetime.timedelta(hours=INS_READY_H)).date() - datetime.timedelta(days=1)


def ins_idx_url(day):
    return INS_IDX_URL.format(y=day.year, q=(day.month - 1) // 3 + 1, d=day.strftime('%Y%m%d'))


def ins_index_ok(text):
    """Odpowiedź 200 to naprawdę indeks dzienny (nagłówek kolumn), a nie strona błędu — inaczej dzień nie może przepaść jako „pusty”."""
    head = (text or '')[:4000]
    return 'Form Type' in head and 'File Name' in head


def ins_parse_index(text):
    """Indeks dzienny wg typu formularza (stała szerokość kolumn): wiersze typu dokładnie „4” (bez 4/A) → [[cik, nazwa, plik], …].
    Każde zgłoszenie raz: indeks wymienia to samo zgłoszenie (numer ACCESSION) pod CIK spółki i pod CIK każdej osoby zgłaszającej
    (25.09.2026: 666 wierszy, 322 zgłoszenia; jedno nawet 8 razy) — liczy się numer zgłoszenia, nie ścieżka (każda ścieżka daje ten sam dokument)."""
    out, seen = [], set()
    for line in text.splitlines():
        m = _INS_ROW.match(line)
        if not m:
            continue
        f = m.group(4); acc = f.rsplit('/', 1)[-1]
        if acc in seen:
            continue
        seen.add(acc); out.append([m.group(2), m.group(1).strip(), f])
    return out


def _ins_num(s):
    try:
        v = float(str(s).strip().replace(',', ''))
    except (TypeError, ValueError):
        return None
    return v if v == v and 0 < v < float('inf') else None


def _ins_val(node, path):
    """Tekst elementu pod ścieżką (brak elementu albo pusty tekst = None)."""
    el = node.find(path) if node is not None else None
    return ((el.text or '').strip() or None) if el is not None else None


def ins_parse_doc(text):
    """Dokument zgłoszenia (.txt z blokiem <XML> ownershipDocument) → {issuer, ticker, buy, sell, n_buy, n_sell}: sumy USD transakcji
    na rynku z tabeli akcji (nonDerivative): kod P + nabycie A = zakup, kod S + zbycie D = sprzedaż; akcje × cena; wiersz bez ceny
    albo liczby jest pomijany (nigdy zero); n_buy/n_sell = liczba takich wierszy w dokumencie."""
    import xml.etree.ElementTree as ET
    root = None
    for m in re.finditer(r'<XML>(.*?)</XML>', text, re.S | re.I):
        body = m.group(1).strip()
        if '<ownershipDocument' not in body:
            continue
        try:
            r = ET.fromstring(body)
        except ET.ParseError as e:
            raise ValueError(f'XML: {e}')
        if r.tag == 'ownershipDocument':
            root = r; break
    if root is None:
        raise ValueError('brak dokumentu ownershipDocument')
    tick = (_ins_val(root, 'issuer/issuerTradingSymbol') or '').upper().replace(' ', '')
    o = {'issuer': _ins_val(root, 'issuer/issuerName'), 'ticker': tick if _INS_TICK.match(tick) and tick not in ('NONE', 'N/A', 'NA') else None,
         'buy': 0.0, 'sell': 0.0, 'n_buy': 0, 'n_sell': 0}
    for tr in root.findall('nonDerivativeTable/nonDerivativeTransaction'):
        code = (_ins_val(tr, 'transactionCoding/transactionCode') or '').upper()
        ad = (_ins_val(tr, 'transactionAmounts/transactionAcquiredDisposedCode/value') or '').upper()
        sh = _ins_num(_ins_val(tr, 'transactionAmounts/transactionShares/value'))
        px = _ins_num(_ins_val(tr, 'transactionAmounts/transactionPricePerShare/value'))
        if sh is None or px is None:
            continue
        if code == 'P' and ad == 'A':
            o['buy'] += sh * px; o['n_buy'] += 1
        elif code == 'S' and ad == 'D':
            o['sell'] += sh * px; o['n_sell'] += 1
    return o


def ins_top(iss, key):
    """Największe sumy dnia wg spółki: [[spółka, ticker, USD], …] (INS_TOP, malejąco), tylko > 0; key: 2 = zakupy, 3 = sprzedaż."""
    rows = [[v[0], v[1], round(v[key], 2)] for v in iss.values()
            if isinstance(v, list) and len(v) >= 4 and isinstance(v[key], (int, float)) and v[key] > 0]
    rows.sort(key=lambda r: (-r[2], str(r[0])))
    return rows[:INS_TOP]


def ins_hist(prev_hist, row, keep=INS_HIST):
    """Historia dzienna [[dzień, zakupy USD, sprzedaż USD, zgłoszeń z zakupem, ze sprzedażą], …]: wiersz tego dnia zastępowany, ostatnie `keep` dni."""
    by = {}
    for r in (prev_hist if isinstance(prev_hist, list) else []):
        if isinstance(r, list) and len(r) >= 5 and isinstance(r[0], str) and _INS_DAY.match(r[0]):
            by[r[0]] = r[:5]
    by[row[0]] = row
    return [by[d] for d in sorted(by)][-keep:]


def _ins_get(url, ua):
    """Jedno zapytanie do EDGAR: nagłówek User-Agent z kontaktem i odstęp ≥ INS_TEMPO od poprzedniego zapytania."""
    wait = _INS_LAST[0] + INS_TEMPO - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    try:
        return get(url, headers={'User-Agent': ua, 'Accept': 'text/plain, */*'}, timeout=INS_TIMEOUT)
    finally:
        _INS_LAST[0] = time.monotonic()


def _ins_err(e):
    """Rodzaj błędu zapytania do EDGAR → (rodzaj, opis): 'brak' — pliku nie ma (404/410 albo 403 z treścią magazynu AccessDenied/NoSuchKey:
    tak urząd odpowiada na dzień bez indeksu); 'blok' — urząd odmawia (403 z inną treścią, np. „Undeclared Automated Tool”, „Request Rate
    Threshold Exceeded”: zły User-Agent albo przekroczony limit; 429) — dalsze zapytania w tym przebiegu tylko pogorszą sprawę;
    'siec' — reszta (5xx, przekroczony czas, DNS…). Nieznana treść 403 = blokada, nigdy „brak pliku” (dzień handlu nie może zniknąć po cichu)."""
    if not isinstance(e, urllib.error.HTTPError):
        return 'siec', mask(str(e))
    try:
        if e.code in (404, 410):
            return 'brak', f'HTTP {e.code}'
        if e.code == 403:
            try:
                body = (e.read(4000) or b'').decode('utf-8', 'replace')
            except Exception:  # noqa — treść nieczytelna = nieznana
                body = ''
            if 'AccessDenied' in body or 'NoSuchKey' in body:
                return 'brak', 'HTTP 403 (brak pliku)'
            why = ('Undeclared Automated Tool' in body and 'nieznany program: User-Agent bez kontaktu') or \
                  ('Rate Threshold' in body and 'przekroczony limit zapytań') or 'odmowa dostępu'
            return 'blok', f'HTTP 403 ({why})'
        if e.code == 429:
            return 'blok', 'HTTP 429 (przekroczony limit zapytań)'
        return 'siec', f'HTTP {e.code}'
    finally:
        try:
            e.close()   # odpowiedź z błędem trzyma połączenie — zamykamy od razu
        except Exception:  # noqa
            pass


def _ins_final(out):
    """Sumy, stosunek, udział, największe spółki i wiersz historii z bieżącego stanu dnia (żadnego odczytanego zgłoszenia = None, nie zero)."""
    iss = out.get('iss') if isinstance(out.get('iss'), dict) else {}
    read = isinstance(out.get('n_parsed'), int) and out['n_parsed'] > 0
    b = round(float(out.get('buys_usd') or 0.0), 2) if read else None
    s = round(float(out.get('sells_usd') or 0.0), 2) if read else None
    out['buys_usd'], out['sells_usd'] = b, s
    # liczby zgłoszeń z zakupem/sprzedażą: tylko gdy coś odczytano (inaczej „0 zgłoszeń” pod „—” udawałoby wiedzę)
    nb, ns = (int(out.get('n_buy') or 0), int(out.get('n_sell') or 0)) if read else (None, None)
    out['n_buy'], out['n_sell'] = nb, ns
    out['ratio'] = round(b / s, 3) if read and s > 0 else None
    out['share'] = round(100.0 * b / (b + s), 1) if read and (b + s) > 0 else None
    out['top_buys'], out['top_sells'] = ins_top(iss, 2), ins_top(iss, 3)
    out['hist'] = ins_hist(out.get('hist'), [out['day'], b, s, nb, ns])
    if isinstance(out.get('n_filings'), int) and isinstance(out.get('n_queued'), int) and out['n_filings'] > out['n_queued']:
        out['notes'].append(f"{out['day']}: {out['n_filings']} zgłoszeń Form 4 — policzono pierwsze {out['n_queued']}")


def build_insider(prev=None, now=None, budget=None, contact=None):
    """data/insider.json — dzienna suma zakupów i sprzedaży insiderów spółek USA na rynku (Form 4). Dzień zgłoszeń D czytany raz,
    od D+1 04:00 UTC: indeks dzienny → dokumenty zgłoszeń typu „4” (każde zgłoszenie raz, ≤ INS_MAX, ≤ 8 zapytań/s, budżet czasu na przebieg;
    reszta dnia zostaje w kolejce na następny przebieg). Brak indeksu (weekend, święto) = dzień zapamiętany jako pusty, nie błąd — ale
    ostatni gotowy dzień bez indeksu jest sprawdzany znowu (indeks może się spóźnić), a blokada urzędu to błąd, nigdy „dzień pusty”.
    contact = adres z sekretu SEC_CONTACT (User-Agent; bez niego wyjątek). Wynik: sumy USD, liczba zgłoszeń z zakupem/sprzedażą, stosunek
    i udział zakupów, największe spółki dnia, historia INS_HIST dni; brak liczby = None, nigdy zero. Źródło i licencja: nagłówek tej części."""
    ua = ins_ua(contact)
    if not ua:
        raise RuntimeError('brak adresu kontaktowego do nagłówka User-Agent (sekret SEC_CONTACT)')
    now = now or _now_utc(); t0 = time.monotonic()
    if budget is None:
        late = _RUN_T0[0] is not None and time.monotonic() - _RUN_T0[0] > INS_LATE
        budget = INS_BUDGET_LATE if late else INS_BUDGET
    prev = prev if isinstance(prev, dict) else {}
    keep = ('day', 'done', 'day_at', 'done_at', 'n_filings', 'n_queued', 'n_parsed', 'n_fail', 'buys_usd', 'sells_usd', 'n_buy', 'n_sell',
            'ratio', 'share', 'top_buys', 'top_sells', 'checked', 'pending', 'iss', 'tries')
    out = {'at': NOW, 'src': INS_SRC, 'ok': {'sec': True}, 'max': INS_MAX, 'notes': []}
    out.update({k: prev[k] for k in keep if k in prev})
    out['hist'] = [r for r in (prev.get('hist') or []) if isinstance(r, list)]
    out['skipped'] = [d for d in (prev.get('skipped') or []) if isinstance(d, str) and _INS_DAY.match(d)][-INS_SKIP_KEEP:]
    errs, n_req, exhausted = [], 0, False
    T = ins_target(now)

    def over():   # budżet czasu: najmniej jedno zapytanie na przebieg (postęp zawsze), potem twarda granica
        return bool(n_req) and time.monotonic() - t0 > budget

    day = out['day'] if isinstance(out.get('day'), str) and _INS_DAY.match(out['day']) else None
    queue = [f for f in (out.get('pending') or []) if isinstance(f, str)] if (day and out.get('done') is False) else []
    if day and not queue and out.get('done') is False:   # stan bez kolejki, a dzień nieskończony — domykamy go z tego, co jest
        out['done'] = True; out['done_at'] = NOW; _ins_final(out)
        for k in ('iss', 'pending', 'tries'):
            out.pop(k, None)
    while True:
        if not queue:
            # nowy dzień: od najstarszego brakującego (po przerwie zbieracza) do T; pierwszy przebieg — INS_BACK_DAYS dni wstecz
            checked = out['checked'] if isinstance(out.get('checked'), str) and _INS_DAY.match(out['checked']) else None
            if (checked and checked >= T.isoformat()) or over():
                break
            first = T - datetime.timedelta(days=INS_BACK_DAYS - 1)
            if day:
                first = max(first, datetime.date.fromisoformat(day) + datetime.timedelta(days=1))
            if checked:
                first = max(first, datetime.date.fromisoformat(checked) + datetime.timedelta(days=1))
            found, rows, d, wait_t = None, [], first, False
            while d <= T:
                if d.isoformat() in out['skipped']:
                    d += datetime.timedelta(days=1); continue
                if over():
                    exhausted = True; break
                try:
                    st, text = _ins_get(ins_idx_url(d), ua); n_req += 1
                except Exception as e:  # noqa — rodzaj błędu decyduje: brak pliku / blokada / sieć
                    n_req += 1
                    kind, msg = _ins_err(e)
                    if kind == 'brak' and d < T:   # dzień bez indeksu (weekend, święto) — zapamiętany, bez ponownych zapytań
                        out['skipped'] = (out['skipped'] + [d.isoformat()])[-INS_SKIP_KEEP:]; out['checked'] = d.isoformat()
                        d += datetime.timedelta(days=1); continue
                    if kind == 'brak':   # ostatni gotowy dzień bez indeksu: może się spóźnić — `checked` bez zmian, ponownie w następnym przebiegu
                        wait_t = True; break
                    errs.append(f'indeks {d.isoformat()}: {msg}'); out['ok']['sec'] = False; exhausted = True; break   # stan bez zmian — dzień ponownie
                if not ins_index_ok(text):
                    errs.append(f'indeks {d.isoformat()}: odpowiedź bez nagłówka indeksu'); out['ok']['sec'] = False; exhausted = True; break
                rows = ins_parse_index(text); found = d; break
            if found is None:
                if not exhausted and not errs and not wait_t:
                    out['checked'] = T.isoformat()   # wszystko do T sprawdzone — do następnego dnia bez zapytań
                break
            day = found.isoformat(); queue = [r[2] for r in rows[:INS_MAX]]
            out.update({'day': day, 'done': False, 'day_at': NOW, 'n_filings': len(rows), 'n_queued': len(queue), 'n_parsed': 0, 'n_fail': 0,
                        'n_buy': 0, 'n_sell': 0, 'buys_usd': 0.0, 'sells_usd': 0.0, 'iss': {}, 'tries': {}, 'checked': day})
            out.pop('done_at', None)
            if not rows:
                out['notes'].append(f'{day}: indeks bez zgłoszeń Form 4')
        # dokumenty zgłoszeń dnia: ≤ 8 zapytań/s, do wyczerpania budżetu czasu. Brak dokumentu (404/410, 403 AccessDenied) = pominięte;
        # blokada urzędu = koniec przebiegu, zgłoszenie zostaje; błąd sieci/serwera = zgłoszenie na koniec kolejki (najwyżej jedna próba
        # na przebieg), po INS_PROBY takich przebiegach pominięte; INS_BLEDY błędów z rzędu = koniec przebiegu
        tries = {k: v for k, v in out['tries'].items() if isinstance(k, str) and isinstance(v, int)} if isinstance(out.get('tries'), dict) else {}
        hit, streak = set(), 0
        while queue:
            if over():
                exhausted = True; break
            f = queue[0]; acc = f.rsplit('/', 1)[-1]
            if acc in hit:   # to zgłoszenie zawiodło już w tym przebiegu — ponowna próba w następnym
                break
            try:
                st, text = _ins_get(INS_DOC_URL.format(f=f), ua); n_req += 1
            except Exception as e:  # noqa — rodzaj błędu decyduje o losie zgłoszenia
                n_req += 1
                kind, msg = _ins_err(e)
                if kind == 'brak':   # zgłoszenie bez dokumentu — pominięte (liczone w n_fail)
                    out['n_fail'] += 1; queue.pop(0); tries.pop(acc, None); streak = 0; continue
                if kind == 'blok':
                    errs.append(f'zgłoszenie {acc}: {msg}'); out['ok']['sec'] = False; exhausted = True; break
                streak += 1; hit.add(acc); tries[acc] = tries.get(acc, 0) + 1; queue.append(queue.pop(0))
                if tries[acc] >= INS_PROBY:
                    queue.pop(); tries.pop(acc, None); out['n_fail'] += 1
                    note = f'{day}: zgłoszenie {acc} pominięte po {INS_PROBY} przebiegach z błędem ({msg})'
                    out['notes'].append(note); META['notes'].append('Insiderzy: ' + note)
                if streak >= INS_BLEDY:
                    errs.append(f'zgłoszenie {acc}: {msg}'); out['ok']['sec'] = False; exhausted = True; break
                continue
            streak = 0; queue.pop(0); tries.pop(acc, None)
            try:
                d = ins_parse_doc(text)
            except ValueError as e:
                out['n_fail'] += 1
                if out['n_fail'] <= 3:
                    out['notes'].append(f'{acc}: {e}')
                continue
            out['n_parsed'] += 1
            if d['buy'] or d['sell']:
                key = d['issuer'] or d['ticker'] or f.split('/')[2]
                rec = out['iss'].get(key) if isinstance(out['iss'].get(key), list) else [d['issuer'] or key, d['ticker'], 0.0, 0.0]
                rec[2] += d['buy']; rec[3] += d['sell']
                if not rec[1] and d['ticker']:
                    rec[1] = d['ticker']
                out['iss'][key] = rec
                out['buys_usd'] = (out.get('buys_usd') or 0.0) + d['buy']; out['sells_usd'] = (out.get('sells_usd') or 0.0) + d['sell']
                out['n_buy'] = (out.get('n_buy') or 0) + (1 if d['n_buy'] else 0); out['n_sell'] = (out.get('n_sell') or 0) + (1 if d['n_sell'] else 0)
        if day and out.get('done') is False:
            out['pending'] = queue; out['tries'] = tries
            out['done'] = not queue
            _ins_final(out)
            if out['done']:
                out['done_at'] = NOW
                for k in ('iss', 'pending', 'tries'):
                    out.pop(k, None)
            else:
                note = f"{day}: dzień w toku — odczytano {out['n_parsed']} z {out['n_queued']} zgłoszeń, reszta w następnym przebiegu"
                out['notes'].append(note); META['notes'].append('Insiderzy: ' + note)
        if queue or exhausted:
            break
    if errs:
        META['errors'].append(mask('Insiderzy: ' + '; '.join(errs)[:300]))
        if not day:
            raise RuntimeError(errs[0])
    for n in out['notes']:
        if 'policzono pierwsze' in n:
            META['notes'].append('Insiderzy: ' + n)
    return out


# --- v121: stres finansowy USA i nastroje na opcjach (bez klucza; obszar stres-opcje) ---
# Indeks stresu: dzienny plik CSV urzędu badawczego przy Skarbie USA (OFR Financial Stress Index — dane rządu USA, domena publiczna, bez warunków użycia;
# publikowany z danymi sprzed 2 dni roboczych — plik z 25.09 kończy się na 23.09; wartość za KAŻDY dzień roboczy, także w święta giełdowe USA
# (25.12, 3.07, 7.09 są w pliku), więc „dni robocze”, nie „sesje”; kolumny: OFR FSI, Credit, Equity valuation, Safe assets, Funding, Volatility,
# United States, Other advanced economies, Emerging markets). Put/call: dzienny plik JSON JEDNEJ giełdy opcji (Cboe, cdn.cboe.com/…/daily/
# RRRR-MM-DD_daily_options; dzień bez pliku = HTTP 403 z treścią XML magazynu plików <Code>AccessDenied</Code>, sprawdzone 26.09.2026; serwer stoi
# za siecią CDN, której blokada też daje 403, ale z inną treścią — to błąd, nie dzień bez sesji: pc_brak_pliku) — warunki giełdy
# („Use of Content”) wymagają wcześniejszej zgody i podpisanej umowy licencyjnej na KAŻDE użycie danych,
# także na bezpłatnym, publicznym panelu z podpisem → część pc włączana dopiero zgodą właściciela wpisaną do CBOE_ZGODA (patrz build_stres).
FSI_URL = 'https://www.financialresearch.gov/financial-stress-index/data/fsi.csv'
FSI_DNI = 400        # dni robocze w pliku (≈ 19 miesięcy): tekst „najniżej / najwyżej z 60 dni roboczych” z zapasem na dłuższe okna
FSI_TIMEOUT = 40     # s — plik ma ok. 0,5 MB (od 2000 r.)
FSI_KOL = (('OFR FSI', 'value'), ('Credit', 'credit'), ('Equity valuation', 'equity'), ('Safe assets', 'safe'), ('Funding', 'funding'),
           ('Volatility', 'vol'), ('United States', 'us'), ('Other advanced economies', 'ae'), ('Emerging markets', 'em'))
PC_URL = 'https://cdn.cboe.com/data/us/options/market_statistics/daily/{d}_daily_options'
PC_DNI = 300         # sesji w pliku
PC_PER_RUN = 10      # najwyżej tyle dni (zapytań) na przebieg — historia dopełniana stopniowo, najnowsze dni najpierw
PC_TEMPO = 0.3       # s między zapytaniami
PC_TIMEOUT = 15
PC_USTALONY = 2      # dni: brak pliku (403/404 z XML magazynu plików — pc_brak_pliku) dla dnia starszego niż tyle dni = dzień bez sesji (zapamiętany, nie pytamy ponownie); młodszy — plik może dopiero powstać
PC_NAZWY = {'TOTAL PUT/CALL RATIO': 'total', 'EQUITY PUT/CALL RATIO': 'equity', 'INDEX PUT/CALL RATIO': 'index'}
PC_ZGODA = os.environ.get('CBOE_ZGODA', '').strip()   # pisemna zgoda giełdy (treść dowolna, np. data i numer umowy); pusta = część put/call wyłączona
STRES_EVERY = 6 * 60   # min — oba źródła są dzienne; co 6 h wystarczy
STRES_RETRY = 60       # min — część z błędem ponawiana po godzinie


def fsi_parse(text, dni=None):
    """CSV indeksu stresu (Date, OFR FSI, składowe, regiony) → {'date', 'value', 'd1', 'd5', 'hist': [[dzień, wartość], …], 'cols': {klucz: {'v', 'd1'}}}.
    Ostatnie `dni` dni z liczbą w kolumnie głównej (dzień bez liczby wypada — brak nie jest zerem); zmiany: d1 = wobec poprzedniego dnia z serii,
    d5 = wobec 5 dni serii wcześniej (dni robocze — indeks ma wartość za każdy dzień roboczy, także w święta giełdowe USA; nie kalendarz);
    składowe: ostatnia wartość i zmiana dzienna, brak → None. Zła treść = wyjątek."""
    dni = FSI_DNI if dni is None else dni
    rd = csv.reader(io.StringIO(str(text or '').lstrip('﻿')))
    try:
        head = [h.strip() for h in next(rd)]
    except StopIteration:
        raise ValueError('pusty plik')
    if 'Date' not in head or 'OFR FSI' not in head:
        raise ValueError('nieznany nagłówek: ' + ', '.join(head[:4])[:80])
    idx = {k: head.index(n) for n, k in FSI_KOL if n in head}
    di = head.index('Date')
    rows = {}
    for r in rd:
        d = r[di].strip() if len(r) > di else ''
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', d):
            continue
        rec = {}
        for k, i in idx.items():
            try:
                v = float(r[i]); rec[k] = v if v == v and abs(v) != float('inf') else None
            except (IndexError, ValueError, TypeError):
                rec[k] = None
        if rec.get('value') is None:
            continue
        rows[d] = rec
    if not rows:
        raise ValueError('brak wierszy z liczbą')
    days = sorted(rows)[-dni:]
    last = rows[days[-1]]

    def diff(k, n):   # zmiana wobec n-tego dnia serii wstecz — tylko z dwóch liczb
        if len(days) <= n:
            return None
        a, b = last.get(k), rows[days[-1 - n]].get(k)
        return round(a - b, 3) if a is not None and b is not None else None
    cols = {k: {'v': last.get(k), 'd1': diff(k, 1)} for _, k in FSI_KOL if k != 'value' and k in idx}
    return {'date': days[-1], 'value': last['value'], 'd1': diff('value', 1), 'd5': diff('value', 5), 'hist': [[d, rows[d]['value']] for d in days], 'cols': cols}


def pc_parse(j):
    """JSON dnia (ratios[]: name, value) → {'total', 'equity', 'index'}: liczba > 0 albo None („0.00” = brak obrotu, nie wskaźnik); bez listy = wyjątek."""
    R = j.get('ratios') if isinstance(j, dict) else None
    if not isinstance(R, list):
        raise ValueError('brak listy ratios')
    out = {k: None for k in PC_NAZWY.values()}
    for r in R:
        k = PC_NAZWY.get(str(r.get('name', '')).strip().upper()) if isinstance(r, dict) else None
        if not k:
            continue
        try:
            v = float(str(r.get('value')).replace(',', '.'))
        except (TypeError, ValueError):
            continue
        if v > 0 and v == v and v != float('inf'):
            out[k] = round(v, 4)
    if all(v is None for v in out.values()):
        raise ValueError('brak wskaźników w ratios')
    return out


def pc_prog(today, dni=None):
    """Najstarszy dzień kalendarzowy okna put/call: dni sesji × 1,5 + 10 (weekendy i święta) — dalej nie pytamy."""
    return today - datetime.timedelta(days=int((PC_DNI if dni is None else dni) * 1.5) + 10)


def pc_dni(maja, brak, today, n=None, dni=None):
    """Dni do pobrania w tym przebiegu: od dziś wstecz, tylko dni robocze, bez dni już w historii (maja) i dni bez sesji (brak); najwyżej n.
    Pełna historia (≥ dni wierszy) nie sięga dalej niż jej najstarszy dzień — bez pytań o dni, które i tak wypadłyby z okna."""
    n = PC_PER_RUN if n is None else n
    dni = PC_DNI if dni is None else dni
    floor = pc_prog(today, dni)
    if len(maja) >= dni:
        floor = max(floor, datetime.date.fromisoformat(min(maja)))
    out, d = [], today
    while d >= floor and len(out) < n:
        s = d.isoformat()
        if d.weekday() < 5 and s not in maja and s not in brak:
            out.append(d)
        d -= datetime.timedelta(days=1)
    return out


def pc_brak_pliku(e):
    """Czy odpowiedź 403/404 to naprawdę „pliku dla tego dnia nie ma”: tylko XML magazynu plików giełdy z <Code>AccessDenied</Code> albo
    <Code>NoSuchKey</Code> (tak odpowiada dzień bez sesji — sprawdzone 26.09.2026). Każda inna treść (strona blokady sieci CDN przed serwerem,
    pusta odpowiedź, błąd odczytu) = False — wtedy to błąd, a dzień NIE jest zapisywany jako dzień bez sesji."""
    try:
        b = e.read(4096)
    except Exception:  # noqa
        return False
    finally:
        try:
            e.close()   # odpowiedź przeczytana — zwalniamy połączenie
        except Exception:  # noqa
            pass
    b = b.decode('utf-8', 'replace') if isinstance(b, (bytes, bytearray)) else str(b or '')
    return bool(re.search(r'<Code>\s*(?:AccessDenied|NoSuchKey)\s*</Code>', b))


def pc_czesc(prev, now=None, errors=None, sleep=None):
    """Część put/call: historia z poprzedniego pliku + do PC_PER_RUN nowych dni (najnowsze najpierw, potem zaległe wstecz do PC_DNI sesji).
    Dzień bez pliku (403/404 z XML magazynu plików — pc_brak_pliku): starszy niż PC_USTALONY dni = dzień bez sesji (zapamiętany w 'brak'),
    młodszy = plik może jeszcze nie istnieć (spróbujemy w następnym przebiegu). 403/404 z inną treścią (np. blokada sieci CDN wobec serwera
    automatu) i każdy inny błąd = błąd i koniec części w tym przebiegu (co jest — zostaje; żaden dzień nie trafia wtedy do 'brak').
    Zwraca (część, liczba pobranych dni)."""
    now = now or _now_utc()
    today = now.date()
    sleep = time.sleep if sleep is None else sleep
    errors = errors if isinstance(errors, list) else []
    P = prev if isinstance(prev, dict) else {}
    hist = {}
    for r in P.get('hist') if isinstance(P.get('hist'), list) else []:
        if isinstance(r, list) and len(r) == 4 and isinstance(r[0], str) and re.match(r'^\d{4}-\d{2}-\d{2}$', r[0]):
            hist[r[0]] = [r[0]] + [v if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None for v in r[1:]]
    brak = {b for b in (P.get('brak') if isinstance(P.get('brak'), list) else []) if isinstance(b, str) and re.match(r'^\d{4}-\d{2}-\d{2}$', b)}
    got = 0
    for i, d in enumerate(pc_dni(set(hist), brak, today)):
        if i:
            sleep(PC_TEMPO)
        s = d.isoformat()
        try:
            r = pc_parse(get_json(PC_URL.format(d=s), timeout=PC_TIMEOUT))
            hist[s] = [s, r['total'], r['equity'], r['index']]; got += 1
        except urllib.error.HTTPError as e:
            if e.code in (403, 404) and pc_brak_pliku(e):
                if (today - d).days >= PC_USTALONY:
                    brak.add(s)
                continue
            errors.append(f'put/call {s}: HTTP {e.code}' + (' bez znacznika braku pliku' if e.code in (403, 404) else '')); break
        except Exception as e:  # noqa
            errors.append(mask(f'put/call {s}: {e}')); break
    if not hist:
        raise ValueError('brak danych')
    rows = [hist[k] for k in sorted(hist)][-PC_DNI:]
    last = rows[-1]
    prog = pc_prog(today).isoformat()
    return {'date': last[0], 'total': last[1], 'equity': last[2], 'index': last[3], 'hist': rows, 'n': len(rows),
            'brak': sorted(b for b in brak if b >= prog)}, got


def stres_czesci():
    """Części pliku stres.json oczekiwane w tym przebiegu: indeks stresu zawsze; put/call tylko ze zgodą giełdy."""
    return ('fsi', 'pc') if PC_ZGODA else ('fsi',)


def build_stres(prev=None, now=None):
    """data/stres.json — część fsi: dzienny indeks stresu finansowego (wartość, zmiana 1 dnia i 5 dni roboczych, 400 dni roboczych historii,
    składowe i regiony); część pc: dzienne wskaźniki put/call jednej giełdy opcji USA (razem, akcje, indeksy; do 300 sesji, dopełniane po 10 dni
    na przebieg, dni bez sesji — tylko potwierdzone XML magazynu plików — zapamiętane). Każda część osobno: błąd = poprzednia wersja tej części
    z własnym czasem (part_at); brak liczby = None, nigdy zero;
    nic i bez poprzedniego pliku = wyjątek.
    Licencje (sprawdzone 26.09.2026): indeks stresu — praca rządu USA (Office of Financial Research, Skarb USA): domena publiczna, podpis
    niewymagany (zdanie o legalnych źródłach publicznych na stronie Źródła wystarcza). Put/call — strona giełdy Cboe „Use of Content”
    (cboe.com/use-of-content) mówi, że użycie JAKICHKOLWIEK danych giełdy — także na publicznym, bezpłatnym panelu z podpisem — wymaga
    wcześniejszej zgody i podpisanej umowy licencyjnej (wniosek na adres podany na tej stronie, odpowiedź zwykle do 5 dni roboczych). Dlatego część pc
    jest wyłączona (pc_off = True, w pliku nie ma klucza 'pc'), dopóki właściciel nie wpisze zgody do zmiennej środowiskowej CBOE_ZGODA
    (np. w strona.yml: CBOE_ZGODA: ${{ vars.CBOE_ZGODA }} — treść dowolna, np. data i numer umowy); bez zgody stare dane put/call
    z poprzedniego pliku też nie są przepisywane, a strona pokazuje wersję samego indeksu (tytuł, opis i nota bez put/call, bez żadnej noty
    o wyłączeniu). Po zgodzie: dopisać podpis giełdy na stronie Źródła (zrCredits), jeśli umowa go wymaga."""
    now = now or _now_utc()
    prev = prev if isinstance(prev, dict) else {}
    pat = prev.get('part_at') if isinstance(prev.get('part_at'), dict) else {}
    out = {'at': NOW, 'src': 'Dzienny indeks stresu finansowego liczony z cen rynkowych (urząd badawczy przy Skarbie USA, plik CSV, domena publiczna)'
                            + ('; dzienne wskaźniki put/call z giełdy opcji USA (plik JSON) — za pisemną zgodą giełdy' if PC_ZGODA else ''),
           'ok': {}, 'part_at': {}, 'pc_off': not PC_ZGODA}
    errs = []

    def keep(k):   # część z błędem: poprzednia wersja z własnym czasem
        out['ok'][k] = False
        if isinstance(prev.get(k), dict):
            out[k] = prev[k]; out['part_at'][k] = pat.get(k) or prev.get('at')

    try:
        _st, body = get(FSI_URL, timeout=FSI_TIMEOUT)
        out['fsi'] = fsi_parse(body); out['ok']['fsi'] = True; out['part_at']['fsi'] = NOW
    except Exception as e:  # noqa
        errs.append(mask(f'indeks stresu: {e}')); keep('fsi')
    if PC_ZGODA:
        pe = []
        try:
            out['pc'], got = pc_czesc(prev.get('pc'), now, pe)
            out['ok']['pc'] = not pe
            out['part_at']['pc'] = NOW if (got or not pe) else (pat.get('pc') or prev.get('at') or NOW)
        except Exception as e:  # noqa
            pe.append(mask(f'put/call: {e}')); keep('pc')
        errs += pe
    if errs:
        META['errors'].append(mask('Stres: ' + '; '.join(errs)[:400]))
    if not any(isinstance(out.get(k), dict) for k in ('fsi', 'pc')):
        raise RuntimeError('brak danych i poprzedniego pliku')
    return out


# --- v121: aukcje papierów skarbowych USA — popyt (bez klucza; dane rządu USA, domena publiczna) ---
# Źródło główne: interfejs danych fiskalnych Skarbu USA (Fiscal Data, zbiór „Treasury Securities Auctions Data”,
# https://fiscaldata.treasury.gov/datasets/treasury-securities-auctions-data/): jedno zapytanie, wszystkie rodzaje papierów, tylko potrzebne
# pola (~180 KB na 13 miesięcy; ten sam host, z którego automat bierze saldo TGA). Zapas: serwis aukcyjny Skarbu (TreasuryDirect TA_WS,
# https://www.treasurydirect.gov/TA_WS/securities/search): jedno zapytanie na rodzaj papieru (bony ≈ 1,3 MB). Oba: dzieła rządu USA,
# domena publiczna (17 U.S.C. § 105), bez klucza. Nazwy urzędów i interfejsów — tutaj i na stronie Źródła; panel opisuje dane zwykłymi słowami.
import statistics   # v121: mediany aukcji (biblioteka standardowa)

AUK_EVERY = 6 * 60        # minuty: plik młodszy = z pamięci (wyniki aukcji przybywają raz dziennie, ok. 13:00 czasu Nowego Jorku)
AUK_RETRY = 60            # minuty: plik z częścią z błędem (np. zbudowany z zapasu) ponawiany po godzinie
AUK_HIST_DAYS = 400       # dni wstecz w zapytaniu: okno median (12 miesięcy) z zapasem
AUK_MED_DAYS = 365        # okno median: 12 miesięcy wstecz od najnowszej aukcji z wynikami
AUK_MED_MIN = 3           # mniej aukcji tego papieru w oknie = brak mediany (nie „mediana” z dwóch liczb)
AUK_LAST = 40             # ile ostatnich aukcji z wynikami trafia do pliku (strona pokazuje 12)
AUK_PAGE = 1500           # wierszy na stronę odpowiedzi (rok ≈ 480 aukcji)
AUK_BUDGET_S = 150        # zapas: po tylu sekundach nie zaczynamy kolejnego zapytania o rodzaj papieru
AUK_PARTS = ('last', 'med12m', 'full')   # full = źródło główne odpowiedziało i nic nie zabrakło
AUK_TYPES = ('Bill', 'Note', 'Bond', 'TIPS', 'FRN')   # CMB (bony zarządzania gotówką) pominięte: nieregularne, mediana nie ma sensu
AUK_TD_TYPES = ('Bill', 'Note', 'Bond', 'TIPS', 'FRN')
AUK_FD_FIELDS = ('auction_date', 'security_type', 'security_term', 'original_security_term', 'reopening', 'cash_management_bill_cmb',
                 'floating_rate', 'inflation_index_security', 'bid_to_cover_ratio', 'comp_accepted', 'indirect_bidder_accepted',
                 'direct_bidder_accepted', 'primary_dealer_accepted', 'total_accepted', 'high_yield', 'high_investment_rate',
                 'high_discnt_margin', 'cusip')
AUK_FD_URL = ('https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query'
              '?sort=-auction_date&page[size]={n}&filter=auction_date:gte:{od}&fields=' + ','.join(AUK_FD_FIELDS))
AUK_TD_URL = ('https://www.treasurydirect.gov/TA_WS/securities/search?format=json&type={typ}&dateFieldName=auctionDate'
              '&startDate={od}&endDate={do}')
AUK_SRC = ('Skarb USA — urzędowe wyniki aukcji papierów skarbowych (interfejs danych fiskalnych; zapas: serwis aukcyjny Skarbu); '
           'dane rządu USA, domena publiczna; udziały kupujących liczone od kwot przyjętych w części konkurencyjnej')
AUK_NOTES = ['aukcje bez wyników (przyszłe) pominięte — brak nie jest zerem',
             'bony zarządzania gotówką (CMB) pominięte: nieregularne, bez mediany',
             'udziały kupujących = procent kwot przyjętych w części konkurencyjnej (pośredni + bezpośredni + dealerzy = 100%)',
             'termin bieżący, gdy jest pełną liczbą lat albo tygodni (bon 6-tygodniowy formalnie „otwiera ponownie” 26-tygodniowy; 2-latka sprzedana jako dodatkowa transza starej 5-latki to 2-latka); termin pierwotny tylko przy niepełnym terminie dodatkowej transzy (9 lat 11 mies. → 10 lat); znacznik dodatkowej transzy tylko przy obligacjach',
             'rentowność: obligacje — najwyższa przyjęta; bony — stopa inwestycyjna; indeksowane inflacją — realna; zmienna stopa — marża dyskontowa',
             f'mediana: aukcje tego samego papieru z {AUK_MED_DAYS} dni od najnowszej, co najmniej {AUK_MED_MIN}']


def _auk_num(v):
    """Liczba z pola tekstowego interfejsu („2.420000”, „null”, „”); śmieci, nieskończoność i NaN = None — nigdy 0."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
    else:
        s = str(v).strip()
        if not s or s.lower() == 'null':
            return None
        try:
            x = float(s)
        except ValueError:
            return None
    return x if x == x and abs(x) != float('inf') else None


def _auk_yes(v):
    return str(v or '').strip().lower() == 'yes'


def auk_wiersz(r, api):
    """Jeden wiersz aukcji w kształcie pliku albo None: bez wyników (przyszła aukcja), CMB, nieznany rodzaj, zła data.
    api: 'fiscaldata' (pola z podkreśleniami) albo 'treasurydirect' (pola camelCase). Udziały kupujących pośrednich / bezpośrednich /
    dealerów = procent kwot przyjętych w części konkurencyjnej (razem 100%); brak którejś kwoty = None. Rentowność wg rodzaju papieru."""
    if not isinstance(r, dict):
        return None
    if api == 'fiscaldata':
        typ = str(r.get('security_type') or '').strip()
        if _auk_yes(r.get('cash_management_bill_cmb')):
            typ = 'CMB'
        elif _auk_yes(r.get('floating_rate')):
            typ = 'FRN'
        elif _auk_yes(r.get('inflation_index_security')):
            typ = 'TIPS'
        date = str(r.get('auction_date') or '')[:10]
        term_now, term_org, reopen = r.get('security_term'), r.get('original_security_term'), _auk_yes(r.get('reopening'))
        btc, comp = _auk_num(r.get('bid_to_cover_ratio')), _auk_num(r.get('comp_accepted'))
        ind, dr, pd, tot = (_auk_num(r.get(k)) for k in ('indirect_bidder_accepted', 'direct_bidder_accepted', 'primary_dealer_accepted', 'total_accepted'))
        hy, hi, hm = (_auk_num(r.get(k)) for k in ('high_yield', 'high_investment_rate', 'high_discnt_margin'))
    else:
        typ = str(r.get('type') or '').strip()
        date = str(r.get('auctionDate') or '')[:10]
        term_now, term_org, reopen = r.get('securityTerm'), r.get('originalSecurityTerm'), _auk_yes(r.get('reopening'))
        btc, comp = _auk_num(r.get('bidToCoverRatio')), _auk_num(r.get('competitiveAccepted'))
        ind, dr, pd, tot = (_auk_num(r.get(k)) for k in ('indirectBidderAccepted', 'directBidderAccepted', 'primaryDealerAccepted', 'totalAccepted'))
        hy, hi, hm = (_auk_num(r.get(k)) for k in ('highYield', 'highInvestmentRate', 'highDiscountMargin'))
    if typ not in AUK_TYPES or not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return None
    try:   # data niemożliwa (np. miesiąc 19) = wiersz pominięty; inaczej mediany wywróciłyby cały przebieg po udanym pobraniu
        datetime.date.fromisoformat(date)
    except ValueError:
        return None
    if btc is None or btc <= 0 or tot is None or tot <= 0:   # bez wyników (przyszła aukcja) — pomijamy; zero nie jest wynikiem
        return None
    # termin: bieżący, gdy jest pełną liczbą lat albo tygodni — bony zawsze, a także 2-latka sprzedana jako dodatkowa transza starej 5-latki
    # (26.01.2026: termin „2-Year”, pierwotny „5-Year” — to aukcja 2-latki); pierwotny tylko przy niepełnym terminie dodatkowej transzy
    # („9-Year 11-Month” → 10 lat, „1-Year 10-Month” zmiennej stopy → 2 lata)
    term_now_s = str(term_now or '').strip()
    if typ == 'Bill' or re.match(r'^\d+-(Year|Week)$', term_now_s):
        term = term_now_s
    else:
        term = str(term_org or term_now_s).strip()
    if not term:
        return None
    reopen = bool(reopen) and typ != 'Bill'   # bony: prawie każda emisja formalnie „otwiera ponownie” starszy CUSIP — dla czytelnika to nie jest informacja
    if comp is None and None not in (ind, dr, pd):
        comp = ind + dr + pd
    pct = lambda x: round(x / comp * 100, 1) if (x is not None and comp is not None and comp > 0) else None   # noqa: E731
    if typ == 'Bill':
        y, yk = hi, 'inv'
    elif typ == 'FRN':
        y, yk = hm, 'dm'
    elif typ == 'TIPS':
        y, yk = hy, 'real'
    else:
        y, yk = hy, 'yld'
    cusip = str(r.get('cusip') or '').strip()
    return {'date': date, 'type': typ, 'term': term, 'k': f'{typ} {term}', 'reopen': reopen, 'btc': round(btc, 2),
            'indirect_pct': pct(ind), 'direct_pct': pct(dr), 'dealer_pct': pct(pd), 'yield': y, 'ykind': yk,
            'accepted_bln': round(tot / 1e9, 3), 'cusip': cusip or None}


def auk_mediany(rows):
    """Mediany 12 miesięcy dla każdego papieru (klucz „rodzaj termin”, np. „Note 10-Year”) z aukcji w oknie AUK_MED_DAYS od najnowszej;
    mniej niż AUK_MED_MIN aukcji = brak mediany (papier nieobecny w wyniku). Wynik: {klucz: {btc, indirect_pct, n, from, to}}."""
    if not rows:
        return {}
    newest = max(r['date'] for r in rows)
    od = (datetime.date.fromisoformat(newest) - datetime.timedelta(days=AUK_MED_DAYS)).isoformat()
    grp = {}
    for r in rows:
        if r['date'] >= od:
            grp.setdefault(r['k'], []).append(r)
    out = {}
    for k in sorted(grp):
        g = grp[k]
        b = [r['btc'] for r in g if r['btc'] is not None]
        i = [r['indirect_pct'] for r in g if r['indirect_pct'] is not None]
        if len(b) < AUK_MED_MIN:
            continue
        out[k] = {'btc': round(statistics.median(b), 2), 'indirect_pct': round(statistics.median(i), 1) if len(i) >= AUK_MED_MIN else None,
                  'n': len(b), 'from': min(r['date'] for r in g), 'to': max(r['date'] for r in g)}
    return out


def auk_fd(now):
    """Źródło główne: jedno zapytanie o aukcje od AUK_HIST_DAYS dni wstecz (wszystkie rodzaje, wybrane pola). Wynik: wiersze z wynikami."""
    od = (now.date() - datetime.timedelta(days=AUK_HIST_DAYS)).isoformat()
    j = get_json(AUK_FD_URL.format(n=AUK_PAGE, od=od), timeout=90)
    data = j.get('data') if isinstance(j, dict) else None
    if not isinstance(data, list):
        raise ValueError('odpowiedź bez listy „data”')
    rows = [w for w in (auk_wiersz(r, 'fiscaldata') for r in data) if w]
    if not rows:
        raise ValueError('brak aukcji z wynikami')
    return rows


def auk_td(now, errors, t0=None):
    """Zapas: serwis aukcyjny Skarbu, jedno zapytanie na rodzaj papieru (zakres dat MM/DD/RRRR). Rodzaj z błędem = wpis w errors,
    reszta zostaje; po AUK_BUDGET_S s bez kolejnych zapytań. Zwraca (wiersze, rodzaje pobrane); nic = wyjątek."""
    t0 = time.monotonic() if t0 is None else t0
    od = (now.date() - datetime.timedelta(days=AUK_HIST_DAYS)).strftime('%m/%d/%Y')
    do = (now.date() + datetime.timedelta(days=14)).strftime('%m/%d/%Y')
    rows, got = [], []
    for typ in AUK_TD_TYPES:
        if time.monotonic() - t0 > AUK_BUDGET_S:
            errors.append(f'serwis aukcyjny {typ}: pominięty (limit czasu)')
            continue
        try:
            j = get_json(AUK_TD_URL.format(typ=typ, od=od, do=do), timeout=60)
            if not isinstance(j, list):
                raise ValueError('odpowiedź nie jest listą')
            rows += [w for w in (auk_wiersz(r, 'treasurydirect') for r in j) if w]
            got.append(typ)
        except Exception as e:  # noqa
            errors.append(mask(f'serwis aukcyjny {typ}: {e}'))
    if not rows:
        raise ValueError('brak aukcji z wynikami')
    return rows, got


def build_aukcje(prev=None, now=None):
    """data/aukcje.json — popyt na aukcjach papierów skarbowych USA: ostatnie AUK_LAST aukcji z wynikami (data, rodzaj, termin, stosunek
    ofert do sprzedaży, udziały kupujących pośrednich / bezpośrednich / dealerów, rentowność, sprzedano w mld USD) i mediany 12 miesięcy
    dla każdego papieru. Źródło główne: interfejs danych fiskalnych Skarbu USA; zapas: serwis aukcyjny Skarbu (rodzaj po rodzaju).
    Aukcje bez wyników (przyszłe) pominięte, nigdy zero; brak liczby = None. Oba źródła zawiodły = wyjątek (main zostawia poprzedni plik).
    Licencja: dzieła rządu USA, domena publiczna (17 U.S.C. § 105); Fiscal Data i TreasuryDirect nie wymagają klucza ani podpisu.
    `prev` nieużywany (każdy przebieg pobiera pełne okno) — zostaje dla jednolitego wywołania z main()."""
    now = now or _now_utc()
    t0 = time.monotonic()
    errors, brak = [], []
    try:
        rows, api = auk_fd(now), 'fiscaldata'
    except Exception as e:  # noqa
        errors.append(mask(f'dane fiskalne: {e}'))
        try:
            rows, got = auk_td(now, errors, t0)
        except Exception as e2:  # noqa
            raise RuntimeError('; '.join(errors + [mask(str(e2))]))
        api = 'treasurydirect'
        brak = [t for t in AUK_TD_TYPES if t not in got]
    seen, uniq = set(), []
    for r in rows:   # jeden wiersz na aukcję (CUSIP + dzień; bez CUSIP — papier + dzień)
        key = (r['cusip'] or r['k'], r['date'])
        if key in seen:
            continue
        seen.add(key); uniq.append(r)
    uniq.sort(key=lambda r: r['date'], reverse=True)   # stabilnie: aukcje tego samego dnia w kolejności źródła
    last = uniq[:AUK_LAST]
    med = auk_mediany(uniq)
    out = {'at': NOW, 'src': AUK_SRC, 'api': api, 'ok': {'last': bool(last), 'med12m': bool(med), 'full': not errors},
           'last': last, 'med12m': med, 'n': len(uniq), 'from': uniq[-1]['date'], 'to': uniq[0]['date'], 'notes': list(AUK_NOTES)}
    if brak:
        out['notes'].append('zapas bez rodzajów: ' + ', '.join(brak))
    if errors:
        META['errors'].append(mask('Aukcje: ' + '; '.join(errors)[:400]))
    return out


def main():
    SAVED.clear()      # v89: TRENDY liczone tylko z plików tego przebiegu
    _DEADLINE[0] = time.monotonic() + SOSO_BUDGET
    _RUN_T0[0] = time.monotonic()      # v95.2: historia wstecz tylko, gdy przebieg nie jest już długi
    _BACK_LATE_NOTE[0] = False
    soso_key = os.environ.get('SOSOVALUE_KEY', '').strip()
    cg_key = os.environ.get('COINGECKO_KEY', '').strip()
    fh_key = os.environ.get('FINNHUB_KEY', '').strip()
    td_key = os.environ.get('TWELVEDATA_KEY', '').strip()
    cmc_key = os.environ.get('COINMARKETCAP_KEY', '').strip()
    fred_key = os.environ.get('FRED_KEY', '').strip()
    eia_key, bls_key, bea_key = (os.environ.get(k, '').strip() for k in ('EIA_KEY', 'BLS_KEY', 'BEA_KEY'))   # v97
    SECRETS[:] = [k for k in (soso_key, cg_key, fh_key, td_key, cmc_key, fred_key, eia_key, bls_key, bea_key) if k]
    # ETF — dane dzienne: SoSoValue pytamy najwyżej raz na godzinę (oszczędza limit 100 000/mies.),
    # między odświeżeniami zachowujemy plik z opublikowanej strony (pole "at" mówi, kiedy pobrano)
    prev_etf = previous('etf') if soso_key else None
    if soso_key and prev_etf and fresh(prev_etf, 55):
        save('etf', prev_etf); META['ok']['sosovalue'] = 'cached'; print('ETF: dane z', prev_etf.get('at'), '— młodsze niż 55 min, bez zapytań do SoSoValue')
    elif soso_key:
        try:
            save('etf', build_etf(soso_key, cg_key, prev_etf)); META['ok']['sosovalue'] = True
        except Exception as e:
            META['errors'].append(mask(f'SoSoValue: {e}')); META['ok']['sosovalue'] = False
            if prev_etf: save('etf', prev_etf); print('SoSoValue zawiódł — zachowano poprzedni etf.json z', prev_etf.get('at'))
    else:
        META['errors'].append('brak SOSOVALUE_KEY'); META['ok']['sosovalue'] = False
    # DZIŚ (Finnhub, klucz właściciela) — decyzja właściciela 24.09 wieczór
    if fh_key:
        try:
            save('dzis', build_day(fh_key)); META['ok']['finnhub'] = True
        except Exception as e:
            META['errors'].append(mask(f'Finnhub: {e}')); META['ok']['finnhub'] = False
            prev = previous('dzis')
            if prev: save('dzis', prev); print('Finnhub zawiódł — zachowano poprzedni dzis.json z', prev.get('at'))
    else:
        META['errors'].append('brak FINNHUB_KEY'); META['ok']['finnhub'] = False
    # CENY (okresy 1T i 1M, Twelve Data, klucz właściciela): najwyżej raz na godzinę — 24 × 14 kredytów = 336 z 800 dziennie
    prev_ceny = previous('ceny') if td_key else None
    if td_key and prev_ceny and fresh(prev_ceny, 55):
        save('ceny', prev_ceny); META['ok']['twelvedata'] = 'cached'; print('CENY: dane z', prev_ceny.get('at'), '— młodsze niż 55 min, bez zapytań do Twelve Data')
    elif td_key:
        try:
            save('ceny', build_prices(td_key)); META['ok']['twelvedata'] = True
        except Exception as e:
            META['errors'].append(mask(f'Twelve Data: {e}')); META['ok']['twelvedata'] = False
            if prev_ceny: save('ceny', prev_ceny); print('Twelve Data zawiódł — zachowano poprzedni ceny.json z', prev_ceny.get('at'))
    else:
        META['errors'].append('brak TWELVEDATA_KEY'); META['ok']['twelvedata'] = False
    # CMC (CoinMarketCap, klucz właściciela): global metrics co przebieg (limit planu Basic: 10 000 kredytów/mies.; 72/dzień)
    if cmc_key:
        try:
            save('cmc', build_cmc(cmc_key)); META['ok']['coinmarketcap'] = True
        except Exception as e:
            META['errors'].append(mask(f'CoinMarketCap: {e}')); META['ok']['coinmarketcap'] = False
            prev = previous('cmc')
            if prev: save('cmc', prev)
    else:
        META['errors'].append('brak COINMARKETCAP_KEY'); META['ok']['coinmarketcap'] = False
    # FRED (klucz właściciela): osiem serii Fed (v50: + depozyt H.4.1), najwyżej raz na 55 min; przy awarii zachowaj poprzedni plik
    prev_fred = previous('fred') if fred_key else None
    if fred_key and prev_fred and fresh(prev_fred, 55):
        save('fred', prev_fred); META['ok']['fred'] = 'cached'; print('FRED: dane z', prev_fred.get('at'), '— młodsze niż 55 min, bez zapytań do FRED')
    elif fred_key:
        try:
            save('fred', build_fred(fred_key)); META['ok']['fred'] = True
        except Exception as e:
            META['errors'].append(mask(f'FRED: {e}')); META['ok']['fred'] = False
            if prev_fred: save('fred', prev_fred); print('FRED zawiódł — zachowano poprzedni fred.json z', prev_fred.get('at'))
    else:
        META['errors'].append('brak FRED_KEY'); META['ok']['fred'] = False
    # TIC (Skarb USA, bez klucza, ~1,6 MB): najwyżej raz na dobę; przy awarii zachowaj poprzedni plik
    prev_tic = previous('tic')
    if prev_tic and fresh(prev_tic, 24 * 60) and 'twn' in prev_tic:   # v50: plik sprzed v48 (bez netto ze wspólnych krajów) pobieramy od nowa
        save('tic', prev_tic); META['ok']['tic'] = 'cached'; print('TIC: dane z', prev_tic.get('at'), '— młodsze niż doba')
    else:
        try:
            save('tic', build_tic()); META['ok']['tic'] = True
        except Exception as e:
            META['errors'].append(mask(f'TIC: {e}')); META['ok']['tic'] = False
            if prev_tic: save('tic', prev_tic); print('TIC zawiódł — zachowano poprzedni tic.json z', prev_tic.get('at'))
    # BIS LBS (bez klucza, ~220 KB, dane kwartalne): najwyżej raz na dobę; przy awarii zachowaj poprzedni plik
    prev_bis = previous('bis')
    if prev_bis and fresh(prev_bis, 24 * 60):
        save('bis', prev_bis); META['ok']['bis'] = 'cached'; print('BIS: dane z', prev_bis.get('at'), '— młodsze niż doba')
    else:
        try:
            save('bis', build_bis()); META['ok']['bis'] = True
        except Exception as e:
            META['errors'].append(mask(f'BIS: {e}')); META['ok']['bis'] = False
            if prev_bis: save('bis', prev_bis); print('BIS zawiódł — zachowano poprzedni bis.json z', prev_bis.get('at'))
    # CFTC TFF (v50, bez klucza): raport w piątki ok. 19:30 UTC (stan na wtorek) — pytamy najwyżej raz na 6 h; przy awarii poprzedni plik
    prev_cftc = previous('cftc')
    if prev_cftc and fresh(prev_cftc, 360):
        save('cftc', prev_cftc); META['ok']['cftc'] = 'cached'; print('CFTC: dane z', prev_cftc.get('at'), '— młodsze niż 6 h')
    else:
        try:
            save('cftc', build_cftc(prev=prev_cftc)); META['ok']['cftc'] = True
        except Exception as e:
            META['errors'].append(mask(f'CFTC: {e}')); META['ok']['cftc'] = False
            if prev_cftc: save('cftc', prev_cftc); print('CFTC zawiódł — zachowano poprzedni cftc.json z', prev_cftc.get('at'))
    # v92: CFTC — surowce (raport disaggregated): publikacja w piątki — najwyżej co 6 h; przy awarii poprzedni plik
    prev_su = previous('surowce')
    if prev_su and fresh(prev_su, 360):
        save('surowce', prev_su); META['ok']['surowce'] = 'cached'
    else:
        try:
            save('surowce', build_surowce(prev=prev_su)); META['ok']['surowce'] = True
        except Exception as e:
            META['errors'].append(mask(f'CFTC surowce: {e}')); META['ok']['surowce'] = False
            if prev_su: save('surowce', prev_su)
    # COIN METRICS (Community, bez klucza): dane dzienne (nowy dzień ok. 02–03 UTC) — plik młodszy niż 60 min bez zapytań;
    # przy awarii zachowaj poprzedni plik (pole "at" mówi, jak stary)
    prev_cm = previous('cm')
    if prev_cm and fresh(prev_cm, 60):
        save('cm', prev_cm); META['ok']['cm'] = 'cached'; print('Coin Metrics: dane z', prev_cm.get('at'), '— młodsze niż 60 min')
    else:
        try:
            save('cm', build_cm()); META['ok']['cm'] = True
        except Exception as e:
            msg = str(e)
            META['errors'].append(mask(msg if msg.startswith('Coin Metrics') else f'Coin Metrics: {msg}')); META['ok']['cm'] = False
            if prev_cm: save('cm', prev_cm); print('Coin Metrics zawiódł — zachowano poprzedni cm.json z', prev_cm.get('at'))
    # MFW — rezerwy walutowe (International Liquidity, bez klucza): dane miesięczne, najwyżej raz na dobę; przy awarii poprzedni plik
    prev_res = previous('rezerwy')
    if prev_res and fresh(prev_res, 24 * 60):
        save('rezerwy', prev_res); META['ok']['imf'] = 'cached'; print('MFW: dane z', prev_res.get('at'), '— młodsze niż doba')
    else:
        try:
            save('rezerwy', build_rezerwy()); META['ok']['imf'] = True
        except Exception as e:
            META['errors'].append(mask(f'MFW rezerwy: {e}')); META['ok']['imf'] = False
            if prev_res: save('rezerwy', prev_res); print('MFW zawiódł — zachowano poprzedni rezerwy.json z', prev_res.get('at'))
    # STOPY banków centralnych (BIS, bez klucza): najwyżej co 6 h; przy awarii poprzedni plik
    prev_st = previous('stopy')
    if prev_st and fresh(prev_st, 360):
        save('stopy', prev_st); META['ok']['stopy'] = 'cached'
    else:
        try:
            save('stopy', build_stopy()); META['ok']['stopy'] = True
        except Exception as e:
            META['errors'].append(mask(f'BIS stopy: {e}')); META['ok']['stopy'] = False
            if prev_st: save('stopy', prev_st)
    # v101: kursy EBC i rentowności 10L (bez klucza) — co godzinę; każda część osobno (strona Źródła: rynki_fx / rynki_ust / rynki_buba)
    prev_ry = previous('rynki')
    if prev_ry and fresh(prev_ry, RYNKI_EVERY) and all((prev_ry.get('ok') or {}).get(k) for k in RYNKI_PX):
        save('rynki', prev_ry)
        for k in RYNKI_PX: META['ok'][f'rynki_{k}'] = 'cached'
    else:
        try:
            ry = build_rynki(prev_ry); save('rynki', ry)
            for k in RYNKI_PX: META['ok'][f'rynki_{k}'] = ry['ok'].get(k, False)
        except Exception as e:
            META['errors'].append(mask(f'Rynki: {e}'))
            for k in RYNKI_PX: META['ok'][f'rynki_{k}'] = False
            if prev_ry: save('rynki', prev_ry)
    # v104: dźwignia i pozycje w krypto (bez klucza) — co godzinę od pełnej budowy; młody plik z częścią z błędem: dobieramy tylko tę część (strona Źródła: dzwignia)
    prev_lv = previous('dzwignia')
    lv_ok = prev_lv.get('ok') if isinstance(prev_lv, dict) and isinstance(prev_lv.get('ok'), dict) else {}
    lv_bad = [k for k in LEV_PX if lv_ok.get(k) is not True]
    lv_young = isinstance(prev_lv, dict) and fresh({'at': prev_lv.get('full_at') or prev_lv.get('at')}, LEV_EVERY)
    if lv_young and not lv_bad:
        save('dzwignia', prev_lv); META['ok']['dzwignia'] = 'cached'
        for k in LEV_PX: META['ok'][f'dzwignia_{k}'] = 'cached'
    else:
        try:
            lv = build_dzwignia(prev_lv, only=set(lv_bad) if lv_young else None); save('dzwignia', lv); META['ok']['dzwignia'] = True
            for k in LEV_PX: META['ok'][f'dzwignia_{k}'] = lv['ok'].get(k, False)
        except Exception as e:
            META['errors'].append(mask(f'Dźwignia: {e}')); META['ok']['dzwignia'] = False
            for k in LEV_PX: META['ok'][f'dzwignia_{k}'] = False
            if prev_lv: save('dzwignia', prev_lv)
    # v105: wieloryby — portfele giełd na Ethereum (bez klucza): co przebieg (kilka żądań zbiorczych); awaria = poprzedni plik i błąd
    # v112: z kluczem ETHERSCAN_KEY (sekret właściciela ETHERSCAN) także transfery ETH natywne — klucz tylko w adresie zapytania, maskowany w błędach
    eth_key = os.environ.get('ETHERSCAN_KEY', '').strip()
    if eth_key:
        SECRETS.append(eth_key)
    else:
        META['notes'].append('brak ETHERSCAN_KEY — transfery ETH natywne w wielorybach wyłączone')
    prev_wh = previous('wieloryby')
    try:
        wh = build_wieloryby(prev_wh, eth_key=eth_key or None); save('wieloryby', wh); META['ok']['wieloryby'] = all(wh['ok'].get(k) for k in ('salda', 'transfery'))
        if eth_key:
            META['ok']['wieloryby_eth'] = bool(wh['ok'].get('eth'))
    except Exception as e:
        META['errors'].append(mask(f'Wieloryby: {e}')); META['ok']['wieloryby'] = False
        if prev_wh: save('wieloryby', prev_wh)
    # v121: ceny krypto — dzienne zamknięcia 10 par USDT z publicznych plików giełdy (bez klucza): pełna budowa najwyżej co godzinę
    # (KC_EVERY, liczone od full_at — ponowienie jednej pary odświeża `at`, ale nie zegar pełnej budowy); młody plik z parą bez powodzenia
    # (błąd albo przerwane dopełnienie) = dobierana tylko ta para; awaria całości = poprzedni plik i błąd
    prev_kc = previous('ceny-krypto')
    kc_ok = prev_kc.get('ok') if isinstance(prev_kc, dict) and isinstance(prev_kc.get('ok'), dict) else {}
    kc_bad = [s for s in TR_CR_SYMS if kc_ok.get(s) is not True]
    kc_young = isinstance(prev_kc, dict) and fresh({'at': prev_kc.get('full_at') or prev_kc.get('at')}, KC_EVERY)
    if kc_young and not kc_bad:
        save('ceny-krypto', prev_kc); META['ok']['ceny-krypto'] = 'cached'
    else:
        try:
            kc = build_ceny_krypto(prev_kc, only=set(kc_bad) if kc_young else None); save('ceny-krypto', kc); META['ok']['ceny-krypto'] = not kc.get('bledy')
        except Exception as e:
            META['errors'].append(mask(f'{KC_LABEL}: {e}')); META['ok']['ceny-krypto'] = False
            if prev_kc: save('ceny-krypto', prev_kc)
    # v121: insiderzy spółek USA — zgłoszenia Form 4 (EDGAR, bez klucza): dzień zgłoszeń czytany raz, od 04:00 UTC dnia następnego,
    # kolejka na kilka przebiegów (budżet czasu); brak indeksu (weekend, święto) = dzień pusty, nie błąd; awaria = poprzedni plik i błąd.
    # Urząd wymaga adresu kontaktowego w User-Agent — tylko z sekretu SEC_CONTACT (maskowany w komunikatach, pilnowany przez straż kluczy);
    # bez niego (albo gdy to nie adres e-mail) część wyłączona: notatka, bez zapytań, poprzedni plik zostaje
    sec_contact = os.environ.get('SEC_CONTACT', '').strip()
    if sec_contact:
        SECRETS.append(sec_contact)
    prev_ins = previous('insider')
    if not ins_ua(sec_contact):
        META['notes'].append(('SEC_CONTACT to nie adres e-mail' if sec_contact else 'brak SEC_CONTACT') + ' — insiderzy (zgłoszenia Form 4) wyłączeni')
        if prev_ins:
            save('insider', prev_ins)
    else:
        try:
            ins = build_insider(prev_ins, contact=sec_contact); save('insider', ins); META['ok']['insider'] = bool((ins.get('ok') or {}).get('sec'))
        except Exception as e:
            META['errors'].append(mask(f'Insiderzy: {e}')); META['ok']['insider'] = False
            if prev_ins: save('insider', prev_ins)
    # v121: stres finansowy USA (indeks stresu — bez klucza) i put/call (tylko z pisemną zgodą giełdy: CBOE_ZGODA) — co 6 h; część z błędem
    # ponawiana po godzinie; zmiana stanu zgody = przebudowa (bez zgody stare put/call nie są przepisywane); awaria = poprzedni plik i błąd
    if not PC_ZGODA:
        META['notes'].append('Stres: część put/call wyłączona (zmienna CBOE_ZGODA pusta)')
    prev_st = previous('stres')
    pok_st = (prev_st or {}).get('ok') or {}
    if prev_st and fresh(prev_st, STRES_EVERY) and bool(prev_st.get('pc_off')) == (not PC_ZGODA) and (all(pok_st.get(k) for k in stres_czesci()) or fresh(prev_st, STRES_RETRY)):
        save('stres', prev_st); META['ok']['stres'] = 'cached'
    else:
        try:
            st_ = build_stres(prev_st); save('stres', st_); META['ok']['stres'] = all(st_['ok'].get(k) for k in stres_czesci())
        except Exception as e:
            META['errors'].append(mask(f'Stres: {e}')); META['ok']['stres'] = False
            if prev_st: save('stres', prev_st)
    # v121: aukcje papierów skarbowych USA (bez klucza; dane rządu USA): co 6 h; plik zbudowany z zapasu albo z błędem części — ponowna próba
    # po godzinie; awaria obu źródeł = poprzedni plik i błąd (strona pokazuje datę i wiek każdej aukcji)
    prev_au = previous('aukcje')
    pok_au = prev_au.get('ok') if isinstance(prev_au, dict) and isinstance(prev_au.get('ok'), dict) else {}
    if prev_au and fresh(prev_au, AUK_EVERY) and (all(pok_au.get(k) for k in AUK_PARTS) or fresh(prev_au, AUK_RETRY)):
        save('aukcje', prev_au); META['ok']['aukcje'] = 'cached'
    else:
        try:
            au = build_aukcje(prev_au); save('aukcje', au); META['ok']['aukcje'] = bool(au['ok'].get('last'))
        except Exception as e:
            META['errors'].append(mask(f'Aukcje: {e}')); META['ok']['aukcje'] = False
            if prev_au: save('aukcje', prev_au)
    # v106: indeksy świata (EODHD, rotacja 20 zapytań na dobę) i notowania ETF (Massive, zapas Tiingo) — klucze właściciela; co godzinę;
    # brak klucza = informacja (notes), nie błąd; awaria = poprzedni plik
    ix_keys = {k: os.environ.get(k, '').strip() for k in IX_KEYS}
    SECRETS.extend(v for v in ix_keys.values() if v)
    for k, what in (('EODHD_KEY', 'indeksy świata wyłączone'), ('MASSIVE_KEY', 'notowania ETF (Massive) wyłączone'), ('TIINGO_KEY', 'zapas notowań ETF (Tiingo) wyłączony')):
        if not ix_keys[k]:
            META['notes'].append(f'brak {k} — {what}')
    ix_any = any(ix_keys[k] for k in ('EODHD_KEY', 'MASSIVE_KEY', 'TIINGO_KEY'))
    prev_ix = previous('indeksy') if ix_any else None
    if ix_any and prev_ix and fresh(prev_ix, IX_EVERY):
        save('indeksy', prev_ix)
        for k, st in (prev_ix.get('ok') or {}).items():
            META['ok']['indeksy_' + k] = 'cached' if st is True else st
    elif ix_any:
        try:
            ix = build_indeksy(ix_keys, prev_ix); save('indeksy', ix)
            for k, st in ix['ok'].items():
                META['ok']['indeksy_' + k] = st
        except Exception as e:
            META['errors'].append(mask(f'Indeksy: {e}'))
            for k in IX_PARTS:
                META['ok']['indeksy_' + k] = False
            if prev_ix:
                save('indeksy', prev_ix)
    # v99: OECD (bez klucza) — co 6 h; część z błędem ponawiana po godzinie
    prev_oe = previous('oecd')
    pok_oe = (prev_oe or {}).get('ok') or {}
    if prev_oe and fresh(prev_oe, OECD_EVERY) and (all(pok_oe.get(k) for k in OECD_Q) or fresh(prev_oe, OECD_RETRY)):
        save('oecd', prev_oe); META['ok']['oecd'] = 'cached'
    else:
        try:
            save('oecd', build_oecd(prev_oe)); META['ok']['oecd'] = True
        except Exception as e:
            META['errors'].append(mask(f'OECD: {e}')); META['ok']['oecd'] = False
            if prev_oe: save('oecd', prev_oe)
    # KURSY — średnie miesięczne EBC (bez klucza): najwyżej co 12 h (miesiąc publikowany raz, na początku następnego)
    prev_k = previous('kursy')
    if prev_k and fresh(prev_k, 720):
        save('kursy', prev_k); META['ok']['kursy'] = 'cached'
    else:
        try:
            save('kursy', build_kursy()); META['ok']['kursy'] = True
        except Exception as e:
            META['errors'].append(mask(f'EBC kursy: {e}')); META['ok']['kursy'] = False
            if prev_k: save('kursy', prev_k)
    # OBCE — zmierzone dzienne przepływy inwestorów zagranicznych (NSDL Indie, TWSE Tajwan): najwyżej co 3 h
    prev_o = previous('obce')
    pok = (prev_o or {}).get('ok') or {}   # v80: brak oczekiwanej części = pobierz od nowa; część z błędem — ponów po 60 min
    miss = [p for p in ('in', 'tw', 'hk', 'br', 'tr', 'th') if prev_o and p not in prev_o and pok.get(p) is not False]
    retry = [p for p, st in pok.items() if st is False]
    if prev_o and isinstance(prev_o.get('br'), dict) and any(isinstance(r, list) and len(r) < 10 for r in (prev_o['br'].get('m') or [])):
        miss.append('br')   # v81: wiersze miesięczne Brazylii bez kolumn banku centralnego — pobierz od razu
    if prev_o and fresh(prev_o, 60) and not miss:   # v91: co godzinę (Indie, Tajwan, Hongkong; część z błędem też ponawiana po 60 min); wolniejsze — OBCE_SLOW
        save('obce', prev_o); META['ok']['obce'] = 'cached'
        for p, st in (prev_o.get('ok') or {}).items():   # v77: stan części z ostatniego pełnego pobrania (błąd zostaje widoczny)
            META['ok']['obce_' + p] = 'cached' if st is True else st
        META['errors'].extend(e for es in (prev_o.get('errs') or {}).values() for e in (es if isinstance(es, list) else []))
    else:
        try:
            save('obce', build_obce(fred_key, prev_o)); META['ok']['obce'] = True
        except Exception as e:
            META['errors'].append(mask(f'obce: {e}')); META['ok']['obce'] = False
            if prev_o: save('obce', prev_o)
    # EER — kursy efektywne BIS (bez klucza): najwyżej co 6 h
    prev_e = previous('eer')
    if prev_e and fresh(prev_e, 360):
        save('eer', prev_e); META['ok']['eer'] = 'cached'
    else:
        try:
            save('eer', build_eer()); META['ok']['eer'] = True
        except Exception as e:
            META['errors'].append(mask(f'BIS kursy efektywne: {e}')); META['ok']['eer'] = False
            if prev_e: save('eer', prev_e)
    # COFER — skład walutowy rezerw świata (MFW, kwartalnie): najwyżej raz na dobę
    prev_c = previous('cofer')
    if prev_c and fresh(prev_c, 1440):
        save('cofer', prev_c); META['ok']['cofer'] = 'cached'
    else:
        try:
            save('cofer', build_cofer()); META['ok']['cofer'] = True
        except Exception as e:
            META['errors'].append(mask(f'MFW COFER: {e}')); META['ok']['cofer'] = False
            if prev_c: save('cofer', prev_c)
    # v70: MFW — bilans płatniczy 37 gospodarek (kwartalnie): najwyżej raz na dobę; awaria = poprzedni plik i błąd
    prev_bl = previous('bilans')
    if prev_bl and fresh(prev_bl, 1440):
        save('bilans', prev_bl); META['ok']['bilans'] = 'cached'
    else:
        try:
            save('bilans', build_bilans()); META['ok']['bilans'] = True
        except Exception as e:
            META['errors'].append(mask(f'MFW bilans płatniczy: {e}')); META['ok']['bilans'] = False
            if prev_bl: save('bilans', prev_bl)
    # v72: SAFE — kupno i sprzedaż walut przez banki w Chinach (miesięcznie): najwyżej raz na dobę; awaria = poprzedni plik i błąd
    prev_sf = previous('safe')
    if prev_sf and fresh(prev_sf, 1440):
        save('safe', prev_sf); META['ok']['safe'] = 'cached'
    else:
        try:
            save('safe', build_safe()); META['ok']['safe'] = True
        except Exception as e:
            META['errors'].append(mask(f'SAFE: {e}')); META['ok']['safe'] = False
            if prev_sf: save('safe', prev_sf)
    # v76: Eurostat — bilans płatniczy krajów UE (miesięcznie): najwyżej raz na dobę; awaria = poprzedni plik i błąd
    prev_ue = previous('ue')
    if prev_ue and fresh(prev_ue, 1440) and 'S121' in str(prev_ue.get('unit', '')):   # v80: plik sprzed v80 (pozostałe z bankiem centralnym) — przebuduj
        save('ue', prev_ue); META['ok']['ue'] = 'cached'
    else:
        try:
            save('ue', build_ue()); META['ok']['ue'] = True
        except Exception as e:
            META['errors'].append(mask(f'Eurostat: {e}')); META['ok']['ue'] = False
            if prev_ue and 'S121' in str(prev_ue.get('unit', '')): save('ue', prev_ue)   # v81: pliku w starym formacie nie publikujemy ponownie
    # v78: Kanada — Statistics Canada (miesięcznie): najwyżej raz na dobę; awaria = poprzedni plik i błąd
    prev_ka = previous('kanada')
    if prev_ka and fresh(prev_ka, 1440):
        save('kanada', prev_ka); META['ok']['kanada'] = 'cached'
    else:
        try:
            save('kanada', build_kanada()); META['ok']['kanada'] = True
        except Exception as e:
            META['errors'].append(mask(f'Statistics Canada: {e}')); META['ok']['kanada'] = False
            if prev_ka: save('kanada', prev_ka)
    # v82: Korea — FSS (miesięcznie): najwyżej raz na dobę; awaria = poprzedni plik i błąd
    prev_kr = previous('korea')
    if prev_kr and fresh(prev_kr, 1440):
        save('korea', prev_kr); META['ok']['korea'] = 'cached'
    else:
        try:
            save('korea', build_korea(fred_key, prev_kr)); META['ok']['korea'] = True
        except Exception as e:
            META['errors'].append(mask(f'FSS: {e}')); META['ok']['korea'] = False
            if prev_kr: save('korea', prev_kr)
    # v87: Polska — MF, nierezydenci w krajowych SPW (miesięcznie): najwyżej raz na dobę; awaria = poprzedni plik i błąd
    prev_sp = previous('spw')
    if prev_sp and fresh(prev_sp, 1440):
        save('spw', prev_sp); META['ok']['spw'] = 'cached'
    else:
        try:
            save('spw', build_spw()); META['ok']['spw'] = True
        except Exception as e:
            META['errors'].append(mask(f'MF SPW: {e}')); META['ok']['spw'] = False
            if prev_sp: save('spw', prev_sp)
    # v88: Meksyk — Banxico (dziennie, z opóźnieniem ok. 1,5 tygodnia): najwyżej co 6 h; awaria = poprzedni plik i błąd
    prev_mx = previous('meksyk')
    old_mx = bool(prev_mx) and any(isinstance(r, list) and len(r) < 6 for r in (prev_mx.get('d') or [])[-1:])   # v88.2: plik sprzed podziału na rodzaje papierów
    if prev_mx and fresh(prev_mx, 360) and not old_mx:
        save('meksyk', prev_mx); META['ok']['meksyk'] = 'cached'
    else:
        try:
            save('meksyk', build_meksyk(fred_key)); META['ok']['meksyk'] = True
        except Exception as e:
            META['errors'].append(mask(f'Banxico: {e}')); META['ok']['meksyk'] = False
            if prev_mx: save('meksyk', prev_mx)
    # KRYPTO (CoinGecko z kluczem właściciela w nagłówku + Alternative.me): najwyżej raz na 55 min (limit Demo 10 000/mies.)
    prev_kr = previous('krypto')
    if prev_kr and fresh(prev_kr, 55):
        save('krypto', prev_kr); META['ok']['krypto'] = 'cached'; print('KRYPTO: dane z', prev_kr.get('at'), '— młodsze niż 55 min')
    else:
        try:
            save('krypto', build_krypto(cg_key)); META['ok']['krypto'] = True
        except Exception as e:
            META['errors'].append(mask(f'krypto: {e}')); META['ok']['krypto'] = False
            if prev_kr: save('krypto', prev_kr); print('rynek krypto zawiódł — zachowano poprzedni krypto.json z', prev_kr.get('at'))
    # INSTYTUCJE (bez klucza): najwyżej raz na 55 min; przy awarii zachowaj poprzedni plik (pole "at" mówi, jak stary)
    prev_inst = previous('instytucje')
    if prev_inst and fresh(prev_inst, 55):
        save('instytucje', prev_inst); META['ok']['instytucje'] = 'cached'; print('INSTYTUCJE: dane z', prev_inst.get('at'), '— młodsze niż 55 min')
    else:
        try:
            save('instytucje', build_instytucje()); META['ok']['instytucje'] = True
        except Exception as e:
            META['errors'].append(mask(f'instytucje: {e}')); META['ok']['instytucje'] = False
            if prev_inst: save('instytucje', prev_inst); print('źródła urzędowe zawiodły — zachowano poprzedni instytucje.json z', prev_inst.get('at'))
    # v90: fundusze ETF w USA (State Street, iShares) — historia NAV i liczby jednostek; odświeżanie w środku (6 h / 2 h); awaria = poprzedni plik
    prev_fu = previous('fundusze')
    try:
        save('fundusze', build_fundusze(prev_fu)); META['ok']['fundusze'] = True
    except Exception as e:
        META['errors'].append(mask(f'fundusze ETF: {e}')); META['ok']['fundusze'] = False
        if prev_fu: save('fundusze', prev_fu)
    # v97: dane rządu USA — EIA (energia, co 6 h), BLS (makro, co 6 h; bez klucza — mniejszy limit), BEA (bilans płatniczy, raz na dobę);
    # każde źródło osobno, awaria zostawia poprzedni plik (brak nie jest zerem)
    for name, fn, key, mins, label, need in (('energia', build_energia, eia_key, 6 * 60, 'EIA', True),
                                             ('usa-makro', build_usa_makro, bls_key, 6 * 60, 'BLS', False),
                                             ('bilans-usa', build_bilans_usa, bea_key, 24 * 60, 'BEA', True)):
        prev_x = previous(name)
        if prev_x and fresh(prev_x, mins):
            save(name, prev_x); META['ok'][label.lower()] = 'cached'; continue
        if need and not key:
            META['errors'].append(f'brak {label}_KEY'); META['ok'][label.lower()] = False
            if prev_x: save(name, prev_x)
            continue
        try:
            save(name, fn(key, prev_x)); META['ok'][label.lower()] = True
        except Exception as e:
            META['errors'].append(mask(f'{label}: {e}')); META['ok'][label.lower()] = False
            if prev_x: save(name, prev_x)
    # v89: TRENDY — z plików zapisanych w tym przebiegu, bez zapytań do sieci; awaria = błąd w meta, pozostałe pliki bez zmian
    try:
        save('trendy', build_trendy(SAVED)); META['ok']['trendy'] = True
    except Exception as e:
        META['errors'].append(mask(f'trendy: {e}')); META['ok']['trendy'] = False
    META['errors'] = [mask(x) for x in META['errors']]; META['notes'] = [mask(x) for x in META['notes']]   # v117: żadna wartość klucza w pliku stanu
    save('meta', META)
    print('błędy:', META['errors'] or 'brak')
    return 0


if __name__ == '__main__':
    sys.exit(main())
