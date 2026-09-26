# Kontrola strony — 26.09.2026, 20:11 (czas polski)

**Wynik: BŁĄD**

❌ Błędów: 1 — wymagają uwagi (szczegóły niżej).

- Strona główna: działa (HTTP 200, 335 ms).
- Ostatni przebieg automatu: 26.09.2026, 20:06 — sprzed 4 min; źródeł: 55, bez odpowiedzi: obce_in; błędów zbieracza: 1.
- Przebiegi Actions w 24 h: 82 (success: 79, failure: 3).
- Pliki danych (wiek): etf 0h26, trendy 0h04, oecd 1h25, rynki 0h26, dzwignia 0h26, wieloryby 0h04, energia 5h27, usa-makro 5h27, bilans-usa 23h29, krypto 0h26, instytucje 0h26, tic 20h44, cm 0h26, fred 0h26, cftc 2h08, ceny 0h26, indeksy 0h44, ceny-krypto 0h04, insider HTTP 404, stres 0h04, aukcje 0h04, robots.txt HTTP 200, sitemap.xml HTTP 200, google433f7c24524100a9.html HTTP 200.
- Notatki automatu: poprzedni ceny-krypto.json: brak na stronie (404) · Ceny krypto: SOL — budżet czasu przebiegu (50 s), ciąg dalszy w następnym przebiegu · Ceny krypto: DOGE — budżet czasu przebiegu (50 s), ciąg dalszy w następnym przebiegu · Ceny krypto: ADA — budżet czasu przebiegu (50 s), ciąg dalszy w następnym przebiegu · Ceny krypto: TRX — budżet czasu przebiegu (50 s), ciąg dalszy w następnym przebiegu · Ceny krypto: LINK — budżet czasu przebiegu (50 s), ciąg dalszy w następnym przebiegu · Ceny krypto: AVAX — budżet czasu przebiegu (50 s), ciąg dalszy w następnym przebiegu · poprzedni insider.json: brak na stronie (404) · brak SEC_CONTACT — insiderzy (zgłoszenia Form 4) wyłączeni · Stres: część put/call wyłączona (zmienna CBOE_ZGODA pusta) · poprzedni stres.json: brak na stronie (404) · poprzedni aukcje.json: brak na stronie (404).

## Świeżość źródeł

| Źródło | Status | Wiek danych | Data danych | Uwaga |
|---|---|---|---|---|
| rynki (kursy EBC, rentowności) | ✅ | 0 h 26 min | 2026-09-26T17:44:31+00:00 | — |
| wieloryby (salda portfeli giełd) | ✅ | 0 h 04 min | 2026-09-26T18:06:24+00:00 | — |
| dźwignia (giełdy pochodnych) | ✅ | 0 h 26 min | 2026-09-26T17:44:31+00:00 | — |
| TGA (Fiscal Data, dziennie) | ✅ | 24 h 00 min | 2026-09-24 | — |
| ETF krypto (SoSoValue, dziennie) | ✅ | 0 h 00 min | 2026-09-25 | — |
| FRED dzienne (RRPONTSYD) | ✅ | 0 h 00 min | 2026-09-25 | — |
| EIA ceny dzienne (publikowane co tydzień) | ✅ | 3 d 18 h | 2026-09-22 | — |
| CFTC (raport tygodniowy) | ✅ | 3 d 18 h | 2026-09-22 | — |
| FRED tygodniowe (WALCL) | ✅ | 2 d 18 h | 2026-09-23 | — |
| TIC (miesięcznie) | ✅ | 56 d 18 h | 2026-07 | — |
| OECD (miesięcznie) | ✅ | 25 d 18 h | 2026-08 | — |
| BLS (miesięcznie) | ✅ | 25 d 18 h | 2026-08 | — |

## Zgodność liczb (porównania krzyżowe)

- Kapitalizacja krypto, dwa źródła: różnica dziś 4.30%, norma (mediana 0 dni) — — ℹ️ historia 0 z 7 dni — bez oceny.
- Cena BTC: 84,098 vs 84,077 USD — różnica 0.02% ✅.
- Cena ETH: 2,689 vs 2,689 USD — różnica 0.00% ✅.
- TGA 2026-09-23: Fiscal Data 947,317 vs FRED 977,084 mln USD — różnica 3.05%, norma (mediana 0 dni) — — ℹ️ historia 0 z 7 dni — bez oceny.
- ETF mapy (dwa źródła, ta sama data): porównane 14 symboli, różnice > 1%: 0 ✅.
- Wieloryby: archiwum ma mniej niż dwa dni — porównanie od jutra.

## Błędy (wymagają uwagi)
- 3 nieudanych przebiegów automatu w 24 h

## Uwagi
- źródła bez odpowiedzi w ostatnim przebiegu: obce_in
- błąd zbieracza: NSDL: Remote end closed connection without response
- ceny-krypto.json: części bez odpowiedzi: ADA, AVAX, DOGE, LINK, SOL, TRX
- insider.json: HTTP 404 (brak pliku)

Kontrola wykonana przez GitHub Actions (plik `narzedzia/kontrola.py`), bez kluczy, tylko odczyt.
