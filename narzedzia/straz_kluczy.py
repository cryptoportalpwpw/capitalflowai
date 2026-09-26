#!/usr/bin/env python3
"""Straż kluczy (v115, część C.1 zadania z 26.09.2026): przed publikacją strony sprawdza, że wartość żadnego sekretu nie występuje w żadnym
pliku `_site/**` ani `data/*.json`. Wartości bierze z env (nazwy jak w kroku „Zbierz dane” w strona.yml) i nigdy ich nie wypisuje — w logu
i podsumowaniu jest tylko nazwa sekretu, plik i pozycja. Znalezisko = kod wyjścia 1 = publikacja przerwana (GitHub wysyła e-mail).
Zbyt krótkie wartości (< 8 znaków) pomijane — dopasowanie byłoby przypadkowe. Python 3.12, biblioteka standardowa."""
import os
import sys

NAZWY = ('SOSOVALUE_KEY', 'COINGECKO_KEY', 'FINNHUB_KEY', 'TWELVEDATA_KEY', 'COINMARKETCAP_KEY', 'FRED_KEY', 'EIA_KEY', 'BLS_KEY', 'BEA_KEY',
         'EODHD_KEY', 'MASSIVE_KEY', 'TIINGO_KEY', 'FMP_KEY', 'ALPHAVANTAGE_KEY', 'ETHERSCAN_KEY', 'CRYPTOPANIC_KEY', 'CENSUS_KEY',
         'SEC_CONTACT', 'GITHUB_TOKEN')
MIN_DL = 8
KATALOGI = ('_site', 'data')


def sekrety(env=None):
    """{nazwa: bajty wartości} — tylko ustawione i wystarczająco długie (< MIN_DL znaków = pominięte, z informacją)."""
    env = os.environ if env is None else env
    out, krotkie = {}, []
    for n in NAZWY:
        v = (env.get(n) or '').strip()
        if not v:
            continue
        if len(v) < MIN_DL:
            krotkie.append(n); continue
        out[n] = v.encode('utf-8')
    return out, krotkie


def pliki(root='.', katalogi=KATALOGI):
    for k in katalogi:
        base = os.path.join(root, k)
        if not os.path.isdir(base):
            continue
        for d, _, fs in os.walk(base):
            for f in sorted(fs):
                yield os.path.join(d, f)


def skanuj(sek, root='.', katalogi=KATALOGI):
    """Lista (nazwa sekretu, plik, pozycja) — każde wystąpienie wartości sekretu w plikach (bajtowo, także wewnątrz JSON/HTML)."""
    trafienia, n = [], 0
    for p in pliki(root, katalogi):
        try:
            with open(p, 'rb') as f:
                b = f.read()
        except OSError:
            continue
        n += 1
        for nazwa, v in sek.items():
            i = b.find(v)
            while i >= 0:
                trafienia.append((nazwa, os.path.relpath(p, root), i)); i = b.find(v, i + 1)
    return trafienia, n


def main():
    sek, krotkie = sekrety()
    tr, n = skanuj(sek, os.environ.get('STRAZ_ROOT', '.'))
    msg = f'straż kluczy: {n} plików, {len(sek)} sekretów sprawdzonych' + (f', pominięte (za krótkie): {", ".join(krotkie)}' if krotkie else '')
    if tr:
        for nazwa, p, i in tr:
            print(f'::error title=straż kluczy::wartość sekretu {nazwa} w pliku {p} (pozycja {i}) — publikacja przerwana')
        print(msg + f' — ZNALEZIONO {len(tr)} wystąpień'); sys.exit(1)
    print(msg + ' — czysto'); sys.exit(0)


if __name__ == '__main__':
    main()
